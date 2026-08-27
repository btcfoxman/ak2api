from __future__ import annotations

from types import SimpleNamespace

from app.akool_client import AkoolClient, MediaUpload, result_urls


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

    assert request["prompt"] == "test{{profile-image-1}}{{profile-video-1}}{{profile-audio-1}}"
    assert [item["type"] for item in request["prompt_info"]] == ["image", "video", "voice"]
    assert request["reference_video_urls"] == ["https://cdn.example.com/video-1"]
    assert request["reference_audio_urls"] == ["https://cdn.example.com/audio-1"]
    assert request["model_name"] == "doubao-seedance-2-0-mini-260615/image-to-video"


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


def test_result_url_prefers_captured_video_fields() -> None:
    assert result_urls(
        {
            "external_video": "https://cdn.example.com/result.mp4",
            "video": "https://cdn.example.com/result.mp4",
        }
    ) == ["https://cdn.example.com/result.mp4"]
