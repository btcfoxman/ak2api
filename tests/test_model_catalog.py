from __future__ import annotations

import pytest

from app.model_catalog import normalize_generation_request


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


def test_minimax_h3_normalizes_768p_and_drops_unsupported_audio() -> None:
    result = normalize_generation_request(
        {
            "model": "minimax/h3",
            "prompt": "animate",
            "duration": 5,
            "resolution": "768p",
            "aspect_ratio": "16:9",
            "image_urls": ["https://example.com/start.png"],
            "audio_urls": ["https://example.com/a.mp3"],
        },
        excess_media_policy="ignore",
    )

    assert result["model"] == "minimax-h3"
    assert result["resolution"] == "768P"
    assert result["_audio"] == []
