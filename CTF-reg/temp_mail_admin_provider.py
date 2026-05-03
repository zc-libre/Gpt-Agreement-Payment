"""cloudflare_temp_email Admin API provider.

This provider intentionally uses only the admin API:
  - POST /admin/new_address creates an address.
  - GET /admin/mails?address=... reads received mail.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html import unescape
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class TempMailAdminConfig:
    api_base_url: str
    admin_auth: str
    custom_auth: str = ""
    enable_prefix: bool = True
    enable_random_subdomain: bool = False


class TempMailAdminProvider:
    """Creates temp addresses and polls OTP mails through Admin API."""

    def __init__(
        self,
        config: TempMailAdminConfig,
        *,
        poll_interval_s: float = 1.0,
    ):
        if not config.api_base_url:
            raise RuntimeError("TempMailAdminProvider 缺配置：api_base_url")
        if not config.admin_auth:
            raise RuntimeError("TempMailAdminProvider 缺配置：admin_auth")
        self.config = config
        self.base_url = config.api_base_url.rstrip("/")
        self.poll_interval_s = max(0.2, poll_interval_s)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    @classmethod
    def from_mail_config(
        cls,
        mail_config: Any,
        *,
        secrets_path: Optional[Path] = None,
        **kwargs,
    ) -> "TempMailAdminProvider":
        secrets = _load_temp_mail_secrets(secrets_path)
        api_base_url = (
            getattr(mail_config, "api_base_url", "")
            or os.getenv("TEMP_MAIL_BASE_URL", "")
            or secrets.get("api_base_url", "")
        ).strip()
        admin_auth = (
            getattr(mail_config, "admin_auth", "")
            or os.getenv("TEMP_MAIL_ADMIN_AUTH", "")
            or secrets.get("admin_auth", "")
        ).strip()
        custom_auth = (
            getattr(mail_config, "custom_auth", "")
            or os.getenv("TEMP_MAIL_CUSTOM_AUTH", "")
            or secrets.get("custom_auth", "")
        ).strip()
        return cls(
            TempMailAdminConfig(
                api_base_url=api_base_url,
                admin_auth=admin_auth,
                custom_auth=custom_auth,
                enable_prefix=bool(getattr(mail_config, "enable_prefix", True)),
                enable_random_subdomain=bool(
                    getattr(mail_config, "enable_random_subdomain", False)
                ),
            ),
            **kwargs,
        )

    def create_address(self, name: str, domain: str) -> str:
        if not domain:
            raise RuntimeError("TempMailAdminProvider.create_address: domain 未配置")
        body = {
            "name": name,
            "domain": domain,
            "enablePrefix": self.config.enable_prefix,
            "enableRandomSubdomain": self.config.enable_random_subdomain,
        }
        data = self._json_request("POST", "/admin/new_address", body=body)
        address = _find_string(data, {"address", "email"})
        if not address or "@" not in address:
            raise RuntimeError(
                "TempMailAdminProvider.create_address: 响应里没有有效 address/email 字段"
            )
        return address.strip().lower()

    def wait_for_otp(
        self,
        email_addr: str,
        *,
        timeout: int = 180,
        issued_after: Optional[float] = None,
    ) -> str:
        issued_after = time.time() if issued_after is None else issued_after
        accept_threshold_s = issued_after - float(os.getenv("TEMP_MAIL_GRACE_S", "60"))
        deadline = time.time() + timeout
        start = time.time()
        polls = 0
        last_log_at = 0.0
        logger.info(
            f"[temp-mail] 等 OTP address={email_addr} timeout={timeout}s "
            f"(issued_after={issued_after:.0f})"
        )
        while time.time() < deadline:
            polls += 1
            mails = self.list_mails(email_addr)
            for mail in mails:
                ts_s = _mail_ts_s(mail)
                if ts_s and ts_s < accept_threshold_s:
                    continue
                raw = extract_raw_mime(mail)
                otp = extract_otp_from_raw(raw, email_addr=email_addr)
                if otp:
                    elapsed = time.time() - start
                    logger.info(
                        f"[temp-mail] 收到 OTP={otp} address={email_addr} "
                        f"poll#{polls} elapsed={elapsed:.1f}s"
                    )
                    return otp
            now = time.time()
            if now - last_log_at >= 30:
                logger.info(
                    f"[temp-mail] 轮询中 address={email_addr} 已等 {int(now - start)}s "
                    f"polls={polls}"
                )
                last_log_at = now
            time.sleep(self.poll_interval_s)
        raise TimeoutError(
            f"TempMailAdminProvider: 等 OTP 超时 {timeout}s address={email_addr}"
        )

    def list_mails(self, email_addr: str, limit: int = 20) -> list[dict]:
        qs = urllib.parse.urlencode(
            {"limit": limit, "offset": 0, "address": email_addr}
        )
        data = self._json_request("GET", f"/admin/mails?{qs}")
        mails = _extract_mail_items(data)
        if mails is None:
            raise RuntimeError(
                "TempMailAdminProvider.list_mails: 响应里没有邮件列表 "
                f"keys={sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}"
            )
        return mails

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
    ) -> Any:
        raw_body = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            "x-admin-auth": self.config.admin_auth,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.config.custom_auth:
            headers["x-custom-auth"] = self.config.custom_auth
        req = urllib.request.Request(
            self.base_url + path,
            data=raw_body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(req, timeout=10) as r:
                raw = r.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"TempMail Admin API {method} {path} → HTTP {e.code}: {raw}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"TempMail Admin API {method} {path} 返回非 JSON: {e}")


def _load_temp_mail_secrets(secrets_path: Optional[Path]) -> dict:
    sp = secrets_path or Path(__file__).resolve().parent.parent / "output" / "secrets.json"
    if not sp.exists():
        return {}
    try:
        return (json.loads(sp.read_text(encoding="utf-8")).get("temp_mail") or {})
    except Exception as e:
        logger.warning(f"读 {sp} 失败: {e}")
        return {}


def _find_string(data: Any, keys: set[str]) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys and isinstance(value, str):
                return value
        for value in data.values():
            found = _find_string(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_string(item, keys)
            if found:
                return found
    return ""


def _extract_mail_items(data: Any) -> Optional[list[dict]]:
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if not isinstance(data, dict):
        return None
    for key in ("results", "mails", "emails", "messages", "items", "list", "data", "result"):
        value = data.get(key)
        if isinstance(value, list):
            return [m for m in value if isinstance(m, dict)]
        if isinstance(value, dict):
            nested = _extract_mail_items(value)
            if nested is not None:
                return nested
    return None


def extract_raw_mime(mail: dict) -> str:
    raw = _find_raw_value(mail)
    if not raw:
        raise RuntimeError(
            "TempMailAdminProvider.extract_raw_mime: 邮件对象没有 raw/source/content/body/message 字段"
        )
    return raw


def _find_raw_value(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("raw", "source", "content", "body", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in data.values():
            found = _find_raw_value(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_raw_value(item)
            if found:
                return found
    return ""


def extract_otp_from_raw(raw: str, *, email_addr: str = "") -> Optional[str]:
    text = _raw_to_search_text(raw)
    addr_digits = "".join(re.findall(r"\d", email_addr))

    def is_from_addr(candidate: str) -> bool:
        return len(addr_digits) >= 6 and candidate in addr_digits

    candidates = [
        r"verification code\s*(?:to continue|is)?[:\s]+(\d{6})\b",
        r"\bcode\s*(?:is|to continue)?[:\s]+(\d{6})\b",
        r"(?:verification|one[-\s]*time|verify|验证码)[^\d]{0,80}(\d{6})\b",
        r"\b(?:chatgpt|openai)\b[^\d]{0,80}(\d{6})\b",
        r"(?<![#&\w])\b(\d{6})\b",
    ]
    for pattern in candidates:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and not is_from_addr(match.group(1)):
            return match.group(1)
    return None


def _raw_to_search_text(raw: str) -> str:
    message = BytesParser(policy=policy.default).parsebytes(
        raw.encode("utf-8", errors="replace")
    )
    parts = [str(message.get("Subject") or "")]
    if message.is_multipart():
        for part in message.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    payload = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    payload = payload.decode("utf-8", errors="replace")
                parts.append(_html_to_text(payload) if ctype == "text/html" else payload)
    else:
        try:
            payload = message.get_content()
        except Exception:
            payload = raw
        parts.append(_html_to_text(payload) if message.get_content_type() == "text/html" else payload)
    parts.append(raw)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _html_to_text(value: str) -> str:
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<!--[\s\S]*?-->", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"#[0-9A-Fa-f]{6}\b", " ", value)
    return unescape(value)


def _mail_ts_s(mail: dict) -> float:
    value = _find_string(mail, {"ts", "timestamp", "created_at", "createdAt", "date"})
    if not value:
        number = _find_number(mail, {"ts", "timestamp", "createdAt"})
        if number is None:
            return 0.0
        return number / 1000.0 if number > 1e10 else number
    if value.isdigit():
        number = float(value)
        return number / 1000.0 if number > 1e10 else number
    return 0.0


def _find_number(data: Any, keys: set[str]) -> Optional[float]:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys and isinstance(value, (int, float)):
                return float(value)
        for value in data.values():
            found = _find_number(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_number(item, keys)
            if found is not None:
                return found
    return None
