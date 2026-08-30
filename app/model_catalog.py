from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any


ASPECT_RATIOS = ("adaptive", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
RESOLUTION_ALIASES = {
    "standard": "480p",
    "480": "480p",
    "480p": "480p",
    "hd": "720p",
    "720": "720p",
    "720p": "720p",
    "full_hd": "1080p",
    "fullhd": "1080p",
    "1080": "1080p",
    "1080p": "1080p",
    "2k": "2k",
    "768": "768P",
    "768p": "768P",
    "4k": "4k",
}


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    label: str
    upstream_model: str
    provider: str
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    max_images: int
    max_videos: int
    max_audio: int
    max_audio_seconds: int
    max_video_seconds: int
    generate_audio: bool = True
    all_in_one_reference: bool = True
    web_search: bool = False
    prompt_max_length: int = 20_000
    audio_type: int = 1
    include_generate_audio: bool = True
    include_all_in_one_reference: bool = True


MODEL_SPECS: dict[str, ModelSpec] = {
    "doubao-seedance-2-0-mini-260615": ModelSpec(
        id="doubao-seedance-2-0-mini-260615",
        label="Seedance 2.0 Mini",
        upstream_model="doubao-seedance-2-0-mini-260615/image-to-video",
        provider="maas",
        durations=tuple(range(4, 16)),
        resolutions=("480p", "720p"),
        aspect_ratios=ASPECT_RATIOS,
        max_images=9,
        max_videos=3,
        max_audio=3,
        max_audio_seconds=15,
        max_video_seconds=15,
        web_search=True,
    ),
    "doubao-seedance-2-0-fast-260128": ModelSpec(
        id="doubao-seedance-2-0-fast-260128",
        label="Seedance 2.0 Fast",
        upstream_model="doubao-seedance-2-0-fast-260128/image-to-video",
        provider="maas",
        durations=tuple(range(4, 16)),
        resolutions=("480p", "720p"),
        aspect_ratios=ASPECT_RATIOS,
        max_images=9,
        max_videos=3,
        max_audio=3,
        max_audio_seconds=15,
        max_video_seconds=15,
        web_search=True,
    ),
    "doubao-seedance-2-0-260128": ModelSpec(
        id="doubao-seedance-2-0-260128",
        label="Seedance 2.0",
        upstream_model="doubao-seedance-2-0-260128/image-to-video",
        provider="maas",
        durations=tuple(range(4, 16)),
        resolutions=("480p", "720p", "1080p", "4k"),
        aspect_ratios=ASPECT_RATIOS,
        max_images=9,
        max_videos=3,
        max_audio=3,
        max_audio_seconds=15,
        max_video_seconds=15,
        web_search=True,
    ),
    "doubao-seedance-2-5": ModelSpec(
        id="doubao-seedance-2-5",
        label="Seedance 2.5 Reference",
        upstream_model="doubao/seedance-2-5/reference-to-video",
        provider="maas",
        durations=tuple(range(4, 31)),
        resolutions=("480p", "720p", "1080p"),
        aspect_ratios=ASPECT_RATIOS,
        max_images=30,
        max_videos=10,
        max_audio=10,
        max_audio_seconds=30,
        max_video_seconds=30,
    ),
    "wan-3.0": ModelSpec(
        id="wan-3.0",
        label="Wan 3.0",
        upstream_model="alibaba/wan-3.0/image-to-video",
        provider="maas",
        durations=tuple(range(4, 16)),
        resolutions=("480P", "720P", "1080P"),
        aspect_ratios=ASPECT_RATIOS,
        max_images=9,
        max_videos=3,
        max_audio=3,
        max_audio_seconds=15,
        max_video_seconds=15,
    ),
    "minimax-h3": ModelSpec(
        id="minimax-h3",
        label="Minimax H3",
        upstream_model="minimax/h3/reference-to-video",
        provider="maas",
        durations=tuple(range(4, 16)),
        resolutions=("768P", "2k"),
        aspect_ratios=ASPECT_RATIOS,
        max_images=9,
        max_videos=3,
        max_audio=3,
        max_audio_seconds=15,
        max_video_seconds=15,
        generate_audio=False,
        all_in_one_reference=False,
        prompt_max_length=7000,
        audio_type=3,
        include_generate_audio=False,
        include_all_in_one_reference=False,
    ),
}

DEFAULT_MODEL_MAP = {
    "doubao-seedance-2-0-mini-260615": "doubao-seedance-2-0-mini-260615",
    "seedance-2.0-mini": "doubao-seedance-2-0-mini-260615",
    "doubao-seedance-2-0-fast-260128": "doubao-seedance-2-0-fast-260128",
    "seedance-2.0-fast": "doubao-seedance-2-0-fast-260128",
    "sd-2-0-fast": "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-2-0-260128": "doubao-seedance-2-0-260128",
    "seedance-2.0": "doubao-seedance-2-0-260128",
    "sd-2-0": "doubao-seedance-2-0-260128",
    "doubao-seedance-2-5": "doubao-seedance-2-5",
    "seedance-2.5": "doubao-seedance-2-5",
    "wan-3.0": "wan-3.0",
    "wan3.0": "wan-3.0",
    "alibaba/wan-3.0": "wan-3.0",
    "alibaba/wan-3.0/image-to-video": "wan-3.0",
    "minimax-h3": "minimax-h3",
    "minimax/h3": "minimax-h3",
    "minimax/h3/reference-to-video": "minimax-h3",
}

MEDIA_LIMITS = {
    "images": max(spec.max_images for spec in MODEL_SPECS.values()),
    "videos": max(spec.max_videos for spec in MODEL_SPECS.values()),
    "audio": max(spec.max_audio for spec in MODEL_SPECS.values()),
}

# Kept for database cost normalization compatibility. Akool uses named tiers.
VIDEO_DIMENSIONS: dict[str, dict[str, tuple[int, int]]] = {}


def parse_model_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        source = value
    else:
        text = str(value or "").strip()
        if not text:
            return dict(DEFAULT_MODEL_MAP)
        try:
            source = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("model_map must be a JSON object") from exc
    if not isinstance(source, dict):
        raise ValueError("model_map must be a JSON object")
    mapping = {
        str(key).strip(): str(target).strip()
        for key, target in source.items()
        if str(key).strip() and str(target).strip()
    }
    unsupported = sorted({target for target in mapping.values() if target not in MODEL_SPECS})
    if unsupported:
        raise ValueError(f"unsupported Akool model targets: {', '.join(unsupported)}")
    return {**DEFAULT_MODEL_MAP, **mapping}


def model_map_json(value: Any) -> str:
    return json.dumps(parse_model_map(value), ensure_ascii=False, separators=(",", ":"))


def mapped_model(model: Any, model_map: Any = None) -> str:
    requested = str(model or "doubao-seedance-2-0-mini-260615").strip()
    target = parse_model_map(model_map).get(requested, requested)
    if target not in MODEL_SPECS:
        raise ValueError(f"unsupported model: {requested}")
    return target


def model_spec(model: Any, model_map: Any = None) -> ModelSpec:
    return MODEL_SPECS[mapped_model(model, model_map)]


def _source_item(value: Any, kind: str, index: int) -> dict[str, str] | None:
    if isinstance(value, str):
        source = value.strip()
        name = ""
    elif isinstance(value, dict):
        source = str(
            value.get("value")
            or value.get("url")
            or value.get(f"{kind}_url")
            or value.get("data")
            or ""
        ).strip()
        name = str(value.get("name") or value.get("filename") or "").strip()
    else:
        return None
    if not source:
        return None
    return {"value": source, "name": name or f"{kind}-{index + 1}"}


def _direct_media(payload: dict[str, Any], kind: str) -> list[dict[str, str]]:
    keys = {
        "image": ("image_urls", "images", "reference_images"),
        "video": ("video_urls", "videos", "reference_videos"),
        "audio": ("audio_urls", "audios", "reference_audios"),
    }[kind]
    items: list[Any] = []
    for key in keys:
        value = payload.get(key)
        if value is not None:
            items.extend(value if isinstance(value, list) else [value])
    singular = payload.get(f"{kind}_url")
    if singular:
        items.append(singular)
    if kind == "image":
        for key in ("image", "first_frame", "last_frame", "tail_image"):
            if payload.get(key):
                items.append(payload[key])
    return [
        item
        for index, value in enumerate(items)
        if (item := _source_item(value, kind, index)) is not None
    ]


def _content_media(payload: dict[str, Any], kind: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for entry in payload.get("content") or []:
        if not isinstance(entry, dict):
            continue
        entry_type = str(entry.get("type") or "").lower()
        if kind not in entry_type and not (kind == "audio" and "voice" in entry_type):
            continue
        if item := _source_item(entry, kind, len(result)):
            result.append(item)
    return result


def _deduplicate(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in items:
        if item["value"] in seen:
            continue
        seen.add(item["value"])
        result.append(item)
    return result


def _normalize_resolution(value: Any, spec: ModelSpec) -> str:
    raw = str(value or spec.resolutions[0]).strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    normalized = RESOLUTION_ALIASES.get(key, raw)
    for supported in spec.resolutions:
        if supported.lower() == normalized.lower():
            return supported
    raise ValueError(
        f"{spec.id} resolution must be one of {', '.join(spec.resolutions)}"
    )


def _apply_media_limit(
    values: list[dict[str, str]],
    *,
    limit: int,
    kind: str,
    spec: ModelSpec,
    policy: str,
) -> list[dict[str, str]]:
    if policy == "strict" and len(values) > limit:
        raise ValueError(f"{spec.id} supports at most {limit} {kind}")
    return values[:limit]


_REFERENCE_PATTERN = re.compile(r"(?i)@(?:图(?:片)?|image|视频|video|音频|声音|audio)\s*\d*")


def normalize_generation_request(
    payload: dict[str, Any],
    model_map: Any = None,
    excess_media_policy: str = "ignore",
    cleanup_prompt_references: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    policy = str(excess_media_policy or "ignore").lower()
    if policy not in {"ignore", "strict"}:
        raise ValueError("excess_media_policy must be ignore or strict")
    spec = model_spec(payload.get("model"), model_map)
    try:
        duration = int(payload.get("duration") or spec.durations[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be an integer number of seconds") from exc
    if duration not in spec.durations:
        raise ValueError(
            f"{spec.id} duration must be one of {', '.join(map(str, spec.durations))} seconds"
        )
    ratio = str(
        payload.get("aspect_ratio")
        or payload.get("aspectRatio")
        or payload.get("ratio")
        or "adaptive"
    ).strip()
    if ratio not in spec.aspect_ratios:
        raise ValueError(
            f"{spec.id} aspect_ratio must be one of {', '.join(spec.aspect_ratios)}"
        )
    prompt = str(payload.get("prompt") or payload.get("input") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if len(prompt) > spec.prompt_max_length:
        raise ValueError(f"{spec.id} prompt exceeds {spec.prompt_max_length} characters")

    images = _apply_media_limit(
        _deduplicate(_direct_media(payload, "image") + _content_media(payload, "image")),
        limit=spec.max_images,
        kind="images",
        spec=spec,
        policy=policy,
    )
    videos = _apply_media_limit(
        _deduplicate(_direct_media(payload, "video") + _content_media(payload, "video")),
        limit=spec.max_videos,
        kind="videos",
        spec=spec,
        policy=policy,
    )
    audio = _apply_media_limit(
        _deduplicate(_direct_media(payload, "audio") + _content_media(payload, "audio")),
        limit=spec.max_audio,
        kind="audio files",
        spec=spec,
        policy=policy,
    )
    if cleanup_prompt_references:
        prompt = re.sub(r"[ \t]{2,}", " ", _REFERENCE_PATTERN.sub("", prompt)).strip()

    requested_generate_audio = payload.get("generate_audio")
    generate_audio = spec.generate_audio and (
        True if requested_generate_audio is None else bool(requested_generate_audio)
    )
    requested_web_search = payload.get("web_search")
    web_search = spec.web_search and (
        True if requested_web_search is None else bool(requested_web_search)
    )
    requested_all_in_one = payload.get("all_in_one_reference")
    all_in_one_reference = spec.all_in_one_reference and (
        True if requested_all_in_one is None else bool(requested_all_in_one)
    )
    normalized = dict(payload)
    normalized.update(
        {
            "kind": "video",
            "model": spec.id,
            "upstream_model": spec.upstream_model,
            "prompt": prompt,
            "duration": duration,
            "resolution": _normalize_resolution(payload.get("resolution"), spec),
            "aspect_ratio": ratio,
            "generate_audio": generate_audio,
            "web_search": web_search,
            "all_in_one_reference": all_in_one_reference,
            "negative_prompt": str(
                payload.get("negative_prompt") or payload.get("negativePrompt") or ""
            ).strip(),
            "_images": images,
            "_videos": videos,
            "_audio": audio,
        }
    )
    return normalized


def public_models(model_map: Any = None) -> list[dict[str, Any]]:
    mapping = parse_model_map(model_map)
    aliases: dict[str, list[str]] = {key: [] for key in MODEL_SPECS}
    for alias, target in mapping.items():
        aliases[target].append(alias)
    result: list[dict[str, Any]] = []
    for spec in MODEL_SPECS.values():
        result.append(
            {
                "id": spec.id,
                "object": "model",
                "type": "video",
                "owned_by": "akool",
                "aliases": sorted(set(aliases[spec.id])),
                "meta": {
                    "label": spec.label,
                    "provider": "Akool",
                    "upstream_model": spec.upstream_model,
                },
                "capabilities": {
                    "durations": list(spec.durations),
                    "aspect_ratios": list(spec.aspect_ratios),
                    "resolutions": list(spec.resolutions),
                    "generate_audio": spec.generate_audio,
                    "media_limits": {
                        "images": spec.max_images,
                        "videos": spec.max_videos,
                        "audio": spec.max_audio,
                    },
                    "max_audio_seconds": spec.max_audio_seconds,
                    "max_video_seconds": spec.max_video_seconds,
                },
            }
        )
    return result


def model_specs_json() -> list[dict[str, Any]]:
    return [asdict(spec) for spec in MODEL_SPECS.values()]
