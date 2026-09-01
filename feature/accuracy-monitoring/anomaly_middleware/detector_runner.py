"""检测运行器（design §4.4 / §6.2）。

多进程并行检测：
- ProcessPoolExecutor + 共享内存零拷贝数据传递。
- 模块级 _worker_init（每进程构造 ILLDetector + 注入 tk2cat）。
- 模块级 _detect_sync（从共享内存零拷贝读取 → 逐候选检测 → 返回结果）。
- 检测任务：fire-and-forget asyncio.create_task，异常全捕获计 error，不影响客户端。
- 进程池崩溃：BrokenProcessPool → 重建 + 计 error + log。
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import shared_memory
from typing import Any, List, Optional, Sequence, Set

import numpy as np

from .logging import get_logger
from .metrics import ILL_NORMAL, Metrics

logger = get_logger()

# 模块级 worker 状态（每进程独立）
_worker_state = {}


def _worker_init(config_path: str, tk2cat, vocab_size: int, topk_n: int):
    """每进程初始化：构造检测器 + 注入词表。"""
    from .detector import ILLDetector

    det = ILLDetector(config_path)
    if tk2cat is not None:
        det.set_vocabulary(tk2cat, vocab_size)
    _worker_state["detector"] = det
    _worker_state["topk_n"] = topk_n


def _detect_sync(metadata: dict):
    """worker 检测入口：从共享内存零拷贝读取 → 逐候选检测 → 返回结果。"""
    shm = shared_memory.SharedMemory(name=metadata["shm_name"])
    try:
        off = metadata["offsets"]
        shapes = metadata["shapes"]
        logprobs = np.ndarray(
            shapes["logprobs"], buffer=shm.buf,
            offset=off["logprobs"], dtype=np.float32,
        )
        token_ids = np.ndarray(
            shapes["token_ids"], buffer=shm.buf,
            offset=off["token_ids"], dtype=np.int32,
        )
        det = _worker_state["detector"]
        topk_n = _worker_state["topk_n"]
        results = []
        for i in range(metadata["num_choices"]):
            n = metadata["choice_lengths"][i]
            res = det.detector(logprobs[i][:n], token_ids[i][:n], topk_n=topk_n)
            results.append([res.is_ill, res.ill_type])
        return results
    finally:
        shm.close()


def _build_shared_memory(
    logprobs_list: List[np.ndarray],
    token_ids_list: List[np.ndarray],
):
    """将 per-choice numpy 数组写入 SharedMemory，返回 (metadata, shm)。"""
    num_choices = len(logprobs_list)
    if num_choices == 0:
        raise ValueError("no choices to detect")

    choice_lengths = [len(lp) for lp in logprobs_list]
    max_tokens = max(choice_lengths) if choice_lengths else 0
    if max_tokens == 0:
        raise ValueError("no tokens to detect")

    topk_n = logprobs_list[0].shape[1] if logprobs_list[0].ndim > 1 else 1

    # 变长候选 padding 到 max_tokens
    logprobs_padded = np.full(
        (num_choices, max_tokens, topk_n), -100.0, dtype=np.float32,
    )
    token_ids_padded = np.zeros(
        (num_choices, max_tokens, topk_n), dtype=np.int32,
    )
    for i, (lp, tid) in enumerate(zip(logprobs_list, token_ids_list)):
        n = choice_lengths[i]
        if n > 0 and lp.ndim > 1:
            cols = min(lp.shape[1], topk_n)
            logprobs_padded[i, :n, :cols] = lp[:, :cols]
            token_ids_padded[i, :n, :cols] = tid[:, :cols]

    # 分配 SharedMemory（对齐 token_ids 起始地址）
    lp_bytes = logprobs_padded.nbytes
    alignment = 16
    lp_aligned = (lp_bytes + alignment - 1) // alignment * alignment
    total_bytes = lp_aligned + token_ids_padded.nbytes

    shm = shared_memory.SharedMemory(create=True, size=total_bytes)

    # 写入数组
    np.ndarray(
        logprobs_padded.shape, buffer=shm.buf, dtype=np.float32,
    )[:] = logprobs_padded
    np.ndarray(
        token_ids_padded.shape, buffer=shm.buf,
        offset=lp_aligned, dtype=np.int32,
    )[:] = token_ids_padded

    metadata = {
        "shm_name": shm.name,
        "num_choices": num_choices,
        "topk_n": topk_n,
        "choice_lengths": choice_lengths,
        "shapes": {
            "logprobs": logprobs_padded.shape,
            "token_ids": token_ids_padded.shape,
        },
        "offsets": {
            "logprobs": 0,
            "token_ids": lp_aligned,
        },
    }
    return metadata, shm


class DetectorRunner:
    def __init__(
        self,
        config_path: str,
        max_workers: int = 4,
        topk_n: Optional[int] = None,
        tk2cat=None,
        vocab_size: Optional[int] = None,
    ) -> None:
        self._config_path = config_path
        self._topk_n = topk_n
        self._tk2cat = tk2cat
        self._vocab_size = vocab_size
        self._max_workers = max(1, max_workers)
        self._executor = ProcessPoolExecutor(
            max_workers=self._max_workers,
            initializer=_worker_init,
            initargs=(config_path, tk2cat, vocab_size, topk_n),
        )

    async def run_async(
        self,
        logprobs_list: List[np.ndarray],
        token_ids_list: List[np.ndarray],
    ):
        metadata, shm = _build_shared_memory(logprobs_list, token_ids_list)
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, _detect_sync, metadata,
            )
        except BrokenProcessPool:
            self._rebuild_pool()
            raise
        finally:
            shm.close()
            shm.unlink()

    def _rebuild_pool(self) -> None:
        """进程池崩溃后重建。"""
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self._executor = ProcessPoolExecutor(
            max_workers=self._max_workers,
            initializer=_worker_init,
            initargs=(
                self._config_path, self._tk2cat,
                self._vocab_size, self._topk_n,
            ),
        )
        logger.warning("检测进程池已重建")

    def shutdown(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def schedule_detection(
    runner: DetectorRunner,
    logprobs_list: List[np.ndarray],
    token_ids_list: List[np.ndarray],
    *,
    request_id: str,
    model: str,
    metrics: Metrics,
    pending_tasks: Set,
    anomaly_store: Any = None,
    prompt: Any = None,
    texts: Optional[Sequence] = None,
    text_tokenids: Optional[Sequence[Sequence[int]]] = None,
    reasoning_contents: Optional[Sequence[Optional[str]]] = None,
) -> asyncio.Task:
    """fire-and-forget 检测任务。异常全捕获计 error，不影响客户端。

    若传入 anomaly_store，则对每个异常候选保存现场（编号 + pickle 落盘）
    并经 metrics 暴露最新异常编号/时间戳。
    """

    async def _run() -> None:
        results = None
        with metrics.detection_duration.time():
            try:
                results = await runner.run_async(logprobs_list, token_ids_list)
                metrics.record_detection(results, model)
            except BrokenProcessPool:
                runner._rebuild_pool()
                logger.error(
                    "检测进程池崩溃, 已重建, request_id=%s 该请求检测失败",
                    request_id,
                )
                metrics.record_error()
            except Exception as exc:
                logger.error(
                    "检测失败 request_id=%s model=%s: %s",
                    request_id, model, exc,
                )
                metrics.record_error()
        # 异常信息保存（不计入检测耗时；保存失败不影响客户端/检测/后续请求）
        if results is not None and anomaly_store is not None:
            try:
                ts = time.time()
                for idx, res in enumerate(results):
                    if not res or len(res) < 2:
                        continue
                    is_ill = bool(res[0])
                    ill_type = int(res[1])
                    if not (is_ill and ill_type != ILL_NORMAL):
                        continue  # 仅异常候选
                    lp = logprobs_list[idx] if idx < len(logprobs_list) else None
                    tid = token_ids_list[idx] if idx < len(token_ids_list) else None
                    record = {
                        "time": ts,
                        "prompt": prompt,
                        "ill_type": ill_type,
                        "topk_logprobs": lp.tolist() if lp is not None else [],
                        "tokens_ids": tid.tolist() if tid is not None else [],
                        "text": texts[idx] if texts and idx < len(texts) else None,
                        "text_tokenid": text_tokenids[idx] if text_tokenids and idx < len(text_tokenids) else [],
                        "reasoning_content": reasoning_contents[idx] if reasoning_contents and idx < len(reasoning_contents) else None,
                        "model_name": model,
                    }
                    aid = await anomaly_store.save(record)
                    metrics.record_anomaly(aid, ts, model)
            except Exception as exc:
                logger.error(
                    "异常信息保存失败 request_id=%s: %s", request_id, exc,
                )

    task = asyncio.create_task(_run())
    pending_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        pending_tasks.discard(t)
        try:
            t.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    task.add_done_callback(_done)
    return task
