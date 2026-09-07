"""e2e: 采样 / 指标 / 降级 / 检测异常隔离（spec §2.6 §2.8 §2.10 §2.11 §2.13）。"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from _helpers import FakeVLLM, build_chat_response, chat_top_entry
from anomaly_middleware.env import PluginConfig, resolve_config_path
from anomaly_middleware.detector_runner import DetectorRunner
from anomaly_middleware import AnomalyMiddleware
from anomaly_middleware.metrics import METRICS_CONTENT_TYPE
from conftest import drain

NI = "你"
pytestmark = pytest.mark.asyncio


class _BrokenRunner:
    """检测即失败的假 runner（模拟子进程崩溃，spec §2.10 检测异常隔离）。"""

    _topk_n = 20

    async def run_async(self, logprobs_list, token_ids_list):
        raise RuntimeError("forced broken for test")

    def shutdown(self):
        pass


def _chat_fn(model="glm-4-7", n_top=20):
    def fn(scope, body):
        e = chat_top_entry(100, NI, -0.1, n_top=n_top)
        return ("json", build_chat_response(model, [e]))
    return fn


async def test_metrics_endpoint(client_factory):
    client, fake, mw = client_factory(_chat_fn())
    resp = await client.get("/anomaly/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == METRICS_CONTENT_TYPE
    assert b"vllm_anomaly" in resp.content
    # 下游无 /anomaly/metrics 路由（fake 未被调用）
    assert len(fake.received) == 0


async def test_monitor_rate_zero_passthrough(client_factory):
    # 采样率 0.0 → 不注入、不检测，请求直接透传
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=0.0)
    resp = await client.post(
        "/v1/chat/completions", json={"model": "m", "messages": []}
    )
    assert resp.status_code == 200
    # 下游收到的 body 未被注入（无 return_tokens_as_token_ids）
    injected = json.loads(fake.received[0][1])
    assert "return_tokens_as_token_ids" not in injected
    assert "logprobs" not in injected
    # 未检测
    await drain(mw)
    text = mw.metrics.render_metrics().decode()
    assert "vllm_anomaly_requests_total 0" in text


async def test_monitor_rate_one_all_injected(client_factory):
    # 采样率 1.0 → 每请求都注入检测
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=1.0, real_runner=True)
    await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    for scope, body in fake.received:
        assert json.loads(body).get("return_tokens_as_token_ids") is True
    await drain(mw)
    text = mw.metrics.render_metrics().decode()
    assert "vllm_anomaly_requests_total 2" in text


async def test_monitor_rate_partial_with_patched_random(client_factory, monkeypatch):
    # 采样率 0.3，patch random 使 0.1<0.3 选中注入，0.5>=0.3 透传
    import anomaly_middleware.middleware as mwmod

    seq = iter([0.1, 0.5])
    monkeypatch.setattr(mwmod.random, "random", lambda: next(seq))
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=0.3, real_runner=True)
    r1 = await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    r2 = await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    b1 = json.loads(fake.received[0][1])
    b2 = json.loads(fake.received[1][1])
    assert b1.get("return_tokens_as_token_ids") is True  # 0.1<0.3 选中注入
    assert "return_tokens_as_token_ids" not in b2  # 0.5>=0.3 透传
    await drain(mw)
    text = mw.metrics.render_metrics().decode()
    assert "vllm_anomaly_requests_total 1" in text  # 仅 1 次检测


async def test_degrade_when_paths_unresolvable(monkeypatch):
    # detector.yaml 不可解析 → 构造期 fail-fast（spec §2.13）。
    # 旧"永久降级透传"行为已移除：缺失配置直接启动失败，不再软降级。
    import anomaly_middleware.middleware as mwmod

    def _raise():
        raise FileNotFoundError("detector.yaml not found")

    monkeypatch.setattr(mwmod, "resolve_config_path", _raise)
    monkeypatch.delenv("VLLM_ANOMALY_ENABLED", raising=False)
    with pytest.raises(FileNotFoundError):
        AnomalyMiddleware(FakeVLLM(lambda *a: None))


async def test_detection_error_isolation(client_factory):
    # 即失败 runner：检测失败计 error，客户端响应不受影响（spec §2.10）
    client, fake, mw = client_factory(_chat_fn())
    mw._runner = _BrokenRunner()
    resp = await client.post(
        "/v1/chat/completions", json={"model": "glm-4-7", "messages": []}
    )
    assert resp.status_code == 200  # 客户端响应不受影响
    # 响应仍正常恢复
    assert resp.json()["choices"][0]["logprobs"] is None
    await drain(mw)
    text = mw.metrics.render_metrics().decode()
    assert "vllm_anomaly_detection_errors_total 1" in text
    # 后续请求正常处理（仍注入恢复；检测仍计 error）
    resp2 = await client.post(
        "/v1/chat/completions", json={"model": "glm-4-7", "messages": []}
    )
    assert resp2.status_code == 200
    await drain(mw)
    text2 = mw.metrics.render_metrics().decode()
    assert "vllm_anomaly_detection_errors_total 2" in text2


async def test_disabled_master_switch_passthrough(client_factory):
    # 总开关 False → 不注入不检测，指标端点仍可达
    client, fake, mw = client_factory(_chat_fn(), enabled=False)
    resp = await client.post(
        "/v1/chat/completions", json={"model": "m", "messages": []}
    )
    assert resp.status_code == 200
    assert "return_tokens_as_token_ids" not in json.loads(fake.received[0][1])
    mresp = await client.get("/anomaly/metrics")
    assert mresp.status_code == 200
    assert b"vllm_anomaly_requests_total 0" in mresp.content


async def test_non_target_passthrough_no_injection(client_factory):
    # 非 POST 或非目标路径 → 透传不改 body
    client, fake, mw = client_factory(_chat_fn())
    await client.post("/v1/some/other", json={"model": "m"})
    await client.get("/v1/chat/completions")
    for scope, body in fake.received:
        if not body:
            continue  # GET 无 body
        assert "return_tokens_as_token_ids" not in json.loads(body)


async def test_non_json_body_passthrough(client_factory):
    # 非 JSON 请求体 → 透传，不注入
    client, fake, mw = client_factory(_chat_fn())
    resp = await client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 200
    # 下游收到原始 body
    assert fake.received[0][1] == b"not json"


async def test_monitor_rate_uses_runtime_attribute(client_factory, monkeypatch):
    """_monitor_rate 独立于 config.monitor_rate——运行时属性驱动采样。"""
    import anomaly_middleware.middleware as mwmod

    seq = iter([0.5])  # 0.5
    monkeypatch.setattr(mwmod.random, "random", lambda: next(seq))
    # config.monitor_rate=0.3, 但 _monitor_rate=1.0
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=0.3, real_runner=True)
    mw._monitor_rate = 1.0  # 覆盖为 1.0
    resp = await client.post(
        "/v1/chat/completions", json={"model": "m", "messages": []}
    )
    assert resp.status_code == 200
    # 0.5 < 1.0 (_monitor_rate) → 选中注入（若读 config.monitor_rate=0.3 则 0.5>=0.3 透传）
    body = json.loads(fake.received[0][1])
    assert body.get("return_tokens_as_token_ids") is True


# --------------------------- 动态更新 E2E --------------------------- #
async def test_dynamic_update_to_zero_then_one(client_factory):
    """运行时 POST rate=0.0 → 后续请求全透传；POST rate=1.0 → 全监控。"""
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=1.0, real_runner=True)

    # 更新为 0.0
    resp = await client.post("/anomaly/config", json={"monitor_rate": 0.0})
    assert resp.status_code == 200
    assert resp.json()["monitor_rate"] == 0.0

    # 发请求 → 透传（不注入）
    resp = await client.post(
        "/v1/chat/completions", json={"model": "m", "messages": []}
    )
    assert resp.status_code == 200
    body = json.loads(fake.received[0][1])
    assert "return_tokens_as_token_ids" not in body

    # 更新为 1.0
    resp = await client.post("/anomaly/config", json={"monitor_rate": 1.0})
    assert resp.json()["monitor_rate"] == 1.0

    # 发请求 → 注入检测
    resp = await client.post(
        "/v1/chat/completions", json={"model": "m", "messages": []}
    )
    assert resp.status_code == 200
    body = json.loads(fake.received[1][1])
    assert body.get("return_tokens_as_token_ids") is True

    await drain(mw)
    text = mw.metrics.render_metrics().decode()
    assert "vllm_anomaly_requests_total 1" in text  # 仅 rate=1.0 后的 1 次检测


async def test_metrics_contains_monitor_rate(client_factory):
    """GET /anomaly/metrics 包含 vllm_anomaly_monitor_rate gauge。"""
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=0.3)
    resp = await client.get("/anomaly/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "vllm_anomaly_monitor_rate" in text
    assert "vllm_anomaly_monitor_rate 0.3" in text


async def test_metrics_reflects_updated_rate(client_factory):
    """POST 更新后 metrics gauge 反映新值。"""
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=1.0)
    await client.post("/anomaly/config", json={"monitor_rate": 0.5})
    resp = await client.get("/anomaly/metrics")
    text = resp.text
    assert "vllm_anomaly_monitor_rate 0.5" in text


async def test_get_config_returns_initial_rate(client_factory):
    """启动后 GET /anomaly/config 返回 env 初始值。"""
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=0.7)
    resp = await client.get("/anomaly/config")
    assert resp.status_code == 200
    assert resp.json()["monitor_rate"] == 0.7


async def test_invalid_update_does_not_change_metrics(client_factory):
    """POST 无效值 → metrics gauge 不变。"""
    client, fake, mw = client_factory(_chat_fn(), monitor_rate=0.3)
    resp = await client.post("/anomaly/config", json={"monitor_rate": 1.5})
    assert resp.status_code == 400
    mresp = await client.get("/anomaly/metrics")
    assert "vllm_anomaly_monitor_rate 0.3" in mresp.text
