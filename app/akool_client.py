from __future__ import annotations

import base64
import io
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from curl_cffi import requests as curl_requests
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import normalize_proxy_url, rewrite_loopback_proxy
from app.cookies import cookie_header_from_records, cookie_records
from app.model_catalog import model_spec


AKOOL_ORIGIN = "https://akool.com"
VERIFY_PATH = "/interface/user-api/api/v6/verify/user"
ACCOUNT_PATH = "/interface/faceswap-api/api/v1/faceswap/user/info"
DAILY_CHECKIN_STATS_PATH = "/interface/faceswap-api/api/v6/content/sign/stats"
DAILY_CHECKIN_PATH = "/interface/faceswap-api/api/v6/content/sign"
SIGNATURE_PATH = "/interface/storagesvc/api/v1/upload/signature"
PROFILE_PATH = "/interface/content-api/api/v7/content/profile/create"
FEE_PATH = "/interface/content-api/api/v7/content/calculateFee"
SUBMIT_PATH = "/interface/content-api/api/v7/content/image2Video/createBySourcePrompt/batch"
LIST_PATH = "/interface/content-api/api/v6/content/resourceResult/list"
IMAGE_MIN_HEIGHT = 301
IMAGE_MAX_HEIGHT = 5999
IMAGE_MIN_ASPECT_RATIO = 0.41
IMAGE_MAX_ASPECT_RATIO = 2.49


class AkoolUpstreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "AKOOL_UPSTREAM_ERROR",
        status_code: int = 502,
        details: Any = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details


class AkoolAuthError(AkoolUpstreamError):
    def __init__(self, message: str = "Akool session is invalid"):
        super().__init__(message, code="AKOOL_AUTH_REQUIRED", status_code=401)


class AkoolRiskBlocked(AkoolUpstreamError):
    def __init__(self, message: str = "Akool browser verification is required"):
        super().__init__(message, code="AKOOL_RISK_BLOCKED", status_code=403)


class AkoolAccountSuspended(AkoolUpstreamError):
    def __init__(self, message: str = "Akool account has been suspended"):
        super().__init__(message, code="AKOOL_ACCOUNT_SUSPENDED", status_code=403)


@dataclass(slots=True)
class MediaUpload:
    profile_id: str
    url: str
    kind: str
    name: str
    content_type: str
    size: int
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    raw: dict[str, Any] | None = None

    def audit_view(self) -> dict[str, Any]:
        result = {
            "profile_id": self.profile_id,
            "url": self.url,
            "kind": self.kind,
            "name": self.name,
            "content_type": self.content_type,
            "size": self.size,
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
        }
        adjustment = (self.raw or {}).get("_image_adjustment")
        if isinstance(adjustment, dict) and adjustment:
            result["image_adjustment"] = adjustment
        return result


def _message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _message(value, "")
                if nested:
                    return nested
    return fallback


def _risk_message(value: str) -> bool:
    text = value.lower()
    return any(
        marker in text
        for marker in (
            "turnstile",
            "cloudflare",
            "verify you are human",
            "captcha",
            "challenge-platform",
            "access denied",
        )
    )


def _auth_message(value: str) -> bool:
    text = value.lower()
    return any(
        marker in text
        for marker in (
            "unauthorized",
            "not logged in",
            "login required",
            "invalid token",
            "token expired",
            "session expired",
        )
    )


def _account_suspended_message(value: str) -> bool:
    text = value.lower()
    return "account has been suspended" in text or "account is suspended" in text


def result_urls(detail: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("external_video", "video", "url", "video_url"):
        value = detail.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            values.append(value)
    for value in detail.get("urls") or detail.get("resultUrls") or []:
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            values.append(value)
    return list(dict.fromkeys(values))


def failure_reason(detail: dict[str, Any]) -> str:
    for key in (
        "error_reason",
        "error_message",
        "fail_msg",
        "error",
        "msg",
        "message",
    ):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            message = _message(value, "")
            if message:
                return message
    return "Akool generation failed"


class AkoolClient:
    def __init__(self, account: dict[str, Any], settings: Any):
        self.account = dict(account)
        self.settings = settings
        self.timeout = int(settings.request_timeout_seconds)
        self.retries = int(settings.request_retries)
        self.user_agent = str(
            account.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        )
        records = cookie_records(
            account.get("cookie_records") or account.get("cookies_json")
        )
        self.cookie_header = str(
            account.get("cookie_header")
            or account.get("cookies")
            or cookie_header_from_records(records)
            or ""
        ).strip()
        self.proxy_url = rewrite_loopback_proxy(
            normalize_proxy_url(str(account.get("proxy_url") or "")),
            str(settings.proxy_host_override or ""),
        )
        self.session = curl_requests.Session(impersonate="chrome")

    def _headers(self, *, json_body: bool = False, referer: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": AKOOL_ORIGIN,
            "Referer": referer or f"{AKOOL_ORIGIN}/zh-cn/",
            "User-Agent": self.user_agent,
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if json_body:
            headers["Content-Type"] = "application/json"
        sec_ch_ua = str(self.account.get("sec_ch_ua") or "").strip()
        sec_ch_platform = str(self.account.get("sec_ch_ua_platform") or "").strip()
        if sec_ch_ua:
            headers["sec-ch-ua"] = sec_ch_ua
        if sec_ch_platform:
            headers["sec-ch-ua-platform"] = sec_ch_platform
        return headers

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: Any = None,
        params: dict[str, Any] | None = None,
        retry: bool | None = None,
        referer: str = "",
    ) -> tuple[Any, Any]:
        url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else f"{AKOOL_ORIGIN}{path_or_url}"
        )
        method = method.upper()
        retry = method == "GET" if retry is None else bool(retry)
        attempts = self.retries + 1 if retry else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(json_body=payload is not None, referer=referer),
                    json=payload,
                    params=params,
                    proxy=self.proxy_url or None,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                content_type = str(response.headers.get("content-type") or "")
                try:
                    body = response.json()
                except Exception:
                    body = {"message": response.text[:2000], "content_type": content_type}
                message = _message(body, f"Akool HTTP {response.status_code}")
                if _account_suspended_message(message):
                    raise AkoolAccountSuspended(message)
                if response.status_code in {401, 419} or _auth_message(message):
                    raise AkoolAuthError(message)
                if response.status_code == 403 or _risk_message(message):
                    raise AkoolRiskBlocked(message)
                if response.status_code == 429:
                    raise AkoolUpstreamError(
                        message,
                        code="RATE_LIMITED",
                        status_code=429,
                        details=body,
                    )
                if response.status_code >= 500:
                    raise AkoolUpstreamError(
                        message,
                        code="AKOOL_HTTP_ERROR",
                        status_code=502,
                        details=body,
                    )
                if response.status_code >= 400:
                    raise AkoolUpstreamError(
                        message,
                        code="PROVIDER_INVALID_REQUEST",
                        status_code=422,
                        details=body,
                    )
                return body, response
            except (AkoolAuthError, AkoolRiskBlocked):
                raise
            except AkoolUpstreamError as exc:
                last_error = exc
                if not retry or exc.status_code < 500 or attempt + 1 >= attempts:
                    raise
            except Exception as exc:
                last_error = exc
                if not retry or attempt + 1 >= attempts:
                    raise AkoolUpstreamError(
                        f"Akool transport failed: {exc}",
                        code="NETWORK_ERROR",
                        details={"url": url, "method": method},
                    ) from exc
            time.sleep(min(2**attempt, 5))
        raise AkoolUpstreamError(str(last_error or "Akool request failed"))

    @staticmethod
    def _require_ok(body: Any, operation: str) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise AkoolUpstreamError(f"{operation} returned a non-object response")
        code = body.get("code")
        if code in (1000, "1000"):
            return body
        message = _message(body, f"{operation} failed")
        if code in (1108, "1108") or _account_suspended_message(message):
            raise AkoolAccountSuspended(message)
        if _risk_message(message):
            raise AkoolRiskBlocked(message)
        if _auth_message(message):
            raise AkoolAuthError(message)
        error_code = "PROVIDER_INVALID_REQUEST"
        status_code = 422
        if code in (1104, "1104") or any(
            value in message.lower() for value in ("credit", "balance", "insufficient")
        ):
            error_code = "INSUFFICIENT_CREDITS"
            status_code = 409
        raise AkoolUpstreamError(
            message,
            code=error_code,
            status_code=status_code,
            details=body,
        )

    def account_state(self) -> dict[str, Any]:
        verified, _ = self._request("GET", VERIFY_PATH, retry=True)
        verified = self._require_ok(verified, "verify user")
        data = verified.get("data") or {}
        user = data.get("user") or {}
        team = data.get("team") or {}
        state, _ = self._request("GET", ACCOUNT_PATH, retry=True)
        state = self._require_ok(state, "account state")
        quota = ((state.get("data") or {}).get("quota_info") or {})
        try:
            credit = float(quota.get("credit") or 0)
        except (TypeError, ValueError):
            credit = 0
        try:
            locked = float(quota.get("lock_credit") or 0)
        except (TypeError, ValueError):
            locked = 0
        priority = quota.get("priority")
        plan = {
            9.5: "Starter",
            9: "Pro",
            8: "ProMax",
            7: "Studio",
            6: "Enterprise",
        }.get(priority, f"Priority {priority}" if priority is not None else "")
        return {
            "balance": credit,
            "available_balance": max(credit - locked, 0),
            "plan": plan,
            "user_id": str(user.get("_id") or ""),
            "uid": user.get("uid"),
            "team_id": str(team.get("_id") or quota.get("team_id") or ""),
            "email": str(user.get("email") or quota.get("email") or self.account.get("email") or ""),
            "token": str(data.get("token") or ""),
            "buckets": {
                "credit": credit,
                "lock_credit": locked,
                "available_credit": max(credit - locked, 0),
                "image": quota.get("image"),
                "video": quota.get("video"),
                "lock_image": quota.get("lock_image"),
                "lock_video": quota.get("lock_video"),
                "priority": priority,
                "pay_level": quota.get("pay_level"),
                "is_fraud": quota.get("is_fraud", user.get("is_fraud")),
                "total_task_cnt": quota.get("total_task_cnt"),
                "subscription_credit": quota.get("subscription_credit") or {},
                "sign_reward_stats": quota.get("sign_reward_stats") or {},
                "last_daily_credit_add_time": quota.get("last_daily_credit_add_time"),
            },
            "raw": {"verify": verified, "account": state},
        }

    def daily_checkin_status(self) -> dict[str, Any]:
        body, _ = self._request(
            "GET",
            DAILY_CHECKIN_STATS_PATH,
            retry=True,
            referer=f"{AKOOL_ORIGIN}/zh-cn/pricing",
        )
        body = self._require_ok(body, "daily check-in status")
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise AkoolUpstreamError("daily check-in status returned invalid data")
        return dict(data)

    def daily_checkin(self) -> dict[str, Any]:
        body, _ = self._request(
            "POST",
            DAILY_CHECKIN_PATH,
            retry=False,
            referer=f"{AKOOL_ORIGIN}/zh-cn/pricing",
        )
        body = self._require_ok(body, "daily check-in")
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise AkoolUpstreamError("daily check-in returned invalid data")
        return dict(data)

    @staticmethod
    def _decode_data_url(value: str) -> tuple[bytes, str] | None:
        match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", value, re.S | re.I)
        if not match:
            return None
        content_type = match.group(1) or "application/octet-stream"
        encoded = match.group(3)
        if match.group(2):
            return base64.b64decode(encoded, validate=False), content_type
        return unquote(encoded).encode("utf-8"), content_type

    def _download_routes(self) -> list[str]:
        values = ["", self.proxy_url]
        fallback = rewrite_loopback_proxy(
            normalize_proxy_url(str(self.settings.media_fallback_proxy_url or "")),
            str(self.settings.proxy_host_override or ""),
        )
        values.append(fallback)
        return list(dict.fromkeys(value for value in values if value or value == ""))

    def _download_source(self, value: str, name: str) -> tuple[bytes, str, str]:
        source = str(value or "").strip()
        if not source:
            raise AkoolUpstreamError("empty media source", code="MEDIA_DOWNLOAD_FAILED")
        if decoded := self._decode_data_url(source):
            data, content_type = decoded
            filename = name or f"upload{mimetypes.guess_extension(content_type) or '.bin'}"
            return data, content_type, filename
        if not source.startswith(("http://", "https://")):
            try:
                data = base64.b64decode(source, validate=True)
            except Exception as exc:
                raise AkoolUpstreamError(
                    "unsupported media source",
                    code="MEDIA_DOWNLOAD_FAILED",
                    status_code=422,
                ) from exc
            return data, "application/octet-stream", name or "upload.bin"

        last_error = ""
        for proxy in self._download_routes():
            try:
                response = curl_requests.get(
                    source,
                    impersonate="chrome",
                    proxy=proxy or None,
                    timeout=int(self.settings.media_timeout_seconds),
                    allow_redirects=True,
                    headers={"User-Agent": self.user_agent},
                )
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    continue
                data = bytes(response.content)
                if len(data) > int(self.settings.media_max_bytes):
                    raise AkoolUpstreamError(
                        "media exceeds configured size limit",
                        code="PROVIDER_INVALID_REQUEST",
                        status_code=422,
                    )
                content_type = str(response.headers.get("content-type") or "").split(";", 1)[0]
                filename = name or Path(urlparse(str(response.url)).path).name or "upload.bin"
                return data, content_type or "application/octet-stream", filename
            except AkoolUpstreamError:
                raise
            except Exception as exc:
                last_error = str(exc)
        raise AkoolUpstreamError(
            f"media download failed: {last_error}",
            code="MEDIA_DOWNLOAD_FAILED",
            status_code=422,
        )

    @staticmethod
    def _extension(filename: str, content_type: str, kind: str) -> str:
        extension = Path(filename).suffix.lower()
        if extension and len(extension) <= 10:
            return extension
        guessed = mimetypes.guess_extension(content_type) or ""
        if guessed:
            return guessed
        return {"image": ".png", "video": ".mp4", "audio": ".mp3"}[kind]

    @staticmethod
    def _prepare_image(
        data: bytes,
        filename: str,
        content_type: str,
        *,
        force: bool = False,
    ) -> tuple[bytes, str, str, dict[str, Any]]:
        try:
            with Image.open(io.BytesIO(data)) as opened:
                detected_format = str(opened.format or "").upper()
                original_size = tuple(int(value) for value in opened.size)
                image = ImageOps.exif_transpose(opened).copy()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AkoolUpstreamError(
                "image data could not be decoded",
                code="PROVIDER_INVALID_REQUEST",
                status_code=422,
            ) from exc

        original_width, original_height = image.size
        if original_width <= 0 or original_height <= 0:
            raise AkoolUpstreamError(
                "image dimensions are invalid",
                code="PROVIDER_INVALID_REQUEST",
                status_code=422,
            )

        operations: list[str] = []
        if image.size != original_size:
            operations.append("exif_transpose")

        width, height = image.size
        aspect_ratio = width / height
        if aspect_ratio < IMAGE_MIN_ASPECT_RATIO:
            target_height = max(1, min(height, int(width / IMAGE_MIN_ASPECT_RATIO)))
            top = max((height - target_height) // 2, 0)
            image = image.crop((0, top, width, top + target_height))
            operations.append("center_crop_min_aspect")
        elif aspect_ratio > IMAGE_MAX_ASPECT_RATIO:
            target_width = max(1, min(width, int(height * IMAGE_MAX_ASPECT_RATIO)))
            left = max((width - target_width) // 2, 0)
            image = image.crop((left, 0, left + target_width, height))
            operations.append("center_crop_max_aspect")

        width, height = image.size
        if height < IMAGE_MIN_HEIGHT or height > IMAGE_MAX_HEIGHT:
            target_height = (
                IMAGE_MIN_HEIGHT if height < IMAGE_MIN_HEIGHT else IMAGE_MAX_HEIGHT
            )
            target_width = max(1, round(width * target_height / height))
            image = image.resize(
                (target_width, target_height),
                Image.Resampling.LANCZOS,
            )
            operations.append(
                "upscale_min_height"
                if height < IMAGE_MIN_HEIGHT
                else "downscale_max_height"
            )

        extension = Path(filename).suffix.lower()
        known_extension = extension in {".jpg", ".jpeg", ".png", ".webp"}
        known_content_type = content_type.lower() in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }
        reencode = bool(force or operations or not known_extension or not known_content_type)
        output_width, output_height = image.size
        info = {
            "original_width": original_width,
            "original_height": original_height,
            "width": output_width,
            "height": output_height,
            "detected_format": detected_format,
            "operations": operations,
            "reencoded": reencode,
        }
        if not reencode:
            return data, content_type, filename, info

        stem = Path(filename).stem.strip() or "upload"
        buffer = io.BytesIO()
        has_alpha = image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info
        )
        if has_alpha:
            image.convert("RGBA").save(buffer, format="PNG", optimize=True)
            output_type = "image/png"
            output_name = f"{stem}-akool.png"
        else:
            image.convert("RGB").save(
                buffer,
                format="JPEG",
                quality=95,
                optimize=True,
            )
            output_type = "image/jpeg"
            output_name = f"{stem}-akool.jpg"
        info["reencode_reason"] = "forced_retry" if force else "compatibility"
        return buffer.getvalue(), output_type, output_name, info

    def _put_signed(self, upload_url: str, data: bytes, content_type: str) -> None:
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                response = curl_requests.put(
                    upload_url,
                    data=data,
                    headers={"Content-Type": content_type},
                    proxy=self.proxy_url or None,
                    timeout=int(self.settings.media_timeout_seconds),
                )
                if 200 <= response.status_code < 300:
                    return
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            except Exception as exc:
                last_error = str(exc)
            if attempt < self.retries:
                time.sleep(min(2**attempt, 5))
        raise AkoolUpstreamError(
            f"S3 upload failed: {last_error}",
            code="MEDIA_UPLOAD_FAILED",
        )

    def upload_media(
        self,
        source: str,
        kind: str,
        name: str = "",
        *,
        force_image_normalization: bool = False,
    ) -> MediaUpload:
        if kind not in {"image", "video", "audio"}:
            raise ValueError(f"unsupported media kind: {kind}")
        data, content_type, filename = self._download_source(source, name)
        image_info: dict[str, Any] = {}
        if kind == "image":
            data, content_type, filename, image_info = self._prepare_image(
                data,
                filename,
                content_type,
                force=force_image_normalization,
            )
        if len(data) > int(self.settings.media_max_bytes):
            raise AkoolUpstreamError(
                "media exceeds configured size limit",
                code="PROVIDER_INVALID_REQUEST",
                status_code=422,
            )
        extension = self._extension(filename, content_type, kind)
        signature_body = {
            "biz_type": "default",
            "file_ext": extension,
            "file_size": len(data),
            "pay_level": int(
                ((self.account.get("balance_details") or {}).get("pay_level") or 2)
            ),
        }
        signature, _ = self._request(
            "POST",
            SIGNATURE_PATH,
            payload=signature_body,
            retry=True,
            referer=f"{AKOOL_ORIGIN}/zh-cn/apps/image-to-video/edit",
        )
        signature = self._require_ok(signature, "upload signature")
        signed = signature.get("data") or {}
        upload_url = str(signed.get("upload_url") or "")
        public_url = str(signed.get("url") or "")
        if not upload_url or not public_url:
            raise AkoolUpstreamError("upload signature response is incomplete")
        signed_content_type = str(signed.get("content_type") or content_type)
        self._put_signed(upload_url, data, signed_content_type)
        profile_input = {
            "url": public_url,
            "from": -1 if kind == "image" else 15,
            "type": {"image": 1, "video": 3, "audio": 4}[kind],
            "file_name": filename,
        }
        profile, _ = self._request(
            "POST",
            PROFILE_PATH,
            payload=profile_input,
            retry=True,
            referer=f"{AKOOL_ORIGIN}/zh-cn/apps/image-to-video/edit",
        )
        profile = self._require_ok(profile, "profile create")
        item = profile.get("data") or {}
        profile_id = str(item.get("_id") or "")
        if not profile_id:
            raise AkoolUpstreamError("profile create completed without an id")
        return MediaUpload(
            profile_id=profile_id,
            url=str(item.get("url") or public_url),
            kind=kind,
            name=filename,
            content_type=signed_content_type,
            size=len(data),
            duration_ms=int(item.get("duration") or 0),
            width=int(item.get("file_width") or image_info.get("width") or 0),
            height=int(item.get("file_height") or image_info.get("height") or 0),
            raw={**item, "_image_adjustment": image_info} if image_info else item,
        )

    @staticmethod
    def _prompt_info(uploads: list[MediaUpload]) -> tuple[list[dict[str, Any]], str]:
        counters = {"image": 0, "video": 0, "audio": 0}
        items: list[dict[str, Any]] = []
        references: list[str] = []
        for upload in uploads:
            counters[upload.kind] += 1
            label = {
                "image": "Image",
                "video": "Video",
                "audio": "Audio",
            }[upload.kind]
            items.append(
                {
                    "id": upload.profile_id,
                    "type": "voice" if upload.kind == "audio" else upload.kind,
                    "url": upload.url,
                    "name": f"{label} {counters[upload.kind]}",
                }
            )
            references.append(f"{{{{{upload.profile_id}}}}}")
        return items, "".join(references)

    def build_generation_request(
        self,
        payload: dict[str, Any],
        uploads: list[MediaUpload],
    ) -> dict[str, Any]:
        spec = model_spec(payload.get("model"))
        images = [item for item in uploads if item.kind == "image"]
        videos = [item for item in uploads if item.kind == "video"]
        audio = [item for item in uploads if item.kind == "audio"]
        audio_seconds = sum(item.duration_ms for item in audio) / 1000
        video_seconds = sum(item.duration_ms for item in videos) / 1000
        if spec.max_audio_seconds and audio_seconds > spec.max_audio_seconds:
            raise AkoolUpstreamError(
                f"Total audio duration cannot exceed {spec.max_audio_seconds} seconds",
                code="PROVIDER_INVALID_REQUEST",
                status_code=422,
            )
        if spec.max_video_seconds and video_seconds > spec.max_video_seconds:
            raise AkoolUpstreamError(
                f"Total video duration cannot exceed {spec.max_video_seconds} seconds",
                code="PROVIDER_INVALID_REQUEST",
                status_code=422,
            )
        video_info, video_references = self._prompt_info(videos)
        other_info, other_references = self._prompt_info(images + audio)
        prompt = str(payload.get("prompt") or "").strip()
        if videos:
            prompt = f"{video_references}\n\n{prompt}{other_references}"
        else:
            prompt = f"{prompt}{other_references}"
        prompt_info = video_info + other_info
        upstream_model = str(payload.get("upstream_model") or spec.upstream_model)
        request = {
            "prompt": prompt,
            "prompt_info": prompt_info,
            "negativePrompt": str(payload.get("negative_prompt") or ""),
            "extendPrompt": bool(payload.get("extend_prompt", True)),
            "audio_type": spec.audio_type,
            "count": 1,
            "resolution": str(payload.get("resolution") or spec.resolutions[0]),
            "modifiers": payload.get("modifiers")
            or [
                {
                    "type": "speed_ramp",
                    "id": "69b3d03c97d2523038c099b0",
                    "config": {"speeds": [-1, -1, -1, -1, -1]},
                }
            ],
            "video_length": int(payload.get("duration") or spec.durations[0]),
            "imageUrl": [item.url for item in images],
            "reference_audio_urls": [item.url for item in audio],
            "model_name": upstream_model,
        }
        ratio = str(payload.get("aspect_ratio") or "adaptive")
        if ratio != "adaptive":
            request["ratio"] = ratio
        if videos:
            video_urls = [item.url for item in videos]
            request["videoUrl"] = video_urls[0] if len(video_urls) == 1 else video_urls
        if spec.include_all_in_one_reference:
            request["all_in_one_reference"] = bool(
                payload.get("all_in_one_reference", spec.all_in_one_reference)
            )
        if spec.include_generate_audio:
            request["generate_audio"] = bool(
                payload.get("generate_audio", spec.generate_audio)
            )
        if spec.id == "doubao-seedance-2-5":
            request["video_extend"] = bool(payload.get("video_extend", False))
        if bool(payload.get("web_search", spec.web_search)):
            request["web_search"] = True
        return request

    def calculate_fee(
        self,
        payload: dict[str, Any],
        uploads: list[MediaUpload],
        upstream_request: dict[str, Any],
    ) -> dict[str, Any]:
        spec = model_spec(payload.get("model"))
        images = [item.profile_id for item in uploads if item.kind == "image"]
        videos = [item.profile_id for item in uploads if item.kind == "video"]
        request_body = {
            "batch_count": 1,
            "model_name": upstream_request["model_name"],
            "resolution": upstream_request["resolution"],
            "options": {
                "duration": int(upstream_request["video_length"]),
                "generate_audio": bool(
                    upstream_request.get("generate_audio", spec.generate_audio)
                ),
                "hasVideo": bool(videos),
                "is_canvas_workflow": False,
                "is_unlimited_model": False,
            },
        }
        if images:
            request_body["options"]["image_profile_ids"] = images
        if videos:
            request_body["options"]["video_profile_ids"] = videos
        body, _ = self._request(
            "POST",
            FEE_PATH,
            payload=request_body,
            retry=True,
            referer=f"{AKOOL_ORIGIN}/zh-cn/apps/image-to-video/edit",
        )
        body = self._require_ok(body, "calculate fee")
        data = body.get("data") or {}
        try:
            fee = float(data.get("fee") or 0)
        except (TypeError, ValueError):
            fee = 0
        return {"fee": fee, "request": request_body, "response": body}

    def generate(self, upstream_request: dict[str, Any]) -> dict[str, Any]:
        body, _ = self._request(
            "POST",
            SUBMIT_PATH,
            payload=upstream_request,
            retry=False,
            referer=f"{AKOOL_ORIGIN}/zh-cn/apps/image-to-video/edit",
        )
        body = self._require_ok(body, "video submit")
        data = body.get("data") or {}
        success = data.get("successList") or []
        if not success or not isinstance(success[0], dict):
            errors = data.get("errorList") or []
            raise AkoolUpstreamError(
                _message(errors[0] if errors else body, "Akool did not create a task"),
                code="GENERATION_FAILED",
                details=body,
            )
        item = success[0]
        resource_id = str(item.get("_id") or "")
        if not resource_id:
            raise AkoolUpstreamError("Akool response did not contain a resource id")
        return {
            "generationId": resource_id,
            "resourceId": resource_id,
            "batchId": str(item.get("batch_id") or ""),
            "providerTaskId": str(item.get("task_id") or ""),
            "status": item.get("video_status"),
            "raw": body,
        }

    def generation_detail(self, resource_id: str) -> dict[str, Any]:
        cursor = ""
        for _ in range(5):
            params: dict[str, Any] = {"type": 15, "size": 20, "media_type": "video"}
            if cursor:
                params["cursor"] = cursor
            body, _ = self._request("GET", LIST_PATH, params=params, retry=True)
            body = self._require_ok(body, "task list")
            data = body.get("data") or {}
            for item in data.get("result") or []:
                if isinstance(item, dict) and str(item.get("_id") or "") == str(resource_id):
                    status_value = int(item.get("video_status") or 0)
                    status = {1: "PENDING", 2: "PROCESSING", 3: "COMPLETE"}.get(
                        status_value,
                        "FAILED"
                        if status_value >= 4
                        or failure_reason(item) != "Akool generation failed"
                        else "PROCESSING",
                    )
                    progress = float(item.get("progress") or (100 if status == "COMPLETE" else 0))
                    return {
                        "generationId": resource_id,
                        "providerTaskId": str(item.get("task_id") or ""),
                        "batchId": str(item.get("batch_id") or ""),
                        "status": status,
                        "providerStatus": status_value,
                        "progress": progress,
                        "urls": result_urls(item),
                        "error": "" if status != "FAILED" else failure_reason(item),
                        "raw": item,
                    }
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                break
        return {
            "generationId": resource_id,
            "status": "PROCESSING",
            "providerStatus": 0,
            "progress": 0,
            "urls": [],
            "error": "",
            "raw": {"not_found_in_recent_pages": True},
        }
