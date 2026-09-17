"""PD 分离模式 proxy 侧检测集成层（design_pd_proxy.md §5）。

单模型假设：一个 proxy 服务一个模型族。`--anomaly-token2category` 指定具体
预生成词表映射文件，启动期一次性 eager 加载（fail-fast）；tk2cat 经
DetectorRunner 建池 initializer 注入 worker——注入路径与单机版完全一致。

职责：
- ProxyAnomalyController：proxy 侧检测控制器（进程内单例）
- ProxyRequestState：单请求上下文（采样/快照/注入结果）
- ProxyStreamProcessor：流式 SSE 重组 + 事件上抛（recompute 拦截）+ 复用
  SSEStreamProcessor（strip 恢复 / 检测数据累积）；非流式字节缓冲 + 整体
  extract/strip
- StaticTokenTextResolver：预生成 token 文本表查表（与 TokenTextResolver.resolve
  同接口，strip 调用侧零改动）

本模块为纯新增：不改动 anomaly_middleware 既有模块，单机 --middleware 模式
不受影响。proxy 未启用检测时（未传 --anomaly-token2category），转发行为与
原 proxy 逐字节一致。
"""
from __future__ import annotations

import json
import os
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .anomaly_store import AnomalyStore
from .detector import ILLDetector
from .detector_runner import DetectorRunner, schedule_detection
from .env import PluginConfig, resolve_config_path
from .extractor import (
    OriginalParams,
    SSEStreamProcessor,
    extract_chat_response,
    extract_chat_text_tokenids,
    extract_completions_response,
    extract_completions_text_tokenids,
    save_original_params,
    strip_chat_response,
    strip_completions_response,
)
from .logging import get_logger
from .metrics import METRICS_CONTENT_TYPE, Metrics
from .middleware import AnomalyMiddleware as _AnomalyMiddlewareBase

logger = get_logger()

# vllm-ascend PD 分离 recompute 信号（D 节点要求 proxy 重发 prefill）
RECOMPUTE_STOP_REASON = "recomputed"
# 请求关联标识响应头（与单机版 spec §2.9 一致）
REQUEST_ID_HEADER = "x-anomaly-request-id"


# --------------------------------------------------------------------------- #
# 静态 token 文本表 + 词表映射文件加载（§5.7）
# --------------------------------------------------------------------------- #
class StaticTokenTextResolver:
    """预生成 token 文本表查表（替代 TokenTextResolver 的 tokenizer.decode）。

    接口与 `TokenTextResolver.resolve(token_id) -> Optional[str]` 完全一致，
    extractor.strip/_token_text 调用侧零改动（duck-typing）。
    文本为空串（离线生成时 decode 失败）→ 返回 None → 走既有 bytes/null 兜底。
    """

    def __init__(self, token_text: Dict[str, str]) -> None:
        self._text = token_text
        self._cache: Dict[int, Optional[str]] = {}

    def resolve(self, token_id: Any) -> Optional[str]:
        try:
            tid = int(token_id)
        except (TypeError, ValueError):
            return None
        if tid in self._cache:
            return self._cache[tid]
        txt = self._text.get(str(tid))
        txt = txt if txt else None
        self._cache[tid] = txt
        return txt


def _load_json_object(path: str, what: str) -> Dict[str, Any]:
    """读取 JSON 对象文件；缺失/非法 → raise（启动期 fail-fast）。"""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"{what}文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise ValueError(f"{what}文件解析失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{what}文件必须是 JSON 对象: {path}")
    return data


def load_token2category(path: str) -> Tuple[Dict[str, str], int]:
    """加载预生成词表类别映射 `{str(token_id): category}` + vocab_size 推断。

    vocab_size 由内容推断（max(键) + 1），与文件名无关（§5.7.2）。
    """
    data = _load_json_object(path, "词表类别映射")
    tk2cat: Dict[str, str] = {}
    max_id = -1
    for k, v in data.items():
        try:
            tid = int(k)
        except (TypeError, ValueError):
            continue
        tk2cat[str(tid)] = str(v)
        if tid > max_id:
            max_id = tid
    if not tk2cat:
        raise ValueError(f"词表类别映射为空: {path}")
    return tk2cat, max_id + 1


def derive_token_text_path(tk2cat_path: str) -> str:
    """文本表默认定位：tk2cat 文件兄弟目录 token_text/ 下同名文件（§5.7.3）。"""
    parent = os.path.dirname(os.path.abspath(tk2cat_path))
    sibling_root = os.path.dirname(parent)
    return os.path.join(sibling_root, "token_text", os.path.basename(tk2cat_path))


# --------------------------------------------------------------------------- #
# 请求上下文
# --------------------------------------------------------------------------- #
@dataclass
class ProxyRequestState:
    """单请求上下文（对应单机版 RequestContext）。"""

    orig: OriginalParams
    is_chat: bool
    stream: bool
    model: Any
    request_id: str
    prompt: Any
    n_detect: int
    processor: "ProxyStreamProcessor"
    detection_scheduled: bool = field(default=False)
    finished: bool = field(default=False)


# --------------------------------------------------------------------------- #
# 流/非流式双态处理器（§5.2 / §5.3 / §5.4）
# --------------------------------------------------------------------------- #
class ProxyStreamProcessor:
    """D 节点响应字节流 → (转发字节, 解析事件)。

    流式：自持 SSE 重组缓冲（LF/CRLF 兼容），每完整事件先解析上抛（供 proxy
    recompute 逻辑复用），再交内部 SSEStreamProcessor 完成 strip 恢复与检测
    数据累积（组件级复用，逻辑与单机版同源）。stop_reason==recomputed 事件
    拦截：上抛但不转发、不累积（不完整流不进检测）。
    非流式：字节缓冲 + 逐 feed 尝试整体解析（修复原 proxy 分片 JSON 解析
    缺陷），finish 时一次性 extract + strip + 重序列化。
    """

    def __init__(
        self,
        is_chat: bool,
        orig: OriginalParams,
        n_detect: int,
        resolver: Any,
        stream: bool,
    ) -> None:
        self._is_chat = is_chat
        self._orig = orig
        self._n_detect = n_detect
        self._resolver = resolver
        self._stream = stream
        self._buffer = bytearray()
        self._sse: Optional[SSEStreamProcessor] = (
            SSEStreamProcessor(is_chat, orig, n_detect, resolver) if stream else None
        )
        # 非流式状态
        self._parsed_done = False
        self._final_json: Optional[Dict[str, Any]] = None
        self._detection_results: List[Tuple[Any, Any]] = []
        self._choice_texts: List[Optional[str]] = []
        self._choice_tokenids: List[List[int]] = []
        self._choice_reasonings: List[Optional[str]] = []
        # 跨 attempt 文本累积（choices[0]，与原 proxy generated_token 语义一致；
        # recompute 重试后 rewrite content 用，检测数据不用）
        self._text_total = ""

    # ---- 输入 ---- #
    def feed(self, chunk: bytes) -> Tuple[bytes, List[Dict[str, Any]]]:
        if self._stream:
            return self._feed_sse(chunk)
        return self._feed_buffered(chunk)

    def flush(self) -> Tuple[bytes, List[Dict[str, Any]]]:
        """排空尾部半事件（仅流式有尾巴）。"""
        if not self._stream or not self._buffer:
            return b"", []
        tail = bytes(self._buffer)
        self._buffer.clear()
        return self._process_event(tail)

    # ---- 流式路径 ---- #
    def _feed_sse(self, chunk: bytes) -> Tuple[bytes, List[Dict[str, Any]]]:
        self._buffer.extend(chunk)
        out = bytearray()
        events: List[Dict[str, Any]] = []
        while True:
            idx, term_len = self._find_boundary()
            if idx < 0:
                break
            event = bytes(self._buffer[:idx])
            del self._buffer[: idx + term_len]
            o, ev = self._process_event(event)
            out += o
            if ev is not None:
                events.append(ev)
        return bytes(out), events

    def _find_boundary(self) -> Tuple[int, int]:
        """最早事件终止符 (idx, term_len)；无则 (-1, 0)。LF/CRLF 兼容。"""
        lf = self._buffer.find(b"\n\n")
        crlf = self._buffer.find(b"\r\n\r\n")
        candidates: List[Tuple[int, int]] = []
        if lf >= 0:
            candidates.append((lf, 2))
        if crlf >= 0:
            candidates.append((crlf, 4))
        if not candidates:
            return -1, 0
        return min(candidates)

    def _process_event(self, event: bytes) -> Tuple[bytes, Optional[Dict[str, Any]]]:
        if not event.strip():
            return b"", None
        parsed = self._parse_event_payload(event)
        if parsed is not None and self._is_recompute_event(parsed):
            # recompute 事件：上抛供 proxy 重建请求；不转发、不累积（旧流不完整）
            return b"", parsed
        # 其余事件整体交内部 SSE 处理器：strip 恢复 + 检测数据累积 + 透传
        out = self._sse.feed(event + b"\n\n")
        return out, parsed

    @staticmethod
    def _parse_event_payload(event: bytes) -> Optional[Dict[str, Any]]:
        lines = [l.rstrip(b"\r") for l in event.split(b"\n")]
        data_lines = [l for l in lines if l.startswith(b"data:")]
        if not data_lines:
            return None
        payload = b"\n".join(l[len(b"data:"):].lstrip(b" ") for l in data_lines)
        if payload.strip() == b"[DONE]":
            return None
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _is_recompute_event(parsed: Dict[str, Any]) -> bool:
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        first = choices[0]
        return (
            isinstance(first, dict)
            and first.get("stop_reason") == RECOMPUTE_STOP_REASON
        )

    # ---- 非流式路径 ---- #
    def _feed_buffered(self, chunk: bytes) -> Tuple[bytes, List[Dict[str, Any]]]:
        self._buffer.extend(chunk)
        if self._parsed_done:
            return b"", []
        try:
            data = json.loads(bytes(self._buffer))
        except Exception:
            return b"", []  # 半包：等待更多字节（finish 时仍不完整则原样透传）
        self._parsed_done = True
        if not isinstance(data, dict):
            self._final_json = None
            return b"", []
        self._final_json = data
        self._text_total += self._choice0_text(data)
        return b"", [data]

    @staticmethod
    def _choice0_text(data: Dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        text = (
            (message.get("content") if isinstance(message, dict) else None)
            or first.get("text")
            or ""
        )
        return text if isinstance(text, str) else ""

    def _finish_buffered(self, rewrite_content: bool) -> bytes:
        if not self._parsed_done or self._final_json is None:
            raw = bytes(self._buffer)
            self._buffer.clear()
            return raw  # 非 JSON（错误页/不完整）→ 原样透传，不注入检测
        data = self._final_json
        choices = data.get("choices")
        ch = choices if isinstance(choices, list) else []
        # 抽取（必须在 strip 之前，token 字段才保持 token_id: 形态）
        if self._is_chat:
            self._detection_results = extract_chat_response(data, self._n_detect)
            self._choice_tokenids = extract_chat_text_tokenids(data)
            self._choice_texts = [
                (c.get("message") or {}).get("content")
                if isinstance(c, dict) else None
                for c in ch
            ]
            self._choice_reasonings = [
                (c.get("message") or {}).get("reasoning_content")
                if isinstance(c, dict) else None
                for c in ch
            ]
            strip_chat_response(data, self._orig, self._resolver)
        else:
            self._detection_results = extract_completions_response(data, self._n_detect)
            self._choice_tokenids = extract_completions_text_tokenids(data)
            self._choice_texts = [
                c.get("text") if isinstance(c, dict) else None for c in ch
            ]
            self._choice_reasonings = [None] * len(ch)
            strip_completions_response(
                data, self._orig, self._resolver, recompute_text_offset=True
            )
        # recompute 重试后的跨 attempt 内容重写（仅首候选，与原 proxy 语义一致）
        if rewrite_content and ch and isinstance(ch[0], dict):
            first = ch[0]
            if self._is_chat:
                message = first.get("message")
                if isinstance(message, dict):
                    message["content"] = self._text_total
            else:
                first["text"] = self._text_total
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    # ---- recompute 重试：重置当前 attempt（§5.4） ---- #
    def reset(self) -> None:
        """丢弃当前 attempt 的重组/缓冲/检测累积；跨 attempt 文本保留。"""
        self._buffer.clear()
        self._parsed_done = False
        self._final_json = None
        self._detection_results = []
        self._choice_texts = []
        self._choice_tokenids = []
        self._choice_reasonings = []
        if self._sse is not None:
            self._sse = SSEStreamProcessor(
                self._is_chat, self._orig, self._n_detect, self._resolver
            )

    # ---- 收尾与检测数据 ---- #
    def finish(self, rewrite_content: bool = False) -> bytes:
        if self._stream:
            out, _ev = self.flush()
            return out
        return self._finish_buffered(rewrite_content)

    def get_detection_data(self) -> Tuple[List[Any], List[Any]]:
        if self._stream:
            return self._sse.get_detection_data()
        logprobs_all = [r[0] for r in self._detection_results]
        token_ids_all = [r[1] for r in self._detection_results]
        return logprobs_all, token_ids_all

    def get_texts(self) -> List[Optional[str]]:
        if self._stream:
            return self._sse.get_choice_texts()
        return list(self._choice_texts)

    def get_tokenids(self) -> List[List[int]]:
        if self._stream:
            return self._sse.get_choice_text_tokenids()
        return list(self._choice_tokenids)

    def get_reasonings(self) -> List[Optional[str]]:
        if self._stream:
            return self._sse.get_choice_reasoning_contents()
        return list(self._choice_reasonings)


# --------------------------------------------------------------------------- #
# ASGI 中间件形态（§5.11 集成模式 B）：复用单机版 __call__，一键包裹 proxy app
# --------------------------------------------------------------------------- #
# proxy 专属环境变量（PluginConfig 之外的增量配置，不改动 env.py）：
#   VLLM_ANOMALY_TOKEN2CATEGORY  tk2cat JSON 文件路径（启用检测必填）
#   VLLM_ANOMALY_TOKEN_TEXT      token 文本表 JSON 文件路径（默认兄弟目录 token_text/ 同名文件）

ENV_TOKEN2CATEGORY = "VLLM_ANOMALY_TOKEN2CATEGORY"
ENV_TOKEN_TEXT = "VLLM_ANOMALY_TOKEN_TEXT"


def build_proxy_middleware(app: Any) -> Any:
    """按 env 构建检测中间件（一行集成入口）。

    - 配置了 VLLM_ANOMALY_TOKEN2CATEGORY（无论开关）→ 包裹：
      enabled=true 检测生效；enabled=false 纯透传（/anomaly/* 端点仍可达报零值，
      与单机版语义一致）。
    - 未配置词表且未显式禁用 → 原样返回 app（用户无检测意图，零开销零端点）。
    - 未配置词表但显式 VLLM_ANOMALY_ENABLED=false → 包裹（端点可达）。

    供 proxy 脚本一行集成 / 独立启动器使用：
        app = build_proxy_middleware(app)
    """
    enabled_raw = os.environ.get("VLLM_ANOMALY_ENABLED")
    enabled = (
        enabled_raw.strip().lower() not in {"0", "false", "no", "off"}
        if enabled_raw
        else True
    )
    tk2cat_path = os.environ.get(ENV_TOKEN2CATEGORY)
    if not tk2cat_path and enabled:
        return app  # 无检测意图 → 原样返回
    return ProxyAnomalyMiddleware(app)


class ProxyAnomalyMiddleware(_AnomalyMiddlewareBase):
    """PD proxy 版检测中间件（单模型假设）。

    完整继承单机版 `AnomalyMiddleware.__call__`：端点（/anomaly/metrics、
    /anomaly/config）、全局采样、ASGI 请求注入（receive 重放 + Content-Length
    修补）、ResponseInterceptor 响应拦截（strip 恢复 / x-anomaly-request-id /
    检测调度）。仅替换初始化——无 tokenizer，改用预生成 tk2cat + 文本表文件
    （fail-fast）。

    与 hook 集成（§5.1 ProxyAnomalyController）二选一，不可叠加：hook 版在
    handler 内采样注入，中间件版在 ASGI 层完成，叠加会导致双重采样/注入。

    recompute 说明：proxy 的 recompute 为"续写"语义（重发请求的 prompt 含已生成
    token），客户端可见流本身就是完整序列——中间件在 ASGI 层无感知 recompute，
    SSE 累积天然覆盖全部 token（含首 token），无需重置逻辑。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.config = PluginConfig.from_env()
        self.metrics = Metrics()
        self._pending_tasks: set = set()
        self._monitor_rate = self.config.monitor_rate
        self.metrics.set_monitor_rate(self._monitor_rate)
        self._resolver = None
        self._tk2cat = None
        self._vocab_size = None
        self._runner = None
        self._anomaly_store = None
        if not self.config.enabled:
            logger.info("精度异常检测 enabled=False，纯透传模式（端点仍可达）")
            return

        tk2cat_path = os.environ.get(ENV_TOKEN2CATEGORY)
        if not tk2cat_path:
            raise ValueError(
                f"启用精度异常检测必须设置环境变量 {ENV_TOKEN2CATEGORY}"
                "=<tk2cat JSON 文件路径>（离线生成见 tools/gen_token_category.py）"
            )
        # 异常本地保存（fail-fast 先于重活）
        self._anomaly_store = AnomalyStore(self.config.save_path, None)
        # 检测器配置
        cfg_path = resolve_config_path()
        # tk2cat + 文本表（fail-fast）
        tk2cat, vocab_size = load_token2category(tk2cat_path)
        text_path = os.environ.get(ENV_TOKEN_TEXT) or derive_token_text_path(tk2cat_path)
        text_map = _load_json_object(text_path, "token 文本表")
        self._resolver = StaticTokenTextResolver(
            {str(k): str(v) for k, v in text_map.items() if v is not None}
        )
        self._tk2cat = tk2cat
        self._vocab_size = vocab_size
        logger.info(
            "词表类别映射已加载: %s (entries=%d, vocab_size=%d); 文本表: %s",
            tk2cat_path, len(tk2cat), vocab_size, text_path,
        )
        # eager 验证 + 检测进程池（与单机版一致）
        ILLDetector(cfg_path)
        self._runner = DetectorRunner(
            cfg_path,
            max_workers=self.config.detector_workers,
            topk_n=self.config.top_logprobs,
            tk2cat=tk2cat,
            vocab_size=vocab_size,
        )
        logger.info(
            "检测进程池已就绪: workers=%d topk_n=%d monitor_rate=%s",
            self.config.detector_workers, self.config.top_logprobs, self._monitor_rate,
        )


# --------------------------------------------------------------------------- #
# 控制器（§5.1，集成模式 A：handler 内挂钩）
# --------------------------------------------------------------------------- #
class ProxyAnomalyController:
    """proxy 侧检测控制器（进程内单例）。

    eager 初始化（无 tokenizer）：
    config(from_env + CLI 覆盖) → AnomalyStore → resolve_config_path
    → 加载 tk2cat/文本表文件（fail-fast）→ StaticTokenTextResolver
    → ILLDetector 验证 → DetectorRunner（建池注入 tk2cat）。
    任一硬依赖失败 → raise，proxy 启动终止（与单机版一致）。
    enabled=False → 纯透传模式（端点仍可达）。
    """

    def __init__(
        self,
        tk2cat_path: Optional[str] = None,
        token_text_path: Optional[str] = None,
        config_path: Optional[str] = None,
        monitor_rate: Optional[float] = None,
        detector_workers: Optional[int] = None,
        save_path: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.config = PluginConfig.from_env()
        if enabled is not None:
            self.config.enabled = bool(enabled)
        if monitor_rate is not None:
            self.config.monitor_rate = float(monitor_rate)
        if detector_workers is not None:
            self.config.detector_workers = int(detector_workers)
        if save_path is not None:
            self.config.save_path = save_path
        if not (0.0 <= self.config.monitor_rate <= 1.0):
            raise ValueError(
                f"monitor_rate 必须为 0.0-1.0, 当前值: {self.config.monitor_rate}"
            )
        self.metrics = Metrics()
        self._pending_tasks: set = set()
        self._resolver: Optional[StaticTokenTextResolver] = None
        self._runner: Optional[DetectorRunner] = None
        self._anomaly_store: Optional[AnomalyStore] = None
        self._monitor_rate = self.config.monitor_rate
        self.metrics.set_monitor_rate(self._monitor_rate)

        if not self.config.enabled:
            logger.info("精度异常检测 enabled=False，纯透传模式（端点仍可达）")
            return

        # 异常本地保存（路径校验 fail-fast，先于重活）
        self._anomaly_store = AnomalyStore(self.config.save_path, None)
        if self.config.save_path is not None:
            logger.info("异常本地保存已开启, 落盘路径: %s", self._anomaly_store.file_path)
        else:
            logger.info("异常本地保存未开启, 仅维护内存异常编号")

        # 检测器配置（默认包内 configs/detector.yaml）
        cfg_path = config_path or resolve_config_path()

        # tk2cat 预生成文件（fail-fast，§5.7.2）
        if tk2cat_path is None:
            raise ValueError(
                "启用精度异常检测必须提供词表类别映射文件"
                "（--anomaly-token2category <file>）"
            )
        tk2cat, vocab_size = load_token2category(tk2cat_path)
        logger.info(
            "词表类别映射已加载: %s (entries=%d, vocab_size=%d)",
            tk2cat_path, len(tk2cat), vocab_size,
        )

        # token 文本表（strip 还原；默认兄弟目录 token_text/ 同名文件，§5.7.3）
        text_path = token_text_path or derive_token_text_path(tk2cat_path)
        text_map = _load_json_object(text_path, "token 文本表")
        self._resolver = StaticTokenTextResolver(
            {str(k): str(v) for k, v in text_map.items() if v is not None}
        )
        logger.info("token 文本表已加载: %s (entries=%d)", text_path, len(text_map))

        # eager 构造验证（numpy/配置/阈值；worker 各自构造）
        ILLDetector(cfg_path)

        # 检测进程池（tk2cat 建池注入，与单机版一致）
        self._runner = DetectorRunner(
            cfg_path,
            max_workers=self.config.detector_workers,
            topk_n=self.config.top_logprobs,
            tk2cat=tk2cat,
            vocab_size=vocab_size,
        )
        logger.info(
            "检测进程池已就绪: workers=%d topk_n=%d",
            self.config.detector_workers, self.config.top_logprobs,
        )

    # ---- 请求入口（§5.5 采样 + §3.1 注入） ---- #
    def before_forward(self, req_data: Any, is_chat: bool) -> Optional[ProxyRequestState]:
        """采样 + 快照 + 注入。返回 None = 未选中/disabled（proxy 走原逻辑透传）。"""
        if not self.config.enabled:
            return None
        if not isinstance(req_data, dict):
            return None
        if random.random() >= self._monitor_rate:
            return None
        orig = save_original_params(req_data, is_chat)
        # 注入检测所需参数（dict 级，规则与 extractor.inject_params 一致：
        # 注入值 = max(客户端原值, N)；return_tokens_as_token_ids 恒为 True）
        n = self.config.top_logprobs
        if is_chat:
            client_top = req_data.get("top_logprobs")
            req_data["top_logprobs"] = (
                max(client_top, n) if client_top is not None else n
            )
            req_data["logprobs"] = True
        else:
            client_logp = req_data.get("logprobs")
            req_data["logprobs"] = (
                max(client_logp, n) if client_logp is not None else n
            )
        req_data["return_tokens_as_token_ids"] = True
        prompt = req_data.get("messages" if is_chat else "prompt")
        stream_flag = bool(req_data.get("stream", False))
        request_id = uuid.uuid4().hex
        model = req_data.get("model") if isinstance(req_data, dict) else None
        processor = ProxyStreamProcessor(
            is_chat=is_chat,
            orig=orig,
            n_detect=self.config.top_logprobs,
            resolver=self._resolver,
            stream=stream_flag,
        )
        return ProxyRequestState(
            orig=orig,
            is_chat=is_chat,
            stream=stream_flag,
            model=model,
            request_id=request_id,
            prompt=prompt,
            n_detect=self.config.top_logprobs,
            processor=processor,
        )

    # ---- 流式/非流式字节处理（异常兜底：原样透传，不影响转发主路径） ---- #
    def feed_stream(self, ctx: Optional[ProxyRequestState], chunk: bytes) -> Tuple[bytes, List[Dict[str, Any]]]:
        if ctx is None or ctx.finished:
            return chunk, []
        try:
            return ctx.processor.feed(chunk)
        except Exception as exc:
            logger.error(
                "流处理异常, 原样透传 request_id=%s: %s", ctx.request_id, exc
            )
            return chunk, []

    def on_recompute(self, ctx: Optional[ProxyRequestState]) -> None:
        """recompute 重试：重置当前 attempt 累积（§5.4）。"""
        if ctx is None or ctx.finished:
            return
        try:
            ctx.processor.reset()
        except Exception as exc:
            logger.error(
                "recompute 重置异常 request_id=%s: %s", ctx.request_id, exc
            )

    def finish(self, ctx: Optional[ProxyRequestState], *, rewrite_content: bool = False) -> bytes:
        """收尾：非流式整体处理返回最终 body；流式返回尾部字节。随后调度检测。"""
        if ctx is None or ctx.finished:
            return b""
        ctx.finished = True
        try:
            out = ctx.processor.finish(rewrite_content=rewrite_content)
        except Exception as exc:
            logger.error("响应收尾异常 request_id=%s: %s", ctx.request_id, exc)
            out = b""
        self._maybe_schedule_detection(ctx)
        return out

    # ---- 检测调度（镜像单机版 ResponseInterceptor._maybe_schedule_detection） ---- #
    def _maybe_schedule_detection(self, ctx: ProxyRequestState) -> None:
        if self._runner is None or ctx.detection_scheduled:
            return
        ctx.detection_scheduled = True
        try:
            logprobs_list, token_ids_list = ctx.processor.get_detection_data()
        except Exception as exc:
            logger.error(
                "获取检测数据失败 request_id=%s: %s", ctx.request_id, exc
            )
            return
        if not token_ids_list or not any(len(t) > 0 for t in token_ids_list):
            return  # 空响应不检测
        try:
            schedule_detection(
                self._runner,
                logprobs_list,
                token_ids_list,
                request_id=ctx.request_id,
                model=ctx.model,
                metrics=self.metrics,
                pending_tasks=self._pending_tasks,
                anomaly_store=self._anomaly_store,
                prompt=ctx.prompt,
                texts=ctx.processor.get_texts(),
                text_tokenids=ctx.processor.get_tokenids(),
                reasoning_contents=ctx.processor.get_reasonings(),
            )
        except RuntimeError as exc:
            logger.warning("无法调度检测任务: %s", exc)

    # ---- 端点数据（§5.10） ---- #
    def render_metrics(self) -> bytes:
        return self.metrics.render_metrics()

    def get_metrics_content_type(self) -> str:
        return METRICS_CONTENT_TYPE

    def get_monitor_rate(self) -> float:
        return self._monitor_rate

    def update_monitor_rate(self, value: Any) -> Tuple[bool, str]:
        """校验并更新运行时监控概率；返回 (ok, error_msg)。"""
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return False, "monitor_rate must be a number"
        if not (0.0 <= rate <= 1.0):
            return False, f"monitor_rate must be 0.0-1.0, got: {rate}"
        self._monitor_rate = rate
        self.metrics.set_monitor_rate(rate)
        logger.info("monitor_rate 已更新: %s", rate)
        return True, ""

    # ---- 生命周期 ---- #
    def shutdown(self) -> None:
        for t in list(self._pending_tasks):
            t.cancel()
        self._pending_tasks.clear()
        if self._runner is not None:
            self._runner.shutdown()
