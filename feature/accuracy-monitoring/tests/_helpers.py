"""测试辅助：构造 vLLM 风格响应 + 模拟下游 ASGI app。"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


def text_bytes(text: str) -> Optional[List[int]]:
    if text is None:
        return None
    return list(text.encode("utf-8"))


def chat_top_entry(
    token_id: int,
    text: str,
    logprob: float,
    n_top: int = 20,
    nan_at: Optional[int] = None,
    vllm_broken_top_bytes: bool = False,
) -> Dict[str, Any]:
    """构造 chat logprobs.content[] 的一个 entry（含 token_id: 前缀）。

    vllm_broken_top_bytes：复现真实 vLLM 在 return_tokens_as_token_ids=true 下的
    破损形态——top_logprobs 的 bytes 是 "token_id:NNN" 字符串本身的字节（非 token 真实字节）。
    """
    b = text_bytes(text)
    tps: List[Dict[str, Any]] = []
    for i in range(n_top):
        cid = 10000 + i
        lp = logprob - i * 0.1
        if nan_at is not None and i == nan_at:
            lp = float("nan")
        top_b = (
            list(f"token_id:{cid}".encode("utf-8"))
            if vllm_broken_top_bytes
            else b
        )
        tps.append(
            {
                "token": f"token_id:{cid}",
                "logprob": lp,
                "bytes": top_b,
            }
        )
    return {
        "token": f"token_id:{token_id}",
        "logprob": logprob,
        "bytes": b,
        "top_logprobs": tps,
    }


def build_chat_response(
    model: str,
    entries: List[Dict[str, Any]],
    n: int = 1,
    reasoning_content: Optional[str] = None,
) -> Dict[str, Any]:
    """构造 chat 非流式响应（n 个 choice，每个相同 entries）。

    reasoning_content：若提供，加入 message.reasoning_content（thinking model）。
    """
    msg: Dict[str, Any] = {"role": "assistant", "content": "x"}
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": i,
                "message": dict(msg),
                "logprobs": {"content": [dict(e) for e in entries]},
                "finish_reason": "stop",
            }
            for i in range(n)
        ],
    }


def build_completions_response(
    model: str,
    token_ids: List[int],
    logprobs_vals: List[float],
    n_top: int = 20,
    n: int = 1,
) -> Dict[str, Any]:
    """构造 completions 非流式响应。"""
    top_logprobs: List[Dict[str, float]] = []
    for i in range(len(token_ids)):
        d: Dict[str, float] = {}
        for j in range(n_top):
            d[f"token_id:{10000 + j}"] = round(logprobs_vals[i] - j * 0.1, 6)
        top_logprobs.append(d)
    lp = {
        "tokens": [f"token_id:{t}" for t in token_ids],
        "token_logprobs": list(logprobs_vals),
        "top_logprobs": top_logprobs,
        "text_offset": [0] + [3 * (i + 1) for i in range(len(token_ids) - 1)],
    }
    return {
        "id": "comp-test",
        "object": "text_completion",
        "model": model,
        "choices": [
            {"index": i, "text": "x", "logprobs": {k: list(v) if isinstance(v, list) else v for k, v in lp.items()}, "finish_reason": "stop"}
            for i in range(n)
        ],
    }


def chat_stream_chunk(
    model: str,
    entry: Dict[str, Any],
    delta_text: str = "x",
    index: int = 0,
    finish: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-stream",
        "model": model,
        "choices": [
            {
                "index": index,
                "delta": {"content": delta_text},
                "logprobs": {"content": [dict(entry)]},
                "finish_reason": finish,
            }
        ],
    }


def completions_stream_chunk(
    model: str,
    token_id: int,
    logprob: float,
    n_top: int = 20,
    index: int = 0,
    finish: Optional[str] = None,
) -> Dict[str, Any]:
    d: Dict[str, float] = {}
    for j in range(n_top):
        d[f"token_id:{10000 + j}"] = round(logprob - j * 0.1, 6)
    return {
        "id": "comp-stream",
        "model": model,
        "choices": [
            {
                "index": index,
                "text": "x",
                "logprobs": {
                    "tokens": [f"token_id:{token_id}"],
                    "token_logprobs": [logprob],
                    "top_logprobs": [d],
                },
                "finish_reason": finish,
            }
        ],
    }


class FakeTokenizer:
    """测试用伪 HF tokenizer：id -> text 字典。"""

    def __init__(self, mapping):
        self._m = mapping

    def decode(self, ids, **kwargs):
        return "".join(self._m.get(i, "") for i in ids)


def install_fake_resolver(mw, mapping):
    """给 mw 注入基于 FakeTokenizer(mapping) 的 TokenTextResolver，并标记已初始化。"""
    from anomaly_middleware.token_resolver import TokenTextResolver

    mw._resolver = TokenTextResolver(FakeTokenizer(mapping))
    mw._resolver_inited = True


class FakeVLLM:
    """模拟 vLLM 下游 ASGI app。

    response_fn(scope, body_dict) -> ("json", dict) | ("stream", chunks, split_index)
    chunks: list of dict（每个序列化为一条 SSE data 事件）；以 None 表示 data: [DONE]。
    split_index: 若提供，则将第 split_index 个事件拆到两个 body 块（测跨块重组）。
    """

    def __init__(self, response_fn: Callable):
        self.response_fn = response_fn
        self.received: List[Tuple[dict, bytes]] = []

    async def __call__(self, scope, receive, send) -> None:
        body = bytearray()
        while True:
            msg = await receive()
            t = msg.get("type")
            if t == "http.request":
                body.extend(msg.get("body", b"") or b"")
                if not msg.get("more_body", False):
                    break
            elif t == "http.disconnect":
                return
        self.received.append((scope, bytes(body)))
        try:
            parsed = json.loads(bytes(body)) if body else {}
        except Exception:
            parsed = {}
        result = self.response_fn(scope, parsed)
        kind = result[0]
        if kind == "json":
            payload = result[1]
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(data)).encode("latin-1")],
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": data, "more_body": False}
            )
        elif kind == "stream":
            chunks = result[1]
            split_index = result[2] if len(result) > 2 else None
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"text/event-stream"]],
                }
            )
            parts: List[bytes] = []
            for idx, chunk in enumerate(chunks):
                if chunk is None:
                    raw = b"data: [DONE]\n\n"
                else:
                    raw = b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"
                if split_index is not None and idx == split_index:
                    half = len(raw) // 2
                    parts.append(raw[:half])
                    parts.append(raw[half:])
                else:
                    parts.append(raw)
            for p in parts:
                await send(
                    {"type": "http.response.body", "body": p, "more_body": True}
                )
            await send(
                {"type": "http.response.body", "body": b"", "more_body": False}
            )
        else:
            raise AssertionError(f"unknown kind {kind}")
