import respx
from httpx import Response


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


@respx.mock
def test_sub2api_ok(client):
    _login(client)
    respx.get("https://sub.example.com/api/v1/admin/accounts").mock(
        return_value=Response(200, json={"data": {"items": [], "total": 0}})
    )
    r = client.post("/api/preflight/sub2api", json={
        "base_url": "https://sub.example.com",
        "api_key": "k",
    })
    body = r.json()
    assert body["status"] == "ok"
    assert body["message"] == "1/1 ok"


@respx.mock
def test_sub2api_http_error(client):
    _login(client)
    respx.get("https://sub.example.com/api/v1/admin/accounts").mock(
        return_value=Response(401, json={"error": "unauthorized"})
    )
    r = client.post("/api/preflight/sub2api", json={
        "base_url": "https://sub.example.com",
        "api_key": "bad",
    })
    body = r.json()
    assert body["status"] == "fail"
    assert "401" in body["checks"][0]["message"]


def test_sub2api_missing_fields(client):
    _login(client)
    r = client.post("/api/preflight/sub2api", json={
        "base_url": "",
        "api_key": "",
    })
    body = r.json()
    assert body["status"] == "fail"
    assert "缺少" in body["checks"][0]["message"]


@respx.mock
def test_sub2api_strips_trailing_slash(client):
    _login(client)
    # 给 base_url 带尾斜杠，preflight 应剥掉再请求
    respx.get("https://sub.example.com/api/v1/admin/accounts").mock(
        return_value=Response(200, json={"data": {"items": []}})
    )
    r = client.post("/api/preflight/sub2api", json={
        "base_url": "https://sub.example.com/",
        "api_key": "k",
    })
    assert r.json()["status"] == "ok"
