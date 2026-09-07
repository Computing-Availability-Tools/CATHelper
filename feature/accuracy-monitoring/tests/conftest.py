"""pytest fixtures：httpx ASGI 客户端工厂 + 检测任务 drain 助手。"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

# 确保父目录在 sys.path（包可导入）；tests 目录由 pytest 自动加入
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from anomaly_middleware import AnomalyMiddleware  # noqa: E402
from anomaly_middleware.env import PluginConfig, resolve_config_path  # noqa: E402
from anomaly_middleware.detector_runner import DetectorRunner  # noqa: E402
from _helpers import FakeVLLM  # noqa: E402


@pytest.fixture
async def client_factory():
    """返回工厂函数 make(response_fn, **cfg) -> (client, fake, mw)。

    用 mw.config 直接覆盖，避免依赖 env；每个 mw 独立 runner。
    """
    created_clients = []
    created_mws = []

    def _make(
        response_fn,
        *,
        top_logprobs: int = 20,
        monitor_rate: float = 1.0,
        enabled: bool = True,
        workers: int = 1,
        metrics_path: str = "/anomaly/metrics",
        config_path: str = "/anomaly/config",
        real_runner: bool = False,
    ):
        fake = FakeVLLM(response_fn)
        # 构造期临时 enabled=False 跳过重活（tokenizer/runner/detector.yaml 硬依赖），
        # 再覆盖 config 为目标状态；_resolver/_anomaly_store 留 None，
        # 由各 e2e 用例按需 install_fake_resolver / 注入 runner（spec §2.13）。
        old = os.environ.get("VLLM_ANOMALY_ENABLED")
        os.environ["VLLM_ANOMALY_ENABLED"] = "0"
        try:
            mw = AnomalyMiddleware(fake)
        finally:
            if old is None:
                os.environ.pop("VLLM_ANOMALY_ENABLED", None)
            else:
                os.environ["VLLM_ANOMALY_ENABLED"] = old
        mw.config = PluginConfig(
            enabled=enabled,
            top_logprobs=top_logprobs,
            metrics_path=metrics_path,
            monitor_rate=monitor_rate,
            detector_workers=workers,
            config_path=config_path,
        )
        mw._monitor_rate = mw.config.monitor_rate
        mw.metrics.set_monitor_rate(mw._monitor_rate)
        mw._resolver = None
        mw._anomaly_store = None
        if real_runner:
            # 真实 DetectorRunner（vendored detector.yaml 可用）；检测在子进程跑，
            # max_workers=1 串行，满足 e2e 检测断言（spec §2.5 §2.7）。
            mw._runner = DetectorRunner(
                resolve_config_path(),
                max_workers=workers,
                topk_n=top_logprobs,
            )
        else:
            mw._runner = None
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mw), base_url="http://test"
        )
        created_clients.append(client)
        created_mws.append(mw)
        return client, fake, mw

    yield _make
    for c in created_clients:
        await c.aclose()
    for mw in created_mws:
        mw.shutdown()


async def drain(mw, timeout: float = 10.0) -> None:
    """等待所有 fire-and-forget 检测任务完成（用于 e2e 断言 metrics）。"""
    tasks = list(mw._pending_tasks)
    if not tasks:
        return
    await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout
    )
