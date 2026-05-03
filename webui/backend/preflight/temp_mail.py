"""Preflight check for cloudflare_temp_email Admin API."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Tuple

from pydantic import BaseModel

from ._common import CheckResult, PreflightResult, aggregate


class TempMailInput(BaseModel):
    api_base_url: str
    admin_auth: str
    custom_auth: str = ""
    domain: str
    enable_random_subdomain: bool = False


def _request(cfg: TempMailInput, method: str, path: str, body: dict | None = None) -> Tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "x-admin-auth": cfg.admin_auth,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cfg.custom_auth:
        headers["x-custom-auth"] = cfg.custom_auth
    req = urllib.request.Request(
        cfg.api_base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw[:200]}
    except Exception as e:
        return -1, {"error": str(e)[:200]}


def _find_address(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("address", "email"):
            value = data.get(key)
            if isinstance(value, str) and "@" in value:
                return value
        for value in data.values():
            found = _find_address(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_address(item)
            if found:
                return found
    return ""


def check(body: dict) -> PreflightResult:
    cfg = TempMailInput.model_validate(body)
    checks: list[CheckResult] = []

    if not cfg.api_base_url.strip():
        checks.append(CheckResult(name="api_base_url", status="fail", message="缺 api_base_url"))
        return aggregate(checks)
    if not cfg.admin_auth.strip():
        checks.append(CheckResult(name="admin_auth", status="fail", message="缺 admin_auth"))
        return aggregate(checks)
    if not cfg.domain.strip():
        checks.append(CheckResult(name="domain", status="fail", message="缺 domain"))
        return aggregate(checks)

    payload = {
        "name": "preflight",
        "domain": cfg.domain.strip(),
        "enablePrefix": True,
        "enableRandomSubdomain": cfg.enable_random_subdomain,
    }
    code, data = _request(cfg, "POST", "/admin/new_address", payload)
    address = _find_address(data)
    if code < 200 or code >= 300 or not address:
        checks.append(
            CheckResult(
                name="new_address",
                status="fail",
                message=f"创建测试地址失败 HTTP {code}: {str(data)[:180]}",
            )
        )
        return aggregate(checks)
    checks.append(
        CheckResult(
            name="new_address",
            status="ok",
            message=f"已创建测试地址 {address}",
        )
    )

    qs = urllib.parse.urlencode({"limit": 1, "offset": 0, "address": address})
    code, data = _request(cfg, "GET", f"/admin/mails?{qs}")
    if code < 200 or code >= 300:
        checks.append(
            CheckResult(
                name="admin_mails",
                status="fail",
                message=f"读取测试地址邮件失败 HTTP {code}: {str(data)[:180]}",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="admin_mails",
                status="ok",
                message="/admin/mails 可访问",
            )
        )

    return aggregate(checks)
