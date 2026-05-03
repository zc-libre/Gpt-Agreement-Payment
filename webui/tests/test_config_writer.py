import json
import importlib.util
from pathlib import Path


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def _seed(tmp_path, monkeypatch):
    pay_ex = tmp_path / "CTF-pay" / "config.paypal.example.json"
    reg_ex = tmp_path / "CTF-reg" / "config.paypal-proxy.example.json"
    pay_ex.parent.mkdir(parents=True)
    reg_ex.parent.mkdir(parents=True)
    pay_ex.write_text(json.dumps({"paypal": {"email": ""}, "captcha": {"api_url": "", "api_key": ""}}))
    reg_ex.write_text(json.dumps({"mail": {"catch_all_domain": ""}, "captcha": {"client_key": ""}}))

    import webui.backend.settings as s
    monkeypatch.setattr(s, "PAY_EXAMPLE_PATH", pay_ex)
    monkeypatch.setattr(s, "REG_EXAMPLE_PATH", reg_ex)
    monkeypatch.setattr(s, "PAY_CONFIG_PATH", tmp_path / "CTF-pay" / "config.paypal.json")
    monkeypatch.setattr(s, "REG_CONFIG_PATH", tmp_path / "CTF-reg" / "config.paypal-proxy.json")
    # 注：conftest 已经把 WEBUI_DATA_DIR 设到 tmp_path，secrets.json 会落
    # 到 tmp_path/secrets.json，下面断言要用这个路径。


def test_export_writes_two_files(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "paypal": {"email": "you@example.com"},
        "cloudflare": {"cf_token": "tok-abc", "zone_names": ["a.com", "b.com"]},
        "temp_mail": {
            "api_base_url": "https://mail.example.com",
            "admin_auth": "admin-secret",
            "custom_auth": "custom-secret",
            "enable_random_subdomain": False,
        },
        "captcha": {"api_url": "https://x", "api_key": "k", "client_key": "k"},
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    reg = json.loads((tmp_path / "CTF-reg" / "config.paypal-proxy.json").read_text())
    assert pay["paypal"]["email"] == "you@example.com"
    assert pay["captcha"]["api_key"] == "k"
    # mail.catch_all_domain(s) 来自 cloudflare zone_names；不再有 imap 字段
    assert reg["mail"]["backend"] == "cloudflare_temp_email_admin"
    assert reg["mail"]["api_base_url"] == "https://mail.example.com"
    assert reg["mail"]["catch_all_domain"] == "a.com"
    assert reg["mail"]["catch_all_domains"] == ["a.com", "b.com"]
    assert reg["mail"]["enable_random_subdomain"] is False
    assert "imap_server" not in reg["mail"]
    assert reg["captcha"]["client_key"] == "k"

    # secrets.json 应该带上 cloudflare zone 信息与 temp_mail Admin API 凭证
    secrets = json.loads((tmp_path / "secrets.json").read_text())
    cf = secrets["cloudflare"]
    assert cf["api_token"] == "tok-abc"
    assert cf["zone_names"] == ["a.com", "b.com"]
    tm = secrets["temp_mail"]
    assert tm["api_base_url"] == "https://mail.example.com"
    assert tm["admin_auth"] == "admin-secret"
    assert tm["custom_auth"] == "custom-secret"


def test_export_backs_up_existing(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    pay_path = tmp_path / "CTF-pay" / "config.paypal.json"
    pay_path.parent.mkdir(parents=True, exist_ok=True)
    pay_path.write_text(json.dumps({"old": True}))

    client.post("/api/config/export", json={"answers": {}})

    backups = list((tmp_path / "CTF-pay").glob("config.paypal.json.bak.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"old": True}


def test_export_writes_gopay_auto_otp(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "payment": {"method": "gopay"},
        "gopay": {
            "country_code": "62",
            "phone_number": "81234567890",
            "pin": "123456",
            "otp_timeout": 240,
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    assert pay["gopay"]["country_code"] == "62"
    assert pay["gopay"]["phone_number"] == "81234567890"
    assert pay["gopay"]["otp"]["source"] == "file"
    assert pay["gopay"]["otp"]["path"] == str(tmp_path / "wa_otp.txt")
    assert pay["gopay"]["otp"]["timeout"] == 240
    assert pay["gopay"]["otp"]["interval"] == 1


def test_export_writes_hosted_checkout_link_mode(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "team_plan": {
            "plan_name": "chatgptteamplan",
            "billing_country": "JP",
            "billing_currency": "JPY",
            "checkout_ui_mode": "hosted",
            "output_url_mode": "provider",
            "is_coupon_from_query_param": True,
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    plan = pay["fresh_checkout"]["plan"]
    assert plan["billing_country"] == "JP"
    assert plan["billing_currency"] == "JPY"
    assert plan["checkout_ui_mode"] == "hosted"
    assert plan["output_url_mode"] == "provider"
    assert plan["is_coupon_from_query_param"] is True


def test_export_preserves_cpa_config(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "cpa": {
            "enabled": True,
            "base_url": "https://cpa.example.com",
            "admin_key": "secret-admin-key",
            "oauth_client_id": "app_test_client",
            "plan_tag": "team",
            "free_plan_tag": "free",
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    assert pay["cpa"]["enabled"] is True
    assert pay["cpa"]["base_url"] == "https://cpa.example.com"
    assert pay["cpa"]["admin_key"] == "secret-admin-key"
    assert pay["cpa"]["oauth_client_id"] == "app_test_client"
    assert pay["cpa"]["plan_tag"] == "team"
    assert pay["cpa"]["free_plan_tag"] == "free"


def test_exported_reg_config_accepts_checkout_link_fields(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "team_plan": {
            "plan_name": "chatgptplusplan",
            "plan_type": "plus",
            "entry_point": "all_plans_pricing_modal",
            "billing_country": "ID",
            "billing_currency": "IDR",
            "checkout_ui_mode": "custom",
            "output_url_mode": "canonical",
            "is_coupon_from_query_param": False,
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    spec = importlib.util.spec_from_file_location("ctf_reg_config_for_test", Path("CTF-reg/config.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cfg = module.Config.from_file(str(tmp_path / "CTF-reg" / "config.paypal-proxy.json"))
    assert cfg.team_plan.plan_name == "chatgptplusplan"
    assert cfg.team_plan.billing_country == "ID"
    assert cfg.team_plan.billing_currency == "IDR"
    assert cfg.team_plan.checkout_ui_mode == "custom"
    assert cfg.team_plan.output_url_mode == "canonical"
    assert cfg.team_plan.is_coupon_from_query_param is False


def test_export_writes_sub2api_config(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "sub2api": {
            "enabled": True,
            "base_url": " https://sub.example.com ",
            "api_key": "sub-secret",
            "oauth_client_id": "app_codex",
            "concurrency": "5",
            "priority": "0",
            "rate_multiplier": "0.5",
            "proxy_id": "",
            "group_ids": "1, 2, abc, 3",
            "timeout_s": "30",
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    sub = pay["sub2api"]
    assert sub["enabled"] is True
    # 字符串字段被 trim
    assert sub["base_url"] == "https://sub.example.com"
    assert sub["api_key"] == "sub-secret"
    assert sub["oauth_client_id"] == "app_codex"
    # 数字字段被转 int / float；priority=0 保留
    assert sub["concurrency"] == 5
    assert sub["priority"] == 0
    assert sub["rate_multiplier"] == 0.5
    # 空值字段不写入
    assert "proxy_id" not in sub
    # group_ids 字符串拆 int 数组，过滤非数字
    assert sub["group_ids"] == [1, 2, 3]
    assert sub["timeout_s"] == 30


def test_export_sub2api_disabled_field_only(client, tmp_path, monkeypatch):
    """关掉 toggle 时 sub2api 段仍写入但仅含 enabled=False，避免 pipeline 误读。"""
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {"sub2api": {"enabled": False, "base_url": "https://x"}}
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200
    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    assert pay["sub2api"]["enabled"] is False
    # base_url 仍保留（用户可能切回开启再用）
    assert pay["sub2api"]["base_url"] == "https://x"


def test_export_requires_auth(client):
    r = client.post("/api/config/export", json={"answers": {}})
    assert r.status_code == 401
