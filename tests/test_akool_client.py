from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from app.akool_client import AkoolClient, AkoolUpstreamError, MediaUpload, result_urls


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        request_timeout_seconds=30,
        request_retries=1,
        proxy_host_override="",
        media_timeout_seconds=30,
        media_max_bytes=1024 * 1024,
        media_fallback_proxy_url="",
    )


def upload(kind: str, index: int, duration_ms: int = 0) -> MediaUpload:
    return MediaUpload(
        profile_id=f"profile-{kind}-{index}",
        url=f"https://cdn.example.com/{kind}-{index}",
        kind=kind,
        name=f"{kind}-{index}",
        content_type="application/octet-stream",
        size=100,
        duration_ms=duration_ms,
    )


def image_bytes(size: tuple[int, int], image_format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 80, 120)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_prepare_image_crops_and_upscales_unsupported_tall_input() -> None:
    data, content_type, filename, info = AkoolClient._prepare_image(
        image_bytes((100, 1000)),
        "image-1",
        "application/octet-stream",
    )

    with Image.open(io.BytesIO(data)) as prepared:
        assert prepared.size == (124, 301)
    assert content_type == "image/jpeg"
    assert filename == "image-1-akool.jpg"
    assert info["operations"] == [
        "center_crop_min_aspect",
        "upscale_min_height",
    ]
    assert info["reencoded"] is True


def test_prepare_image_crops_overly_wide_input() -> None:
    data, _, _, info = AkoolClient._prepare_image(
        image_bytes((3000, 500)),
        "wide.png",
        "image/png",
    )

    with Image.open(io.BytesIO(data)) as prepared:
        assert prepared.size == (1245, 500)
    assert info["operations"] == ["center_crop_max_aspect"]


def test_prepare_image_preserves_compatible_known_input_unless_forced() -> None:
    source = image_bytes((640, 480), "JPEG")
    data, content_type, filename, info = AkoolClient._prepare_image(
        source,
        "ready.jpg",
        "image/jpeg",
    )
    assert data == source
    assert content_type == "image/jpeg"
    assert filename == "ready.jpg"
    assert info["reencoded"] is False

    forced, _, forced_name, forced_info = AkoolClient._prepare_image(
        source,
        "ready.jpg",
        "image/jpeg",
        force=True,
    )
    assert forced
    assert forced_name == "ready-akool.jpg"
    assert forced_info["reencode_reason"] == "forced_retry"


def test_generation_request_matches_captured_protocol() -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    uploads = [upload("image", 1), upload("video", 1, 3000), upload("audio", 1, 4000)]

    request = client.build_generation_request(
        {
            "model": "doubao-seedance-2-0-mini-260615",
            "prompt": "test",
            "duration": 4,
            "resolution": "480p",
            "aspect_ratio": "adaptive",
            "generate_audio": True,
            "web_search": True,
        },
        uploads,
    )

    assert request["prompt"] == (
        "{{profile-video-1}}\n\ntest{{profile-image-1}}{{profile-audio-1}}"
    )
    assert [item["type"] for item in request["prompt_info"]] == ["video", "image", "voice"]
    assert request["videoUrl"] == "https://cdn.example.com/video-1"
    assert "reference_video_urls" not in request
    assert request["reference_audio_urls"] == ["https://cdn.example.com/audio-1"]
    assert request["model_name"] == "doubao-seedance-2-0-mini-260615/image-to-video"
    assert "ratio" not in request


def test_wan_30_request_matches_captured_protocol() -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    request = client.build_generation_request(
        {
            "model": "wan-3.0",
            "prompt": "animate all references",
            "duration": 10,
            "resolution": "720P",
            "aspect_ratio": "9:16",
        },
        [upload("image", 1), upload("video", 1, 2000), upload("audio", 1, 3000)],
    )

    assert request["model_name"] == "alibaba/wan-3.0/image-to-video"
    assert request["audio_type"] == 1
    assert request["ratio"] == "9:16"
    assert request["all_in_one_reference"] is True
    assert request["generate_audio"] is True
    assert request["videoUrl"] == "https://cdn.example.com/video-1"


def test_minimax_h3_request_matches_reference_to_video_capture() -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    request = client.build_generation_request(
        {
            "model": "minimax-h3",
            "prompt": "animate all references",
            "duration": 4,
            "resolution": "768P",
            "aspect_ratio": "4:3",
        },
        [
            upload("image", 1),
            upload("image", 2),
            upload("video", 1, 2000),
            upload("audio", 1, 3000),
        ],
    )

    assert request["model_name"] == "minimax/h3/reference-to-video"
    assert request["audio_type"] == 3
    assert request["ratio"] == "4:3"
    assert request["videoUrl"] == "https://cdn.example.com/video-1"
    assert request["reference_audio_urls"] == ["https://cdn.example.com/audio-1"]
    assert "all_in_one_reference" not in request
    assert "generate_audio" not in request
    assert "web_search" not in request


def test_seedance_20_with_video_keeps_captured_image_to_video_model() -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    request = client.build_generation_request(
        {
            "model": "doubao-seedance-2-0-260128",
            "prompt": "animate",
            "duration": 10,
            "resolution": "1080p",
            "aspect_ratio": "1:1",
        },
        [upload("video", 1, 2000)],
    )

    assert request["model_name"] == "doubao-seedance-2-0-260128/image-to-video"
    assert request["web_search"] is True


def test_seedance_25_request_matches_all_in_one_reference_capture() -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    uploads = [
        upload("image", 1),
        upload("video", 1, 2000),
        upload("audio", 1, 4000),
    ]

    request = client.build_generation_request(
        {
            "model": "doubao-seedance-2-5",
            "prompt": "reference everything",
            "duration": 4,
            "resolution": "480p",
            "aspect_ratio": "21:9",
            "generate_audio": True,
            "all_in_one_reference": True,
        },
        uploads,
    )

    assert request["model_name"] == "doubao/seedance-2-5/reference-to-video"
    assert request["ratio"] == "21:9"
    assert request["video_extend"] is False
    assert request["videoUrl"] == "https://cdn.example.com/video-1"
    assert request["all_in_one_reference"] is True
    assert request["prompt"].startswith("{{profile-video-1}}\n\nreference everything")
    assert [item["type"] for item in request["prompt_info"]] == ["video", "image", "voice"]
    assert "reference_video_urls" not in request
    assert "web_search" not in request


def test_seedance_25_uses_video_url_array_for_multiple_videos() -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    request = client.build_generation_request(
        {
            "model": "doubao-seedance-2-5",
            "prompt": "reference both videos",
            "duration": 4,
            "resolution": "480p",
            "aspect_ratio": "16:9",
        },
        [upload("video", 1, 2000), upload("video", 2, 2000)],
    )

    assert request["videoUrl"] == [
        "https://cdn.example.com/video-1",
        "https://cdn.example.com/video-2",
    ]
    assert request["prompt"].startswith(
        "{{profile-video-1}}{{profile-video-2}}\n\nreference both videos"
    )


def test_generate_classifies_upstream_code_1104_as_insufficient_credit(monkeypatch) -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    body = {
        "code": 1104,
        "msg": "your credits is not enough",
        "data": {"is_pop_upgrade": False, "sub_info": {}},
    }
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: (body, object()))

    with pytest.raises(AkoolUpstreamError) as raised:
        client.generate({"model_name": "doubao/seedance-2-5/reference-to-video"})

    assert raised.value.code == "INSUFFICIENT_CREDITS"
    assert raised.value.status_code == 409
    assert raised.value.details == body


def test_calculate_fee_always_uses_credit_mode(monkeypatch) -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    uploads = [upload("image", 1), upload("video", 1, 3000)]
    captured = {}

    def request(method, path, **kwargs):
        captured.update(kwargs["payload"])
        return {"code": 1000, "data": {"fee": 7}}, object()

    monkeypatch.setattr(client, "_request", request)
    result = client.calculate_fee(
        {},
        uploads,
        {
            "model_name": "doubao-seedance-2-0-mini-260615/image-to-video",
            "resolution": "480p",
            "video_length": 4,
            "generate_audio": True,
        },
    )

    assert result["fee"] == 7
    assert captured["options"]["is_unlimited_model"] is False
    assert captured["options"]["hasVideo"] is True
    assert captured["options"]["image_profile_ids"] == ["profile-image-1"]


def test_minimax_h3_fee_disables_generated_audio_without_submit_field(monkeypatch) -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    captured = {}

    def request(method, path, **kwargs):
        captured.update(kwargs["payload"])
        return {"code": 1000, "data": {"fee": 18}}, object()

    monkeypatch.setattr(client, "_request", request)
    result = client.calculate_fee(
        {"model": "minimax-h3"},
        [upload("image", 1), upload("video", 1, 2000)],
        {
            "model_name": "minimax/h3/reference-to-video",
            "resolution": "768P",
            "video_length": 4,
        },
    )

    assert result["fee"] == 18
    assert captured["options"]["generate_audio"] is False
    assert captured["options"]["hasVideo"] is True


def test_result_url_prefers_captured_video_fields() -> None:
    assert result_urls(
        {
            "external_video": "https://cdn.example.com/result.mp4",
            "video": "https://cdn.example.com/result.mp4",
        }
    ) == ["https://cdn.example.com/result.mp4"]


def test_generation_status_four_is_failed_with_upstream_reason(monkeypatch) -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    body = {
        "code": 1000,
        "data": {
            "result": [
                {
                    "_id": "resource-failed",
                    "video_status": 4,
                    "error_reason": "Request failed. Please check your network and try again.",
                }
            ]
        },
    }
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: (body, object()))

    detail = client.generation_detail("resource-failed")

    assert detail["status"] == "FAILED"
    assert detail["providerStatus"] == 4
    assert detail["error"] == "Request failed. Please check your network and try again."


def test_daily_checkin_matches_captured_status_then_submit_protocol(monkeypatch) -> None:
    client = AkoolClient({"cookie_header": "token=test"}, settings())
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return {
                "code": 1000,
                "msg": "OK",
                "data": {
                    "today_signed": False,
                    "can_checkedin": True,
                    "last_sign": "2026-09-01",
                },
            }, object()
        return {
            "code": 1000,
            "msg": "OK",
            "data": {"day_number": 1, "credits": 5, "total_credits": 25},
        }, object()

    monkeypatch.setattr(client, "_request", request)

    status = client.daily_checkin_status()
    reward = client.daily_checkin()

    assert status["can_checkedin"] is True
    assert reward["credits"] == 5
    assert calls[0][0:2] == (
        "GET",
        "/interface/faceswap-api/api/v6/content/sign/stats",
    )
    assert calls[0][2]["retry"] is True
    assert calls[1][0:2] == (
        "POST",
        "/interface/faceswap-api/api/v6/content/sign",
    )
    assert calls[1][2]["retry"] is False
    assert all(call[2]["referer"].endswith("/zh-cn/pricing") for call in calls)
