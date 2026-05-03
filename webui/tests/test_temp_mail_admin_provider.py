import importlib.util
import json
import sys
from pathlib import Path


def _load_provider_module():
    path = Path(__file__).resolve().parents[2] / "CTF-reg" / "temp_mail_admin_provider.py"
    spec = importlib.util.spec_from_file_location("temp_mail_admin_provider", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self._body


class _FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, req, timeout=None):
        body = req.data.decode() if req.data else ""
        self.requests.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        status, payload = self.responses.pop(0)
        return _FakeResp(status, payload)


def test_create_address_requests_root_domain_by_default(monkeypatch):
    mod = _load_provider_module()
    opener = _FakeOpener([
        (200, {"data": {"address": "preflight@abc.example.com"}}),
    ])
    monkeypatch.setattr(mod.urllib.request, "build_opener", lambda *a, **k: opener)

    provider = mod.TempMailAdminProvider(
        mod.TempMailAdminConfig(
            api_base_url="https://mail.example.com",
            admin_auth="admin-secret",
            enable_random_subdomain=False,
        )
    )
    address = provider.create_address("preflight", "example.com")

    assert address == "preflight@abc.example.com"
    method, url, headers, body = opener.requests[0]
    assert method == "POST"
    assert url == "https://mail.example.com/admin/new_address"
    assert headers["X-admin-auth"] == "admin-secret"
    assert json.loads(body) == {
        "name": "preflight",
        "domain": "example.com",
        "enablePrefix": True,
        "enableRandomSubdomain": False,
    }


def test_extract_raw_mime_accepts_known_field_shapes():
    mod = _load_provider_module()
    assert mod.extract_raw_mime({"raw": "Subject: x\n\ncode 111111"}) == "Subject: x\n\ncode 111111"
    assert mod.extract_raw_mime({"source": "Subject: x\n\ncode 222222"}) == "Subject: x\n\ncode 222222"
    assert mod.extract_raw_mime({"nested": {"content": "Subject: x\n\ncode 333333"}}) == "Subject: x\n\ncode 333333"


def test_list_mails_accepts_results_shape(monkeypatch):
    mod = _load_provider_module()
    opener = _FakeOpener([
        (200, {"results": [], "count": 0}),
    ])
    monkeypatch.setattr(mod.urllib.request, "build_opener", lambda *a, **k: opener)

    provider = mod.TempMailAdminProvider(
        mod.TempMailAdminConfig(
            api_base_url="https://mail.example.com",
            admin_auth="admin-secret",
        )
    )

    assert provider.list_mails("empty@example.com") == []


def test_extract_raw_mime_fails_on_unknown_shape():
    mod = _load_provider_module()
    try:
        mod.extract_raw_mime({"id": "mail-1", "subject": "no raw"})
    except RuntimeError as e:
        assert "raw/source/content/body/message" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_extract_otp_from_raw_mime_html():
    mod = _load_provider_module()
    raw = (
        "Subject: Your OpenAI verification code\n"
        "Content-Type: text/html; charset=utf-8\n\n"
        "<html><style>.x{color:#353740}</style>"
        "<body>Your verification code to continue: <b>941275</b></body></html>"
    )
    assert mod.extract_otp_from_raw(raw, email_addr="abc123@example.com") == "941275"
