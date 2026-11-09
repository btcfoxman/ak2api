"""Pure Akool error evidence; HTTP status alone never proves acceptance.

These helpers do not decide whether a paid request can be retried. Media
validation and content moderation do not indicate that login should repeat.
"""
from __future__ import annotations

import re
from typing import Any


def _text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def is_suspended_message(message: Any) -> bool:
    value = _text(message)
    return bool(re.search(r"(?:account|user).{0,20}(?:has been |is |was )?(?:suspended|banned|disabled)", value)) or any(
        text in value for text in ("账号已封禁", "账户已封禁", "账号被封禁", "账户被封禁", "账号已停用", "账户已停用", "账号被禁用", "账户被禁用")
    )


def is_moderation_failure(message: Any) -> bool:
    value = _text(message)
    if any(text in value for text in (
        "content was flagged by our moderation system", "content violates safety rules",
        "content policy violation", "blocked by content policy", "content moderation failed",
        "content moderation rejected",
        "copyright restrictions", "copyright verification failed", "copyright review failed",
        "sensitive content", "inappropriate content", "prohibited content", "nsfw content",
        "敏感或违规", "违规内容", "敏感内容", "内容违规", "文字违规", "文本违规",
        "图片违规", "视频违规", "音频违规", "版权审核未通过", "版权审核不通过",
        "版权限制", "侵犯版权", "侵犯第三方权益", "违反社区规范", "含有不当内容",
        "内容审核失败", "图片版权审核失败", "视频审核失败", "文本审核失败",
    )):
        return True
    return bool(re.search(
        r"(?:content|prompt|image|video|audio|input).{0,100}(?:flagged|blocked|rejected|failed|violat(?:e|es|ed|ing)|contains?).{0,60}(?:moderation|content policy|safety (?:rules|policy)|copyright|sensitive information)|"
        r"(?:violat(?:e|es|ed|ing)|against).{0,40}(?:content policy|safety rules|community guidelines|copyright)|"
        r"(?:内容|文本|文字|图片|视频|音频|提示词|素材).{0,30}(?:审核|审查).{0,20}(?:不通过|未通过|拒绝)", value
    ))


def is_media_duration_failure(message: Any) -> bool:
    value = _text(message)
    if is_moderation_failure(value):
        return False
    return bool(re.search(
        r"\b(?:duration|length)\b.{0,60}\b(?:must|should|between|at least|at most|exceed\w*|too (?:short|long)|unsupported|not supported|invalid|out of range)\b|"
        r"\b(?:unsupported|invalid|excessive|too short|too long)\s+(?:(?:input|reference|audio|video|media)\s+)*(?:duration|length)\b|"
        r"时长.{0,24}(?:不支持|不符合|超出|超过|过长|过短|至少|最多|必须|应当|需在|不能|不满足)|"
        r"不支持(?:的)?(?:素材|参考|音频|视频)*时长", value
    ))


def is_media_download_failure(message: Any) -> bool:
    value = _text(message)
    if is_moderation_failure(value):
        return False
    return bool(re.search(
        r"(?:素材|参考图|参考视频|参考音频).{0,40}(?:下载|获取|读取|加载).{0,25}(?:失败|超时|不可用|无法)|"
        r"(?:无法|不能).{0,15}(?:下载|获取|读取|加载).{0,25}(?:素材|参考图|参考视频|参考音频)|"
        r"(?:input|reference|media|image|video|audio).{0,80}(?:download|fetch).{0,40}(?:failed|timed out|timeout)|"
        r"(?:input|reference|media|image|video|audio).{0,80}(?:could not|cannot|failed to|unable to).{0,20}(?:fetch|download|load)|"
        r"(?:failed|unable|cannot|could not).{0,15}(?:download|fetch).{0,40}(?:input|reference|media|image|video|audio)", value
    ))


def is_media_constraint_failure(message: Any) -> bool:
    value = _text(message)
    if is_moderation_failure(value) or is_media_download_failure(value):
        return False
    if _has_auth_evidence(value) or _has_rate_evidence(value):
        return False
    if is_media_duration_failure(value):
        return True
    if any(text in value for text in (
        "image upload failed or format not supported", "invalid image", "invalid video", "invalid audio",
        "unsupported image", "unsupported video", "unsupported audio", "unsupported mime", "unsupported file type",
        "invalid mime", "file format is not supported", "file format not supported", "invalid file format",
        "素材格式不支持", "素材尺寸不符合", "参考图片尺寸不符合", "图片尺寸不符合", "图片格式不支持",
        "视频格式不支持", "音频格式不支持", "不支持的素材格式", "不支持的文件类型",
    )):
        return True
    subject = bool(re.search(
        r"\b(?:image|video|audio|media|reference|width|height|resolution|pixels?|aspect ratio|file (?:type|size|format)|mime)\b|"
        r"素材|参考图|参考视频|参考音频|图片|视频|音频|尺寸|分辨率|宽度|高度|格式|文件类型", value
    ))
    invalid = bool(re.search(
        r"must|should|not supported|unsupported|invalid|incorrect|out of range|exceed|too (?:small|large|many)|"
        r"less than|greater than|at least|at most|not exceed|不支持|不符合|超出|超过|过大|过小|太多|无效|必须|不能|不满足", value
    ))
    return subject and invalid


def _has_rate_evidence(value: str) -> bool:
    return any(text in value for text in (
        "too many requests", "rate limit", "rate_limit", "ratelimit", "request limit exceeded",
        "request frequency", "请求过于频繁", "请求太频繁", "请求频率过高", "请求频率超过", "操作过于频繁",
        "操作太频繁", "请求频繁", "操作频繁", "访问频率过高", "速率限制", "请求被限流", "触发限流",
    ))


def is_rate_limit_message(message: Any) -> bool:
    value = _text(message)
    if is_moderation_failure(value) or is_media_constraint_failure(value) or is_media_download_failure(value):
        return False
    return _has_rate_evidence(value)


def _has_auth_evidence(value: str) -> bool:
    return any(text in value for text in (
        "unauthorized", "not logged in", "login required", "invalid token", "token expired", "expired token",
        "session expired", "session is invalid", "authentication required", "authentication expired",
        "登录已过期", "登录过期", "登录失效", "登录状态已失效", "请先登录", "尚未登录", "未登录",
        "会话过期", "会话已过期", "会话失效", "令牌无效", "无效令牌", "令牌已过期", "认证已过期",
    ))


def is_auth_message(message: Any) -> bool:
    value = _text(message)
    if is_suspended_message(value) or is_moderation_failure(value) or is_media_constraint_failure(value) or is_media_download_failure(value) or is_rate_limit_message(value):
        return False
    return _has_auth_evidence(value)


def is_risk_message(message: Any) -> bool:
    value = _text(message)
    if is_suspended_message(value) or is_moderation_failure(value) or is_media_constraint_failure(value) or is_media_download_failure(value) or is_rate_limit_message(value) or _has_auth_evidence(value):
        return False
    return any(text in value for text in (
        "turnstile", "cloudflare", "verify you are human", "confirm you are human", "checking your browser",
        "captcha", "challenge-platform", "access denied", "security checkpoint", "human verification",
        "人机验证", "人机校验", "验证您是人类", "确认您是人类", "请完成验证码", "验证码校验失败",
        "安全验证", "浏览器验证", "风控拦截", "触发风控", "访问被拒绝", "访问被拒", "访问受限",
    ))


def classify_error_message(message: Any) -> str | None:
    for kind, check in (
        ("suspended", is_suspended_message),
        ("moderation", is_moderation_failure),
        ("media_duration", is_media_duration_failure),
        ("media_download", is_media_download_failure),
        ("media_constraints", is_media_constraint_failure),
        ("rate_limit", is_rate_limit_message),
        ("auth", is_auth_message),
        ("risk", is_risk_message),
    ):
        if check(message):
            return kind
    return None
