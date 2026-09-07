"""动态配置端点单元测试：GET /anomaly/config（spec §2.8 动态更新）。"""
from __future__ import annotations

import json

import pytest

from anomaly_middleware import AnomalyMiddleware
from anomaly_middleware.env import PluginConfig
from tests._helpers import FakeVLLM


def _make_mw(fake_app, **cfg):
    import os
    old = os.environ.get("VLLM_ANOMALY_ENABLED")
    os.environ["VLLM_ANOMALY_ENABLED"] = "0"
    try:
        mw = AnomalyMiddleware(fake_app)
    finally:
        if old is None:
            os.environ.pop("VLLM_ANOMALY_ENABLED", None)
        else:
            os.environ["VLLM_ANOMALY_ENABLED"] = old
    mw.config = PluginConfig(
        enabled=cfg.get("enabled", True),
        top_logprobs=cfg.get("top_logprobs", 20),
        monitor_rate=cfg.get("monitor_rate", 1.0),
        config_path=cfg.get("config_path", "/anomaly/config"),
    )
    mw._monitor_rate = mw.config.monitor_rate
    mw.metrics.set_monitor_rate(mw._monitor_rate)
    return mw


async def _drive(mw, method, path, body=b"", headers=None):
    sent = []
    async def send(m):
        sent.append(m)
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [[b"content-type", b"application/json"], [b"content-length", str(len(body)).encode()]],
    }
    msg = {"delivered": False}
    async def receive():
        if not msg["delivered"]:
            msg["delivered"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    await mw(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_get_config_returns_current_rate():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.3)
    sent = await _drive(mw, "GET", "/anomaly/config")
    assert sent[0]["status"] == 200
    hdrs = dict((h[0], h[1]) for h in sent[0]["headers"])
    assert hdrs[b"content-type"] == b"application/json; charset=utf-8"
    body = json.loads(sent[1]["body"])
    assert body == {"monitor_rate": 0.3}


@pytest.mark.asyncio
async def test_get_config_default_rate():
    mw = _make_mw(FakeVLLM(lambda *a: None))
    sent = await _drive(mw, "GET", "/anomaly/config")
    body = json.loads(sent[1]["body"])
    assert body == {"monitor_rate": 1.0}


@pytest.mark.asyncio
async def test_get_config_custom_path():
    mw = _make_mw(FakeVLLM(lambda *a: ("json", {"ok": True})), config_path="/custom/cfg")
    sent = await _drive(mw, "GET", "/custom/cfg")
    assert sent[0]["status"] == 200
    # 默认路径不再拦截
    sent2 = await _drive(mw, "GET", "/anomaly/config")
    # 透传给下游（FakeVLLM 会处理）
    assert len(sent2) >= 2  # 下游响应了


@pytest.mark.asyncio
async def test_config_endpoint_works_when_disabled():
    mw = _make_mw(FakeVLLM(lambda *a: None), enabled=False)
    sent = await _drive(mw, "GET", "/anomaly/config")
    assert sent[0]["status"] == 200
    body = json.loads(sent[1]["body"])
    assert body == {"monitor_rate": 1.0}


# --------------------------- POST 更新 --------------------------- #
@pytest.mark.asyncio
async def test_post_update_valid():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=1.0)
    body = json.dumps({"monitor_rate": 0.3}).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 200
    resp = json.loads(sent[1]["body"])
    assert resp == {"monitor_rate": 0.3}
    assert mw._monitor_rate == 0.3
    # gauge 也更新
    text = mw.metrics.render_metrics().decode()
    assert "vllm_anomaly_monitor_rate 0.3" in text


@pytest.mark.asyncio
async def test_post_update_boundary_0():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=1.0)
    body = json.dumps({"monitor_rate": 0.0}).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 200
    assert mw._monitor_rate == 0.0


@pytest.mark.asyncio
async def test_post_update_boundary_1():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.0)
    body = json.dumps({"monitor_rate": 1.0}).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 200
    assert mw._monitor_rate == 1.0


@pytest.mark.asyncio
async def test_post_invalid_json():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.5)
    sent = await _drive(mw, "POST", "/anomaly/config", body=b"not json")
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["error"] == "invalid JSON body"
    assert mw._monitor_rate == 0.5  # 不变


@pytest.mark.asyncio
async def test_post_non_dict():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.5)
    body = json.dumps([1, 2, 3]).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["error"] == "body must be a JSON object"
    assert mw._monitor_rate == 0.5


@pytest.mark.asyncio
async def test_post_missing_field():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.5)
    body = json.dumps({}).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["error"] == "missing 'monitor_rate' field"
    assert mw._monitor_rate == 0.5


@pytest.mark.asyncio
async def test_post_non_number():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.5)
    body = json.dumps({"monitor_rate": "abc"}).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["error"] == "monitor_rate must be a number"
    assert mw._monitor_rate == 0.5


@pytest.mark.asyncio
async def test_post_out_of_range():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.5)
    body = json.dumps({"monitor_rate": 1.5}).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 400
    assert "must be 0.0-1.0" in json.loads(sent[1]["body"])["error"]
    assert mw._monitor_rate == 0.5


@pytest.mark.asyncio
async def test_post_negative():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=0.5)
    body = json.dumps({"monitor_rate": -0.1}).encode()
    sent = await _drive(mw, "POST", "/anomaly/config", body=body)
    assert sent[0]["status"] == 400
    assert mw._monitor_rate == 0.5


@pytest.mark.asyncio
async def test_get_after_update():
    mw = _make_mw(FakeVLLM(lambda *a: None), monitor_rate=1.0)
    body = json.dumps({"monitor_rate": 0.5}).encode()
    await _drive(mw, "POST", "/anomaly/config", body=body)
    sent = await _drive(mw, "GET", "/anomaly/config")
    assert json.loads(sent[1]["body"]) == {"monitor_rate": 0.5}


@pytest.mark.asyncio
async def test_other_method_passthrough():
    mw = _make_mw(FakeVLLM(lambda *a: ("json", {"ok": True})))
    sent = await _drive(mw, "PUT", "/anomaly/config")
    # 透传给下游 FakeVLLM → 200
    assert sent[0]["status"] == 200
