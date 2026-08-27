from __future__ import annotations

import json
from http.cookies import SimpleCookie
from typing import Any


def normalize_cookie_header(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith(("[", "{")):
        converted = _json_cookie_header(value)
        if converted:
            return converted
    cookie = SimpleCookie()
    try:
        cookie.load(value)
    except Exception:
        return "; ".join(part.strip() for part in value.split(";") if "=" in part)
    return "; ".join(f"{name}={item.value}" for name, item in cookie.items())


def _json_cookie_header(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    values: list[dict[str, Any]]
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        values = payload["cookies"]
    elif isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        return "; ".join(f"{key}={value}" for key, value in payload.items())
    else:
        return ""
    return "; ".join(
        f"{item.get('name')}={item.get('value', '')}"
        for item in values
        if isinstance(item, dict) and item.get("name")
    )


def cookie_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [
            dict(item) for item in value if isinstance(item, dict) and item.get("name")
        ]
    if isinstance(value, dict):
        nested = value.get("cookies")
        if isinstance(nested, list):
            return cookie_records(nested)
    if isinstance(value, str) and value.strip().startswith(("[", "{")):
        try:
            return cookie_records(json.loads(value))
        except json.JSONDecodeError:
            return []
    return []


def cookie_header_from_records(records: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{item.get('name')}={item.get('value', '')}"
        for item in records
        if item.get("name")
    )


def masked_cookie_header(header: str) -> str:
    names = [
        part.partition("=")[0].strip()
        for part in normalize_cookie_header(header).split(";")
        if "=" in part
    ]
    return "; ".join(f"{name}=***" for name in names)
