"""collector：轮询各实例 `/anomaly/metrics` + 解析 Prometheus 文本 + 快照 → 事件流。

- 每个活跃实例持有独立 asyncio 轮询 task；新增/恢复创建（建基线），暂停取消
  （保留数据），删除取消并清除数据；单实例慢不阻塞整体。
- 解析复用 prometheus_client.parser；个别序列损坏跳过，其余照常。
- 失败/超时/非 200 → 实例标记离线，快照保留上次值，不影响其他实例。
- 计数器回绕/实例重启（值变小）→ 事件合成忽略负增量。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import events as events_mod
from .alerts import AlertEngine
from .config import ILL_TYPES, PollConfig
from .events import DeltaSummary, EventSynthesizer, Snapshot
from .store import Store, TrendPoint

logger = logging.getLogger("webui.collector")

REQUESTS_METRIC = "vllm_anomaly_requests_total"
DETECTED_METRIC = "vllm_anomaly_detected_total"
ERRORS_METRIC = "vllm_anomaly_detection_errors_total"
DURATION_METRIC = "vllm_anomaly_detection_duration_seconds"

# gauge 指标名 → 异常类型名
GAUGE_NAMES = {
    "vllm_anomaly_last_rare_character": "rare_character",
    "vllm_anomaly_last_garbled": "garbled",
    "vllm_anomaly_last_repetition": "repetition",
    "vllm_anomaly_last_nan_value": "nan_value",
}


class MetricsParseError(Exception):
    """Prometheus 文本整体不可解析。"""


class PollError(Exception):
    """实例轮询失败（超时/非 200 等）。"""


def _canonical_family_name(full_name: str) -> str:
    """prometheus_client 解析器对 counter 族返回去掉 `_total` 后缀的规范名。

    较新版本（>=0.16）family.name 为规范名（如 `vllm_anomaly_requests`），
    而样本 s.name 仍为完整名（`vllm_anomaly_requests_total`）。
    统一按「完整名 或 规范名」匹配，兼容两代解析器。
    """
    return full_name[:-6] if full_name.endswith("_total") else full_name


def _family_matches(family_name: str, full_name: str) -> bool:
    return family_name == full_name or family_name == _canonical_family_name(full_name)


def _histogram_quantile(buckets: List[Tuple[float, float]], q: float, count: int) -> float:
    """基于累积桶线性插值求分位数（Prometheus histogram_quantile 近似）。"""
    if count <= 0 or not buckets:
        return 0.0
    rank = q * count
    last_finite: Optional[float] = None
    prev_le: Optional[float] = None
    prev_cum = 0.0
    for le, c in buckets:
        if math.isinf(le):
            continue
        last_finite = le
        if c >= rank:
            if prev_le is None or c == prev_cum:
                return le
            return prev_le + (le - prev_le) * ((rank - prev_cum) / (c - prev_cum))
        prev_le, prev_cum = le, c
    return last_finite if last_finite is not None else 0.0


def _histogram_stats(samples: Iterable[Any]) -> Dict[str, float]:
    """从直方图样本计算 count/sum/mean/p50/p95。"""
    buckets: List[Tuple[float, float]] = []
    total = 0.0
    total_sum = 0.0
    for s in samples:
        nm = s.name
        le = s.labels.get("le") if s.labels else None
        try:
            if nm.endswith("_bucket") and le is not None:
                le_val = float("inf") if le == "+Inf" else float(le)
                buckets.append((le_val, float(s.value)))
            elif nm.endswith("_sum"):
                total_sum = float(s.value)
            elif nm.endswith("_count"):
                total = float(s.value)
        except (TypeError, ValueError):
            continue
    buckets.sort(key=lambda x: x[0])
    count = int(total)
    mean = total_sum / count if count else 0.0
    return {
        "count": count,
        "sum": total_sum,
        "mean": mean,
        "p50": _histogram_quantile(buckets, 0.50, count),
        "p95": _histogram_quantile(buckets, 0.95, count),
    }


def parse_metrics_text(text: str) -> Dict[str, Any]:
    """解析 Prometheus 文本为计数器/仪表盘/直方图快照字典。

    返回 {requests, errors, detected{(ill,model,choice):n}, gauges{(gauge,model):0/1},
    hist_stats{count,sum,mean,p50,p95} | None}。
    个别序列损坏 → 跳过该序列记日志；整体不可解析 → 抛 MetricsParseError。
    """
    from prometheus_client.parser import text_string_to_metric_families

    requests = 0
    errors = 0
    detected: Dict[Tuple[int, str, str], int] = {}
    gauges: Dict[Tuple[str, str], int] = {}
    hist_samples: List[Any] = []

    try:
        families = text_string_to_metric_families(text)
        for family in families:
            try:
                name = family.name
                if _family_matches(name, REQUESTS_METRIC):
                    for s in family.samples:
                        requests = int(float(s.value))
                        break
                elif _family_matches(name, ERRORS_METRIC):
                    for s in family.samples:
                        errors = int(float(s.value))
                        break
                elif _family_matches(name, DETECTED_METRIC):
                    for s in family.samples:
                        try:
                            ill = int(s.labels["ill_type"])
                            model = s.labels["model"]
                            choice = s.labels["choice_index"]
                            detected[(ill, model, choice)] = int(float(s.value))
                        except (KeyError, TypeError, ValueError) as exc:
                            logger.warning("跳过损坏的 detected 序列 %s: %s", s.name, exc)
                elif name in GAUGE_NAMES:
                    for s in family.samples:
                        try:
                            model = s.labels["model"]
                            gauges[(GAUGE_NAMES[name], model)] = int(float(s.value))
                        except (KeyError, TypeError, ValueError) as exc:
                            logger.warning("跳过损坏的 gauge 序列 %s: %s", s.name, exc)
                elif name == DURATION_METRIC:
                    hist_samples.extend(family.samples)
            except Exception as exc:  # noqa: BLE001 —— 个别 family 损坏不拖垮整体
                logger.warning("跳过指标 family %s: %s", getattr(family, "name", "?"), exc)
    except Exception as exc:  # noqa: BLE001 —— 整体文本不可解析
        raise MetricsParseError(f"Prometheus 文本解析失败: {exc}") from exc

    hist_stats = _histogram_stats(hist_samples) if hist_samples else None
    return {
        "requests": requests,
        "errors": errors,
        "detected": detected,
        "gauges": gauges,
        "hist_stats": hist_stats,
    }


class Collector:
    """轮询调度 + 快照解析 + 事件/趋势/告警落盘 + 轮询 task 生命周期管理。"""

    def __init__(
        self,
        *,
        store: Store,
        synthesizer: EventSynthesizer,
        alerts: AlertEngine,
        poll_cfg: PollConfig,
        client_factory=None,
    ) -> None:
        self._store = store
        self._synth = synthesizer
        self._alerts = alerts
        self._poll = poll_cfg
        self._client_factory = client_factory or _default_client_factory
        self._client = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._instances: Dict[str, Any] = {}  # name -> InstanceConfig

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._client is None:
            self._client = self._client_factory(self._poll.http_timeout_seconds)

    async def shutdown(self) -> None:
        for name, t in list(self._tasks.items()):
            t.cancel()
            self._tasks.pop(name, None)
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # 实例 apply（统一入口；幂等 no-op）
    # ------------------------------------------------------------------ #
    def apply_instances(self, instances: List[Any]) -> None:
        """desired-state 收敛：增/删/暂停/恢复/URL 变更。重复应用相同列表为幂等 no-op。"""
        if self._client is None:
            self.start()
        new_map = {i.name: i for i in instances}

        # 删除：取消 task + 清除数据
        for name in [n for n in self._instances if n not in new_map]:
            self._teardown_task(name, purge=True)

        for name, inst in new_map.items():
            prev = self._instances.get(name)
            if inst.paused:
                if name in self._tasks:
                    self._teardown_task(name, purge=False)
                self._store.set_state(name, "paused")
            else:
                reconfigured = prev is not None and prev.url != inst.url
                if name not in self._tasks or reconfigured:
                    if name in self._tasks:
                        self._teardown_task(name, purge=False)
                    self._synth.reset(name)  # 首次/恢复/重配：下一份快照仅作基线
                    self._store.set_state(name, "offline")
                    self._tasks[name] = asyncio.create_task(self._poll_loop(inst))
                self._store.set_url(name, inst.url)
        self._instances = new_map

    def _teardown_task(self, name: str, purge: bool) -> None:
        t = self._tasks.pop(name, None)
        if t is not None:
            t.cancel()
        self._synth.reset(name)
        if purge:
            self._store.purge_instance(name)
        elif name in self._instances:
            pass

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #
    async def poll_once(self, name: str) -> Optional[DeltaSummary]:
        """同步执行一次轮询（供测试）。返回 DeltaSummary 或 None（基线轮）。"""
        inst = self._instances.get(name)
        if inst is None:
            return None
        return await self._poll_once(inst)

    async def _poll_loop(self, inst) -> None:
        while True:
            try:
                await self._poll_once(inst)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 轮询失败绝不抛到主流程
                logger.warning("实例 %s 轮询失败: %s", inst.name, exc)
                self._store.set_state(inst.name, "offline")
            await asyncio.sleep(self._poll.interval_seconds)

    async def _poll_once(self, inst) -> Optional[DeltaSummary]:
        url = inst.url.rstrip("/") + "/anomaly/metrics"
        resp = await self._client.get(url)
        if resp.status_code != 200:
            raise PollError(f"实例 {inst.name} 返回 HTTP {resp.status_code}")
        try:
            parsed = parse_metrics_text(resp.text)
        except MetricsParseError as exc:
            raise PollError(f"实例 {inst.name} 指标解析失败: {exc}") from exc

        snapshot = Snapshot(
            instance=inst.name,
            requests=parsed["requests"],
            errors=parsed["errors"],
            detected=parsed["detected"],
            gauges=parsed["gauges"],
            duration=parsed["hist_stats"],
        )
        self._store.set_state(inst.name, "online")
        # 记录 metrics 观测到的模型名（含正常请求；供导入模型对应校验使用）
        self._store.observe_models(inst.name, snapshot.models)

        delta = self._synth.process(snapshot)
        if delta is None:
            return None
        self._store.record_delta(inst.name, delta)
        self._record_trends(snapshot, delta)
        for e in delta.events:
            self._alerts.ingest(e)
        return delta

    def _record_trends(self, snapshot: Snapshot, delta: DeltaSummary) -> None:
        models = snapshot.models
        if not models:
            models = ["unknown"]
        for m in models:
            pm = delta.per_model.get(m)
            point = TrendPoint(
                ts=snapshot.ts,
                requests=float(delta.requests),
                errors=float(delta.errors),
                anomalies=float(sum(pm.values())) if pm else 0.0,
                by_type={t: float(pm.get(t, 0)) if pm else 0.0 for t in ILL_TYPES},
            )
            self._store.record_trend(snapshot.instance, m, point)


def _default_client_factory(timeout: float):
    import httpx

    return httpx.AsyncClient(timeout=timeout)