from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.model_catalog import DEFAULT_MODEL_MAP


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def normalize_proxy_url(proxy_url: str) -> str:
    value = str(proxy_url or "").strip()
    if not value:
        return ""
    if "://" not in value and re.fullmatch(r"(?:\[[^]]+]|[^\s:/]+):\d+", value):
        return f"socks5://{value}"
    return value


def rewrite_loopback_proxy(proxy_url: str, host_override: str) -> str:
    proxy_url = normalize_proxy_url(proxy_url)
    host_override = str(host_override or "").strip().strip("[]")
    if not proxy_url or not host_override:
        return proxy_url
    parsed = urlsplit(proxy_url)
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        return proxy_url
    userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    host = f"[{host_override}]" if ":" in host_override else host_override
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(parsed._replace(netloc=f"{userinfo}{host}{port}"))


@dataclass(slots=True)
class Settings:
    api_key: str
    admin_token: str
    sync_token: str
    database_path: str
    task_workers: int
    task_queue_capacity: int
    poll_interval_seconds: int
    task_timeout_seconds: int
    synchronous_timeout_seconds: int
    request_timeout_seconds: int
    request_retries: int
    account_maintenance_interval_seconds: int
    account_maintenance_workers: int
    account_default_concurrency: int
    media_timeout_seconds: int
    media_max_bytes: int
    media_fallback_proxy_url: str
    browser_recovery_enabled: bool
    browser_timeout_seconds: int
    browser_login_workers: int
    browser_login_stagger_seconds: float
    browser_challenge_grace_seconds: int
    chrome_executable: str
    chrome_user_data_root: str
    chrome_cdp_base_port: int
    chrome_headless: bool
    proxy_host_override: str
    proxy_pool_enabled: bool
    proxy_pool: str
    low_balance_disable_threshold: float
    excess_media_policy: str
    prompt_media_reference_cleanup_enabled: bool
    model_map: str
    schema_version: str


def load_settings() -> Settings:
    data_dir = Path(_env("AK_DATA_DIR", "data"))
    api_key = _env("AK_API_KEY", "sk-test-api-key")
    return Settings(
        api_key=api_key,
        admin_token=_env("AK_ADMIN_TOKEN", api_key),
        sync_token=_env("AK_SYNC_TOKEN", api_key),
        database_path=_env("AK_DATABASE_PATH", str(data_dir / "ak2api.db")),
        task_workers=_env_int("AK_TASK_WORKERS", 10, 1, 50),
        task_queue_capacity=_env_int("AK_TASK_QUEUE_CAPACITY", 100, 0, 5000),
        poll_interval_seconds=_env_int("AK_POLL_INTERVAL_SECONDS", 10, 2, 120),
        task_timeout_seconds=_env_int("AK_TASK_TIMEOUT_SECONDS", 1800, 60, 7200),
        synchronous_timeout_seconds=_env_int(
            "AK_SYNCHRONOUS_TIMEOUT_SECONDS", 900, 30, 3600
        ),
        request_timeout_seconds=_env_int("AK_REQUEST_TIMEOUT_SECONDS", 90, 10, 600),
        request_retries=_env_int("AK_REQUEST_RETRIES", 2, 0, 5),
        account_maintenance_interval_seconds=_env_int(
            "AK_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS", 300, 30, 86400
        ),
        account_maintenance_workers=_env_int(
            "AK_ACCOUNT_MAINTENANCE_WORKERS", 3, 1, 20
        ),
        account_default_concurrency=_env_int(
            "AK_ACCOUNT_DEFAULT_CONCURRENCY", 8, 1, 100
        ),
        media_timeout_seconds=_env_int("AK_MEDIA_TIMEOUT_SECONDS", 180, 10, 900),
        media_max_bytes=_env_int(
            "AK_MEDIA_MAX_BYTES", 100 * 1024 * 1024, 1024, 250 * 1024 * 1024
        ),
        media_fallback_proxy_url=_env(
            "AK_MEDIA_FALLBACK_PROXY_URL", "http://127.0.0.1:10809"
        ),
        browser_recovery_enabled=_env_bool("AK_BROWSER_RECOVERY_ENABLED", True),
        browser_timeout_seconds=_env_int("AK_BROWSER_TIMEOUT_SECONDS", 180, 30, 900),
        browser_login_workers=_env_int("AK_BROWSER_LOGIN_WORKERS", 2, 1, 10),
        browser_login_stagger_seconds=_env_float(
            "AK_BROWSER_LOGIN_STAGGER_SECONDS", 3, 0, 60
        ),
        browser_challenge_grace_seconds=_env_int(
            "AK_BROWSER_CHALLENGE_GRACE_SECONDS", 12, 3, 120
        ),
        chrome_executable=_env("AK_CHROME_EXECUTABLE", ""),
        chrome_user_data_root=_env(
            "AK_CHROME_USER_DATA_ROOT", "data/ak-chrome-profiles"
        ),
        chrome_cdp_base_port=_env_int("AK_CHROME_CDP_BASE_PORT", 19800, 1024, 64000),
        chrome_headless=_env_bool("AK_CHROME_HEADLESS", False),
        proxy_host_override=_env("AK_PROXY_HOST_OVERRIDE", ""),
        proxy_pool_enabled=_env_bool("AK_PROXY_POOL_ENABLED", True),
        proxy_pool=_env("AK_PROXY_POOL", ""),
        low_balance_disable_threshold=_env_float(
            "AK_LOW_BALANCE_DISABLE_THRESHOLD", 1, 0, 1_000_000_000
        ),
        excess_media_policy=_env("AK_EXCESS_MEDIA_POLICY", "ignore").lower(),
        prompt_media_reference_cleanup_enabled=_env_bool(
            "AK_PROMPT_MEDIA_REFERENCE_CLEANUP_ENABLED", False
        ),
        model_map=_env(
            "AK_MODEL_MAP",
            json.dumps(DEFAULT_MODEL_MAP, ensure_ascii=False, separators=(",", ":")),
        ),
        schema_version=_env("AK_SCHEMA_VERSION", "0.1.0"),
    )


settings = load_settings()
