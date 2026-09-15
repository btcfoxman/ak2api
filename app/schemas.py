from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowserCookie(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1)
    value: str = ""
    domain: str = ""
    path: str = "/"


class AccountUpsert(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str = Field(min_length=1, max_length=120)
    email: str = Field(default="", max_length=320)
    password: str = ""
    access_token: str = ""
    user_id: str = ""
    team_id: str = ""
    cookies: str = ""
    cookie_header: str = ""
    cookie_records: list[BrowserCookie] = Field(default_factory=list)
    user_agent: str = ""
    sec_ch_ua: str = ""
    sec_ch_ua_platform: str = ""
    proxy_url: str = Field(default="", max_length=500)
    profile_dir: str = ""
    cdp_port: int | None = Field(default=None, ge=1024, le=65535)
    enabled: bool = True
    auto_login: bool = True
    max_concurrency: int | None = Field(default=None, ge=1, le=100)
    use_proxy_pool: bool = True
    last_balance: int | float | str | None = None
    plan: str = ""


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    password: str | None = None
    access_token: str | None = None
    user_id: str | None = None
    team_id: str | None = None
    cookies: str | None = None
    cookie_header: str | None = None
    cookie_records: list[BrowserCookie] | None = None
    proxy_url: str | None = Field(default=None, max_length=500)
    profile_dir: str | None = None
    cdp_port: int | None = Field(default=None, ge=1024, le=65535)
    enabled: bool | None = None
    auto_login: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=100)


class AccountProfileReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy_url: str | None = Field(default=None, max_length=500)
    use_proxy_pool: bool = True
    start_login: bool = True


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    action: Literal["click", "scroll", "login"]
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.5, ge=0, le=1)
    delta_y: int = Field(default=0, ge=-1000, le=1000)


class AccountSyncRequest(AccountUpsert):
    name: str = Field(default="", max_length=120)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_workers: int | None = Field(default=None, ge=1, le=50)
    task_queue_capacity: int | None = Field(default=None, ge=0, le=5000)
    poll_interval_seconds: int | None = Field(default=None, ge=2, le=120)
    task_timeout_seconds: int | None = Field(default=None, ge=60, le=7200)
    request_timeout_seconds: int | None = Field(default=None, ge=10, le=600)
    request_retries: int | None = Field(default=None, ge=0, le=5)
    account_maintenance_interval_seconds: int | None = Field(
        default=None, ge=30, le=86400
    )
    account_maintenance_workers: int | None = Field(default=None, ge=1, le=20)
    daily_checkin_enabled: bool | None = None
    browser_recovery_enabled: bool | None = None
    browser_timeout_seconds: int | None = Field(default=None, ge=30, le=900)
    browser_login_workers: int | None = Field(default=None, ge=1, le=10)
    browser_login_stagger_seconds: float | None = Field(default=None, ge=0, le=60)
    browser_challenge_grace_seconds: int | None = Field(default=None, ge=3, le=120)
    chrome_executable: str | None = None
    chrome_user_data_root: str | None = None
    chrome_headless: bool | None = None
    proxy_host_override: str | None = None
    proxy_pool_enabled: bool | None = None
    proxy_pool: str | None = None
    low_balance_disable_threshold: float | None = Field(default=None, ge=0)
    excess_media_policy: Literal["ignore", "strict"] | None = None
    allow_video_reference_inputs: bool | None = None
    prompt_media_reference_cleanup_enabled: bool | None = None
    model_map: str | None = Field(default=None, max_length=20000)


class GenerationTaskCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(
        default="doubao-seedance-2-0-mini-260615",
        min_length=1,
        max_length=160,
    )
    prompt: str = Field(min_length=1, max_length=30000)
    content: list[dict[str, Any]] = Field(default_factory=list)
    image_urls: list[Any] = Field(default_factory=list)
    video_urls: list[Any] = Field(default_factory=list)
    audio_urls: list[Any] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None
    size: str = ""
    aspect_ratio: str = "adaptive"
    duration: int = 4
    resolution: str = "480p"
    n: int = Field(default=1, ge=1, le=1)
    generate_audio: bool | None = None
    web_search: bool | None = None
    extend_prompt: bool = True
    video_extend: bool | None = None
    all_in_one_reference: bool | None = None
    negative_prompt: str = ""
    account_id: int | None = None
