"""middleware 助手 + 分派单元测试：_read_all_body / _make_replay_receive /
_patch_scope_content_length / __call__ 分派（spec §2.1 §2.10 §2.11 §2.13）。"""
from __future__ import annotations

import json

import pytest

from anomaly_middleware.middleware import (
    AnomalyMiddleware,
    _make_replay_receive,
    _patch_scope_content_length,
    _read_all_body,
)
from anomaly_middleware.metrics import METRICS_CONTENT_TYPE
from anomaly_middleware.env import PluginConfig


# --------------------------- _read_all_body --------------------------- #
@pytest.mark.asyncio
async def test_read_all_body_single():
    async def receive():
        return {"type": "http.request", "body": b"hello", "more_body": False}
    assert await _read_all_body(receive) == b"hello"


@pytest.mark.asyncio
async def test_read_all_body_multi_chunk():
    msgs = [
        {"type": "http.request", "body": b"foo", "more_body": True},
        {"type": "http.request", "body": b"bar", "more_body": False},
    ]
    async def receive():
        return msgs.pop(0)
    assert await _read_all_body(receive) == b"foobar"


@pytest.mark.asyncio
async def test_read_all_body_disconnect():
    async def receive():
        return {"type": "http.disconnect"}
    assert await _read_all_body(receive) == b""


# --------------------------- _make_replay_receive --------------------------- #
@pytest.mark.asyncio
async def test_replay_receive_first_synthetic_then_delegate():
    calls = []
    async def original_receive():
        calls.append("orig")
        return {"type": "http.disconnect"}
    replay = _make_replay_receive(original_receive, b"BODY")
    first = await replay()
    assert first == {"type": "http.request", "body": b"BODY", "more_body": False}
    second = await replay()
    assert second == {"type": "http.disconnect"}
    assert calls == ["orig"]


@pytest.mark.asyncio
async def test_replay_receive_never_empty_body_second_call():
    # 二次读绝不返回空 body 的 http.request
    async def original_receive():
        return {"type": "http.disconnect"}
    replay = _make_replay_receive(original_receive, b"B")
    await replay()
    second = await replay()
    assert second["type"] == "http.disconnect"  # 非 http.request


# --------------------------- _patch_scope_content_length --------------------------- #
def test_patch_scope_rewrites_existing_cl():
    scope = {"headers": [[b"content-type", b"application/json"], [b"content-length", b"5"]]}
    new = _patch_scope_content_length(scope, 99)
    assert new is not scope  # 浅拷贝
    assert dict(new["headers"])["content-length".encode()] in (b"99",)
    # 原始 scope 未变
    assert scope["headers"][1][1] == b"5"


def test_patch_scope_adds_missing_cl():
    scope = {"headers": [[b"content-type", b"application/json"]]}
    new = _patch_scope_content_length(scope, 42)
    cls = dict((h[0], h[1]) for h in new["headers"])
    assert cls[b"content-length"] == b"42"


def test_patch_scope_headers_copied_not_shared():
    scope = {"headers": [[b"content-length", b"1"]]}
    new = _patch_scope_content_length(scope, 2)
    new["headers"][0][1] = b"x"
    # 原始 headers 列表未被改
    assert scope["headers"][0][1] == b"1"


# --------------------------- AnomalyMiddleware 分派 --------------------------- #
def _make_mw_with_fake(fake_app, **cfg):
    # 构造期临时 enabled=False 跳过重活（tokenizer/runner/detector.yaml 硬依赖），
    # 构造后再覆盖 config 为目标状态；_runner/_resolver/_anomaly_store 留 None，
    # 分派/路由/错误透传/无 choices 的请求不触达检测（spec §2.13）。
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
    )
    mw._monitor_rate = mw.config.monitor_rate
    mw.metrics.set_monitor_rate(mw._monitor_rate)
    # _runner/_resolver/_anomaly_store 已为 None（enabled=False 构造）
    return mw


def _metric_value(metrics_text: bytes, name: str) -> float:
    """解析 Prometheus 文本中指定 counter 的当前值（跳过 # HELP/# TYPE）。"""
    for line in metrics_text.decode().splitlines():
        if line.startswith(f"{name} ") and not line.startswith("#"):
            return float(line.split()[-1])
    return 0.0


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append((scope.get("type"), scope.get("method"), scope.get("path")))
        if scope.get("type") != "http":
            return  # 非 http（lifespan 等）不发送 http 消息
        # 读掉 body（避免下游未消费）
        async for _ in _recv_iter(receive):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _recv_iter(receive):
    while True:
        msg = await receive()
        if msg["type"] == "http.request" and not msg.get("more_body"):
            yield msg
            return
        if msg["type"] == "http.disconnect":
            return
        yield msg


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
async def test_dispatch_non_http_passthrough():
    rec = _Recorder()
    mw = _make_mw_with_fake(rec)

    async def nope():
        return None

    await mw({"type": "lifespan"}, nope, nope)  # 非 http → 透传给下游
    assert rec.calls == [("lifespan", None, None)]


@pytest.mark.asyncio
async def test_dispatch_get_metrics_endpoint():
    rec = _Recorder()
    mw = _make_mw_with_fake(rec)
    sent = await _drive(mw, "GET", "/anomaly/metrics")
    assert sent[0]["status"] == 200
    hdrs = dict((h[0], h[1]) for h in sent[0]["headers"])
    assert hdrs[b"content-type"] == METRICS_CONTENT_TYPE.encode("latin-1")
    assert b"vllm_anomaly" in sent[1]["body"]
    assert rec.calls == []  # 下游未被调用


@pytest.mark.asyncio
async def test_dispatch_get_models_passthrough():
    rec = _Recorder()
    mw = _make_mw_with_fake(rec)
    await _drive(mw, "GET", "/v1/models")
    assert len(rec.calls) == 1
    assert rec.calls[0][1] == "GET"


@pytest.mark.asyncio
async def test_dispatch_get_chat_completions_passthrough():
    # spec §2.1：GET /v1/chat/completions → 原样转发
    rec = _Recorder()
    mw = _make_mw_with_fake(rec)
    await _drive(mw, "GET", "/v1/chat/completions")
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_disabled_passthrough_no_body_read():
    # spec §2.11/§2.13：enabled=False → 不注入不检测，透传
    rec = _Recorder()
    mw = _make_mw_with_fake(rec, enabled=False)
    body = json.dumps({"model": "m", "messages": []}).encode()
    await _drive(mw, "POST", "/v1/chat/completions", body=body)
    assert len(rec.calls) == 1


# --------------------------- 错误响应透传（spec §2.6：不改状态码/头部/body） --------------------------- #
class _ErrorApp:
    """返回指定 status + body 的下游，用于验证错误响应不被中间件改动。"""

    def __init__(self, status, body: bytes):
        self.status = status
        self.body = body
        self.received = []

    async def __call__(self, scope, receive, send):
        buf = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                buf.extend(msg.get("body", b"") or b"")
                if not msg.get("more_body"):
                    break
            elif msg["type"] == "http.disconnect":
                return
        self.received.append(bytes(buf))
        await send({"type": "http.response.start",
                    "status": self.status,
                    "headers": [[b"content-type", b"application/json"]]})
        await send({"type": "http.response.body", "body": self.body,
                    "more_body": False})


@pytest.mark.asyncio
async def test_error_response_json_status_and_body_preserved():
    """下游 400 + 错误 JSON body -> 状态码/错误消息保留，不调度检测。"""
    err = json.dumps({"object": "error", "message": "bad request",
                      "type": "invalid_request_error", "code": 400}).encode()
    app = _ErrorApp(400, err)
    mw = _make_mw_with_fake(app)
    sent = await _drive(mw, "POST", "/v1/chat/completions",
                        body=json.dumps({"model": "m", "messages": []}).encode())
    start = sent[0]
    assert start["status"] == 400  # 状态码保留
    body = sent[1]["body"]
    parsed = json.loads(body)
    assert parsed["message"] == "bad request"  # 错误消息保留
    # 无 choices -> 不抽取不检测（requests_total 保持 0）
    assert "choices" not in parsed
    assert _metric_value(mw.metrics.render_metrics(),
                         "vllm_anomaly_requests_total") == 0.0


@pytest.mark.asyncio
async def test_error_response_non_json_raw_passthrough():
    """下游 500 + 非 JSON body -> 原样透传（不改 body），不调度检测。"""
    app = _ErrorApp(500, b"<html>Internal Server Error</html>")
    mw = _make_mw_with_fake(app)
    sent = await _drive(mw, "POST", "/v1/chat/completions",
                        body=json.dumps({"model": "m", "messages": []}).encode())
    assert sent[0]["status"] == 500
    assert sent[1]["body"] == b"<html>Internal Server Error</html>"
    assert _metric_value(mw.metrics.render_metrics(),
                         "vllm_anomaly_requests_total") == 0.0


# --------------------------- 构造期配置非法 -> fail-fast（spec §2.13） --------------------------- #
def test_init_invalid_env_raises(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_TOP_LOGPROBS", "0")  # 非法 -> from_env 抛 ValueError
    with pytest.raises(ValueError):
        AnomalyMiddleware(_Recorder())


# --------------------------- 终端 body 幂等（spec §3.3） --------------------------- #
@pytest.mark.asyncio
async def test_interceptor_ignores_repeated_terminal_body():
    """终端 body(more_body=False) 后重复的 body 消息被忽略，不二次调度检测。"""
    import anomaly_middleware.middleware as mwmod
    from anomaly_middleware.extractor import OriginalParams
    from anomaly_middleware.metrics import Metrics

    sent = []
    async def _send(m):
        sent.append(m)

    ctx = mwmod.RequestContext(
        orig=OriginalParams(True, None, None, False, 1, False),
        is_chat=True, model="m", request_id="rid",
        will_detect=True, top_logprobs=20,
    )
    runner = object()  # 无真实 runner；_maybe_schedule_detection 会因 runner None 跳过
    ic = mwmod.ResponseInterceptor(
        _send, ctx=ctx, runner=runner, metrics=Metrics(),
        pending_tasks=set(),
    )
    # 非流式：start + 完整 body + 重复终端 body
    await ic({"type": "http.response.start", "status": 200,
              "headers": [[b"content-type", b"application/json"]]})
    body = json.dumps({"id": "c", "model": "m", "choices": []}).encode()
    await ic({"type": "http.response.body", "body": body, "more_body": False})
    n_send = len(sent)
    await ic({"type": "http.response.body", "body": b"x", "more_body": False})
    await ic({"type": "http.response.body", "body": b"y"})  # more_body 缺省 -> 视为终端
    assert len(sent) == n_send  # 重复终端 body 不再下发
    assert ic._finished is True


# --------------------------- _ensure_resolver（已移除） --------------------------- #
# 重构后 tokenizer 在 AnomalyMiddleware.__init__ 启动期同步加载（spec §2.15），
# 加载失败即终止启动（fail-fast，不软降级），不再有运行期 lazy _ensure_resolver。
# 相关覆盖由 test_token_resolver.py（需 transformers）与 e2e 承担。
