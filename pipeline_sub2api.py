"""sub2api 上传 — 与 CPA 平行的下游推送通道。

设计原则（参照 AutoTeam/src/autoteam/sub2api_sync.py）：
- 单条 push（注册+支付成功后立即推送）；不做对账/批量同步/删除
- 接口与 ``pipeline._cpa_import_after_team`` 完全平行：传 refresh_token 进来，
  内部自带 OpenAI OAuth refresh + JWT 解析，然后 POST 到
  ``{base_url}/api/v1/admin/accounts/batch``
- best-effort：异常不抛，统一返回字符串状态
- 不依赖 pipeline 内部 helper（避免循环 import）

返回值约定（与 cpa_import 状态集合保持一致风格）:
  - ``ok``           上传成功
  - ``skipped``      未启用 / 缺 base_url / 缺 api_key / 缺 email
  - ``no_token``     refresh_token 与 fallback access_token 都没有，无法上传
  - ``fail_refresh`` refresh 阶段失败但仍尝试裸导入；最终上传失败时记此值
  - ``fail_upload``  HTTP 错误 / 网络异常
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


PLATFORM_OPENAI = "openai"
ACCOUNT_TYPE_OAUTH = "oauth"
DEFAULT_TIMEOUT_S = 20


# ---------------------------------------------------------------------------
# JWT / 时间工具
# ---------------------------------------------------------------------------


def _parse_jwt_payload(token: str) -> dict[str, Any]:
    """解 JWT payload。失败返回 {}（不抛）。"""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode())
    except Exception:
        return {}


def _to_rfc3339_utc(value: Any) -> str:
    """epoch / ISO 字符串 → RFC3339 UTC。空值返回空串。"""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    text = str(value).strip()
    if not text:
        return ""
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return text  # 透传，让 sub2api 自己报错以便排查


# ---------------------------------------------------------------------------
# OpenAI OAuth refresh（与 _cpa_import_after_team 中逻辑等价，独立实现以便单测）
# ---------------------------------------------------------------------------


def refresh_openai_tokens(
    refresh_token: str, client_id: str, *, timeout: int = 20
) -> dict[str, str]:
    """用 refresh_token 换一组新的 OpenAI tokens。

    返回 dict（任何字段缺失时为空字符串）：
        access_token / id_token / refresh_token / account_id / expired_iso
    失败时返回 {}（调用方按 fail_refresh 处理）。
    """
    if not refresh_token or not client_id:
        return {}
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "scope": "openid email profile offline_access",
        }
    ).encode()
    req = urllib.request.Request(
        "https://auth.openai.com/oauth/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            tok = json.loads(r.read().decode())
    except Exception:
        return {}

    access_token = tok.get("access_token", "") or ""
    id_token = tok.get("id_token", "") or access_token
    new_rt = tok.get("refresh_token", refresh_token) or refresh_token

    account_id = ""
    expired_iso = ""
    if access_token:
        claims = _parse_jwt_payload(access_token)
        oai_auth = claims.get("https://api.openai.com/auth") or {}
        account_id = oai_auth.get("chatgpt_account_id", "") or ""
        if claims.get("exp"):
            expired_iso = _to_rfc3339_utc(claims["exp"])
    return {
        "access_token": access_token,
        "id_token": id_token,
        "refresh_token": new_rt,
        "account_id": account_id,
        "expired_iso": expired_iso,
    }


# ---------------------------------------------------------------------------
# Credentials & payload 构造（对齐 AutoTeam build_credentials）
# ---------------------------------------------------------------------------


def build_credentials(
    *,
    email: str,
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    account_id: str = "",
    expired_iso: str = "",
) -> dict[str, Any]:
    """从 OpenAI tokens 构造 sub2api 的 OAuth credentials。

    解析 id_token 拿 chatgpt_user_id / organization_id / plan_type / subscription_expires_at。
    解析 access_token 拿 client_id。
    缺失的字段不写入（避免上游覆盖空值）。
    """
    creds: dict[str, Any] = {}
    if access_token:
        creds["access_token"] = access_token
    if refresh_token:
        creds["refresh_token"] = refresh_token
    if id_token:
        creds["id_token"] = id_token
    if expired_iso:
        creds["expires_at"] = expired_iso
    if email:
        creds["email"] = email

    id_claims = _parse_jwt_payload(id_token) if id_token else {}
    openai_auth = id_claims.get("https://api.openai.com/auth") or {}
    access_claims = _parse_jwt_payload(access_token) if access_token else {}

    final_account_id = (
        account_id or openai_auth.get("chatgpt_account_id", "") or ""
    )
    if final_account_id:
        creds["chatgpt_account_id"] = final_account_id

    user_id = openai_auth.get("chatgpt_user_id", "") or ""
    if user_id:
        creds["chatgpt_user_id"] = user_id

    organization_id = openai_auth.get("organization_id", "") or ""
    if organization_id:
        creds["organization_id"] = organization_id

    plan_type = openai_auth.get("chatgpt_plan_type", "") or ""
    if plan_type:
        creds["plan_type"] = plan_type

    subscription_until = (
        openai_auth.get("chatgpt_subscription_active_until", "") or ""
    )
    if subscription_until:
        creds["subscription_expires_at"] = subscription_until

    client_id = (access_claims.get("client_id") if isinstance(access_claims, dict) else "") or ""
    if client_id:
        creds["client_id"] = client_id

    return creds


def _build_account_payload(
    name: str, credentials: dict[str, Any], cfg: dict[str, Any]
) -> dict[str, Any]:
    """构造 ``POST /accounts/batch`` 的单个 account 项。"""
    payload: dict[str, Any] = {
        "name": name,
        "platform": PLATFORM_OPENAI,
        "type": ACCOUNT_TYPE_OAUTH,
        "credentials": credentials,
    }

    concurrency = cfg.get("concurrency")
    if isinstance(concurrency, int) and concurrency > 0:
        payload["concurrency"] = concurrency

    priority = cfg.get("priority")
    if isinstance(priority, int) and priority >= 0:
        payload["priority"] = priority

    rate_multiplier = cfg.get("rate_multiplier")
    if isinstance(rate_multiplier, (int, float)) and rate_multiplier >= 0:
        payload["rate_multiplier"] = float(rate_multiplier)

    proxy_id = cfg.get("proxy_id")
    if isinstance(proxy_id, int) and proxy_id > 0:
        payload["proxy_id"] = proxy_id

    group_ids = cfg.get("group_ids")
    if isinstance(group_ids, list) and group_ids:
        cleaned = [
            int(g)
            for g in group_ids
            if isinstance(g, int) or (isinstance(g, str) and g.strip().lstrip("-").isdigit())
        ]
        if cleaned:
            payload["group_ids"] = cleaned

    return payload


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_post_json(url: str, *, headers: dict[str, str], body: dict, timeout: int) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            body_text = ""
        return e.code, body_text


def _http_get(url: str, *, headers: dict[str, str], timeout: int) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            body_text = ""
        return e.code, body_text


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def push_after_team(
    email: str,
    sid: str,
    sub2api_cfg: dict,
    *,
    refresh_token: str = "",
    is_free: bool = False,
    fallback_access_token: str = "",
    fallback_id_token: str = "",
    fallback_account_id: str = "",
) -> str:
    """单条 push 账号到 sub2api（支付/注册成功后调用）。

    Args:
        email: 账号邮箱（也是 sub2api account.name）
        sid: session_id（仅日志用，与 _cpa_import_after_team 保持签名一致）
        sub2api_cfg: 见 ``pipeline_sub2api`` 模块顶部说明
        refresh_token: 显式传入的 OpenAI refresh_token（建议传）
        is_free: True 时 name 后缀用 ``free_plan_tag``，与 CPA 行为对齐
        fallback_access_token / fallback_id_token / fallback_account_id:
            refresh 失败或缺 RT 时的兜底 token，用于裸导入（best effort）

    Returns:
        ok / skipped / no_token / fail_refresh / fail_upload
    """
    if not sub2api_cfg or not sub2api_cfg.get("enabled"):
        return "skipped"
    base_url = (sub2api_cfg.get("base_url") or "").rstrip("/")
    api_key = (sub2api_cfg.get("api_key") or "").strip()
    if not base_url or not api_key or not email:
        return "skipped"

    timeout = int(sub2api_cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
    client_id = (sub2api_cfg.get("oauth_client_id") or "").strip()

    access_token = ""
    id_token = ""
    new_rt = (refresh_token or "").strip()
    account_id = ""
    expired_iso = ""
    refresh_failed = False

    if new_rt and client_id:
        tokens = refresh_openai_tokens(new_rt, client_id, timeout=timeout)
        if tokens:
            access_token = tokens.get("access_token", "")
            id_token = tokens.get("id_token", "")
            new_rt = tokens.get("refresh_token", new_rt) or new_rt
            account_id = tokens.get("account_id", "")
            expired_iso = tokens.get("expired_iso", "")
        else:
            refresh_failed = True
            print(f"[sub2api] {email} refresh_token 交换失败（仍尝试裸导入）")

    # 兜底：如果 refresh 没拿到 access_token，用 fallback
    if not access_token and fallback_access_token:
        access_token = fallback_access_token.strip()
        id_token = (fallback_id_token or fallback_access_token).strip()
        if not account_id:
            account_id = (fallback_account_id or "").strip()
        if not expired_iso:
            claims = _parse_jwt_payload(access_token)
            if claims.get("exp"):
                expired_iso = _to_rfc3339_utc(claims["exp"])

    if not access_token:
        print(f"[sub2api] {email} 无 access_token 也无 refresh_token，跳过")
        return "no_token"

    credentials = build_credentials(
        email=email,
        access_token=access_token,
        refresh_token=new_rt,
        id_token=id_token,
        account_id=account_id,
        expired_iso=expired_iso,
    )
    payload = _build_account_payload(email, credentials, sub2api_cfg)

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    url = f"{base_url}/api/v1/admin/accounts/batch"
    try:
        status, body_text = _http_post_json(
            url, headers=headers, body={"accounts": [payload]}, timeout=timeout
        )
    except Exception as e:
        print(f"[sub2api] ✗ {email} 上传异常: {e}")
        return "fail_upload"

    if status >= 400:
        snippet = body_text[:200]
        print(f"[sub2api] ✗ {email} 上传失败 http={status} {snippet}")
        return "fail_refresh" if refresh_failed else "fail_upload"

    # 解析返回判断创建是否真的成功（sub2api 批接口可能 200 但 success=0）
    ok = False
    try:
        data = json.loads(body_text or "{}").get("data") or {}
        ok = int(data.get("success") or 0) >= 1
    except Exception:
        # 解析失败也认为 ok（HTTP 200 已说明请求被接受）
        ok = True

    if not ok:
        print(f"[sub2api] ✗ {email} 服务端报告 success=0: {body_text[:200]}")
        return "fail_refresh" if refresh_failed else "fail_upload"

    print(f"[sub2api] ✓ {email} 已上传 → {base_url}  account_id={(account_id or '?')[:8]}")
    return "ok"


# ---------------------------------------------------------------------------
# Preflight ping（webui/backend/preflight/sub2api.py 调用）
# ---------------------------------------------------------------------------


def ping(base_url: str, api_key: str, *, timeout: int = 15) -> tuple[bool, str, str]:
    """连通性检查：列一个账号。返回 (ok, message, details)。

    成功条件：HTTP 200 且 body 是合法 JSON（含 ``data`` 或 ``code`` 字段）。
    """
    base_url = (base_url or "").rstrip("/")
    if not base_url or not api_key:
        return False, "缺少 base_url / api_key", ""

    url = (
        f"{base_url}/api/v1/admin/accounts"
        f"?platform={PLATFORM_OPENAI}&type={ACCOUNT_TYPE_OAUTH}&page=1&page_size=1"
    )
    headers = {"x-api-key": api_key}
    try:
        status, body_text = _http_get(url, headers=headers, timeout=timeout)
    except Exception as e:
        return False, f"请求异常: {e}", ""

    if status != 200:
        return False, f"HTTP {status}", body_text[:500]
    try:
        json.loads(body_text or "{}")
    except Exception:
        return False, "响应非 JSON", body_text[:500]
    return True, "连通正常", ""
