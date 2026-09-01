"""抽取/恢复 + SSEStreamProcessor + 请求参数快照/注入（design §3.1/§3.2/§3.3, spec §2.2/§2.3/§2.4）。

纯数据层：不涉及 ASGI。middleware.py 负责 ASGI 集成（读 body、重放 receive、patch scope）。
本模块负责：请求体参数快照、强制注入、响应抽取（供检测）、响应恢复（供客户端）、SSE 跨块重组。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

TOKEN_ID_PREFIX = "token_id:"


@dataclass
class OriginalParams:
    """客户端原始采集参数快照（供恢复）。"""

    is_chat: bool
    logprobs: Any  # chat: bool|None；completions: int|None（数量）
    top_logprobs: Optional[int]  # chat only
    return_tokens_as_token_ids: bool
    n: int
    stream: bool


# --------------------------------------------------------------------------- #
# 请求参数：快照 / 注入（§3.1 / §2.2）
# --------------------------------------------------------------------------- #
def save_original_params(body: Any, is_chat: bool) -> OriginalParams:
    """注入前缓存客户端原始采集参数（供 §3.2 响应恢复）。"""
    if not isinstance(body, dict):
        return OriginalParams(is_chat, None, None, False, 1, False)
    if is_chat:
        logprobs = body.get("logprobs")  # bool|None
        top_logprobs = body.get("top_logprobs")  # int|None
    else:
        logprobs = body.get("logprobs")  # int|None（数量）
        top_logprobs = None
    rtati = body.get("return_tokens_as_token_ids", False)
    try:
        n = int(body.get("n", 1) or 1)
    except (TypeError, ValueError):
        n = 1
    stream = bool(body.get("stream", False))
    return OriginalParams(
        is_chat=is_chat,
        logprobs=logprobs,
        top_logprobs=top_logprobs,
        return_tokens_as_token_ids=bool(rtati),
        n=n,
        stream=stream,
    )


def inject_params(body: Any, is_chat: bool, n_detect: int) -> bytes:
    """强制注入检测所需参数，返回新 body 字节。

    chat：logprobs=True、top_logprobs=max(客户端,N)、return_tokens_as_token_ids=True
    completions：logprobs=max(客户端,N)、return_tokens_as_token_ids=True

    中间件不做客户端参数合法性判断：按规则注入后透传，接受 vLLM 原生结果。
    """
    nb = dict(body) if isinstance(body, dict) else {}
    if is_chat:
        client_top = body.get("top_logprobs") if isinstance(body, dict) else None
        if client_top is not None:
            nb["top_logprobs"] = max(client_top, n_detect)
        else:
            nb["top_logprobs"] = n_detect
        nb["logprobs"] = True
    else:
        client_logp = body.get("logprobs") if isinstance(body, dict) else None
        if client_logp is not None:
            nb["logprobs"] = max(client_logp, n_detect)
        else:
            nb["logprobs"] = n_detect
    nb["return_tokens_as_token_ids"] = True
    return json.dumps(nb, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def parse_token_id(value: Any) -> int:
    """兼容 "token_id:NNN" 与纯数字串；失败返回 -1。"""
    if value is None:
        return -1
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(TOKEN_ID_PREFIX):
            s = s[len(TOKEN_ID_PREFIX):].strip()
        try:
            return int(s)
        except ValueError:
            return -1
    return -1


def _decode_bytes(b: Any) -> Optional[str]:
    """从 utf-8 字节列表解码文本；无 bytes/失败 → None（绝不留 token_id:）。"""
    if b is None:
        return None
    if isinstance(b, str):
        return b
    if isinstance(b, (list, tuple)):
        try:
            arr = bytes(int(x) & 0xFF for x in b)
            return arr.decode("utf-8")
        except Exception:
            return None
    return None


def _restore_token_bytes(text: Optional[str], current: Any) -> Any:
    """将 bytes 与恢复后的 token 文本对齐。

    vLLM 在 return_tokens_as_token_ids=True 时把 bytes 置为 "token_id:NNN" 的 ASCII 字节；
    strip 恢复 token 文本后，bytes 必须同步恢复为文本的 UTF-8 字节，否则向客户端泄漏 token_id:。
    """
    if text is None:
        return None
    cur = _decode_bytes(current)
    if cur is not None and cur == text and not cur.startswith(TOKEN_ID_PREFIX):
        return current
    return list(text.encode("utf-8"))


def _token_text(
    token_id_value: Any,
    bytes_value: Any,
    resolver: Any,
    *,
    fallback_to_id: bool = False,
) -> Optional[str]:
    """统一 token_id -> 文本（spec §5，resolver 优先 + §4.7 降级例外）。

    1) 优先 resolver：覆盖 chat 主 token / top_logprobs / completions 全部字段。
    2) resolver 缺失/未解析 → 退回 bytes，仅当解码出真实文本（不含 token_id: 前缀）。
       该守卫独立修复泄漏：chat top_logprobs 的破损 bytes 被置 null，绝不回写 token_id:。
    3) 都无 → fallback_to_id=True 时返回 `token_id:NNN`（§4.7 降级例外：客户端请求了
       topk + 未设 return_tokens_as_token_ids + resolver 不可用，保证 topk logprob 数据不丢失）；
       否则 None。
    """
    if resolver is not None:
        tid = parse_token_id(token_id_value)
        if tid >= 0:
            txt = resolver.resolve(tid)
            if txt is not None:
                return txt
    if bytes_value is not None:
        s = _decode_bytes(bytes_value)
        if s is not None and not s.startswith(TOKEN_ID_PREFIX):
            return s
    if fallback_to_id:
        tid = parse_token_id(token_id_value)
        if tid >= 0:
            return f"{TOKEN_ID_PREFIX}{tid}"
    return None


def _truncate_topk(d: Dict[int, float], n: Optional[int]) -> Tuple[List[float], List[int]]:
    """按 logprob 降序取前 n 项（供检测，截断到 N）。

    返回 (logprobs, token_ids) 列表，不足 n 项时用 (-100.0, 0) 填充。
    """
    if not n or n <= 0 or not d:
        return [-100.0] * max(n or 0, 1), [0] * max(n or 0, 1)
    items = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]
    logprobs = [v for _, v in items]
    token_ids = [k for k, _ in items]
    while len(logprobs) < n:
        logprobs.append(-100.0)
        token_ids.append(0)
    return logprobs, token_ids


def _build_arrays(
    lp_list: List[List[float]], tid_list: List[List[int]]
) -> Tuple[np.ndarray, np.ndarray]:
    """将 per-position 列表构建为 2D numpy 数组。"""
    if not lp_list:
        return np.empty((0, 0), dtype=np.float32), np.empty((0, 0), dtype=np.int32)
    return np.array(lp_list, dtype=np.float32), np.array(tid_list, dtype=np.int32)


# --------------------------------------------------------------------------- #
# 抽取（供检测）：per choice -> (topk_list, tokens_list)
# --------------------------------------------------------------------------- #
def extract_chat_response(
    data: Any, n_detect: int
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """chat 非流式：choices[].logprobs.content[]。返回 per choice (logprobs, token_ids) 数组。"""
    results: List[Tuple[np.ndarray, np.ndarray]] = []
    if not isinstance(data, dict):
        return results
    choices = data.get("choices")
    if not isinstance(choices, list):
        return results
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        lp = choice.get("logprobs")
        lp_list: List[List[float]] = []
        tid_list: List[List[int]] = []
        if isinstance(lp, dict):
            content = lp.get("content")
            if isinstance(content, list):
                for entry in content:
                    if not isinstance(entry, dict):
                        continue
                    tps = entry.get("top_logprobs")
                    if not isinstance(tps, list):
                        tps = []
                    d: Dict[int, float] = {}
                    for tp in tps:
                        if not isinstance(tp, dict):
                            continue
                        tid = parse_token_id(tp.get("token"))
                        if tid >= 0:
                            try:
                                d[tid] = float(tp.get("logprob"))
                            except (TypeError, ValueError):
                                pass
                    lps, tids = _truncate_topk(d, n_detect)
                    lp_list.append(lps)
                    tid_list.append(tids)
        logprobs, token_ids = _build_arrays(lp_list, tid_list)
        results.append((logprobs, token_ids))
    return results


def extract_completions_response(
    data: Any, n_detect: int
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """completions 非流式：choices[].logprobs{tokens[],token_logprobs[],top_logprobs[]}。"""
    results: List[Tuple[np.ndarray, np.ndarray]] = []
    if not isinstance(data, dict):
        return results
    choices = data.get("choices")
    if not isinstance(choices, list):
        return results
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        lp = choice.get("logprobs")
        lp_list: List[List[float]] = []
        tid_list: List[List[int]] = []
        if isinstance(lp, dict):
            top_logprobs = lp.get("top_logprobs")
            if isinstance(top_logprobs, list):
                for pos in top_logprobs:
                    if not isinstance(pos, dict):
                        lps, tids = _truncate_topk({}, n_detect)
                        lp_list.append(lps)
                        tid_list.append(tids)
                        continue
                    d: Dict[int, float] = {}
                    for k, v in pos.items():
                        tid = parse_token_id(k)
                        if tid >= 0:
                            try:
                                d[tid] = float(v)
                            except (TypeError, ValueError):
                                pass
                    lps, tids = _truncate_topk(d, n_detect)
                    lp_list.append(lps)
                    tid_list.append(tids)
        logprobs, token_ids = _build_arrays(lp_list, tid_list)
        results.append((logprobs, token_ids))
    return results


def extract_chat_text_tokenids(data: Any) -> List[List[int]]:
    """chat 非流式：从 choices[].logprobs.content[].token 提取实际生成 token_id 序列。

    每个内容条目的 token 字段格式为 "token_id:NNN"（因注入 return_tokens_as_token_ids=true）。
    parse_token_id 解析失败返回 -1（保留位置对齐，列表长度 = content[] 长度）。
    必须在 strip_chat_response 之前调用（strip 会将 token 替换为解码文本）。
    """
    results: List[List[int]] = []
    if not isinstance(data, dict):
        return results
    choices = data.get("choices")
    if not isinstance(choices, list):
        return results
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        tokenids: List[int] = []
        lp = choice.get("logprobs")
        if isinstance(lp, dict):
            content = lp.get("content")
            if isinstance(content, list):
                for entry in content:
                    if isinstance(entry, dict):
                        tokenids.append(parse_token_id(entry.get("token")))
                    else:
                        tokenids.append(-1)
        results.append(tokenids)
    return results


def extract_completions_text_tokenids(data: Any) -> List[List[int]]:
    """completions 非流式：从 choices[].logprobs.tokens[] 提取实际生成 token_id 序列。

    必须在 strip_completions_response 之前调用。
    """
    results: List[List[int]] = []
    if not isinstance(data, dict):
        return results
    choices = data.get("choices")
    if not isinstance(choices, list):
        return results
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        tokenids: List[int] = []
        lp = choice.get("logprobs")
        if isinstance(lp, dict):
            tokens = lp.get("tokens")
            if isinstance(tokens, list):
                for t in tokens:
                    tokenids.append(parse_token_id(t))
        results.append(tokenids)
    return results


# --------------------------------------------------------------------------- #
# 恢复（供客户端，按原始参数）
# --------------------------------------------------------------------------- #
def _recompute_text_offset(tokens: List[Any]) -> List[int]:
    """text_offset 按恢复后的真实 token 文本重算（字符级前缀和）。

    vLLM 在 return_tokens_as_token_ids=True 时按 "token_id:NNN" 代理串长度计算
    text_offset（非流式）；strip 恢复真实文本后必须重算，否则泄漏代理串长度（TC-004 v2）。
    仅非流式适用：流式逐事件时 vLLM 按真实文本计算 offset，无需重算。
    """
    out: List[int] = []
    total = 0
    for t in tokens:
        out.append(total)
        if isinstance(t, str):
            total += len(t)
    return out


def strip_chat_response(
    data: Any,
    orig: OriginalParams,
    resolver: Any = None,
) -> None:
    """chat 响应恢复（原位修改 data）。"""
    if not isinstance(data, dict):
        return
    choices = data.get("choices")
    if not isinstance(choices, list):
        return
    # §4.7 降级例外：客户端请求 topk + 未设 rtati + resolver 不可用 → 三层兜底落 token_id:NNN
    fallback_to_id = (
        not orig.return_tokens_as_token_ids
        and resolver is None
        and orig.logprobs is True
        and (orig.top_logprobs or 0) > 0
    )
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        lp = choice.get("logprobs")
        if lp is None:
            continue
        if not orig.logprobs:
            # 客户端未请求 logprobs → null
            choice["logprobs"] = None
            continue
        m = orig.top_logprobs  # 客户端 M（int|None）
        content = lp.get("content")
        if isinstance(content, list):
            for entry in content:
                if not isinstance(entry, dict):
                    continue
                tps = entry.get("top_logprobs")
                if not isinstance(tps, list):
                    tps = []
                if m is not None:
                    tps = tps[:m]
                else:
                    tps = []
                if not orig.return_tokens_as_token_ids:
                    entry["token"] = _token_text(
                        entry.get("token"),
                        entry.get("bytes"),
                        resolver,
                        fallback_to_id=fallback_to_id,
                    )
                    entry["bytes"] = _restore_token_bytes(
                        entry["token"], entry.get("bytes")
                    )
                    for tp in tps:
                        if isinstance(tp, dict):
                            tp["token"] = _token_text(
                                tp.get("token"),
                                tp.get("bytes"),
                                resolver,
                                fallback_to_id=fallback_to_id,
                            )
                            tp["bytes"] = _restore_token_bytes(
                                tp["token"], tp.get("bytes")
                            )
                entry["top_logprobs"] = tps


def strip_completions_response(
    data: Any,
    orig: OriginalParams,
    resolver: Any = None,
    *,
    recompute_text_offset: bool = False,
) -> None:
    """completions 响应恢复（原位修改 data）。

    resolver 可用时 tokens[] / top_logprobs[] 还原为真实文本（resolver 优先）；
    resolver 不可用 + 触发降级（§4.7 例外）→ 回退 token_id:NNN，保证 topk logprob 数据不丢失；
    resolver 不可用 + 未触发 → 退回 null（绝不留 token_id:）。
    """
    if not isinstance(data, dict):
        return
    choices = data.get("choices")
    if not isinstance(choices, list):
        return
    # §4.7 降级例外：completions 客户端请求 topk(logprobs>0) + 未设 rtati + resolver 不可用
    fallback_to_id = (
        not orig.return_tokens_as_token_ids
        and resolver is None
        and (orig.logprobs or 0) > 0
    )
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        lp = choice.get("logprobs")
        if lp is None:
            continue
        if not orig.logprobs:
            choice["logprobs"] = None
            continue
        m = orig.logprobs  # completions 的 logprobs 即数量 M
        top_logprobs = lp.get("top_logprobs")
        if isinstance(top_logprobs, list):
            new_tlp: List[Any] = []
            for pos in top_logprobs:
                if not isinstance(pos, dict):
                    new_tlp.append(None)
                    continue
                if m is not None:
                    items = list(pos.items())[:m]
                else:
                    items = []
                if orig.return_tokens_as_token_ids:
                    new_tlp.append({k: v for k, v in items})
                else:
                    rebuilt: Dict[str, Any] = {}
                    for k, v in items:
                        txt = _token_text(
                            k, None, resolver, fallback_to_id=fallback_to_id
                        )  # completions 无 bytes
                        if txt is not None:
                            rebuilt[txt] = v
                    new_tlp.append(rebuilt if rebuilt else None)
            lp["top_logprobs"] = new_tlp
        if not orig.return_tokens_as_token_ids:
            toks = lp.get("tokens")
            if isinstance(toks, list):
                lp["tokens"] = [
                    _token_text(t, None, resolver, fallback_to_id=fallback_to_id)
                    for t in toks
                ]
                if recompute_text_offset:
                    lp["text_offset"] = _recompute_text_offset(lp["tokens"])


# --------------------------------------------------------------------------- #
# SSE 流式处理器（§3.3 / §2.4）
# --------------------------------------------------------------------------- #
class SSEStreamProcessor:
    """跨块事件重组 + 每块恢复 + per-choice 累积检测数据。

    转发：增量无状态（每块即发）；检测数据：跨块有状态 append。
    """

    def __init__(self, is_chat: bool, orig: OriginalParams, n_detect: int, resolver: Any = None) -> None:
        self._is_chat = is_chat
        self._orig = orig
        self._n_detect = n_detect
        self._resolver = resolver
        self._buffer = bytearray()
        # chat 累积：choice_index -> list[entry]
        self._chat_acc: Dict[int, List[Dict[str, Any]]] = {}
        # completions 累积：choice_index -> {tokens, token_logprobs, top_logprobs}
        self._comp_acc: Dict[int, Dict[str, List[Any]]] = {}
        # 文本累积（choice_index -> 拼接后的输出文本），用于异常保存
        self._text_acc: Dict[int, str] = {}
        # 思维链文本累积（choice_index -> 拼接后的思维链文本），仅 chat thinking model
        self._reasoning_acc: Dict[int, str] = {}

    # ---- 转发接口 ---- #
    def feed(self, chunk: bytes) -> bytes:
        self._buffer.extend(chunk)
        out = bytearray()
        while True:
            idx, term_len = self._find_event_boundary()
            if idx < 0:
                break
            event = bytes(self._buffer[:idx])
            del self._buffer[: idx + term_len]
            out += self._process_event(event)
        return bytes(out)

    def flush(self) -> bytes:
        if not self._buffer:
            return b""
        tail = bytes(self._buffer)
        self._buffer.clear()
        return self._process_event(tail)

    def _find_event_boundary(self) -> Tuple[int, int]:
        """返回最早的事件终止符 (idx, term_len)；无则 (-1, 0)。

        兼容 LF（\\n\\n）与 CRLF（\\r\\n\\r\\n）两种 SSE 事件终止符（§3.3）。
        """
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

    # ---- 事件处理 ---- #
    def _process_event(self, event: bytes) -> bytes:
        if not event.strip():
            return b""
        lines = [l.rstrip(b"\r") for l in event.split(b"\n")]
        data_lines = [l for l in lines if l.startswith(b"data:")]
        other_lines = [l for l in lines if not l.startswith(b"data:")]
        if not data_lines:
            # keep-alive / 注释：原样透传
            return event + b"\n\n"
        # 聚合 data 负载（多 data 行用 \n 连接）
        payload = b"\n".join(l[len(b"data:"):].lstrip(b" ") for l in data_lines)
        if payload.strip() == b"[DONE]":
            # 终端 [DONE] 原样透传
            return event + b"\n\n"
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:
            # 非 JSON 原样透传
            return event + b"\n\n"
        if not isinstance(parsed, dict):
            return event + b"\n\n"
        # 累积检测数据
        self._extract_streaming(parsed)
        # 每块无状态恢复
        self._strip_streaming(parsed)
        # 重序列化
        new_data = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
        out = bytearray(b"data: ")
        out += new_data
        out += b"\n"
        if other_lines:
            out += b"\n".join(other_lines)
            out += b"\n"
        out += b"\n\n"
        return bytes(out)

    def _extract_streaming(self, parsed: Dict[str, Any]) -> None:
        choices = parsed.get("choices")
        if not isinstance(choices, list):
            return
        for ci, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            # 流式 n>1 时每个 chunk 通常只带一个 choice（带 index 字段），
            # 必须按真实 choice.index 分组，不能按 chunk 内位置（否则 n 个候选合并成一组）
            cidx = choice.get("index", ci)
            # 文本累积（供异常保存；与 logprobs 累积独立）
            if self._is_chat:
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    c = delta.get("content")
                    if isinstance(c, str):
                        self._text_acc[cidx] = self._text_acc.get(cidx, "") + c
                    rc = delta.get("reasoning_content")
                    if isinstance(rc, str):
                        self._reasoning_acc[cidx] = self._reasoning_acc.get(cidx, "") + rc
            else:
                c = choice.get("text")
                if isinstance(c, str):
                    self._text_acc[cidx] = self._text_acc.get(cidx, "") + c
            lp = choice.get("logprobs")
            if lp is None:
                continue
            if self._is_chat:
                content = lp.get("content")
                if isinstance(content, list):
                    acc = self._chat_acc.setdefault(cidx, [])
                    # latest-longest-wins 防御（累积式）
                    if acc and content and _entry_token(content[0]) == _entry_token(acc[0]) and len(content) > len(acc):
                        acc.clear()
                    # 深拷贝：后续 strip 会原位改写 parsed 里的 entry/top_logprobs，
                    # 必须与检测数据解耦，否则检测数据被客户端参数截断（回归）
                    acc.extend(copy.deepcopy(e) for e in content if isinstance(e, dict))
            else:
                toks = lp.get("tokens")
                tl = lp.get("token_logprobs")
                tlp = lp.get("top_logprobs")
                toks = list(toks) if isinstance(toks, list) else []
                tl = list(tl) if isinstance(tl, list) else []
                tlp = list(tlp) if isinstance(tlp, list) else []
                acc = self._comp_acc.setdefault(
                    cidx, {"tokens": [], "token_logprobs": [], "top_logprobs": []}
                )
                if not acc["tokens"]:
                    acc["tokens"] = toks
                    acc["token_logprobs"] = tl
                    acc["top_logprobs"] = tlp
                elif toks and toks[0] == acc["tokens"][0] and len(toks) > len(acc["tokens"]):
                    # 累积式：latest-longest-wins
                    acc["tokens"] = toks
                    acc["token_logprobs"] = tl
                    acc["top_logprobs"] = tlp
                else:
                    acc["tokens"].extend(toks)
                    acc["token_logprobs"].extend(tl)
                    acc["top_logprobs"].extend(tlp)

    def _strip_streaming(self, parsed: Dict[str, Any]) -> None:
        if self._is_chat:
            strip_chat_response(
                parsed, self._orig, self._resolver
            )
        else:
            strip_completions_response(
                parsed, self._orig, self._resolver
            )

    # ---- 检测数据 ---- #
    def get_choice_texts(self) -> List[Optional[str]]:
        """返回 per-choice 输出文本，按 choice index 升序（与 get_detection_data 对齐）。"""
        if self._is_chat:
            keys = sorted(self._chat_acc.keys())
        else:
            keys = sorted(self._comp_acc.keys())
        return [self._text_acc.get(ci) for ci in keys]

    def get_choice_text_tokenids(self) -> List[List[int]]:
        """返回 per-choice 实际生成 token_id 序列（与 get_detection_data 对齐）。

        chat：从 _chat_acc 累积的 content entries 的 token 字段解析。
        completions：从 _comp_acc 累积的 tokens[] 解析。
        """
        results: List[List[int]] = []
        if self._is_chat:
            for ci in sorted(self._chat_acc.keys()):
                content = self._chat_acc[ci]
                tokenids: List[int] = []
                for entry in content:
                    if isinstance(entry, dict):
                        tokenids.append(parse_token_id(entry.get("token")))
                    else:
                        tokenids.append(-1)
                results.append(tokenids)
        else:
            for ci in sorted(self._comp_acc.keys()):
                acc = self._comp_acc[ci]
                tokens = acc.get("tokens", [])
                results.append([parse_token_id(t) for t in tokens])
        return results

    def get_choice_reasoning_contents(self) -> List[Optional[str]]:
        """返回 per-choice 思维链文本（仅 chat；completions 返回 None 列表）。"""
        if self._is_chat:
            keys = sorted(self._chat_acc.keys())
        else:
            keys = sorted(self._comp_acc.keys())
        return [self._reasoning_acc.get(ci) for ci in keys]

    def get_detection_data(
        self,
    ) -> Tuple[List[np.ndarray], List[np.ndarray]]:
        logprobs_all: List[np.ndarray] = []
        token_ids_all: List[np.ndarray] = []
        if self._is_chat:
            for ci in sorted(self._chat_acc.keys()):
                content = self._chat_acc[ci]
                lp_list: List[List[float]] = []
                tid_list: List[List[int]] = []
                for entry in content:
                    tps = entry.get("top_logprobs")
                    if not isinstance(tps, list):
                        tps = []
                    d: Dict[int, float] = {}
                    for tp in tps:
                        if not isinstance(tp, dict):
                            continue
                        tid = parse_token_id(tp.get("token"))
                        if tid >= 0:
                            try:
                                d[tid] = float(tp.get("logprob"))
                            except (TypeError, ValueError):
                                pass
                    lps, tids = _truncate_topk(d, self._n_detect)
                    lp_list.append(lps)
                    tid_list.append(tids)
                logprobs, token_ids = _build_arrays(lp_list, tid_list)
                logprobs_all.append(logprobs)
                token_ids_all.append(token_ids)
        else:
            for ci in sorted(self._comp_acc.keys()):
                acc = self._comp_acc[ci]
                tlp = acc["top_logprobs"]
                lp_list: List[List[float]] = []
                tid_list: List[List[int]] = []
                for pos in tlp:
                    if not isinstance(pos, dict):
                        lps, tids = _truncate_topk({}, self._n_detect)
                        lp_list.append(lps)
                        tid_list.append(tids)
                        continue
                    d = {}
                    for k, v in pos.items():
                        tid = parse_token_id(k)
                        if tid >= 0:
                            try:
                                d[tid] = float(v)
                            except (TypeError, ValueError):
                                pass
                    lps, tids = _truncate_topk(d, self._n_detect)
                    lp_list.append(lps)
                    tid_list.append(tids)
                logprobs, token_ids = _build_arrays(lp_list, tid_list)
                logprobs_all.append(logprobs)
                token_ids_all.append(token_ids)
        return logprobs_all, token_ids_all


def _entry_token(entry: Any) -> Any:
    if isinstance(entry, dict):
        return entry.get("token")
    return None
