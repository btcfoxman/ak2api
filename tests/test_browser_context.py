from __future__ import annotations

from app.browser_context import _safe_cookie_records, _safe_page_url


def test_cookie_header_is_converted_to_valid_cdp_records() -> None:
    records = _safe_cookie_records(
        {
            "cookie_header": "token=abc; refresh_token=def==; session_id=session",
            "cookie_records": [],
        }
    )

    assert {item["name"] for item in records} == {
        "token",
        "refresh_token",
        "session_id",
    }
    assert all(item["domain"] == ".akool.com" for item in records)
    assert all(item["value"] is not None for item in records)


def test_page_url_redacts_login_secrets() -> None:
    value = _safe_page_url(
        "https://akool.com/zh-cn/login?email=user%40example.com&password=private&next=%2Fapp#fragment"
    )

    assert "private" not in value
    assert "password=REDACTED" in value
    assert "email=user%40example.com" in value
    assert "#fragment" not in value
