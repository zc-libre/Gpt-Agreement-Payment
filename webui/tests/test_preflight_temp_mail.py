import json


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


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
        self.requests.append((req.get_method(), req.full_url, req.data.decode() if req.data else ""))
        status, payload = self.responses.pop(0)
        return _FakeResp(status, payload)


def test_temp_mail_preflight_checks_new_address_and_admin_mails(client, monkeypatch):
    _login(client)
    import urllib.request

    opener = _FakeOpener([
        (200, {"data": {"address": "preflight@r8.example.com"}}),
        (200, {"data": []}),
    ])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: opener)

    r = client.post("/api/preflight/temp_mail", json={
        "api_base_url": "https://mail.example.com",
        "admin_auth": "admin-secret",
        "domain": "example.com",
        "enable_random_subdomain": False,
    })

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert opener.requests[0][0] == "POST"
    assert opener.requests[0][1] == "https://mail.example.com/admin/new_address"
    assert json.loads(opener.requests[0][2])["enableRandomSubdomain"] is False
    assert opener.requests[1][0] == "GET"
    assert "/admin/mails?" in opener.requests[1][1]


def test_temp_mail_preflight_fails_when_create_returns_no_address(client, monkeypatch):
    _login(client)
    import urllib.request

    opener = _FakeOpener([(200, {"data": {"id": "addr-1"}})])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: opener)

    r = client.post("/api/preflight/temp_mail", json={
        "api_base_url": "https://mail.example.com",
        "admin_auth": "admin-secret",
        "domain": "example.com",
    })

    assert r.status_code == 200
    assert r.json()["status"] == "fail"
