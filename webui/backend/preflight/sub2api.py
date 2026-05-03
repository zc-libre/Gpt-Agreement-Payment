import httpx
from pydantic import BaseModel
from ._common import CheckResult, PreflightResult, aggregate


class Sub2ApiInput(BaseModel):
    base_url: str
    api_key: str


def check(body: dict) -> PreflightResult:
    """sub2api 连通性检查：列一个账号验证 base_url + api_key。"""
    cfg = Sub2ApiInput.model_validate(body)
    base = (cfg.base_url or "").rstrip("/")
    if not base or not cfg.api_key:
        return aggregate([CheckResult(
            name="connect", status="fail",
            message="缺少 base_url / api_key",
        )])

    url = f"{base}/api/v1/admin/accounts"
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(
                url,
                params={
                    "platform": "openai",
                    "type": "oauth",
                    "page": 1,
                    "page_size": 1,
                },
                headers={"x-api-key": cfg.api_key},
            )
    except httpx.HTTPError as e:
        return aggregate([CheckResult(
            name="connect", status="fail", message=str(e),
        )])

    if r.status_code != 200:
        return aggregate([CheckResult(
            name="connect", status="fail",
            message=f"HTTP {r.status_code}",
            details=r.text[:1000],
        )])
    try:
        r.json()
    except Exception:
        return aggregate([CheckResult(
            name="connect", status="fail",
            message="响应非 JSON",
            details=r.text[:1000],
        )])
    return aggregate([CheckResult(
        name="connect", status="ok", message="连通正常",
    )])
