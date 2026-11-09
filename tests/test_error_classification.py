"""Protocol error evidence must not convert validation/review into auth replay."""
import pytest

from app.error_classification import (
    classify_error_message,
    is_auth_message,
    is_media_constraint_failure,
    is_media_download_failure,
    is_media_duration_failure,
    is_moderation_failure,
    is_rate_limit_message,
    is_risk_message,
    is_suspended_message,
)


@pytest.mark.parametrize("kind,message", [
    ("media_constraints", "Image upload failed or format not supported. Please re-upload the image."),
    ("media_constraints", "Input image is invalid or unsupported. Please try a different image."),
    ("media_constraints", "Image height must be greater than 300 and less than 6000 pixels"),
    ("media_constraints", "Aspect ratio must be between 0.4 and 2.5."),
    ("media_constraints", "Unsupported MIME type: multipart/form-data"),
    ("media_constraints", "参考图片尺寸不符合模型要求，请调整图片尺寸"),
    ("media_constraints", "不支持的文件类型，请更换素材"),
    ("media_constraints", "Unsupported image format at https://assets.example/media?duration=4"),
    ("media_duration", "Audio reference duration must be between 3s and 12s"),
    ("media_duration", "Audio reference duration must be between 5s and 30s"),
    ("media_duration", "Video duration must be at least 4 seconds"),
    ("media_duration", "Input video length exceeds the maximum"),
    ("media_duration", "参考音频时长过长，最多支持30秒"),
    ("media_duration", "素材时长不符合要求"),
    ("media_download", "Reference image download failed"),
    ("media_download", "Could not download the reference audio"),
    ("media_download", "Input 'audio_3' could not be fetched from 'media.example.com'"),
    ("media_download", "素材下载超时，请检查素材链接"),
    ("media_download", "无法加载参考视频"),
    ("moderation", "Your content was flagged by our moderation system."),
    ("moderation", "The image was rejected by our moderation system."),
    ("moderation", "Sorry, the content violates safety rules. Please try a different image or description."),
    ("moderation", "Generation failed because the generated content contains sensitive information."),
    ("moderation", "Sorry, this content cannot be created due to copyright restrictions."),
    ("moderation", "Input image copyright verification failed."),
    ("moderation", "This request was blocked by content policy."),
    ("moderation", "输入图片版权审核未通过"),
    ("moderation", "视频审核失败，请修改描述"),
    ("moderation", "提示词内容含有敏感或违规情况"),
    ("risk", "Cloudflare human verification required"),
    ("risk", "Verify you are human"),
    ("risk", "Access denied"),
    ("risk", "请完成人机验证后再试"),
    ("risk", "当前请求触发风控拦截"),
    ("auth", "Unauthorized: token expired"),
    ("auth", "Authentication required"),
    ("auth", "Session is invalid"),
    ("auth", "登录已过期，请先登录"),
    ("auth", "令牌无效，请重新认证"),
    ("auth", "Invalid token for image generation"),
    ("rate_limit", "Too many requests from your IP, please retry after 28 seconds"),
    ("rate_limit", "Request limit exceeded"),
    ("rate_limit", "请求频繁，请稍后重试"),
    ("rate_limit", "操作过于频繁，已触发限流"),
    ("rate_limit", "Too many requests to the video endpoint"),
    ("suspended", "Your account has been suspended"),
    ("suspended", "The account is disabled"),
    ("suspended", "账号已封禁，请联系客服"),
])
def test_explicit_english_and_chinese_error_evidence(kind, message):
    assert classify_error_message(message) == kind


@pytest.mark.parametrize("message", [
    "Image upload failed or format not supported. Please re-upload the image.",
    "参考图片尺寸不符合模型要求",
    "Audio reference duration must be between 3s and 12s",
    "Input image copyright verification failed.",
    "Your content violates safety rules. Please login again.",
    "视频审核失败，请修改描述",
])
def test_media_and_moderation_evidence_never_requests_login_or_human_verification(message):
    assert not is_auth_message(message)
    assert not is_risk_message(message)
    assert not is_rate_limit_message(message)


@pytest.mark.parametrize("message", [None, "", 200, "HTTP 200", "ok", "success",
    "Image uploaded successfully", "duration=4", "Invalid parameters", "Unknown provider error"])
def test_absent_or_ambiguous_evidence_does_not_infer_success_or_an_error_class(message):
    assert classify_error_message(message) is None


def test_actual_kling_format_failure_is_not_moderation_or_login():
    message = "Image upload failed or format not supported. Please re-upload the image."
    assert is_media_constraint_failure(message)
    assert not is_moderation_failure(message)
    assert not is_auth_message(message)
    assert not is_risk_message(message)


def test_count_limit_is_not_a_request_rate_limit():
    assert not is_rate_limit_message("Too many reference images; maximum count is 3")


def test_suspension_is_distinct_from_login_or_manual_challenge():
    assert is_suspended_message("The account has been suspended")
    assert not is_auth_message("The account has been suspended")
    assert not is_risk_message("The account has been suspended")


def test_download_failure_is_not_an_unsupported_media_error():
    assert is_media_download_failure("Reference image download timed out")
    assert not is_media_constraint_failure("Reference image download timed out")
    assert not is_media_duration_failure("Reference image download timed out")
