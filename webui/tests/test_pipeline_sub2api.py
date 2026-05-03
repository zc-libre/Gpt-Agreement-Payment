"""pipeline_sub2api 单元测试。

测试目标：JWT 解析、credentials 构造、push_after_team 各分支返回值、
preflight ping 行为。HTTP 层用 monkeypatch 替换 _http_post_json / _http_get /
refresh_openai_tokens 来避开真实网络调用。
"""

import base64
import json

import pipeline_sub2api


# ---------------------------------------------------------------------------
# JWT / 时间工具
# ---------------------------------------------------------------------------


def _b64url(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


def _make_jwt(payload: dict) -> str:
    return f"{_b64url({'alg': 'HS256'})}.{_b64url(payload)}.sig"


def test_parse_jwt_payload_normal():
    tok = _make_jwt({"foo": "bar", "exp": 1700000000})
    out = pipeline_sub2api._parse_jwt_payload(tok)
    assert out["foo"] == "bar"
    assert out["exp"] == 1700000000


def test_parse_jwt_payload_invalid_returns_empty():
    assert pipeline_sub2api._parse_jwt_payload("") == {}
    assert pipeline_sub2api._parse_jwt_payload("not.a.jwt!!") == {}
    assert pipeline_sub2api._parse_jwt_payload("singlepart") == {}


def test_to_rfc3339_epoch():
    s = pipeline_sub2api._to_rfc3339_utc(1700000000)
    assert s.endswith("Z")
    assert "2023" in s


def test_to_rfc3339_iso_string():
    s = pipeline_sub2api._to_rfc3339_utc("2026-05-04T12:34:56Z")
    assert s == "2026-05-04T12:34:56Z"


def test_to_rfc3339_empty():
    assert pipeline_sub2api._to_rfc3339_utc("") == ""
    assert pipeline_sub2api._to_rfc3339_utc(None) == ""


# ---------------------------------------------------------------------------
# build_credentials
# ---------------------------------------------------------------------------


def test_build_credentials_minimal():
    creds = pipeline_sub2api.build_credentials(
        email="x@y.com",
        access_token="at",
        refresh_token="rt",
        id_token="id",
    )
    # JWT 解析失败时不会写额外字段；access_token 也无 client_id
    assert creds == {
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": "id",
        "email": "x@y.com",
    }


def test_build_credentials_extracts_id_token_claims():
    id_token = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_user_id": "u-1",
            "organization_id": "o-1",
            "chatgpt_plan_type": "team",
            "chatgpt_subscription_active_until": "2026-12-31T00:00:00Z",
            "chatgpt_account_id": "acc-from-id",
            "email": "from-id@x.com",
        }
    })
    creds = pipeline_sub2api.build_credentials(
        email="explicit@x.com",
        id_token=id_token,
    )
    assert creds["chatgpt_user_id"] == "u-1"
    assert creds["organization_id"] == "o-1"
    assert creds["plan_type"] == "team"
    assert creds["subscription_expires_at"] == "2026-12-31T00:00:00Z"
    assert creds["chatgpt_account_id"] == "acc-from-id"
    # 显式 email 参数优先于 JWT 中的 email
    assert creds["email"] == "explicit@x.com"


def test_build_credentials_extracts_access_token_client_id():
    at = _make_jwt({"client_id": "app_xyz"})
    creds = pipeline_sub2api.build_credentials(
        email="x@y.com", access_token=at,
    )
    assert creds["client_id"] == "app_xyz"


def test_build_credentials_skips_empty_fields():
    creds = pipeline_sub2api.build_credentials(email="x@y.com")
    # 只留 email
    assert creds == {"email": "x@y.com"}


# ---------------------------------------------------------------------------
# _build_account_payload — defaults 过滤逻辑
# ---------------------------------------------------------------------------


def test_build_account_payload_omits_empty_defaults():
    payload = pipeline_sub2api._build_account_payload(
        "e@x.com", {"access_token": "at"}, {},
    )
    assert payload["name"] == "e@x.com"
    assert payload["platform"] == "openai"
    assert payload["type"] == "oauth"
    # 没有 concurrency/priority/rate_multiplier/proxy_id/group_ids
    for k in ("concurrency", "priority", "rate_multiplier", "proxy_id", "group_ids"):
        assert k not in payload


def test_build_account_payload_includes_defaults():
    payload = pipeline_sub2api._build_account_payload(
        "e@x.com",
        {"access_token": "at"},
        {
            "concurrency": 5,
            "priority": 10,
            "rate_multiplier": 0.5,
            "proxy_id": 7,
            "group_ids": [1, 2, "3", "abc"],
        },
    )
    assert payload["concurrency"] == 5
    assert payload["priority"] == 10
    assert payload["rate_multiplier"] == 0.5
    assert payload["proxy_id"] == 7
    # 字符串 "abc" 应被过滤
    assert payload["group_ids"] == [1, 2, 3]


def test_build_account_payload_priority_zero_kept():
    """priority=0 是合法值（最低优先级），不能误过滤。"""
    payload = pipeline_sub2api._build_account_payload(
        "e@x.com", {"access_token": "at"}, {"priority": 0},
    )
    assert payload["priority"] == 0


# ---------------------------------------------------------------------------
# push_after_team — enabled / 缺字段判断
# ---------------------------------------------------------------------------


def test_push_skipped_when_disabled():
    assert pipeline_sub2api.push_after_team("e@x.com", "sid", {}) == "skipped"
    assert pipeline_sub2api.push_after_team(
        "e@x.com", "sid", {"enabled": False, "base_url": "x", "api_key": "k"},
    ) == "skipped"


def test_push_skipped_when_missing_base_url():
    cfg = {"enabled": True, "api_key": "k"}
    assert pipeline_sub2api.push_after_team("e@x.com", "sid", cfg) == "skipped"


def test_push_skipped_when_missing_api_key():
    cfg = {"enabled": True, "base_url": "https://x"}
    assert pipeline_sub2api.push_after_team("e@x.com", "sid", cfg) == "skipped"


def test_push_skipped_when_missing_email():
    cfg = {"enabled": True, "base_url": "https://x", "api_key": "k"}
    assert pipeline_sub2api.push_after_team("", "sid", cfg) == "skipped"


def test_push_no_token():
    """既没有 refresh_token 也没有 fallback access_token → no_token。"""
    cfg = {"enabled": True, "base_url": "https://x", "api_key": "k"}
    assert pipeline_sub2api.push_after_team("e@x.com", "sid", cfg) == "no_token"


# ---------------------------------------------------------------------------
# push_after_team — 成功路径
# ---------------------------------------------------------------------------


def _ok_response_body() -> str:
    return json.dumps({"data": {"success": 1, "failed": 0, "results": []}})


def test_push_ok_with_fallback_access_token(monkeypatch):
    """无 refresh_token，但有 fallback access_token → 走裸导入。"""
    captured = {}

    def fake_post(url, *, headers, body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, _ok_response_body()

    monkeypatch.setattr(pipeline_sub2api, "_http_post_json", fake_post)
    cfg = {"enabled": True, "base_url": "https://x", "api_key": "k"}
    res = pipeline_sub2api.push_after_team(
        "e@x.com", "sid", cfg,
        fallback_access_token="at-stub",
        fallback_id_token="id-stub",
        fallback_account_id="acc-1",
    )
    assert res == "ok"
    assert captured["url"] == "https://x/api/v1/admin/accounts/batch"
    assert captured["headers"]["x-api-key"] == "k"
    accounts = captured["body"]["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["name"] == "e@x.com"
    assert accounts[0]["platform"] == "openai"
    assert accounts[0]["type"] == "oauth"
    assert accounts[0]["credentials"]["access_token"] == "at-stub"
    assert accounts[0]["credentials"]["chatgpt_account_id"] == "acc-1"


def test_push_ok_with_refresh(monkeypatch):
    """有 refresh_token + oauth_client_id → 走 OAuth refresh，使用 fresh tokens。"""
    monkeypatch.setattr(
        pipeline_sub2api,
        "refresh_openai_tokens",
        lambda rt, cid, *, timeout: {
            "access_token": "at-fresh",
            "id_token": "id-fresh",
            "refresh_token": rt,
            "account_id": "acc-x",
            "expired_iso": "2026-01-01T00:00:00Z",
        },
    )
    captured = {}

    def fake_post(url, *, headers, body, timeout):
        captured["body"] = body
        return 200, _ok_response_body()

    monkeypatch.setattr(pipeline_sub2api, "_http_post_json", fake_post)
    cfg = {
        "enabled": True,
        "base_url": "https://x",
        "api_key": "k",
        "oauth_client_id": "app-xyz",
        "concurrency": 5,
        "group_ids": [1, 2],
        "proxy_id": 3,
    }
    res = pipeline_sub2api.push_after_team(
        "e@x.com", "sid", cfg, refresh_token="rt-old",
    )
    assert res == "ok"
    account = captured["body"]["accounts"][0]
    assert account["credentials"]["access_token"] == "at-fresh"
    assert account["credentials"]["expires_at"] == "2026-01-01T00:00:00Z"
    assert account["concurrency"] == 5
    assert account["group_ids"] == [1, 2]
    assert account["proxy_id"] == 3


def test_push_strips_trailing_slash_in_base_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        pipeline_sub2api, "_http_post_json",
        lambda url, *, headers, body, timeout: (captured.setdefault("url", url), 200, _ok_response_body())[1:],
    )
    cfg = {"enabled": True, "base_url": "https://x/", "api_key": "k"}
    pipeline_sub2api.push_after_team(
        "e@x.com", "sid", cfg, fallback_access_token="at",
    )
    assert captured["url"] == "https://x/api/v1/admin/accounts/batch"


# ---------------------------------------------------------------------------
# push_after_team — 失败路径
# ---------------------------------------------------------------------------


def test_push_http_5xx_returns_fail_upload(monkeypatch):
    monkeypatch.setattr(
        pipeline_sub2api, "_http_post_json",
        lambda *a, **kw: (500, '{"error":"bad"}'),
    )
    cfg = {"enabled": True, "base_url": "https://x", "api_key": "k"}
    res = pipeline_sub2api.push_after_team(
        "e@x.com", "sid", cfg, fallback_access_token="at",
    )
    assert res == "fail_upload"


def test_push_server_reports_no_success(monkeypatch):
    """HTTP 200 但 data.success=0 → 视为上传失败。"""
    monkeypatch.setattr(
        pipeline_sub2api, "_http_post_json",
        lambda *a, **kw: (200, json.dumps({"data": {"success": 0, "failed": 1}})),
    )
    cfg = {"enabled": True, "base_url": "https://x", "api_key": "k"}
    res = pipeline_sub2api.push_after_team(
        "e@x.com", "sid", cfg, fallback_access_token="at",
    )
    assert res == "fail_upload"


def test_push_returns_fail_refresh_when_refresh_failed_then_upload_failed(monkeypatch):
    """refresh 失败 → 仍尝试用 fallback 裸导入；裸导入失败 → fail_refresh。"""
    monkeypatch.setattr(
        pipeline_sub2api, "refresh_openai_tokens",
        lambda rt, cid, *, timeout: {},  # refresh fail
    )
    monkeypatch.setattr(
        pipeline_sub2api, "_http_post_json",
        lambda *a, **kw: (500, ""),
    )
    cfg = {
        "enabled": True, "base_url": "https://x", "api_key": "k",
        "oauth_client_id": "app",
    }
    res = pipeline_sub2api.push_after_team(
        "e@x.com", "sid", cfg,
        refresh_token="rt", fallback_access_token="at-stub",
    )
    assert res == "fail_refresh"


def test_push_post_exception_returns_fail_upload(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("net down")

    monkeypatch.setattr(pipeline_sub2api, "_http_post_json", boom)
    cfg = {"enabled": True, "base_url": "https://x", "api_key": "k"}
    res = pipeline_sub2api.push_after_team(
        "e@x.com", "sid", cfg, fallback_access_token="at",
    )
    assert res == "fail_upload"


# ---------------------------------------------------------------------------
# ping — preflight 路径
# ---------------------------------------------------------------------------


def test_ping_missing_fields():
    ok, msg, _ = pipeline_sub2api.ping("", "k")
    assert not ok
    ok, msg, _ = pipeline_sub2api.ping("https://x", "")
    assert not ok


def test_ping_ok(monkeypatch):
    monkeypatch.setattr(
        pipeline_sub2api, "_http_get",
        lambda *a, **kw: (200, json.dumps({"data": {"items": []}})),
    )
    ok, msg, _ = pipeline_sub2api.ping("https://x", "k")
    assert ok


def test_ping_http_error(monkeypatch):
    monkeypatch.setattr(
        pipeline_sub2api, "_http_get",
        lambda *a, **kw: (401, "unauthorized"),
    )
    ok, msg, details = pipeline_sub2api.ping("https://x", "k")
    assert not ok
    assert "401" in msg


def test_ping_non_json_response(monkeypatch):
    monkeypatch.setattr(
        pipeline_sub2api, "_http_get",
        lambda *a, **kw: (200, "<html>not json</html>"),
    )
    ok, msg, _ = pipeline_sub2api.ping("https://x", "k")
    assert not ok
    assert "JSON" in msg
