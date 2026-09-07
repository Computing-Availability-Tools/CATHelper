"""AnomalyMiddleware + RequestContext + ResponseInterceptor + ASGI 助手（design §4.1/§4.2, spec §3）。

纯 ASGI 中间件（非 Starlette BaseHTTPMiddleware），SSE 增量转发，不缓冲整流。
检测 fire-and-forget，客户端永远不等待检测。
"""
from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Set, Tuple

import numpy as np

from .env import PluginConfig, resolve_config_path
from .anomaly_store import AnomalyStore
from .detector_runner import DetectorRunner, schedule_detection
from .extractor import (
    OriginalParams,
    SSEStreamProcessor,
    extract_chat_response,
    extract_chat_text_tokenids,
    extract_completions_response,
    extract_completions_text_tokenids,
    inject_params,
    save_original_params,
    strip_chat_response,
    strip_completions_response,
)
from .logging import get_logger
from .metrics import METRICS_CONTENT_TYPE, Metrics

logger = get_logger()

TARGET_PATHS = ("/v1/chat/completions", "/v1/completions")


@dataclass
class RequestContext:
    orig: OriginalParams
    is_chat: bool
    model: str
    request_id: str
    will_detect: bool
    top_logprobs: int
    prompt: Any = None


# --------------------------------------------------------------------------- #
# ASGI 请求助手（§3.1 / §3.2）
# --------------------------------------------------------------------------- #
async def _read_all_body(receive: Callable[[], Awaitable[dict]]) -> bytes:
    """聚合所有 http.request body 至 more_body=False；遇 http.disconnect 中止。"""
    body = bytearray()
    while True:
        msg = await receive()
        t = msg.get("type")
        if t == "http.request":
            chunk = msg.get("body", b"") or b""
            if chunk:
                body.extend(chunk)
            if not msg.get("more_body", False):
                break
        elif t == "http.disconnect":
            break
        # 其它消息忽略
    return bytes(body)


def _make_replay_receive(
    original_receive: Callable[[], Awaitable[dict]], body: bytes
) -> Callable[[], Awaitable[dict]]:
    """首次返回合成单条 http.request(body, more_body=False)；后续委托原始 receive()。

    绝不返回空 body 的 http.request（vLLM 会重复处理请求）。
    """
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original_receive()

    return receive


def _patch_scope_content_length(scope: dict, length: int) -> dict:
    """浅拷贝 scope + 拷贝 headers，改写/补 content-length。"""
    new_scope = dict(scope)
    new_headers = [list(h) for h in scope.get("headers", [])]
    cl = str(length).encode("latin-1")
    found = False
    for h in new_headers:
        if h and len(h) >= 2 and h[0].lower() == b"content-length":
            h[1] = cl
            found = True
            break
    if not found:
        new_headers.append([b"content-length", cl])
    new_scope["headers"] = new_headers
    return new_scope


def _is_chat_path(path: str) -> bool:
    return path == "/v1/chat/completions"


# --------------------------------------------------------------------------- #
# ResponseInterceptor（§4.2）
# --------------------------------------------------------------------------- #
class ResponseInterceptor:
    def __init__(
        self,
        send: Callable[[dict], Awaitable[None]],
        *,
        ctx: RequestContext,
        runner: Optional[DetectorRunner],
        metrics: Metrics,
        pending_tasks: Set,
        resolver: Any = None,
        anomaly_store: Any = None,
    ) -> None:
        self._send = send
        self._ctx = ctx
        self._runner = runner
        self._metrics = metrics
        self._pending_tasks = pending_tasks
        self._resolver = resolver
        self._anomaly_store = anomaly_store
        self._is_streaming = False
        self._start_msg: Optional[dict] = None
        self._body_buf = bytearray()
        self._sse: Optional[SSEStreamProcessor] = None
        self._finished = False
        self._detection_scheduled = False
        self._detection_results: List[Tuple[np.ndarray, np.ndarray]] = []
        self._choice_texts: List[Any] = []
        self._choice_text_tokenids: List[List[int]] = []
        self._choice_reasoning_contents: List[Any] = []

    async def __call__(self, message: dict) -> None:
        t = message.get("type")
        if t == "http.response.start":
            await self._on_start(message)
        elif t == "http.response.body":
            await self._on_body(message)
        else:
            await self._send(message)

    async def _on_start(self, message: dict) -> None:
        headers = [list(h) for h in message.get("headers", [])]
        headers.append(
            [b"x-anomaly-request-id", self._ctx.request_id.encode("latin-1")]
        )
        msg = dict(message)
        msg["headers"] = headers
        ct = b""
        for h in headers:
            if h and len(h) >= 2 and h[0].lower() == b"content-type":
                ct = (h[1] or b"").lower()
                break
        self._is_streaming = b"text/event-stream" in ct
        if self._is_streaming:
            self._sse = SSEStreamProcessor(
                self._ctx.is_chat, self._ctx.orig, self._ctx.top_logprobs, self._resolver
            )
            await self._send(msg)  # 流式立即发 start
        else:
            self._start_msg = msg  # 非流式缓冲，发 body 前 patch CL 再发

    async def _on_body(self, message: dict) -> None:
        if self._finished:
            return  # 防重复终端 body
        body = message.get("body", b"") or b""
        more = message.get("more_body", False)
        if self._is_streaming:
            await self._on_body_streaming(body, more)
        else:
            await self._on_body_nonstreaming(body, more)

    async def _on_body_streaming(self, body: bytes, more: bool) -> None:
        out = self._sse.feed(body)
        if more:
            if out:
                await self._send(
                    {"type": "http.response.body", "body": out, "more_body": True}
                )
            return
        # 终端块
        tail = self._sse.flush()
        final = (out or b"") + tail
        await self._send(
            {"type": "http.response.body", "body": final, "more_body": False}
        )
        self._finished = True
        self._maybe_schedule_detection()

    async def _on_body_nonstreaming(self, body: bytes, more: bool) -> None:
        self._body_buf.extend(body)
        if more or self._finished:
            return
        final = self._process_complete()
        await self._send_start(final)
        await self._send(
            {"type": "http.response.body", "body": final, "more_body": False}
        )
        self._finished = True
        self._maybe_schedule_detection()

    def _process_complete(self) -> bytes:
        raw = bytes(self._body_buf)
        try:
            data = json.loads(raw)
        except Exception:
            return raw  # 非 JSON（错误页）→ 原样透传，不注入检测
        if not isinstance(data, dict):
            return raw
        choices = data.get("choices")
        if isinstance(choices, list):
            self._choice_texts = []
            for c in choices:
                if not isinstance(c, dict):
                    self._choice_texts.append(None)
                    continue
                if self._ctx.is_chat:
                    msg = c.get("message")
                    self._choice_texts.append(
                        msg.get("content") if isinstance(msg, dict) else None
                    )
                else:
                    self._choice_texts.append(c.get("text"))
        ch = choices if isinstance(choices, list) else []
        if self._ctx.is_chat:
            self._detection_results = extract_chat_response(
                data, self._ctx.top_logprobs
            )
            self._choice_text_tokenids = extract_chat_text_tokenids(data)
            self._choice_reasoning_contents = [
                (c.get("message") or {}).get("reasoning_content")
                if isinstance(c, dict) else None
                for c in ch
            ]
            strip_chat_response(
                data, self._ctx.orig, self._resolver,
            )
        else:
            self._detection_results = extract_completions_response(
                data, self._ctx.top_logprobs
            )
            self._choice_text_tokenids = extract_completions_text_tokenids(data)
            self._choice_reasoning_contents = [None] * len(ch)
            strip_completions_response(
                data, self._ctx.orig, self._resolver,
                recompute_text_offset=True,
            )
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def get_choice_texts(self) -> List[Any]:
        """per-choice 输出文本（非流式取 _choice_texts；流式取 SSE 累积）。"""
        if self._is_streaming:
            return self._sse.get_choice_texts() if self._sse is not None else []
        return list(self._choice_texts)

    def get_choice_text_tokenids(self) -> List[List[int]]:
        """per-choice 实际生成 token_id 序列（非流式取 _choice_text_tokenids；流式取 SSE 累积）。"""
        if self._is_streaming:
            return self._sse.get_choice_text_tokenids() if self._sse is not None else []
        return list(self._choice_text_tokenids)

    def get_choice_reasoning_contents(self) -> List[Any]:
        """per-choice 思维链文本（非流式取 _choice_reasoning_contents；流式取 SSE 累积）。"""
        if self._is_streaming:
            return self._sse.get_choice_reasoning_contents() if self._sse is not None else []
        return list(self._choice_reasoning_contents)

    async def _send_start(self, final: bytes) -> None:
        msg = self._start_msg
        if msg is None:
            msg = {"type": "http.response.start", "status": 200, "headers": []}
        headers = [list(h) for h in msg.get("headers", [])]
        cl = str(len(final)).encode("latin-1")
        found = False
        for h in headers:
            if h and len(h) >= 2 and h[0].lower() == b"content-length":
                h[1] = cl
                found = True
                break
        if not found:
            headers.append([b"content-length", cl])
        out = dict(msg)
        out["headers"] = headers
        await self._send(out)

    def _maybe_schedule_detection(self) -> None:
        if (
            not self._ctx.will_detect
            or self._detection_scheduled
            or self._runner is None
        ):
            return
        self._detection_scheduled = True
        try:
            logprobs_list, token_ids_list = self._get_detection_inputs()
        except Exception as exc:
            logger.error("获取检测数据失败: %s", exc)
            return
        if not token_ids_list or not any(len(tid) > 0 for tid in token_ids_list):
            return  # 空响应不检测
        try:
            schedule_detection(
                self._runner,
                logprobs_list,
                token_ids_list,
                request_id=self._ctx.request_id,
                model=self._ctx.model,
                metrics=self._metrics,
                pending_tasks=self._pending_tasks,
                anomaly_store=self._anomaly_store,
                prompt=self._ctx.prompt,
                texts=self.get_choice_texts(),
                text_tokenids=self.get_choice_text_tokenids(),
                reasoning_contents=self.get_choice_reasoning_contents(),
            )
        except RuntimeError as exc:
            # 无运行事件循环等：记录后跳过（不影响客户端）
            logger.warning("无法调度检测任务: %s", exc)

    def _get_detection_inputs(
        self,
    ) -> Tuple[List[np.ndarray], List[np.ndarray]]:
        if self._is_streaming:
            return self._sse.get_detection_data()
        logprobs_all = [r[0] for r in self._detection_results]
        token_ids_all = [r[1] for r in self._detection_results]
        return logprobs_all, token_ids_all


# --------------------------------------------------------------------------- #
# AnomalyMiddleware（§4.1）
# --------------------------------------------------------------------------- #
class AnomalyMiddleware:
    def __init__(self, app: Callable) -> None:
        self.app = app
        # ① PluginConfig.from_env() — fail-fast on invalid
        self.config = PluginConfig.from_env()
        self.metrics = Metrics()
        self._pending_tasks: Set = set()
        self._resolver = None
        self._tk2cat = None
        self._vocab_size: Optional[int] = None
        self._runner: Optional[DetectorRunner] = None
        self._anomaly_store: Any = None
        self._monitor_rate = self.config.monitor_rate
        self.metrics.set_monitor_rate(self._monitor_rate)

        if not self.config.enabled:
            # ② enabled=False → skip all checks, pure passthrough
            return

        # ③ 构造 AnomalyStore（异常本地保存路径校验，fail-fast，先于重活）
        #    save_path 未设则仅维护内存异常编号；设置则校验目录存在性。
        from .token_resolver import parse_vllm_argv
        info = parse_vllm_argv()
        served_name = info.model if info is not None else None
        self._anomaly_store = AnomalyStore(self.config.save_path, served_name)
        if self.config.save_path is not None:
            logger.info(
                "异常本地保存已开启, 落盘路径: %s", self._anomaly_store.file_path
            )
        else:
            logger.info("异常本地保存未开启, 仅维护内存异常编号")

        # ④ resolve_config_path() — fail-fast on missing
        cfg_path = resolve_config_path()

        # ⑤ 加载 tokenizer — fail-fast on failure
        from .token_resolver import acquire_tokenizer_sync, TokenTextResolver
        tok = acquire_tokenizer_sync(explicit=self.config.tokenizer_model)
        self._resolver = TokenTextResolver(tok)
        logger.info("tokenizer 已加载")

        # ⑤ 生成 tk2cat — soft degrade
        try:
            from .token_categorizer import generate_tk2cat
            self._tk2cat, self._vocab_size = generate_tk2cat(tok)
            logger.info(
                "tk2cat 已加载 (vocab_size=%d)",
                self._vocab_size,
            )
        except Exception as exc:
            logger.warning(
                "tk2cat 生成失败, 检测降级为无词表: %s", exc
            )

        # ⑥ eager 构造 ILLDetector — fail-fast on failure
        from .detector import ILLDetector
        ILLDetector(cfg_path)

        # ⑦ 构造 DetectorRunner（含 ProcessPoolExecutor）
        self._runner = DetectorRunner(
            cfg_path,
            max_workers=self.config.detector_workers,
            topk_n=self.config.top_logprobs,
            tk2cat=self._tk2cat,
            vocab_size=self._vocab_size,
        )

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        # 指标端点（内联，仅 GET）
        if method == "GET" and path == self.config.metrics_path:
            await self._serve_metrics(send)
            return
        # 配置端点（内联，GET 查询 / POST 更新）
        if path == self.config.config_path:
            if method == "POST":
                await self._handle_config_update(scope, receive, send)
                return
            elif method == "GET":
                await self._serve_config(send)
                return
            else:
                await self.app(scope, receive, send)
                return
        # 降级或非目标 → 透传（不读 body）
        if not self.config.enabled:
            await self.app(scope, receive, send)
            return
        if method != "POST" or path not in TARGET_PATHS:
            await self.app(scope, receive, send)
            return
        # 采样：未中 → 纯透传（不读 body、不注入、不恢复、不检测）
        if random.random() >= self._monitor_rate:
            await self.app(scope, receive, send)
            return
        # 选中：读 body
        raw = await _read_all_body(receive)
        try:
            body = json.loads(raw)
        except Exception:
            body = None
        if body is None:
            # 非 JSON → 原样重放透传（vLLM 返回 400）
            replay = _make_replay_receive(receive, raw)
            await self.app(scope, replay, send)
            return
        # JSON 解析成功但非 dict（list/str/num）→ 不拦截，正常注入后透传，
        # 由 vLLM 原生处理（Bug #3 原则：中间件不做额外判断）。
        is_chat = _is_chat_path(path)
        orig = save_original_params(body, is_chat)
        prompt = body.get("messages") if is_chat else body.get("prompt")
        new_body = inject_params(body, is_chat, self.config.top_logprobs)
        new_scope = _patch_scope_content_length(scope, len(new_body))
        request_id = uuid.uuid4().hex
        model = body.get("model") if isinstance(body, dict) else "unknown"
        ctx = RequestContext(
            orig=orig,
            is_chat=is_chat,
            model=model,
            request_id=request_id,
            will_detect=True,
            top_logprobs=self.config.top_logprobs,
            prompt=prompt,
        )
        replay = _make_replay_receive(receive, new_body)
        interceptor = ResponseInterceptor(
            send,
            ctx=ctx,
            runner=self._runner,
            metrics=self.metrics,
            pending_tasks=self._pending_tasks,
            resolver=self._resolver,
            anomaly_store=self._anomaly_store,
        )
        await self.app(new_scope, replay, interceptor)

    async def _serve_metrics(self, send: Callable[[dict], Awaitable[None]]) -> None:
        body = self.metrics.render_metrics()
        headers = [
            [b"content-type", METRICS_CONTENT_TYPE.encode("latin-1")],
            [b"content-length", str(len(body)).encode("latin-1")],
        ]
        await send(
            {"type": "http.response.start", "status": 200, "headers": headers}
        )
        await send(
            {"type": "http.response.body", "body": body, "more_body": False}
        )

    async def _send_json(self, send, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        headers = [
            [b"content-type", b"application/json; charset=utf-8"],
            [b"content-length", str(len(body)).encode("latin-1")],
        ]
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send(
            {"type": "http.response.body", "body": body, "more_body": False}
        )

    async def _serve_config(self, send) -> None:
        await self._send_json(send, 200, {"monitor_rate": self._monitor_rate})

    async def _handle_config_update(self, scope, receive, send) -> None:
        raw = await _read_all_body(receive)
        try:
            data = json.loads(raw)
        except Exception:
            await self._send_json(send, 400, {"error": "invalid JSON body"})
            return
        if not isinstance(data, dict):
            await self._send_json(send, 400, {"error": "body must be a JSON object"})
            return
        if "monitor_rate" not in data:
            await self._send_json(send, 400, {"error": "missing 'monitor_rate' field"})
            return
        value = data["monitor_rate"]
        try:
            rate = float(value)
        except (TypeError, ValueError):
            await self._send_json(send, 400, {"error": "monitor_rate must be a number"})
            return
        if not (0.0 <= rate <= 1.0):
            await self._send_json(send, 400, {
                "error": f"monitor_rate must be 0.0-1.0, got: {rate}"
            })
            return
        self._monitor_rate = rate
        self.metrics.set_monitor_rate(rate)
        logger.info("monitor_rate 已更新: %s", rate)
        await self._send_json(send, 200, {"monitor_rate": rate})

    def shutdown(self) -> None:
        for t in list(self._pending_tasks):
            t.cancel()
        self._pending_tasks.clear()
        if self._runner is not None:
            self._runner.shutdown()
