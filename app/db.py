from __future__ import annotations

import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.model_catalog import RESOLUTION_ALIASES, VIDEO_DIMENSIONS


ACCOUNT_SECRET_FIELDS = {
    "password",
    "access_token",
    "cookie_header",
    "cookies_json",
}

_COST_RESOLUTION_LABELS = {
    "standard": "480p",
    "hd": "720p",
    "full_hd": "1080p",
    "4k": "4k",
}
_COST_RESOLUTION_BY_DIMENSIONS = {
    dimensions: _COST_RESOLUTION_LABELS[tier]
    for tier, aspect_ratios in VIDEO_DIMENSIONS.items()
    for dimensions in aspect_ratios.values()
}
_BUILTIN_VIDEO_COST_RATES: dict[tuple[str, str], Decimal] = {}


def now_ts() -> int:
    return int(time.time())


def now_usage_ts() -> int:
    return time.time_ns()


def normalize_account_identity(value: Any) -> str:
    return str(value or "").strip().casefold()


def account_identity_key(payload: dict[str, Any]) -> str:
    email = normalize_account_identity(payload.get("email"))
    name = normalize_account_identity(payload.get("name"))
    if not email and "@" in name:
        email = name
    if email:
        return f"email:{email}"
    return f"name:{name}" if name else ""


def _row(value: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(value) if value is not None else None


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _cost_integer(value: Any, default: int = 0) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _normalize_cost_resolution(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("×", "x")
    if not raw or raw == "-":
        return "-"
    if "x" in raw:
        left, _, right = raw.partition("x")
        width = max(_cost_integer(left), 0)
        height = max(_cost_integer(right), 0)
        if width and height:
            return _COST_RESOLUTION_BY_DIMENSIONS.get(
                (width, height),
                f"{width}x{height}",
            )
    return RESOLUTION_ALIASES.get(raw.replace("-", "_"), raw)


def _model_cost_key(payload: dict[str, Any]) -> tuple[Any, ...]:
    width = max(_cost_integer(payload.get("width")), 0)
    height = max(_cost_integer(payload.get("height")), 0)
    resolution = (
        _normalize_cost_resolution(f"{width}x{height}")
        if width and height
        else _normalize_cost_resolution(
            payload.get("resolution") or payload.get("mode")
        )
    )
    return (
        str(payload.get("model") or "").strip(),
        max(_cost_integer(payload.get("duration")), 0),
        resolution,
    )


def _builtin_video_cost(payload: dict[str, Any]) -> float:
    model, duration, resolution = _model_cost_key(payload)
    rate = _BUILTIN_VIDEO_COST_RATES.get((model, resolution))
    if rate is None or duration <= 0:
        return 0
    return float(int(rate * Decimal(duration)))


def _builtin_video_cost_rows() -> list[dict[str, Any]]:
    return [
        {
            "model": model,
            "duration": None,
            "resolution": resolution,
            "cost": None,
            "latest_cost": None,
            "samples": 0,
            "last_task_id": "",
            "updated_at": None,
            "source": "builtin_rate",
            "rate_per_second": float(rate),
        }
        for (model, resolution), rate in sorted(_BUILTIN_VIDEO_COST_RATES.items())
    ]


def _final_generation_cost(value: Any, fallback: Any = 0) -> float:
    response = _json(value, {})
    attempts = response.get("attempts") if isinstance(response, dict) else None
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            generated = attempt.get("generate") if isinstance(attempt, dict) else None
            if not isinstance(generated, dict):
                continue
            try:
                cost = float(
                    generated.get("fee")
                    or generated.get("actual_cost")
                    or generated.get("apiCreditCost")
                    or 0
                )
            except (TypeError, ValueError):
                continue
            if cost > 0:
                return round(cost, 2)
    try:
        return round(float(fallback or 0), 2)
    except (TypeError, ValueError):
        return 0


class Database:
    def __init__(self, path: str, default_concurrency: int = 8):
        self.path = str(path)
        self.default_concurrency = max(int(default_concurrency), 1)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    cognito_sub TEXT NOT NULL DEFAULT '',
                    access_token TEXT NOT NULL DEFAULT '',
                    access_token_expires_at INTEGER,
                    cookie_header TEXT NOT NULL DEFAULT '',
                    cookies_json TEXT NOT NULL DEFAULT '[]',
                    user_agent TEXT NOT NULL DEFAULT '',
                    sec_ch_ua TEXT NOT NULL DEFAULT '',
                    sec_ch_ua_platform TEXT NOT NULL DEFAULT '',
                    proxy_url TEXT NOT NULL DEFAULT '',
                    profile_dir TEXT NOT NULL DEFAULT '',
                    cdp_port INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    auto_login INTEGER NOT NULL DEFAULT 1,
                    max_concurrency INTEGER NOT NULL DEFAULT 8,
                    active_tasks INTEGER NOT NULL DEFAULT 0,
                    total_uses INTEGER NOT NULL DEFAULT 0,
                    last_balance REAL,
                    balance_details_json TEXT NOT NULL DEFAULT '{}',
                    plan TEXT NOT NULL DEFAULT '',
                    tos_hash TEXT NOT NULL DEFAULT '',
                    tos_accepted_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_checked_at INTEGER,
                    last_login_at INTEGER,
                    last_used_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ak_accounts_available
                    ON accounts(enabled, status, active_tasks, total_uses);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    request_json TEXT NOT NULL DEFAULT '{}',
                    caller_request_json TEXT NOT NULL DEFAULT '{}',
                    upstream_request_json TEXT NOT NULL DEFAULT '{}',
                    upstream_response_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress INTEGER NOT NULL DEFAULT 0,
                    channel TEXT NOT NULL DEFAULT '',
                    account_id INTEGER,
                    generation_id TEXT NOT NULL DEFAULT '',
                    result_urls_json TEXT NOT NULL DEFAULT '[]',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    raw_status_json TEXT NOT NULL DEFAULT '{}',
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    reserved_cost REAL NOT NULL DEFAULT 0,
                    actual_cost REAL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ak_tasks_status
                    ON tasks(status, created_at);

                CREATE TABLE IF NOT EXISTS model_cost_records (
                    task_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    duration INTEGER NOT NULL DEFAULT 0,
                    resolution TEXT NOT NULL DEFAULT '-',
                    cost REAL NOT NULL,
                    recorded_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            account_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
            }
            if "balance_details_json" not in account_columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN "
                    "balance_details_json TEXT NOT NULL DEFAULT '{}'"
                )
            for column, definition in (
                ("tos_hash", "TEXT NOT NULL DEFAULT ''"),
                ("tos_accepted_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in account_columns:
                    connection.execute(
                        f"ALTER TABLE accounts ADD COLUMN {column} {definition}"
                    )
            task_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            for column, definition in (
                ("channel", "TEXT NOT NULL DEFAULT ''"),
                ("estimated_cost", "REAL NOT NULL DEFAULT 0"),
                ("reserved_cost", "REAL NOT NULL DEFAULT 0"),
                ("actual_cost", "REAL"),
            ):
                if column not in task_columns:
                    connection.execute(
                        f"ALTER TABLE tasks ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ak_tasks_account_reservation
                ON tasks(account_id)
                WHERE reserved_cost > 0
                """
            )
            self._ensure_model_cost_schema(connection)
            connection.execute(
                "UPDATE accounts SET active_tasks = 0, updated_at = ?",
                (now_ts(),),
            )
            connection.execute(
                "UPDATE tasks SET reserved_cost = 0 WHERE reserved_cost != 0"
            )
            self._normalize_model_cost_resolutions(connection)
            self._backfill_model_cost_records(connection)

    def upsert_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = now_ts()
        name = str(payload.get("name") or payload.get("email") or "").strip()
        if not name:
            raise ValueError("account name or email is required")
        cookie_header = str(
            payload.get("cookie_header") or payload.get("cookies") or ""
        ).strip()
        cookie_values = payload.get("cookie_records")
        if cookie_values is None:
            cookie_values = payload.get("cookies_json")
        records = _json(cookie_values, [])
        if not isinstance(records, list):
            records = []
        access_token = str(
            payload.get("access_token") or payload.get("token") or ""
        ).strip()
        max_concurrency = max(
            int(payload.get("max_concurrency") or self.default_concurrency),
            1,
        )
        balance_details = payload.get("balance_details")
        if balance_details is None:
            balance_details = payload.get("balance_details_json")
        values = {
            "name": name,
            "email": str(payload.get("email") or "").strip(),
            "password": str(payload.get("password") or ""),
            "user_id": str(payload.get("user_id") or "").strip(),
            "cognito_sub": str(
                payload.get("team_id") or payload.get("cognito_sub") or ""
            ).strip(),
            "access_token": access_token,
            "access_token_expires_at": payload.get("access_token_expires_at"),
            "cookie_header": cookie_header,
            "cookies_json": json.dumps(records, ensure_ascii=False),
            "user_agent": str(payload.get("user_agent") or "").strip(),
            "sec_ch_ua": str(payload.get("sec_ch_ua") or "").strip(),
            "sec_ch_ua_platform": str(payload.get("sec_ch_ua_platform") or "").strip(),
            "proxy_url": str(payload.get("proxy_url") or "").strip(),
            "profile_dir": str(payload.get("profile_dir") or "").strip(),
            "cdp_port": payload.get("cdp_port"),
            "enabled": 1 if payload.get("enabled", True) else 0,
            "auto_login": 1 if payload.get("auto_login", True) else 0,
            "max_concurrency": max_concurrency,
            "last_balance": payload.get("last_balance"),
            "balance_details_json": json.dumps(
                _json(balance_details, {}), ensure_ascii=False
            ),
            "plan": str(payload.get("plan") or "").strip(),
            "status": str(payload.get("status") or "pending").strip(),
            "last_error": str(payload.get("last_error") or "").strip(),
            "last_checked_at": payload.get("last_checked_at"),
            "last_login_at": payload.get("last_login_at"),
            "now": now,
        }
        with self._lock, self.connect() as connection:
            existing = self._find_account_row(connection, values)
            if existing is not None:
                values["name"] = str(existing["name"])
            connection.execute(
                """
                INSERT INTO accounts (
                    name, email, password, user_id, cognito_sub, access_token,
                    access_token_expires_at, cookie_header, cookies_json,
                    user_agent, sec_ch_ua, sec_ch_ua_platform, proxy_url,
                    profile_dir, cdp_port, enabled, auto_login, max_concurrency,
                    last_balance, balance_details_json, plan, status, last_error, last_checked_at,
                    last_login_at, created_at, updated_at
                ) VALUES (
                    :name, :email, :password, :user_id, :cognito_sub, :access_token,
                    :access_token_expires_at, :cookie_header, :cookies_json,
                    :user_agent, :sec_ch_ua, :sec_ch_ua_platform, :proxy_url,
                    :profile_dir, :cdp_port, :enabled, :auto_login, :max_concurrency,
                    :last_balance, :balance_details_json, :plan, :status, :last_error, :last_checked_at,
                    :last_login_at, :now, :now
                )
                ON CONFLICT(name) DO UPDATE SET
                    email = CASE WHEN excluded.email != '' THEN excluded.email ELSE accounts.email END,
                    password = CASE WHEN excluded.password != '' THEN excluded.password ELSE accounts.password END,
                    user_id = CASE WHEN excluded.user_id != '' THEN excluded.user_id ELSE accounts.user_id END,
                    cognito_sub = CASE WHEN excluded.cognito_sub != '' THEN excluded.cognito_sub ELSE accounts.cognito_sub END,
                    access_token = CASE WHEN excluded.access_token != '' THEN excluded.access_token ELSE accounts.access_token END,
                    access_token_expires_at = COALESCE(excluded.access_token_expires_at, accounts.access_token_expires_at),
                    cookie_header = CASE WHEN excluded.cookie_header != '' THEN excluded.cookie_header ELSE accounts.cookie_header END,
                    cookies_json = CASE WHEN excluded.cookies_json != '[]' THEN excluded.cookies_json ELSE accounts.cookies_json END,
                    user_agent = CASE WHEN excluded.user_agent != '' THEN excluded.user_agent ELSE accounts.user_agent END,
                    sec_ch_ua = CASE WHEN excluded.sec_ch_ua != '' THEN excluded.sec_ch_ua ELSE accounts.sec_ch_ua END,
                    sec_ch_ua_platform = CASE WHEN excluded.sec_ch_ua_platform != '' THEN excluded.sec_ch_ua_platform ELSE accounts.sec_ch_ua_platform END,
                    proxy_url = CASE WHEN excluded.proxy_url != '' THEN excluded.proxy_url ELSE accounts.proxy_url END,
                    profile_dir = CASE WHEN excluded.profile_dir != '' THEN excluded.profile_dir ELSE accounts.profile_dir END,
                    cdp_port = COALESCE(excluded.cdp_port, accounts.cdp_port),
                    enabled = excluded.enabled,
                    auto_login = excluded.auto_login,
                    max_concurrency = excluded.max_concurrency,
                    last_balance = COALESCE(excluded.last_balance, accounts.last_balance),
                    balance_details_json = CASE
                        WHEN excluded.balance_details_json != '{}'
                        THEN excluded.balance_details_json
                        ELSE accounts.balance_details_json
                    END,
                    plan = CASE WHEN excluded.plan != '' THEN excluded.plan ELSE accounts.plan END,
                    status = CASE
                        WHEN excluded.access_token != '' OR excluded.cookie_header != ''
                        THEN excluded.status
                        ELSE accounts.status
                    END,
                    last_error = CASE
                        WHEN excluded.access_token != '' OR excluded.cookie_header != ''
                        THEN excluded.last_error
                        ELSE accounts.last_error
                    END,
                    last_checked_at = COALESCE(excluded.last_checked_at, accounts.last_checked_at),
                    last_login_at = COALESCE(excluded.last_login_at, accounts.last_login_at),
                    updated_at = excluded.updated_at
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM accounts WHERE name = ?",
                (values["name"],),
            ).fetchone()
        return self._account_view(dict(row), include_secrets=True)

    def _find_account_row(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
    ) -> sqlite3.Row | None:
        email = normalize_account_identity(payload.get("email"))
        name = normalize_account_identity(payload.get("name"))
        if not email and "@" in name:
            email = name
        if email:
            row = connection.execute(
                """
                SELECT * FROM accounts
                WHERE email != '' AND lower(trim(email)) = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (email,),
            ).fetchone()
            if row is not None:
                return row
            row = connection.execute(
                """
                SELECT * FROM accounts
                WHERE email = '' AND lower(trim(name)) = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (email,),
            ).fetchone()
            if row is not None:
                return row
        if not name:
            return None
        row = connection.execute(
            """
            SELECT * FROM accounts
            WHERE lower(trim(name)) = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (name,),
        ).fetchone()
        if (
            row is not None
            and email
            and normalize_account_identity(row["email"])
            and normalize_account_identity(row["email"]) != email
        ):
            raise ValueError("account name is already assigned to another email")
        return row

    def find_account_by_identity(
        self,
        payload: dict[str, Any],
        *,
        include_secrets: bool = False,
    ) -> dict[str, Any] | None:
        with self._lock, self.connect() as connection:
            row = self._find_account_row(connection, payload)
        return (
            self._account_view(dict(row), include_secrets=include_secrets)
            if row is not None
            else None
        )

    def list_accounts(self, *, include_secrets: bool = False) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    account.*,
                    COALESCE((
                        SELECT SUM(task.reserved_cost)
                        FROM tasks AS task
                        WHERE task.account_id = account.id
                          AND task.reserved_cost > 0
                    ), 0) AS reserved_balance
                FROM accounts AS account
                ORDER BY account.id ASC
                """
            ).fetchall()
        return [
            self._account_view(dict(row), include_secrets=include_secrets)
            for row in rows
        ]

    def get_account(
        self,
        account_id: int,
        *,
        include_secrets: bool = True,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    account.*,
                    COALESCE((
                        SELECT SUM(task.reserved_cost)
                        FROM tasks AS task
                        WHERE task.account_id = account.id
                          AND task.reserved_cost > 0
                    ), 0) AS reserved_balance
                FROM accounts AS account
                WHERE account.id = ?
                """,
                (int(account_id),),
            ).fetchone()
        return (
            self._account_view(dict(row), include_secrets=include_secrets)
            if row
            else None
        )

    def _account_view(
        self,
        account: dict[str, Any],
        *,
        include_secrets: bool,
    ) -> dict[str, Any]:
        value = dict(account)
        for key in ("enabled", "auto_login"):
            value[key] = bool(value.get(key))
        value["cookie_records"] = _json(value.get("cookies_json"), [])
        value["balance_details"] = _json(value.get("balance_details_json"), {})
        max_concurrency = max(int(value.get("max_concurrency") or 1), 1)
        value["max_concurrency"] = max_concurrency
        reserved_balance = max(float(value.get("reserved_balance") or 0), 0)
        value["reserved_balance"] = round(reserved_balance, 2)
        raw_balance = value.get("last_balance")
        value["available_balance"] = (
            None
            if raw_balance is None
            else round(max(float(raw_balance) - reserved_balance, 0), 2)
        )
        usable = value["enabled"] and value.get("status") in {
            "active",
            "pending",
        }
        value["available_slots"] = (
            max(max_concurrency - int(value.get("active_tasks") or 0), 0)
            if usable
            else 0
        )
        if not include_secrets:
            for key in ACCOUNT_SECRET_FIELDS:
                value[key] = "***" if value.get(key) else ""
            value["cookie_records"] = [
                {
                    "name": item.get("name"),
                    "domain": item.get("domain"),
                    "path": item.get("path", "/"),
                    "value": "***",
                }
                for item in value["cookie_records"]
                if isinstance(item, dict)
            ]
        value["cookies"] = value.get("cookie_header") or ""
        value["token"] = value.get("access_token") or ""
        value["team_id"] = value.get("cognito_sub") or ""
        return value

    def update_account(
        self,
        account_id: int,
        changes: dict[str, Any],
    ) -> dict[str, Any] | None:
        aliases = {
            "cookies": "cookie_header",
            "token": "access_token",
            "team_id": "cognito_sub",
            "cookie_records": "cookies_json",
            "balance_details": "balance_details_json",
        }
        allowed = {
            "name",
            "email",
            "password",
            "user_id",
            "cognito_sub",
            "access_token",
            "access_token_expires_at",
            "cookie_header",
            "cookies_json",
            "user_agent",
            "sec_ch_ua",
            "sec_ch_ua_platform",
            "proxy_url",
            "profile_dir",
            "cdp_port",
            "enabled",
            "auto_login",
            "max_concurrency",
            "active_tasks",
            "total_uses",
            "last_balance",
            "balance_details_json",
            "plan",
            "tos_hash",
            "tos_accepted_at",
            "status",
            "last_error",
            "last_checked_at",
            "last_login_at",
            "last_used_at",
        }
        clean: dict[str, Any] = {}
        for raw_key, raw_value in changes.items():
            key = aliases.get(raw_key, raw_key)
            if key not in allowed:
                continue
            if key in {"cookies_json", "balance_details_json"} and not isinstance(
                raw_value, str
            ):
                fallback = [] if key == "cookies_json" else {}
                raw_value = json.dumps(raw_value or fallback, ensure_ascii=False)
            clean[key] = raw_value
        if not clean:
            return self.get_account(account_id)
        for key in ("enabled", "auto_login"):
            if key in clean:
                clean[key] = 1 if clean[key] else 0
        if "max_concurrency" in clean:
            clean["max_concurrency"] = max(int(clean["max_concurrency"]), 1)
        clean["updated_at"] = now_ts()
        clean["id"] = int(account_id)
        assignments = ", ".join(f"{key} = :{key}" for key in clean if key != "id")
        with self._lock, self.connect() as connection:
            connection.execute(
                f"UPDATE accounts SET {assignments} WHERE id = :id",
                clean,
            )
        return self.get_account(account_id)

    def disable_account_for_low_balance_if_idle(
        self,
        account_id: int,
        threshold: float,
    ) -> dict[str, Any] | None:
        """Disable a low-balance account only when it has no active tasks."""
        threshold = max(float(threshold or 0), 0)
        now = now_ts()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT last_balance
                FROM accounts
                WHERE id = ?
                  AND enabled = 1
                  AND active_tasks = 0
                  AND last_balance IS NOT NULL
                  AND last_balance < ?
                """,
                (int(account_id), threshold),
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            balance = float(row["last_balance"])
            cursor = connection.execute(
                """
                UPDATE accounts
                SET enabled = 0,
                    status = 'disabled_low_balance',
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                  AND enabled = 1
                  AND active_tasks = 0
                  AND last_balance IS NOT NULL
                  AND last_balance < ?
                """,
                (
                    f"余额 {balance:g} 低于自动禁用阈值 {threshold:g}",
                    now,
                    int(account_id),
                    threshold,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            return None
        return self.get_account(account_id, include_secrets=False)

    def delete_account(self, account_id: int) -> bool:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM accounts WHERE id = ?",
                (int(account_id),),
            ).fetchone()
            if not row:
                return False
            if bool(row["enabled"]):
                raise ValueError("account must be disabled before deletion")
            cursor = connection.execute(
                "DELETE FROM accounts WHERE id = ?",
                (int(account_id),),
            )
        return cursor.rowcount > 0

    def available_account_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM accounts
                WHERE enabled = 1
                  AND status IN ('active', 'pending')
                """
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def active_task_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM tasks
                WHERE status NOT IN ('succeeded', 'failed', 'expired')
                """
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def acquire_account(
        self,
        preferred_id: int | None = None,
        *,
        exclude_ids: set[int] | None = None,
        kind: str = "",
        minimum_balance: float = 0,
        quota_mode: str = "all",
        non_video_threshold: float = 320,
        task_id: str | None = None,
        recovering: bool = False,
        reservation_cost: float = 0,
    ) -> dict[str, Any] | None:
        if quota_mode == "video_only" and kind != "video":
            return None
        reservation_cost = max(float(reservation_cost or 0), 0)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parameters: list[Any] = []
            eligibility_clause = (
                "candidate.enabled = 1 "
                "AND candidate.status IN ('active', 'pending')"
            )
            if recovering and preferred_id is not None:
                eligibility_clause = (
                    f"(({eligibility_clause}) OR candidate.id = ?)"
                )
                parameters.append(int(preferred_id))
            preferred_clause = ""
            if preferred_id is not None:
                preferred_clause = "AND candidate.id = ?"
                parameters.append(int(preferred_id))
            excluded_clause = ""
            excluded = sorted({int(value) for value in (exclude_ids or set())})
            if excluded:
                placeholders = ", ".join("?" for _ in excluded)
                excluded_clause = f"AND candidate.id NOT IN ({placeholders})"
                parameters.extend(excluded)
            balance_clause = ""
            minimum_balance = max(
                float(minimum_balance or 0),
                reservation_cost,
                0,
            )
            if minimum_balance > 0:
                balance_clause = "AND candidate.available_balance >= ?"
                parameters.append(minimum_balance)
            quota_clause = ""
            if quota_mode == "auto":
                if kind == "video":
                    quota_clause = (
                        "AND (candidate.available_balance IS NULL "
                        "OR candidate.available_balance >= ?)"
                    )
                else:
                    quota_clause = (
                        "AND (candidate.available_balance IS NULL "
                        "OR candidate.available_balance < ?)"
                    )
                parameters.append(max(float(non_video_threshold or 0), 0))
            row = connection.execute(
                f"""
                WITH candidates AS (
                    SELECT
                        account.*,
                        COALESCE((
                            SELECT SUM(task.reserved_cost)
                            FROM tasks AS task
                            WHERE task.account_id = account.id
                              AND task.reserved_cost > 0
                        ), 0) AS reserved_balance,
                        CASE
                            WHEN account.last_balance IS NULL THEN NULL
                            ELSE account.last_balance - COALESCE((
                                SELECT SUM(task.reserved_cost)
                                FROM tasks AS task
                                WHERE task.account_id = account.id
                                  AND task.reserved_cost > 0
                            ), 0)
                        END AS available_balance
                    FROM accounts AS account
                )
                SELECT candidate.*
                FROM candidates AS candidate
                WHERE {eligibility_clause}
                  AND candidate.active_tasks < candidate.max_concurrency
                  {preferred_clause}
                  {excluded_clause}
                  {balance_clause}
                  {quota_clause}
                  AND (
                    TRIM(candidate.proxy_url) = ''
                    OR NOT EXISTS (
                      SELECT 1
                      FROM accounts AS busy
                      WHERE busy.id != candidate.id
                        AND busy.active_tasks > 0
                        AND busy.proxy_url = candidate.proxy_url
                    )
                )
                ORDER BY
                  CASE WHEN candidate.available_balance IS NULL THEN 1 ELSE 0 END ASC,
                  candidate.available_balance ASC,
                  CASE
                    WHEN candidate.available_balance IS NULL THEN candidate.total_uses
                    ELSE candidate.id
                  END ASC,
                  CASE
                    WHEN candidate.available_balance IS NULL THEN candidate.active_tasks
                    ELSE 0
                  END ASC,
                  CASE
                    WHEN candidate.available_balance IS NULL
                      AND candidate.last_used_at IS NOT NULL THEN 1
                    ELSE 0
                  END ASC,
                  candidate.last_used_at ASC,
                  candidate.id ASC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            now = now_ts()
            if task_id:
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET account_id = ?, reserved_cost = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (int(row["id"]), reservation_cost, now, str(task_id)),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise ValueError("task not found while reserving account balance")
            connection.execute(
                """
                UPDATE accounts
                SET active_tasks = active_tasks + 1,
                    total_uses = total_uses + 1,
                    last_used_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_usage_ts(), now, int(row["id"])),
            )
            connection.commit()
        return self.get_account(int(row["id"]), include_secrets=True)

    def release_account(self, account_id: int, *, task_id: str | None = None) -> None:
        with self._lock, self.connect() as connection:
            if task_id:
                connection.execute(
                    """
                    UPDATE tasks
                    SET reserved_cost = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_ts(), str(task_id)),
                )
            connection.execute(
                """
                UPDATE accounts
                SET active_tasks = CASE
                    WHEN active_tasks > 0 THEN active_tasks - 1
                    ELSE 0
                END,
                updated_at = ?
                WHERE id = ?
                """,
                (now_ts(), int(account_id)),
            )

    def reserve_task_balance(
        self,
        task_id: str,
        account_id: int,
        cost: float,
    ) -> bool:
        cost = max(float(cost or 0), 0)
        if cost <= 0:
            return True
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT reserved_cost FROM tasks WHERE id = ?",
                (str(task_id),),
            ).fetchone()
            if task is None:
                connection.rollback()
                return False
            if float(task["reserved_cost"] or 0) >= cost:
                connection.commit()
                return True
            account = connection.execute(
                "SELECT last_balance FROM accounts WHERE id = ?",
                (int(account_id),),
            ).fetchone()
            reserved = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_cost), 0) AS cost
                FROM tasks
                WHERE account_id = ? AND id != ? AND reserved_cost > 0
                """,
                (int(account_id), str(task_id)),
            ).fetchone()
            if account is None or account["last_balance"] is None:
                connection.rollback()
                return False
            available = float(account["last_balance"] or 0) - float(
                reserved["cost"] or 0
            )
            if available < cost:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE tasks
                SET account_id = ?, reserved_cost = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(account_id), cost, now_ts(), str(task_id)),
            )
            connection.commit()
        return True

    def settle_task_balance(
        self,
        task_id: str,
        account_id: int,
        *,
        actual_cost: float | None,
        balance_snapshot_fresh: bool,
    ) -> bool:
        numeric_cost = max(float(actual_cost or 0), 0)
        if not balance_snapshot_fresh and numeric_cost <= 0:
            return False
        now = now_ts()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not balance_snapshot_fresh and numeric_cost > 0:
                connection.execute(
                    """
                    UPDATE accounts
                    SET last_balance = CASE
                        WHEN last_balance IS NULL THEN NULL
                        WHEN last_balance > ? THEN last_balance - ?
                        ELSE 0
                    END,
                    updated_at = ?
                    WHERE id = ?
                    """,
                    (numeric_cost, numeric_cost, now, int(account_id)),
                )
            cursor = connection.execute(
                """
                UPDATE tasks
                SET reserved_cost = 0, updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (now, str(task_id), int(account_id)),
            )
            connection.commit()
        return cursor.rowcount > 0

    def create_task(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        caller_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = now_ts()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, kind, model, prompt, request_json,
                    caller_request_json, status, progress, estimated_cost,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                """,
                (
                    task_id,
                    str(payload.get("kind") or ""),
                    str(payload.get("model") or ""),
                    str(payload.get("prompt") or ""),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(caller_request or payload, ensure_ascii=False),
                    float(payload.get("_estimated_cost") or 0),
                    now,
                    now,
                ),
            )
        return self.get_task(task_id) or {}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (str(task_id),),
            ).fetchone()
        value = _row(row)
        if not value:
            return None
        for source, target, fallback in (
            ("request_json", "request", {}),
            ("caller_request_json", "caller_request", {}),
            ("upstream_request_json", "upstream_request", {}),
            ("upstream_response_json", "upstream_response", {}),
            ("result_urls_json", "result_urls", []),
            ("raw_status_json", "raw_status", {}),
        ):
            value[target] = _json(value.pop(source, None), fallback)
        return value

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?",
                (min(max(int(limit), 1), 500),),
            ).fetchall()
        return [
            task for row in rows if (task := self.get_task(str(row["id"]))) is not None
        ]

    def list_task_summaries(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    tasks.id, tasks.kind, tasks.model, tasks.prompt,
                    tasks.status, tasks.progress, tasks.channel, tasks.account_id,
                    tasks.generation_id, tasks.result_urls_json,
                    tasks.thumbnail_url, tasks.error_code, tasks.error_message,
                    tasks.created_at, tasks.updated_at, tasks.completed_at,
                    tasks.estimated_cost, tasks.actual_cost,
                    accounts.name AS account_name,
                    json_extract(tasks.request_json, '$.width') AS request_width,
                    json_extract(tasks.request_json, '$.height') AS request_height,
                    json_extract(tasks.request_json, '$.duration') AS request_duration,
                    json_extract(tasks.request_json, '$.resolution') AS request_resolution,
                    json_extract(tasks.request_json, '$.aspect_ratio') AS request_aspect_ratio,
                    COALESCE(json_array_length(json_extract(tasks.request_json, '$._images')), 0)
                        AS image_count,
                    COALESCE(json_array_length(json_extract(tasks.request_json, '$._videos')), 0)
                        AS video_count,
                    COALESCE(json_array_length(json_extract(tasks.request_json, '$._audio')), 0)
                        AS audio_count
                FROM tasks
                LEFT JOIN accounts ON accounts.id = tasks.account_id
                ORDER BY tasks.created_at DESC
                LIMIT ?
                """,
                (min(max(int(limit), 1), 100),),
            ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            request = {
                "width": value.pop("request_width", None),
                "height": value.pop("request_height", None),
                "duration": value.pop("request_duration", None),
                "resolution": value.pop("request_resolution", None),
                "aspect_ratio": value.pop("request_aspect_ratio", None),
            }
            media_inputs: list[dict[str, Any]] = []
            media_index = 0
            for kind, count_key in (
                ("image", "image_count"),
                ("video", "video_count"),
                ("audio", "audio_count"),
            ):
                count = int(value.pop(count_key, 0) or 0)
                for kind_index in range(count):
                    media_inputs.append(
                        {
                            "kind": kind,
                            "label": f"{kind_index + 1}",
                            "url": f"/api/tasks/{value['id']}/media/{media_index}",
                        }
                    )
                    media_index += 1
            value["request"] = request
            value["media_inputs"] = media_inputs
            value["result_urls"] = _json(value.pop("result_urls_json", None), [])
            summaries.append(value)
        return summaries

    def update_task(self, task_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "progress",
            "channel",
            "account_id",
            "generation_id",
            "thumbnail_url",
            "error_code",
            "error_message",
            "completed_at",
            "estimated_cost",
            "actual_cost",
        }
        clean = {key: value for key, value in changes.items() if key in allowed}
        for source, target in (
            ("upstream_request", "upstream_request_json"),
            ("upstream_response", "upstream_response_json"),
            ("result_urls", "result_urls_json"),
            ("raw_status", "raw_status_json"),
        ):
            if source in changes:
                clean[target] = json.dumps(changes[source], ensure_ascii=False)
        if not clean:
            return self.get_task(task_id)
        clean["updated_at"] = now_ts()
        clean["id"] = str(task_id)
        assignments = ", ".join(f"{key} = :{key}" for key in clean if key != "id")
        with self._lock, self.connect() as connection:
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = :id",
                clean,
            )
        return self.get_task(task_id)

    def recoverable_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM tasks
                WHERE status IN (
                    'queued', 'preparing', 'submitted', 'running'
                )
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [
            task for row in rows if (task := self.get_task(str(row["id"]))) is not None
        ]

    def clear_finished_tasks(self) -> int:
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM tasks
                WHERE status IN ('succeeded', 'failed', 'expired')
                """
            )
        return int(cursor.rowcount or 0)

    def record_model_cost(
        self,
        task_id: str,
        payload: dict[str, Any],
        cost: float,
        recorded_at: int | None = None,
    ) -> bool:
        numeric_cost = round(float(cost or 0), 2)
        if not task_id or numeric_cost <= 0:
            return False
        with self._lock, self.connect() as connection:
            cursor = self._insert_model_cost_record(
                connection,
                str(task_id),
                payload,
                numeric_cost,
                int(recorded_at or now_ts()),
            )
        return cursor.rowcount > 0

    def estimate_cost(self, payload: dict[str, Any], sample_limit: int = 20) -> float:
        del sample_limit
        key = _model_cost_key(payload)
        with self._lock, self.connect() as connection:
            self._backfill_model_cost_records(connection)
            row = connection.execute(
                """
                SELECT MAX(cost) AS cost
                FROM model_cost_records
                WHERE model = ? AND duration = ? AND resolution = ?
                """,
                key,
            ).fetchone()
        actual_cost = round(float(row["cost"] or 0), 2) if row else 0
        return actual_cost if actual_cost > 0 else _builtin_video_cost(payload)

    def list_model_costs(
        self,
        limit: int = 200,
        *,
        include_builtin_rates: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock, self.connect() as connection:
            self._backfill_model_cost_records(connection)
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        *,
                        MAX(cost) OVER (
                            PARTITION BY model, duration, resolution
                        ) AS reference_cost,
                        COUNT(*) OVER (
                            PARTITION BY model, duration, resolution
                        ) AS samples,
                        ROW_NUMBER() OVER (
                            PARTITION BY model, duration, resolution
                            ORDER BY recorded_at DESC, task_id DESC
                        ) AS row_number
                    FROM model_cost_records
                )
                SELECT
                    model, duration, resolution, reference_cost AS cost,
                    cost AS latest_cost, samples, task_id AS last_task_id,
                    recorded_at AS updated_at
                FROM ranked
                WHERE row_number = 1
                ORDER BY model, duration, resolution
                LIMIT ?
                """,
                (min(max(int(limit), 1), 500),),
            ).fetchall()
        actual = [dict(row) for row in rows]
        if not include_builtin_rates:
            return actual
        decorated = [
            {
                **item,
                "source": "actual",
                "rate_per_second": (
                    float(rate)
                    if (
                        rate := _BUILTIN_VIDEO_COST_RATES.get(
                            (str(item["model"]), str(item["resolution"]))
                        )
                    )
                    is not None
                    else None
                ),
            }
            for item in actual
        ]
        return (decorated + _builtin_video_cost_rows())[: min(max(int(limit), 1), 500)]

    @staticmethod
    def _insert_model_cost_record(
        connection: sqlite3.Connection,
        task_id: str,
        payload: dict[str, Any],
        cost: float,
        recorded_at: int,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            INSERT OR IGNORE INTO model_cost_records (
                task_id, model, duration, resolution, cost, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(task_id),
                *_model_cost_key(payload),
                round(float(cost), 2),
                int(recorded_at),
            ),
        )

    def _backfill_model_cost_records(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id, request_json, upstream_response_json, actual_cost,
                   completed_at, updated_at
            FROM tasks
            WHERE status = 'succeeded'
              AND actual_cost IS NOT NULL
              AND actual_cost > 0
              AND NOT EXISTS (
                  SELECT 1 FROM model_cost_records
                  WHERE model_cost_records.task_id = tasks.id
              )
            ORDER BY completed_at ASC, created_at ASC
            """
        ).fetchall()
        for row in rows:
            payload = _json(row["request_json"], {})
            if not isinstance(payload, dict):
                continue
            self._insert_model_cost_record(
                connection,
                str(row["id"]),
                payload,
                _final_generation_cost(
                    row["upstream_response_json"],
                    row["actual_cost"],
                ),
                int(row["completed_at"] or row["updated_at"] or now_ts()),
            )

    @staticmethod
    def _ensure_model_cost_schema(connection: sqlite3.Connection) -> None:
        expected_columns = [
            "task_id",
            "model",
            "duration",
            "resolution",
            "cost",
            "recorded_at",
        ]
        columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(model_cost_records)"
            ).fetchall()
        ]
        if columns != expected_columns:
            legacy_rows = connection.execute(
                "SELECT * FROM model_cost_records"
            ).fetchall()
            connection.execute("DROP INDEX IF EXISTS idx_hg_model_cost_lookup")
            connection.execute("DROP TABLE model_cost_records")
            connection.execute(
                """
                CREATE TABLE model_cost_records (
                    task_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    duration INTEGER NOT NULL DEFAULT 0,
                    resolution TEXT NOT NULL DEFAULT '-',
                    cost REAL NOT NULL,
                    recorded_at INTEGER NOT NULL
                )
                """
            )
            legacy_columns = set(columns)
            for row in legacy_rows:
                raw_resolution = (
                    row["resolution"] if "resolution" in legacy_columns else ""
                )
                if not raw_resolution and {"width", "height"}.issubset(legacy_columns):
                    raw_resolution = f"{row['width']}x{row['height']}"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO model_cost_records (
                        task_id, model, duration, resolution, cost, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["task_id"]),
                        str(row["model"] or ""),
                        max(_cost_integer(row["duration"]), 0),
                        _normalize_cost_resolution(raw_resolution),
                        float(row["cost"] or 0),
                        _cost_integer(row["recorded_at"], now_ts()),
                    ),
                )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_hg_model_cost_lookup
            ON model_cost_records(model, duration, resolution, recorded_at)
            """
        )

    @staticmethod
    def _normalize_model_cost_resolutions(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT DISTINCT resolution FROM model_cost_records"
        ).fetchall()
        updates = [
            (normalized, str(row["resolution"]))
            for row in rows
            if (normalized := _normalize_cost_resolution(row["resolution"]))
            != str(row["resolution"])
        ]
        if updates:
            connection.executemany(
                "UPDATE model_cost_records SET resolution = ? WHERE resolution = ?",
                updates,
            )

    def proxy_assignment_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT proxy_url, COUNT(*) AS count
                FROM accounts
                WHERE TRIM(proxy_url) != ''
                GROUP BY proxy_url
                """
            ).fetchall()
        return {str(row["proxy_url"]): int(row["count"] or 0) for row in rows}

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (str(key),),
            ).fetchone()
        return str(row["value"]) if row else default

    def set_settings(self, values: dict[str, Any]) -> None:
        with self._lock, self.connect() as connection:
            for key, value in values.items():
                connection.execute(
                    """
                    INSERT INTO settings(key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(key), str(value)),
                )
