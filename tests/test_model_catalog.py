from __future__ import annotations

import pytest

from app.model_catalog import normalize_generation_request
from app.schemas import GenerationTaskCreate


def payload() -> dict:
    return {
        "model": "seedance-2.0-mini",
        "prompt": "保持所有参考素材主体一致",
        "duration": 4,
        "resolution": "480p",
        "aspect_ratio": "adaptive",
        "image_urls": [f"https://example.com/i-{index}.png" for index in range(10)],
        "video_urls": [f"https://example.com/v-{index}.mp4" for index in range(4)],
        "audio_urls": [f"https://example.com/a-{index}.mp3" for index in range(4)],
    }


def test_seedance_mini_ignores_excess_media() -> None:
    result = normalize_generation_request(payload(), excess_media_policy="ignore")

    assert result["model"] == "doubao-seedance-2-0-mini-260615"
    assert len(result["_images"]) == 9
    assert len(result["_videos"]) == 3
    assert len(result["_audio"]) == 3


def test_strict_policy_rejects_excess_media() -> None:
    with pytest.raises(ValueError, match="at most 9 images"):
        normalize_generation_request(payload(), excess_media_policy="strict")


def test_minimax_h3_normalizes_reference_media_and_disables_generated_audio() -> None:
    result = normalize_generation_request(
        {
            "model": "minimax/h3",
            "prompt": "animate",
            "duration": 5,
            "resolution": "768p",
            "aspect_ratio": "16:9",
            "image_urls": [
                f"https://example.com/image-{index}.png" for index in range(4)
            ],
            "video_urls": ["https://example.com/reference.mp4"],
            "audio_urls": ["https://example.com/a.mp3"],
            "generate_audio": True,
            "web_search": True,
            "all_in_one_reference": True,
        },
        excess_media_policy="ignore",
    )

    assert result["model"] == "minimax-h3"
    assert result["upstream_model"] == "minimax/h3/reference-to-video"
    assert result["resolution"] == "768P"
    assert len(result["_images"]) == 4
    assert len(result["_videos"]) == 1
    assert len(result["_audio"]) == 1
    assert result["generate_audio"] is False
    assert result["web_search"] is False
    assert result["all_in_one_reference"] is False


def test_wan_30_uses_captured_resolution_casing() -> None:
    result = normalize_generation_request(
        {
            "model": "alibaba/wan-3.0",
            "prompt": "animate all references",
            "duration": 10,
            "resolution": "720p",
            "aspect_ratio": "9:16",
            "image_urls": ["https://example.com/reference.png"],
            "video_urls": ["https://example.com/reference.mp4"],
            "audio_urls": ["https://example.com/reference.mp3"],
        }
    )

    assert result["model"] == "wan-3.0"
    assert result["upstream_model"] == "alibaba/wan-3.0/image-to-video"
    assert result["resolution"] == "720P"
    assert result["generate_audio"] is True
    assert result["all_in_one_reference"] is True


def test_sd_aliases_map_to_captured_seedance_models() -> None:
    standard = normalize_generation_request(
        {"model": "sd-2-0", "prompt": "animate", "duration": 10}
    )
    fast = normalize_generation_request(
        {"model": "sd-2-0-fast", "prompt": "animate", "duration": 10}
    )

    assert standard["model"] == "doubao-seedance-2-0-260128"
    assert fast["model"] == "doubao-seedance-2-0-fast-260128"


def test_seedance_25_keeps_full_all_in_one_reference_capacity() -> None:
    external_request = GenerationTaskCreate(
        **{
            "model": "seedance-2.5",
            "prompt": "use every reference",
            "duration": 30,
            "resolution": "1080p",
            "aspect_ratio": "21:9",
            "image_urls": [
                f"https://example.com/image-{index}.png" for index in range(30)
            ],
            "video_urls": [
                f"https://example.com/video-{index}.mp4" for index in range(10)
            ],
            "audio_urls": [
                f"https://example.com/audio-{index}.mp3" for index in range(10)
            ],
        }
    ).model_dump(exclude_none=True)
    result = normalize_generation_request(
        external_request,
        excess_media_policy="strict",
    )

    assert result["model"] == "doubao-seedance-2-5"
    assert result["upstream_model"] == "doubao/seedance-2-5/reference-to-video"
    assert result["all_in_one_reference"] is True
    assert result["generate_audio"] is True
    assert result["web_search"] is False
    assert len(result["_images"]) == 30
    assert len(result["_videos"]) == 10
    assert len(result["_audio"]) == 10
