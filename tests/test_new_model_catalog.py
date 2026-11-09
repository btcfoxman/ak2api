"""Contracts from AK #78's 2026-11-09 mergedList and successful UI submits.

No provider request is executed; source values are intentionally limited to
public model controls and media constraints, without credentials or prompts.
"""
import pytest

from app.model_catalog import MODEL_SPECS, model_spec, normalize_generation_request, public_models


def request(model, **changes):
    return {"model": model, "prompt": "A fictional scene", **changes}


@pytest.mark.parametrize("name,target", [
    ("kling-4.0", "kling-4.0"),
    ("kwaivgi/kling-v4.0/reference-to-video", "kling-4.0"),
    ("vidu-q4", "vidu-q4"),
    ("vidu/q4-preview/reference-to-video", "vidu-q4"),
])
def test_captured_reference_model_names_have_stable_public_ids(name, target):
    normalized = normalize_generation_request(request(name))
    assert normalized["model"] == target
    assert normalized["upstream_model"] == MODEL_SPECS[target].upstream_model


@pytest.mark.parametrize("duration", range(3, 31))
def test_kling_duration_range_matches_reference_catalog(duration):
    assert normalize_generation_request(request("kling-4.0", duration=duration))["duration"] == duration


@pytest.mark.parametrize("duration", range(3, 17))
def test_vidu_duration_range_matches_reference_catalog(duration):
    assert normalize_generation_request(request("vidu-q4", duration=duration))["duration"] == duration


@pytest.mark.parametrize("model,duration", [
    ("kling-4.0", 2), ("kling-4.0", 31), ("vidu-q4", 2), ("vidu-q4", 17),
])
def test_outside_catalog_duration_is_rejected_before_upstream_work(model, duration):
    with pytest.raises(ValueError, match="duration must be one of"):
        normalize_generation_request(request(model, duration=duration))


@pytest.mark.parametrize("model,resolutions", [
    ("kling-4.0", ("720p", "1080p")),
    ("vidu-q4", ("540p", "720p", "1080p", "2k", "4k")),
])
def test_resolution_enum_retains_captured_casing(model, resolutions):
    for value in resolutions:
        assert normalize_generation_request(request(model, resolution=value.upper()))["resolution"] == value
    with pytest.raises(ValueError, match="resolution must be one of"):
        normalize_generation_request(request(model, resolution="480p"))


def test_catalog_defaults_and_successful_website_controls_are_retained():
    kling = normalize_generation_request(request("kling-4.0"))
    vidu = normalize_generation_request(request("vidu-q4"))
    assert (kling["duration"], kling["resolution"], kling["aspect_ratio"]) == (10, "720p", "adaptive")
    assert (vidu["duration"], vidu["resolution"], vidu["aspect_ratio"]) == (5, "720p", "16:9")
    assert normalize_generation_request(request("kling-4.0", aspect_ratio="auto"))["aspect_ratio"] == "adaptive"
    assert normalize_generation_request(request("kling-4.0", duration=3, resolution="720p", aspect_ratio="21:9"))["aspect_ratio"] == "21:9"
    assert normalize_generation_request(request("vidu-q4", duration=4, resolution="540p", aspect_ratio="16:9"))["resolution"] == "540p"
    assert normalize_generation_request(request("vidu-q4", duration=3, resolution="720p", aspect_ratio="1:1"))["duration"] == 3
    for model, ratio in (("kling-4.0", "4:3"), ("vidu-q4", "21:9")):
        with pytest.raises(ValueError, match="aspect_ratio must be one of"):
            normalize_generation_request(request(model, aspect_ratio=ratio))


@pytest.mark.parametrize("policy", ["strict", "ignore"])
@pytest.mark.parametrize("key", ["video_urls", "videos", "reference_videos", "video_url"])
def test_vidu_rejects_video_references_even_when_excess_policy_ignores(policy, key):
    value = "https://fixture.invalid/input.mp4"
    with pytest.raises(ValueError, match="vidu-q4 does not support video references"):
        normalize_generation_request(request("vidu-q4", **{key: value}), excess_media_policy=policy)


def test_vidu_content_video_reference_is_also_rejected():
    with pytest.raises(ValueError, match="does not support video references"):
        normalize_generation_request(request("vidu-q4", content=[{
            "type": "video_url", "video_url": "https://fixture.invalid/input.mp4",
        }]))


@pytest.mark.parametrize("policy", ["strict", "ignore"])
def test_kling_mixed_fifteen_reference_limit_is_not_bypassed_by_per_kind_limits(policy):
    refs = {
        "image_urls": [f"https://fixture.invalid/image-{i}.png" for i in range(10)],
        "video_urls": [f"https://fixture.invalid/video-{i}.mp4" for i in range(3)],
        "audio_urls": [f"https://fixture.invalid/audio-{i}.mp3" for i in range(3)],
    }
    with pytest.raises(ValueError, match="at most 15 reference inputs in total"):
        normalize_generation_request(request("kling-4.0", **refs), excess_media_policy=policy)
    refs["audio_urls"].pop()
    normalized = normalize_generation_request(request("kling-4.0", **refs), excess_media_policy=policy)
    assert sum(len(normalized[key]) for key in ("_images", "_videos", "_audio")) == 15


def test_global_reference_limit_applies_before_ignore_can_discard_media():
    with pytest.raises(ValueError, match="at most 15 reference inputs in total"):
        normalize_generation_request(request("kling-4.0", image_urls=[
            f"https://fixture.invalid/image-{i}.png" for i in range(16)
        ]), excess_media_policy="ignore")


def test_vidu_accepts_twelve_images_and_three_audio_with_no_video():
    normalized = normalize_generation_request(request("vidu-q4",
        image_urls=[f"https://fixture.invalid/image-{i}.png" for i in range(12)],
        audio_urls=[f"https://fixture.invalid/audio-{i}.mp3" for i in range(3)],
    ), excess_media_policy="strict")
    assert (len(normalized["_images"]), len(normalized["_audio"])) == (12, 3)
    assert normalize_generation_request(request("vidu-q4"))["_images"] == []
    assert not model_spec("vidu-q4").requires_text_video_image


@pytest.mark.parametrize("model,limit", [("kling-4.0", 8000), ("vidu-q4", 5000)])
def test_model_specific_prompt_length_is_checked_before_submission(model, limit):
    normalize_generation_request(request(model, prompt="x"*limit))
    with pytest.raises(ValueError, match="prompt exceeds"):
        normalize_generation_request(request(model, prompt="x"*(limit+1)))


def test_model_controls_preserve_reference_audio_without_forcing_an_audio_type():
    kling = model_spec("kling-4.0")
    vidu = model_spec("vidu-q4")
    assert (kling.model_mode, vidu.model_mode) == ("references", "references")
    assert not kling.include_audio_type and not vidu.include_audio_type
    assert kling.generate_audio and not kling.include_generate_audio
    assert kling.fee_generate_audio is False
    assert vidu.generate_audio and vidu.include_generate_audio
    assert (kling.min_audio_seconds, kling.max_audio_seconds) == (5, 30)
    assert (vidu.min_audio_seconds, vidu.max_audio_seconds) == (3, 12)
    assert kling.audio_duration_per_item and vidu.audio_duration_per_item
    assert kling.min_video_seconds == 3 and kling.max_video_seconds == 30
    assert vidu.audio_content_types == ("audio/mpeg",)


@pytest.mark.parametrize("model", [
    "doubao-seedance-2-0-mini-260615", "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-2-0-260128", "doubao-seedance-2-5", "wan-3.0", "minimax-h3",
])
def test_existing_model_default_and_media_policy_remain_unchanged(model):
    spec = model_spec(model)
    normalized = normalize_generation_request(request(model))
    assert normalized["duration"] == spec.durations[0]
    assert normalized["resolution"] == spec.resolutions[0]
    assert normalized["aspect_ratio"] == "adaptive"
    assert not spec.model_mode and spec.include_audio_type
    assert not spec.min_audio_seconds and not spec.min_video_seconds
    assert not spec.max_reference_inputs and not spec.audio_duration_per_item
    assert not spec.audio_content_types and spec.requires_text_video_image


def test_public_models_describe_actual_reference_constraints_and_defaults():
    models = {row["id"]: row["capabilities"] for row in public_models()}
    assert models["kling-4.0"]["media_limits"] == {"images": 10, "videos": 5, "audio": 3}
    assert models["vidu-q4"]["media_limits"] == {"images": 12, "videos": 0, "audio": 3}
    for model in ("kling-4.0", "vidu-q4"):
        assert models[model]["max_reference_inputs"] == 15
        assert models[model]["audio_duration_per_item"]
        assert models[model]["default_resolution"] == "720p"
    assert models["vidu-q4"]["audio_content_types"] == ["audio/mpeg"]
