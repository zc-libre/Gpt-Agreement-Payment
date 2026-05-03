import json
import time
from pathlib import Path
from . import settings as s
from .db import get_db


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
    bak.write_bytes(path.read_bytes())
    return bak


def _payment_method(answers: dict) -> str:
    return (answers.get("payment") or {}).get("method", "both")


def _to_int_or_none(v) -> int | None:
    """字符串/数字 → int。空/非法 → None（不写入 config，避免覆盖默认）。"""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float_or_none(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_group_ids(v) -> list[int]:
    """接受 list / 逗号分隔字符串 / 空。返回 int 列表（去掉非数字）。"""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        items = v
    elif isinstance(v, str):
        items = [p.strip() for p in v.split(",")]
    else:
        return []
    out: list[int] = []
    for it in items:
        if isinstance(it, int):
            out.append(it)
            continue
        s = str(it).strip()
        if s and s.lstrip("-").isdigit():
            out.append(int(s))
    return out


def _normalize_sub2api(raw: dict) -> dict:
    """把 wizard 表单字段标准化为 pipeline_sub2api 期望的 dict。

    保留 enabled / base_url / api_key / oauth_client_id 原值；
    把 concurrency / priority / proxy_id 转 int，rate_multiplier 转 float，
    group_ids 把字符串拆 int 数组。空值字段不写入 config。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {
        "enabled": bool(raw.get("enabled")),
    }
    for k in ("base_url", "api_key", "oauth_client_id"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()

    concurrency = _to_int_or_none(raw.get("concurrency"))
    if concurrency is not None:
        out["concurrency"] = concurrency

    priority = _to_int_or_none(raw.get("priority"))
    if priority is not None:
        out["priority"] = priority

    rate = _to_float_or_none(raw.get("rate_multiplier"))
    if rate is not None:
        out["rate_multiplier"] = rate

    proxy_id = _to_int_or_none(raw.get("proxy_id"))
    if proxy_id is not None:
        out["proxy_id"] = proxy_id

    group_ids = _parse_group_ids(raw.get("group_ids"))
    if group_ids:
        out["group_ids"] = group_ids

    timeout_s = _to_int_or_none(raw.get("timeout_s"))
    if timeout_s is not None:
        out["timeout_s"] = timeout_s

    return out


def _project_pay(answers: dict) -> dict:
    """Map flat wizard answers onto CTF-pay config schema."""
    out: dict = {}
    pm = _payment_method(answers)
    if "paypal" in answers and pm in ("paypal", "both"):
        out["paypal"] = answers["paypal"]
    if "captcha" in answers:
        out["captcha"] = {
            "api_url": answers["captcha"].get("api_url", ""),
            "api_key": answers["captcha"].get("api_key") or answers["captcha"].get("client_key", ""),
        }
    if "team_system" in answers:
        out["team_system"] = answers["team_system"]
    if "cpa" in answers:
        out["cpa"] = answers["cpa"]
    if "sub2api" in answers:
        out["sub2api"] = _normalize_sub2api(answers["sub2api"])
    if pm == "gopay" and "gopay" in answers:
        gp = answers["gopay"] or {}
        if all(gp.get(k) for k in ("country_code", "phone_number", "pin")):
            out["gopay"] = {
                "country_code": str(gp["country_code"]).lstrip("+"),
                "phone_number": str(gp["phone_number"]),
                "pin": str(gp["pin"]),
            }
            if gp.get("midtrans_client_id"):
                out["gopay"]["midtrans_client_id"] = gp["midtrans_client_id"]
            out["gopay"]["otp"] = {
                "source": "auto",
                "timeout": int(gp.get("otp_timeout") or 300),
                "interval": 1,
            }
    if "team_plan" in answers:
        tp = answers["team_plan"] or {}
        plan: dict = {}
        for k in (
            "plan_name",
            "entry_point",
            "promo_campaign_id",
            "price_interval",
            "workspace_name",
            "seat_quantity",
            "billing_country",
            "billing_currency",
            "checkout_ui_mode",
            "output_url_mode",
            "is_coupon_from_query_param",
        ):
            if k in tp and tp[k] not in (None, ""):
                plan[k] = tp[k]
        if plan:
            out["fresh_checkout"] = {"plan": plan}
    if "daemon" in answers:
        out["daemon"] = answers["daemon"]
    if "stripe_runtime" in answers and pm in ("card", "both"):
        out["runtime"] = answers["stripe_runtime"]
    if "card" in answers and pm in ("card", "both"):
        out["cards"] = [answers["card"]]
    if "proxy" in answers:
        proxy = answers["proxy"]
        mode = proxy.get("mode")
        if mode == "webshare" and proxy.get("api_key"):
            gost_port = int(proxy.get("gost_listen_port", 18898))
            out["webshare"] = {
                "enabled": True,
                "api_key": proxy["api_key"],
                "lock_country": proxy.get("lock_country", "US"),
                "refresh_threshold": proxy.get("refresh_threshold", 2),
                "zone_rotate_after_ip_rotations": proxy.get("zone_rotate_after_ip_rotations", 2),
                "zone_rotate_on_reg_fails": proxy.get("zone_rotate_on_reg_fails", 3),
                "no_rotation_cooldown_s": proxy.get("no_rotation_cooldown_s", 10800),
                "gost_listen_port": gost_port,
                "sync_team_proxy": proxy.get("sync_team_proxy", True),
            }
            # webshare 模式下 pipeline._ensure_gost_alive 会拉起本地 gost 中继；
            # card.py 直接连这个地址出网（避开 example 模板透传的 USER:PASS 占位）
            out["proxy"] = f"socks5://127.0.0.1:{gost_port}"
        elif mode == "none":
            out["proxy"] = ""
        elif proxy.get("url"):
            out["proxy"] = proxy["url"]
    return out


def _project_reg(answers: dict) -> dict:
    """Map flat wizard answers onto CTF-reg config schema."""
    out: dict = {}
    pm = _payment_method(answers)
    # mail.catch_all_domain(s) 来自 Step03 Cloudflare 的 zone_names。
    # OTP 走 cloudflare_temp_email Admin API；Step04 写 temp_mail 凭证。
    # IMAP 字段（imap_server/port/email/auth_code）已彻底删除；CF 平台凭证
    # 落 SQLite runtime_meta[secrets]，Admin API 凭证落 temp_mail 段。
    zones = (answers.get("cloudflare") or {}).get("zone_names") or []
    temp_mail = answers.get("temp_mail") or answers.get("cloudflare_kv") or {}
    if zones:
        out["mail"] = {
            "backend": "cloudflare_temp_email_admin",
            "api_base_url": temp_mail.get("api_base_url", ""),
            "admin_auth": "",
            "custom_auth": "",
            "catch_all_domain": zones[0],
            "catch_all_domains": list(zones),
            "enable_prefix": True,
            "enable_random_subdomain": temp_mail.get("enable_random_subdomain", False),
        }
    if "card" in answers and pm in ("card", "both"):
        out["card"] = {k: answers["card"].get(k, "") for k in ("number", "cvc", "exp_month", "exp_year")}
    if "billing" in answers:
        out["billing"] = answers["billing"]
    if "team_plan" in answers:
        out["team_plan"] = answers["team_plan"]
    if "captcha" in answers:
        out["captcha"] = {"client_key": answers["captcha"].get("client_key") or answers["captcha"].get("api_key", "")}
    if "proxy" in answers:
        proxy = answers["proxy"]
        mode = proxy.get("mode")
        if mode == "webshare" and proxy.get("api_key"):
            gost_port = int(proxy.get("gost_listen_port", 18898))
            out["proxy"] = f"socks5://127.0.0.1:{gost_port}"
        elif mode == "none":
            out["proxy"] = ""
        elif proxy.get("url"):
            out["proxy"] = proxy["url"]
    return out


def _write_secrets(answers: dict) -> str | None:
    """合并 Cloudflare + cloudflare_temp_email Admin API 凭证到
    SQLite runtime_meta[secrets]。

    - cloudflare 段: api_token / zone_names（Step03）
    - temp_mail 段: api_base_url / admin_auth / custom_auth（Step04 Admin API）

    返回存储位置描述；如无任何字段则返回 None。
    """
    cf = answers.get("cloudflare") or {}
    temp_mail = answers.get("temp_mail") or answers.get("cloudflare_kv") or {}

    cf_section: dict = {}
    if cf.get("cf_token"):
        cf_section["api_token"] = cf["cf_token"]
    if cf.get("zone_names"):
        cf_section["zone_names"] = list(cf["zone_names"])

    temp_mail_section: dict = {}
    if temp_mail.get("api_base_url"):
        temp_mail_section["api_base_url"] = temp_mail["api_base_url"]
    if temp_mail.get("admin_auth"):
        temp_mail_section["admin_auth"] = temp_mail["admin_auth"]
    if temp_mail.get("custom_auth"):
        temp_mail_section["custom_auth"] = temp_mail["custom_auth"]

    if not cf_section and not temp_mail_section:
        return None

    db = get_db()
    existing = db.get_runtime_json("secrets", {})
    if not isinstance(existing, dict):
        existing = {}
    if cf_section:
        existing.setdefault("cloudflare", {}).update(cf_section)
    if temp_mail_section:
        existing.setdefault("temp_mail", {}).update(temp_mail_section)
    db.set_runtime_json("secrets", existing)
    return "sqlite:runtime_meta/secrets"


def write_configs(answers: dict) -> dict:
    """Returns {pay_path, reg_path, secrets_path, backups: [path, ...]}."""
    pay_skeleton = json.loads(s.PAY_EXAMPLE_PATH.read_text(encoding="utf-8"))
    reg_skeleton = json.loads(s.REG_EXAMPLE_PATH.read_text(encoding="utf-8"))

    # Skeleton 里 auto_register.config_path 默认指向 .example.json 模板，
    # 直接 merge 后 pipeline 子进程会读到模板。用 wizard 实际写的真实
    # reg 路径覆盖它。
    auth = pay_skeleton.setdefault("fresh_checkout", {}).setdefault("auth", {})
    auto = auth.setdefault("auto_register", {})
    auto["config_path"] = str(s.REG_CONFIG_PATH)

    pay = _deep_merge(pay_skeleton, _project_pay(answers))
    reg = _deep_merge(reg_skeleton, _project_reg(answers))

    backups = []
    for p in (s.PAY_CONFIG_PATH, s.REG_CONFIG_PATH):
        b = _backup(p)
        if b:
            backups.append(str(b))

    s.PAY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    s.REG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    s.PAY_CONFIG_PATH.write_text(json.dumps(pay, ensure_ascii=False, indent=2), encoding="utf-8")
    s.REG_CONFIG_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")

    secrets_path = _write_secrets(answers)

    return {
        "pay_path": str(s.PAY_CONFIG_PATH),
        "reg_path": str(s.REG_CONFIG_PATH),
        "secrets_path": secrets_path,
        "backups": backups,
    }
