"""异常本地保存单元测试：AnomalyStore + env save_path + metrics record_anomaly
+ detector_runner _run 保存逻辑（fake runner，不依赖 pyyaml/transformers）。

design: docs/superpowers/specs/2026-08-24-anomaly-local-save-design.md
"""
from __future__ import annotations

import asyncio
import json
import os
import pickle

import numpy as np
import pytest

from anomaly_middleware.anomaly_store import AnomalyStore, sanitize_model_name
from anomaly_middleware.env import PluginConfig
from anomaly_middleware.metrics import Metrics
from anomaly_middleware.detector_runner import schedule_detection
from anomaly_middleware.extractor import SSEStreamProcessor, OriginalParams


# --------------------------- sanitize_model_name --------------------------- #
def test_sanitize_repo_id():
    assert sanitize_model_name("Qwen/Qwen3-0.6B") == "Qwen3-0.6B"


def test_sanitize_path():
    assert sanitize_model_name("/data/models/Qwen3-0.6B") == "Qwen3-0.6B"


def test_sanitize_illegal_chars():
    assert sanitize_model_name("mymodel:v2?") == "mymodel_v2"


def test_sanitize_empty():
    assert sanitize_model_name(None) == "anomalies"
    assert sanitize_model_name("") == "anomalies"


# --------------------------- AnomalyStore: 关闭模式 --------------------------- #
async def test_store_disabled_counter_only(tmp_path):
    store = AnomalyStore(save_path=None, model_name="x")
    assert store.enabled is False
    assert store.file_path is None
    aid1 = await store.save({"ill_type": 1})
    aid2 = await store.save({"ill_type": 2})
    assert aid1 == 1
    assert aid2 == 2
    assert store.counter == 2
    assert not os.path.exists(str(tmp_path / "x.pkl"))  # 无文件


# --------------------------- AnomalyStore: 文件模式 --------------------------- #
async def test_store_file_mode_creates_on_first_save(tmp_path):
    fp = tmp_path / "out.pkl"
    store = AnomalyStore(save_path=str(fp), model_name="m")
    assert store.enabled is True
    assert store.file_path == str(fp)
    aid = await store.save({"ill_type": 3, "text": "hi"})
    assert aid == 1
    assert os.path.isfile(str(fp))
    with open(fp, "rb") as f:
        data = pickle.load(f)
    assert 1 in data
    assert data[1]["ill_type"] == 3
    assert data[1]["text"] == "hi"


async def test_store_file_mode_increments(tmp_path):
    fp = tmp_path / "out.pkl"
    store = AnomalyStore(save_path=str(fp))
    a = await store.save({"i": 1})
    b = await store.save({"i": 2})
    c = await store.save({"i": 3})
    assert (a, b, c) == (1, 2, 3)
    with open(fp, "rb") as f:
        data = pickle.load(f)
    assert set(data.keys()) == {1, 2, 3}


# --------------------------- 编号同步（防重启覆盖）--------------------------- #
async def test_store_syncs_from_existing_file_max_key(tmp_path):
    fp = tmp_path / "out.pkl"
    # 预置文件：已有编号 5、8 的记录
    with open(fp, "wb") as f:
        pickle.dump({5: {"x": 1}, 8: {"x": 2}}, f)
    store = AnomalyStore(save_path=str(fp))
    aid = await store.save({"new": True})
    assert aid == 9  # max(0, 8) + 1，不覆盖
    with open(fp, "rb") as f:
        data = pickle.load(f)
    assert 5 in data and 8 in data and 9 in data
    assert data[9]["new"] is True


async def test_store_subsequent_uses_counter_only(tmp_path):
    fp = tmp_path / "out.pkl"
    with open(fp, "wb") as f:
        pickle.dump({10: {"x": 1}}, f)
    store = AnomalyStore(save_path=str(fp))
    a = await store.save({"i": 1})
    b = await store.save({"i": 2})
    assert a == 11
    assert b == 12


# --------------------------- 文件夹模式 --------------------------- #
async def test_store_folder_mode_uses_model_name(tmp_path):
    d = tmp_path / "savedir"
    d.mkdir()
    store = AnomalyStore(save_path=str(d), model_name="Qwen/Qwen3-0.6B")
    assert store.file_path == str(d / "Qwen3-0.6B.pkl")
    await store.save({"i": 1})
    assert os.path.isfile(str(d / "Qwen3-0.6B.pkl"))


# --------------------------- 路径存在性校验（fail-fast）--------------------------- #
def test_store_file_mode_missing_parent_raises(tmp_path):
    fp = tmp_path / "no_such_dir" / "out.pkl"
    with pytest.raises(FileNotFoundError, match="保存的路径不存在"):
        AnomalyStore(save_path=str(fp))


def test_store_folder_mode_missing_dir_raises(tmp_path):
    d = tmp_path / "no_such_dir"
    with pytest.raises(FileNotFoundError, match="保存的路径不存在"):
        AnomalyStore(save_path=str(d), model_name="m")


# --------------------------- env: save_path --------------------------- #
def test_env_save_path_default_none(monkeypatch):
    for k in list(os.environ):
        if k.startswith("VLLM_ANOMALY"):
            monkeypatch.delenv(k, raising=False)
    assert PluginConfig.from_env().save_path is None


def test_env_save_path_set(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("VLLM_ANOMALY"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VLLM_ANOMALY_SAVE_PATH", str(tmp_path / "a.pkl"))
    assert PluginConfig.from_env().save_path == str(tmp_path / "a.pkl")


def test_env_save_path_must_be_absolute(monkeypatch):
    for k in list(os.environ):
        if k.startswith("VLLM_ANOMALY"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VLLM_ANOMALY_SAVE_PATH", "relative/path.pkl")
    with pytest.raises(ValueError, match="绝对路径"):
        PluginConfig.from_env()


# --------------------------- metrics: record_anomaly --------------------------- #
def test_metrics_record_anomaly_sets_gauges():
    m = Metrics()
    m.record_anomaly(7, 1234567.5, "mymodel")
    out = m.render_metrics().decode("utf-8")
    assert "vllm_anomaly_last_id" in out
    assert "vllm_anomaly_last_timestamp_seconds" in out
    assert "mymodel" in out
    # 值可解析
    assert "7" in out


def test_metrics_record_anomaly_exception_swallowed():
    m = Metrics()
    # 传入非法值不应抛
    m.record_anomaly(None, "bad", "m")  # type: ignore[arg-type]
    m.record_anomaly(1, 1.0, "m")


# --------------------------- detector_runner._run 保存逻辑 --------------------------- #
class _FakeRunner:
    """假检测 runner：返回固定 results，不依赖 pyyaml/transformers。"""

    def __init__(self, results):
        self._results = results

    async def run_async(self, logprobs_list, token_ids_list):
        return self._results

    def _rebuild_pool(self):
        pass


async def test_schedule_detection_saves_anomalous_choices_only(tmp_path):
    fp = tmp_path / "anom.pkl"
    store = AnomalyStore(save_path=str(fp))
    metrics = Metrics()
    runner = _FakeRunner([[True, 1], [False, 0], [True, 3]])
    lp = [np.array([[0.1, 0.2]], dtype=np.float32) for _ in range(3)]
    tid = [np.array([[10, 20]], dtype=np.int32) for _ in range(3)]
    texts = ["hello", "normal", "repeat"]
    pending = set()

    task = schedule_detection(
        runner, lp, tid,
        request_id="req-1", model="mymodel", metrics=metrics,
        pending_tasks=pending,
        anomaly_store=store, prompt="user msg", texts=texts,
    )
    await task

    with open(fp, "rb") as f:
        data = pickle.load(f)
    # 仅异常候选（choice 0 与 2）被保存
    assert set(data.keys()) == {1, 2}
    assert data[1]["ill_type"] == 1
    assert data[1]["text"] == "hello"
    assert data[1]["prompt"] == "user msg"
    assert data[1]["model_name"] == "mymodel"
    assert data[1]["topk_logprobs"][0] == pytest.approx([0.1, 0.2])
    assert data[1]["tokens_ids"] == [[10, 20]]
    assert data[2]["ill_type"] == 3
    assert data[2]["text"] == "repeat"
    assert "time" in data[1] and isinstance(data[1]["time"], float)
    # metrics gauge：最新异常编号
    out = metrics.render_metrics().decode("utf-8")
    assert "vllm_anomaly_last_id" in out
    assert "vllm_anomaly_last_timestamp_seconds" in out


async def test_schedule_detection_no_store_still_works():
    # anomaly_store=None 时保存逻辑跳过，不报错
    metrics = Metrics()
    runner = _FakeRunner([[True, 1]])
    lp = [np.array([[0.1]], dtype=np.float32)]
    tid = [np.array([[1]], dtype=np.int32)]
    pending = set()
    task = schedule_detection(
        runner, lp, tid,
        request_id="req-2", model="m", metrics=metrics,
        pending_tasks=pending,
        anomaly_store=None, prompt=None, texts=None,
    )
    await task  # 不应抛


async def test_schedule_detection_normal_result_not_saved(tmp_path):
    fp = tmp_path / "anom.pkl"
    store = AnomalyStore(save_path=str(fp))
    metrics = Metrics()
    runner = _FakeRunner([[False, 0]])  # 正常
    lp = [np.array([[0.1]], dtype=np.float32)]
    tid = [np.array([[1]], dtype=np.int32)]
    task = schedule_detection(
        runner, lp, tid,
        request_id="req-3", model="m", metrics=metrics,
        pending_tasks=set(),
        anomaly_store=store, prompt="p", texts=["t"],
    )
    await task
    assert not os.path.exists(str(fp))  # 无异常 → 无保存（文件未创建）
    assert store.counter == 0


async def test_schedule_detection_saves_text_tokenid(tmp_path):
    fp = tmp_path / "anom.pkl"
    store = AnomalyStore(save_path=str(fp))
    metrics = Metrics()
    runner = _FakeRunner([[True, 1]])
    lp = [np.array([[0.1, 0.2]], dtype=np.float32)]
    tid = [np.array([[10, 20]], dtype=np.int32)]
    task = schedule_detection(
        runner, lp, tid,
        request_id="req-1", model="m", metrics=metrics,
        pending_tasks=set(),
        anomaly_store=store, prompt="msg", texts=["hello"],
        text_tokenids=[[100, 200]],
        reasoning_contents=["thinking..."],
    )
    await task
    with open(fp, "rb") as f:
        data = pickle.load(f)
    assert data[1]["text_tokenid"] == [100, 200]
    assert data[1]["reasoning_content"] == "thinking..."


async def test_schedule_detection_text_tokenid_defaults_empty(tmp_path):
    fp = tmp_path / "anom.pkl"
    store = AnomalyStore(save_path=str(fp))
    metrics = Metrics()
    runner = _FakeRunner([[True, 1]])
    lp = [np.array([[0.1]], dtype=np.float32)]
    tid = [np.array([[1]], dtype=np.int32)]
    task = schedule_detection(
        runner, lp, tid,
        request_id="req-2", model="m", metrics=metrics,
        pending_tasks=set(),
        anomaly_store=store, prompt="p", texts=["t"],
    )
    await task
    with open(fp, "rb") as f:
        data = pickle.load(f)
    assert data[1]["text_tokenid"] == []
    assert data[1]["reasoning_content"] is None


# --------------------------- SSE 文本累积（供异常保存）--------------------------- #
def _entry(token_id, logprob=-0.1, n=3):
    tps = [
        {"token": f"token_id:{10000+i}", "logprob": logprob - i * 0.1, "bytes": None}
        for i in range(n)
    ]
    return {
        "token": f"token_id:{token_id}", "logprob": logprob,
        "bytes": None, "top_logprobs": tps,
    }


def _sse_bytes(chunk):
    return b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"


def _chat_chunk(delta_text, entry, index=0):
    return {
        "id": "x", "model": "m",
        "choices": [{
            "index": index,
            "delta": {"content": delta_text},
            "logprobs": {"content": [dict(entry)]},
            "finish_reason": None,
        }],
    }


def _comp_chunk(text, token_id, logprob=-0.1, n=3, index=0):
    d = {f"token_id:{10000+i}": round(logprob - i * 0.1, 6) for i in range(n)}
    return {
        "id": "x", "model": "m",
        "choices": [{
            "index": index, "text": text,
            "logprobs": {
                "tokens": [f"token_id:{token_id}"],
                "token_logprobs": [logprob],
                "top_logprobs": [d],
            },
            "finish_reason": None,
        }],
    }


def _orig(is_chat, stream=True, rtati=False):
    return OriginalParams(
        is_chat=is_chat, logprobs=True, top_logprobs=3,
        return_tokens_as_token_ids=rtati, n=1, stream=stream,
    )


def test_sse_chat_text_accumulation_aligned_with_detection():
    sse = SSEStreamProcessor(True, _orig(True), 3, resolver=None)
    e = _entry(100)
    sse.feed(_sse_bytes(_chat_chunk("a", e)))
    sse.feed(_sse_bytes(_chat_chunk("b", e)))
    sse.feed(b"data: [DONE]\n\n")
    sse.flush()
    texts = sse.get_choice_texts()
    assert texts == ["ab"]
    lp, tid = sse.get_detection_data()
    assert len(lp) == 1 and len(tid) == 1  # 单 choice
    assert len(lp[0]) == 2  # 2 个 token


def test_sse_completions_text_accumulation():
    sse = SSEStreamProcessor(False, _orig(False), 3, resolver=None)
    sse.feed(_sse_bytes(_comp_chunk("foo", 100)))
    sse.feed(_sse_bytes(_comp_chunk("bar", 101)))
    sse.feed(b"data: [DONE]\n\n")
    sse.flush()
    assert sse.get_choice_texts() == ["foobar"]


def test_sse_multi_choice_text_alignment():
    sse = SSEStreamProcessor(True, _orig(True), 3, resolver=None)
    e = _entry(100)
    # choice 0 与 choice 1 交替到达
    sse.feed(_sse_bytes(_chat_chunk("a", e, index=0)))
    sse.feed(_sse_bytes(_chat_chunk("x", e, index=1)))
    sse.feed(_sse_bytes(_chat_chunk("b", e, index=0)))
    sse.feed(_sse_bytes(_chat_chunk("y", e, index=1)))
    sse.flush()
    texts = sse.get_choice_texts()
    lp, tid = sse.get_detection_data()
    assert texts == ["ab", "xy"]
    assert len(lp) == 2 and len(tid) == 2


def test_sse_text_captured_across_logprobs_gap():
    # 首块仅有 delta 文本无 logprobs（异常形态），后续块带 logprobs：
    # 文本仍需完整累积；choice 因后续块进入检测数据，get_choice_texts 对齐返回完整文本
    sse = SSEStreamProcessor(True, _orig(True), 3, resolver=None)
    e = _entry(100)
    sse.feed(_sse_bytes({
        "id": "x", "model": "m",
        "choices": [{
            "index": 0, "delta": {"content": "a"},
            "logprobs": None, "finish_reason": None,
        }],
    }))
    sse.feed(_sse_bytes(_chat_chunk("b", e)))
    sse.flush()
    assert sse.get_choice_texts() == ["ab"]
    lp, tid = sse.get_detection_data()
    assert len(lp) == 1 and len(tid) == 1


# --------------------------- SSE text_tokenids + reasoning --------------------------- #
def test_sse_chat_text_tokenids_aligned():
    sse = SSEStreamProcessor(True, _orig(True), 3, resolver=None)
    e = _entry(100)
    sse.feed(_sse_bytes(_chat_chunk("a", e)))
    sse.feed(_sse_bytes(_chat_chunk("b", e)))
    sse.feed(b"data: [DONE]\n\n")
    sse.flush()
    tokenids = sse.get_choice_text_tokenids()
    assert tokenids == [[100, 100]]
    lp, tid = sse.get_detection_data()
    assert len(tid[0]) == 2  # aligned


def test_sse_chat_reasoning_content_accumulation():
    sse = SSEStreamProcessor(True, _orig(True), 3, resolver=None)
    e = _entry(100)
    # thinking chunk: delta.reasoning_content, no delta.content
    sse.feed(_sse_bytes({
        "id": "x", "model": "m",
        "choices": [{
            "index": 0,
            "delta": {"reasoning_content": "think"},
            "logprobs": {"content": [dict(e)]},
            "finish_reason": None,
        }],
    }))
    # output chunk: delta.content
    sse.feed(_sse_bytes(_chat_chunk("answer", e)))
    sse.feed(b"data: [DONE]\n\n")
    sse.flush()
    assert sse.get_choice_texts() == ["answer"]
    assert sse.get_choice_reasoning_contents() == ["think"]
    tokenids = sse.get_choice_text_tokenids()
    assert len(tokenids[0]) == 2  # thinking + output = 2 tokens


def test_sse_completions_text_tokenids():
    sse = SSEStreamProcessor(False, _orig(False), 3, resolver=None)
    sse.feed(_sse_bytes(_comp_chunk("foo", 100)))
    sse.feed(_sse_bytes(_comp_chunk("bar", 101)))
    sse.feed(b"data: [DONE]\n\n")
    sse.flush()
    assert sse.get_choice_text_tokenids() == [[100, 101]]
    assert sse.get_choice_reasoning_contents() == [None]


def test_sse_multi_choice_text_tokenids():
    sse = SSEStreamProcessor(True, _orig(True), 3, resolver=None)
    e = _entry(100)
    sse.feed(_sse_bytes(_chat_chunk("a", e, index=0)))
    sse.feed(_sse_bytes(_chat_chunk("x", e, index=1)))
    sse.feed(_sse_bytes(_chat_chunk("b", e, index=0)))
    sse.flush()
    tokenids = sse.get_choice_text_tokenids()
    assert tokenids == [[100, 100], [100]]


# --------------------------- ResponseInterceptor text_tokenid --------------------------- #
async def test_interceptor_nonstream_chat_text_tokenids():
    from anomaly_middleware.middleware import ResponseInterceptor, RequestContext

    sent = []
    async def send(msg):
        sent.append(msg)

    ctx = RequestContext(
        orig=OriginalParams(is_chat=True, logprobs=True, top_logprobs=3,
                           return_tokens_as_token_ids=False, n=1, stream=False),
        is_chat=True, model="m", request_id="r", will_detect=True, top_logprobs=3,
    )
    interceptor = ResponseInterceptor(
        send, ctx=ctx, runner=None, metrics=Metrics(),
        pending_tasks=set(), resolver=None, anomaly_store=None,
    )
    from _helpers import chat_top_entry, build_chat_response
    NI = "你"
    e1 = chat_top_entry(100, NI, -0.1, n_top=5)
    e2 = chat_top_entry(200, "好", -0.2, n_top=5)
    data = build_chat_response("m", [e1, e2], reasoning_content="thinking...")
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    await interceptor({"type": "http.response.start", "status": 200, "headers": []})
    await interceptor({"type": "http.response.body", "body": body, "more_body": False})

    assert interceptor.get_choice_text_tokenids() == [[100, 200]]
    assert interceptor.get_choice_reasoning_contents() == ["thinking..."]


async def test_interceptor_nonstream_completions_text_tokenids():
    from anomaly_middleware.middleware import ResponseInterceptor, RequestContext
    from _helpers import build_completions_response

    sent = []
    async def send(msg):
        sent.append(msg)

    ctx = RequestContext(
        orig=OriginalParams(is_chat=False, logprobs=3, top_logprobs=None,
                           return_tokens_as_token_ids=False, n=1, stream=False),
        is_chat=False, model="m", request_id="r", will_detect=True, top_logprobs=3,
    )
    interceptor = ResponseInterceptor(
        send, ctx=ctx, runner=None, metrics=Metrics(),
        pending_tasks=set(), resolver=None, anomaly_store=None,
    )
    data = build_completions_response("m", [100, 200], [-0.1, -0.2], n_top=5)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    await interceptor({"type": "http.response.start", "status": 200, "headers": []})
    await interceptor({"type": "http.response.body", "body": body, "more_body": False})

    assert interceptor.get_choice_text_tokenids() == [[100, 200]]
    assert interceptor.get_choice_reasoning_contents() == [None]


async def test_interceptor_streaming_chat_text_tokenids_and_reasoning():
    from anomaly_middleware.middleware import ResponseInterceptor, RequestContext

    sent = []
    async def send(msg):
        sent.append(msg)

    ctx = RequestContext(
        orig=OriginalParams(is_chat=True, logprobs=None, top_logprobs=None,
                           return_tokens_as_token_ids=False, n=1, stream=True),
        is_chat=True, model="m", request_id="r", will_detect=True, top_logprobs=3,
    )
    interceptor = ResponseInterceptor(
        send, ctx=ctx, runner=None, metrics=Metrics(),
        pending_tasks=set(), resolver=None, anomaly_store=None,
    )
    e = _entry(100)
    # thinking chunk
    chunk1 = {
        "id": "x", "model": "m",
        "choices": [{
            "index": 0,
            "delta": {"reasoning_content": "think"},
            "logprobs": {"content": [dict(e)]},
            "finish_reason": None,
        }],
    }
    # output chunk
    chunk2 = _chat_chunk("answer", e)
    sse_data = b"data: " + json.dumps(chunk1).encode() + b"\n\n"
    sse_data += b"data: " + json.dumps(chunk2).encode() + b"\n\n"
    sse_data += b"data: [DONE]\n\n"

    await interceptor({"type": "http.response.start", "status": 200,
                       "headers": [[b"content-type", b"text/event-stream"]]})
    await interceptor({"type": "http.response.body", "body": sse_data, "more_body": False})

    assert interceptor.get_choice_text_tokenids() == [[100, 100]]
    assert interceptor.get_choice_reasoning_contents() == ["think"]
    assert interceptor.get_choice_texts() == ["answer"]
