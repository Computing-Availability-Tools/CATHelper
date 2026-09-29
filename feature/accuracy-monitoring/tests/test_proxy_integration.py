"""PD 分离 proxy 集成层单元测试（design_pd_proxy.md §5 / §10）。

覆盖：StaticTokenTextResolver / load_token2category / before_forward 注入与采样 /
ProxyStreamProcessor 流式（SSE 重组/还原/检测累积/[DONE]/CRLF/recompute 拦截与重置）/
非流式（分片缓冲/整体 extract+strip/recompute 重写）/ 空响应不检测 / 动态配置。

检测进程池以 StubRunner 替身注入，不启动真实 ProcessPoolExecutor。
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from anomaly_middleware import proxy_integration as pi
from _helpers import chat_top_entry, chat_stream_chunk, completions_stream_chunk


# --------------------------------------------------------------------------- #
# 替身与构造辅助
# --------------------------------------------------------------------------- #
class StubRunner:
    """DetectorRunner 替身：记录调度调用，避免真实进程池。"""

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.closed = False

    async def run_async(self, logprobs_list, token_ids_list):
        self.calls.append((logprobs_list, token_ids_list))
        return [[False, 0] for _ in token_ids_list]

    def shutdown(self):
        self.closed = True

    def _rebuild_pool(self):
        pass


def make_controller(
    monkeypatch,
    tmp_path,
    *,
    monitor_rate=1.0,
    enabled=True,
    tk2cat=None,
    token_text=None,
):
    """创建控制器（tk2cat/文本表写临时目录，DetectorRunner/ILLDetector 打桩）。"""
    tk2cat = tk2cat if tk2cat is not None else {"0": "english_latin", "1": "chinese_cjk"}
    token_text = token_text if token_text is not None else {"0": "a", "1": "你"}
    d = tmp_path / "token2category"
    t = tmp_path / "token_text"
    d.mkdir(exist_ok=True)
    t.mkdir(exist_ok=True)
    f1 = d / "m_2.json"
    f2 = t / "m_2.json"
    f1.write_text(json.dumps(tk2cat), encoding="utf-8")
    f2.write_text(json.dumps(token_text), encoding="utf-8")

    runners = []

    def _fake_runner(*args, **kwargs):
        r = StubRunner()
        runners.append(r)
        return r

    monkeypatch.setattr(pi, "DetectorRunner", _fake_runner)
    monkeypatch.setattr(pi, "ILLDetector", lambda *a, **k: object())
    ctl = pi.ProxyAnomalyController(
        tk2cat_path=str(f1),
        monitor_rate=monitor_rate,
        detector_workers=1,
        enabled=enabled,
    )
    return ctl, runners


def sse(data) -> bytes:
    return b"data: " + json.dumps(data).encode("utf-8") + b"\n\n"


async def adrain(ctl, timeout: float = 10.0) -> None:
    """等待 fire-and-forget 检测任务完成（与 tests/conftest.drain 等价）。"""
    tasks = list(ctl._pending_tasks)
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout
        )


# --------------------------------------------------------------------------- #
# StaticTokenTextResolver / 文件加载
# --------------------------------------------------------------------------- #
def test_static_resolver_basic():
    r = pi.StaticTokenTextResolver({"0": "a", "1": "", "2": "你"})
    assert r.resolve(0) == "a"
    assert r.resolve("0") == "a"
    assert r.resolve(1) is None      # 空串 → None（走 bytes/null 兜底）
    assert r.resolve(999) is None    # 缺失
    assert r.resolve(None) is None
    assert r.resolve(2) == "你"
    assert r._cache[2] == "你"       # 缓存命中


def test_load_token2category(tmp_path):
    f = tmp_path / "m_100.json"
    f.write_text(json.dumps({"0": "chinese_cjk", "5": "english_latin"}), encoding="utf-8")
    tk2cat, vocab = pi.load_token2category(str(f))
    assert tk2cat == {"0": "chinese_cjk", "5": "english_latin"}
    assert vocab == 6  # max(键)+1，与文件名无关


def test_load_token2category_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        pi.load_token2category(str(tmp_path / "nope.json"))


def test_load_token2category_bad(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json{", encoding="utf-8")
    with pytest.raises(ValueError):
        pi.load_token2category(str(f))
    f2 = tmp_path / "arr.json"
    f2.write_text("[1,2]", encoding="utf-8")
    with pytest.raises(ValueError):
        pi.load_token2category(str(f2))
    f3 = tmp_path / "empty.json"
    f3.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        pi.load_token2category(str(f3))


def test_derive_token_text_path(tmp_path):
    p = tmp_path / "token2category" / "m_2.json"
    expected = tmp_path / "token_text" / "m_2.json"
    assert pi.derive_token_text_path(str(p)) == str(expected)


# --------------------------------------------------------------------------- #
# 控制器：初始化与采样
# --------------------------------------------------------------------------- #
def test_controller_disabled_passthrough(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path, enabled=False)
    assert ctl.before_forward({"model": "m", "messages": []}, True) is None
    # 端点仍可达
    assert b"vllm_anomaly_monitor_rate" in ctl.render_metrics()
    assert ctl.get_monitor_rate() == 1.0


def test_before_forward_inject_and_snapshot(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path)
    req = {
        "model": "my-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "top_logprobs": 5,
        "n": 2,
    }
    ctx = ctl.before_forward(req, True)
    assert ctx is not None
    # 注入值 = max(客户端, N)
    assert req["top_logprobs"] == 20
    assert req["logprobs"] is True
    assert req["return_tokens_as_token_ids"] is True
    # 快照保留原值
    assert ctx.orig.top_logprobs == 5
    assert ctx.orig.n == 2
    assert ctx.orig.stream is True
    assert ctx.model == "my-model"
    assert ctx.prompt == [{"role": "user", "content": "hi"}]
    assert ctx.stream is True
    assert ctx.request_id


def test_before_forward_completions_inject(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "prompt": "p", "stream": False, "logprobs": 10}
    ctx = ctl.before_forward(req, False)
    assert req["logprobs"] == 20  # max(10, 20)
    assert ctx.orig.logprobs == 10
    assert ctx.prompt == "p"
    assert ctx.stream is False


def test_before_forward_rate_zero(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path, monitor_rate=0.0)
    req = {"model": "m", "prompt": "p", "stream": False}
    assert ctl.before_forward(req, False) is None


def test_before_forward_non_dict(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path)
    assert ctl.before_forward([1, 2], False) is None


def test_update_monitor_rate(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path)
    ok, err = ctl.update_monitor_rate(0.3)
    assert ok and err == "" and ctl.get_monitor_rate() == 0.3
    ok, err = ctl.update_monitor_rate(1.5)
    assert not ok and "0.0-1.0" in err
    assert ctl.get_monitor_rate() == 0.3
    ok, err = ctl.update_monitor_rate("abc")
    assert not ok and ctl.get_monitor_rate() == 0.3


# --------------------------------------------------------------------------- #
# 流式（chat SSE）
# --------------------------------------------------------------------------- #
async def test_stream_chat_flow(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    ctx = ctl.before_forward(req, True)
    e1 = chat_stream_chunk("m", chat_top_entry(0, "a", -0.1), delta_text="a")
    e2 = chat_stream_chunk("m", chat_top_entry(1, "你", -0.2), delta_text="你")
    payload = sse(e1) + sse(e2) + b"data: [DONE]\n\n"
    # 任意字节边界分片（含半事件）
    cut = payload.index(b"\n\n") + 1  # 切在第一个事件终止符中间
    out1, ev1 = ctl.feed_stream(ctx, payload[:cut])
    out2, ev2 = ctl.feed_stream(ctx, payload[cut:])
    out = out1 + out2
    assert b"token_id:" not in out          # strip 还原无泄漏
    assert b'"logprobs":null' in out or b'"logprobs": null' in out  # 客户端未请求 → null
    assert b"[DONE]" in out                 # 终端透传
    all_events = ev1 + ev2
    assert len(all_events) == 2             # [DONE] 不上抛
    # 流结束：调度检测（含 per-choice 数据）
    tail = ctl.finish(ctx)
    assert tail == b""
    await adrain(ctl)
    assert len(runners) == 1 and len(runners[0].calls) == 1
    lp_list, ti_list = runners[0].calls[0]
    assert len(lp_list) == 1
    assert ti_list[0].shape[0] == 2                     # 两个 token 位置
    assert ti_list[0][:, 0].tolist() == [10000, 10000]  # top-1 序列（helper 的 top 条目 id）
    # 文本对齐
    assert ctx.processor.get_texts() == ["a你"]


async def test_stream_recompute_holdback_and_reset(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    ctx = ctl.before_forward(req, True)
    e1 = chat_stream_chunk("m", chat_top_entry(0, "a", -0.1), delta_text="a")
    rec = chat_stream_chunk("m", chat_top_entry(1, "b", -0.2), delta_text="b")
    rec["choices"][0]["stop_reason"] = "recomputed"
    e2 = chat_stream_chunk("m", chat_top_entry(2, "c", -0.3), delta_text="c")
    out1, ev1 = ctl.feed_stream(ctx, sse(e1))
    out2, ev2 = ctl.feed_stream(ctx, sse(rec))
    assert ev2 and ev2[0]["choices"][0]["stop_reason"] == "recomputed"
    assert b"recomputed" not in out2        # 拦截：不转发给客户端
    ctl.on_recompute(ctx)                   # 重置：丢弃不完整流
    out3, ev3 = ctl.feed_stream(ctx, sse(e2) + b"data: [DONE]\n\n")
    assert b"token_id:" not in out3
    ctl.finish(ctx)
    await adrain(ctl)
    assert len(runners[0].calls) == 1
    _, ti_list = runners[0].calls[0]
    assert ti_list[0][:, 0].tolist() == [10000]  # 仅最终 attempt 数据，旧流不重复检测
    assert ctx.processor.get_texts() == ["c"]


async def test_stream_crlf_and_keepalive(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    ctx = ctl.before_forward(req, True)
    e1 = chat_stream_chunk("m", chat_top_entry(0, "a", -0.1), delta_text="a")
    raw = sse(e1).replace(b"\n\n", b"\r\n\r\n") + b": keep-alive\n\n" + b"data: [DONE]\r\n\r\n"
    out, ev = ctl.feed_stream(ctx, raw)
    assert b"keep-alive" in out
    assert b"[DONE]" in out
    assert len(ev) == 1
    ctl.finish(ctx)


async def test_stream_empty_no_detection(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    ctx = ctl.before_forward(req, True)
    out, ev = ctl.feed_stream(ctx, b"data: [DONE]\n\n")
    assert b"[DONE]" in out
    ctl.finish(ctx)
    await adrain(ctl)
    assert runners == [] or runners[0].calls == []  # 空响应不检测


async def test_finished_ctx_passthrough(monkeypatch, tmp_path):
    ctl, _ = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "messages": [], "stream": True}
    ctx = ctl.before_forward(req, True)
    ctl.finish(ctx)
    out, ev = ctl.feed_stream(ctx, b"anything")
    assert out == b"anything" and ev == []  # 已收尾 → 原样透传


# --------------------------------------------------------------------------- #
# 非流式
# --------------------------------------------------------------------------- #
async def test_buffered_completions_flow(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "prompt": "p", "stream": False}
    ctx = ctl.before_forward(req, False)
    body = {
        "id": "c1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "text": "hi",
                "logprobs": {
                    "tokens": ["token_id:0", "token_id:1"],
                    "token_logprobs": [-0.1, -0.2],
                    "top_logprobs": [
                        {"token_id:0": -0.1, "token_id:9": -0.9},
                        {"token_id:1": -0.2},
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": 2},
    }
    raw = json.dumps(body).encode("utf-8")
    out1, ev1 = ctl.feed_stream(ctx, raw[:12])   # 分片：半包不解析不转发
    assert out1 == b"" and ev1 == []
    out2, ev2 = ctl.feed_stream(ctx, raw[12:])
    assert out2 == b"" and len(ev2) == 1         # 完整后整体解析上抛
    final = ctl.finish(ctx)
    data = json.loads(final)
    assert data["choices"][0]["logprobs"] is None
    assert "token_id:" not in final.decode("utf-8")
    await adrain(ctl)
    assert len(runners[0].calls) == 1
    lp_list, ti_list = runners[0].calls[0]
    assert ti_list[0][:, 0].tolist() == [0, 1]
    # 文本对齐（finish 后取）
    assert ctx.processor.get_texts() == ["hi"]


async def test_buffered_recompute_rewrite(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "prompt": "p", "stream": False}
    ctx = ctl.before_forward(req, False)
    a1 = {
        "id": "1",
        "choices": [{"index": 0, "text": "AB", "logprobs": None, "stop_reason": "recomputed"}],
        "usage": {"completion_tokens": 2},
    }
    out, ev = ctl.feed_stream(ctx, json.dumps(a1).encode("utf-8"))
    assert out == b"" and len(ev) == 1           # attempt1 全程不转发
    ctl.on_recompute(ctx)
    a2 = {
        "id": "1",
        "choices": [{"index": 0, "text": "CD", "logprobs": None}],
        "usage": {"completion_tokens": 2},
    }
    out2, ev2 = ctl.feed_stream(ctx, json.dumps(a2).encode("utf-8"))
    assert out2 == b""
    final = ctl.finish(ctx, rewrite_content=True)
    data = json.loads(final)
    assert data["choices"][0]["text"] == "ABCD"  # 跨 attempt 重写（与原 proxy 语义一致）
    await adrain(ctl)
    assert runners[0].calls == []                # attempt2 无 logprobs → 空数据不检测


async def test_buffered_non_json_passthrough(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "prompt": "p", "stream": False}
    ctx = ctl.before_forward(req, False)
    raw = b"<html>bad gateway</html>"
    ctl.feed_stream(ctx, raw)
    final = ctl.finish(ctx)
    assert final == raw                          # 非 JSON 原样透传，不检测
    assert runners == [] or runners[0].calls == []


# --------------------------------------------------------------------------- #
# 指标与关停
# --------------------------------------------------------------------------- #
async def test_metrics_recorded_after_detection(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    req = {"model": "m", "prompt": "p", "stream": False}
    ctx = ctl.before_forward(req, False)
    body = {
        "id": "c",
        "choices": [
            {
                "index": 0,
                "text": "x",
                "logprobs": {
                    "tokens": ["token_id:0"],
                    "token_logprobs": [-0.1],
                    "top_logprobs": [{"token_id:0": -0.1}],
                },
            }
        ],
    }
    ctl.feed_stream(ctx, json.dumps(body).encode("utf-8"))
    ctl.finish(ctx)
    await adrain(ctl)
    text = ctl.render_metrics().decode("utf-8")
    assert "vllm_anomaly_requests_total" in text
    assert 'model="m"' in text


def test_shutdown_cancels_pending(monkeypatch, tmp_path):
    ctl, runners = make_controller(monkeypatch, tmp_path)
    ctl.shutdown()
    assert runners[0].closed is True


# --------------------------------------------------------------------------- #
# ASGI 中间件形态（集成模式 B：一键包裹 / 独立启动器）
# --------------------------------------------------------------------------- #
def _write_vocab_files(tmp_path, tk2cat=None, token_text=None):
    tk2cat = tk2cat if tk2cat is not None else {"0": "english_latin", "1": "chinese_cjk"}
    token_text = token_text if token_text is not None else {"0": "a", "1": "你"}
    d = tmp_path / "token2category"
    t = tmp_path / "token_text"
    d.mkdir(exist_ok=True)
    t.mkdir(exist_ok=True)
    f1 = d / "m_2.json"
    f2 = t / "m_2.json"
    f1.write_text(json.dumps(tk2cat), encoding="utf-8")
    f2.write_text(json.dumps(token_text), encoding="utf-8")
    return str(f1), str(f2)


class FakeDownstream:
    """最小 ASGI 下游：记录收到的请求体，返回预置响应。"""

    def __init__(self, start, body_chunks):
        self.start = start
        self.body_chunks = body_chunks
        self.received_raw = None

    async def __call__(self, scope, receive, send):
        raw = b""
        while True:
            msg = await receive()
            raw += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        self.received_raw = raw
        await send(dict(self.start))
        for i, chunk in enumerate(self.body_chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": i < len(self.body_chunks) - 1,
                }
            )


def _make_scope(path, body: bytes, method: str = "POST"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }


def _make_receive(body: bytes):
    state = {"n": 0}

    async def receive():
        state["n"] += 1
        if state["n"] == 1:
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


def _make_send(buf):
    async def send(msg):
        buf.append(msg)

    return send


async def _collect(send_buf):
    start = next(m for m in send_buf if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in send_buf if m["type"] == "http.response.body")
    return start, body


async def test_build_proxy_middleware_passthrough(monkeypatch):
    monkeypatch.delenv("VLLM_ANOMALY_TOKEN2CATEGORY", raising=False)
    monkeypatch.delenv("VLLM_ANOMALY_ENABLED", raising=False)
    app = object()
    assert pi.build_proxy_middleware(app) is app  # 未配置 → 原样返回


async def test_proxy_middleware_requires_env(monkeypatch, tmp_path):
    monkeypatch.delenv("VLLM_ANOMALY_TOKEN2CATEGORY", raising=False)
    monkeypatch.setattr(pi, "DetectorRunner", StubRunner)
    monkeypatch.setattr(pi, "ILLDetector", lambda *a, **k: object())
    with pytest.raises(ValueError):
        pi.ProxyAnomalyMiddleware(object())


async def test_proxy_middleware_e2e_stream(monkeypatch, tmp_path):
    f1, _ = _write_vocab_files(tmp_path)  # 文本表在兄弟目录，验证自动定位
    monkeypatch.setenv("VLLM_ANOMALY_TOKEN2CATEGORY", f1)
    runners = []
    monkeypatch.setattr(
        pi, "DetectorRunner", lambda *a, **k: runners.append(StubRunner()) or runners[-1]
    )
    monkeypatch.setattr(pi, "ILLDetector", lambda *a, **k: object())

    e1 = chat_stream_chunk("m", chat_top_entry(0, "a", -0.1), delta_text="a")
    e2 = chat_stream_chunk("m", chat_top_entry(1, "你", -0.2), delta_text="你")
    downstream = FakeDownstream(
        {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]},
        [sse(e1), sse(e2) + b"data: [DONE]\n\n"],
    )
    mw = pi.ProxyAnomalyMiddleware(downstream)

    req = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    body = json.dumps(req).encode("utf-8")
    send_buf = []
    await mw(
        _make_scope("/v1/chat/completions", body),
        _make_receive(body),
        _make_send(send_buf),
    )
    # 下游收到注入后的请求体
    injected = json.loads(downstream.received_raw)
    assert injected["logprobs"] is True
    assert injected["top_logprobs"] == 20
    assert injected["return_tokens_as_token_ids"] is True
    # 客户端响应：关联头 + strip 还原 + [DONE] 透传
    start, resp_body = await _collect(send_buf)
    headers = dict((k.lower(), v) for k, v in start["headers"])
    assert b"x-anomaly-request-id" in headers
    assert b"token_id:" not in resp_body
    assert b"[DONE]" in resp_body
    assert b'"logprobs":null' in resp_body or b'"logprobs": null' in resp_body
    # 检测已调度
    await adrain(mw)
    assert runners and len(runners[0].calls) == 1
    _, ti_list = runners[0].calls[0]
    assert ti_list[0].shape[0] == 2


async def test_proxy_middleware_passthrough_non_target(monkeypatch, tmp_path):
    f1, _ = _write_vocab_files(tmp_path)
    monkeypatch.setenv("VLLM_ANOMALY_TOKEN2CATEGORY", f1)
    monkeypatch.setattr(pi, "DetectorRunner", StubRunner)
    monkeypatch.setattr(pi, "ILLDetector", lambda *a, **k: object())
    downstream = FakeDownstream(
        {"type": "http.response.start", "status": 200, "headers": []},
        [b'{"status": "ok"}'],
    )
    mw = pi.ProxyAnomalyMiddleware(downstream)
    body = b'{"request_id": "abc"}'
    send_buf = []
    await mw(_make_scope("/v1/metaserver", body), _make_receive(body), _make_send(send_buf))
    assert downstream.received_raw == body  # 非目标路径：请求体逐字节透传
    start, resp = await _collect(send_buf)
    assert resp == b'{"status": "ok"}'


async def test_proxy_middleware_metrics_endpoint(monkeypatch, tmp_path):
    f1, _ = _write_vocab_files(tmp_path)
    monkeypatch.setenv("VLLM_ANOMALY_TOKEN2CATEGORY", f1)
    monkeypatch.setattr(pi, "DetectorRunner", StubRunner)
    monkeypatch.setattr(pi, "ILLDetector", lambda *a, **k: object())
    called = {"n": 0}

    async def downstream(scope, receive, send):
        called["n"] += 1  # 端点由中间件内联应答，不应触达下游
        raise AssertionError("should not reach downstream")

    mw = pi.ProxyAnomalyMiddleware(downstream)
    send_buf = []
    await mw(
        _make_scope("/anomaly/metrics", b"", method="GET"),
        _make_receive(b""),
        _make_send(send_buf),
    )
    start, resp = await _collect(send_buf)
    assert start["status"] == 200
    assert b"vllm_anomaly_monitor_rate" in resp
    assert called["n"] == 0


async def test_proxy_middleware_disabled_passthrough(monkeypatch, tmp_path):
    f1, _ = _write_vocab_files(tmp_path)
    monkeypatch.setenv("VLLM_ANOMALY_TOKEN2CATEGORY", f1)
    monkeypatch.setenv("VLLM_ANOMALY_ENABLED", "0")
    monkeypatch.setattr(pi, "DetectorRunner", StubRunner)
    monkeypatch.setattr(pi, "ILLDetector", lambda *a, **k: object())
    downstream = FakeDownstream(
        {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]},
        [sse(chat_stream_chunk("m", chat_top_entry(0, "a", -0.1), delta_text="a")) + b"data: [DONE]\n\n"],
    )
    mw = pi.ProxyAnomalyMiddleware(downstream)
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")
    send_buf = []
    await mw(_make_scope("/v1/chat/completions", body), _make_receive(body), _make_send(send_buf))
    # enabled=False：不注入（下游收到原 body）、响应逐字节透传；端点仍可达
    assert json.loads(downstream.received_raw).get("logprobs") is None
    _, resp = await _collect(send_buf)
    assert b"token_id:" in resp  # 原样透传（含 token_id: 形态）
    send_buf2 = []
    await mw(_make_scope("/anomaly/metrics", b"", method="GET"), _make_receive(b""), _make_send(send_buf2))
    start, resp2 = await _collect(send_buf2)
    assert start["status"] == 200 and b"vllm_anomaly_monitor_rate" in resp2
