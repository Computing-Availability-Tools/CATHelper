"""异常事件合成：对比相邻快照，计数器增量 → 异常事件流。

- detected_total 每个 (ill_type, model, choice_index) 系列增量 > 0 → 合成独立事件
  （同轮多次增量合成多条独立事件，各带时间戳）。
- requests_total / detection_errors_total 增量计入 DeltaSummary。
- 计数器回绕/实例重启（值变小）→ 基线随快照重置，忽略负增量，不产生虚假事件。
- 对外 ill_type 统一转为字符串名（§5.1，与中间件 parse_ill_type_name 一致）。

模块持有每实例上一份快照；恢复轮询（resume）后首份快照仅作基线（reset）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# ill_type 数字 → 字符串名（中间件 metrics.py: ILL_RARE..ILL_NAN）
ILL_TYPE_NAMES: Dict[int, str] = {
    1: "rare_character",
    2: "garbled",
    3: "repetition",
    4: "nan_value",
}

# 未知 ill_type 的兜底名（配置校验已拒绝未知字符串，此处仅防御指标标签异常）
UNKNOWN_ILL_TYPE = "unknown"


def ill_type_name(ill_type: int) -> str:
    return ILL_TYPE_NAMES.get(int(ill_type), UNKNOWN_ILL_TYPE)


@dataclass(frozen=True)
class AnomalyEvent:
    id: int
    ts: float
    instance: str
    model: str
    ill_type: str
    choice_index: str
    source: str = "live"  # "live" | "imported"


@dataclass
class DeltaSummary:
    """一次轮询的增量口径汇总（驱动实例统计/趋势/全局聚合）。"""

    requests: int = 0
    errors: int = 0
    anomalies_total: int = 0
    by_type: Dict[str, int] = field(default_factory=lambda: {v: 0 for v in ILL_TYPE_NAMES.values()})
    by_model: Dict[str, int] = field(default_factory=dict)
    per_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    events: List[AnomalyEvent] = field(default_factory=list)
    duration: Optional[Dict[str, float]] = None  # mean/p50/p95，随快照带上


class Snapshot:
    """某实例一次轮询解析出的指标快照。"""

    def __init__(
        self,
        instance: str,
        ts: Optional[float] = None,
        requests: int = 0,
        errors: int = 0,
        detected: Optional[Dict[Tuple[int, str, str], int]] = None,
        gauges: Optional[Dict[Tuple[str, str], int]] = None,
        duration: Optional[Dict[str, float]] = None,
    ):
        self.instance = instance
        self.ts = ts if ts is not None else time.time()
        self.requests = requests
        self.errors = errors
        self.detected = detected or {}
        self.gauges = gauges or {}
        self.duration = duration

    @property
    def models(self) -> List[str]:
        models = {m for (_, m, _) in self.detected}
        models.update(m for (_, m) in self.gauges)
        return sorted(models) or ["unknown"]


class EventSynthesizer:
    """维护每实例上一快照，产出增量事件与 DeltaSummary。"""

    def __init__(self) -> None:
        self._prev: Dict[str, Snapshot] = {}
        # 事件 id 由外部（store）分配，避免模块间依赖；默认自增
        self._next_id = 1

    def set_id_source(self, allocer) -> None:
        self._id_allocer = allocer

    def reset(self, instance: str) -> None:
        """暂停/删除/恢复：删除基线，使下一份快照仅作基线、跳过增量。"""
        self._prev.pop(instance, None)

    def clear(self) -> None:
        self._prev.clear()

    def process(self, cur: Snapshot) -> Optional[DeltaSummary]:
        """对比前后快照；无基线（首次/恢复后首份）→ 仅设基线返回 None。"""
        prev = self._prev.get(cur.instance)
        self._prev[cur.instance] = cur
        if prev is None:
            return None
        return self._diff(prev, cur)

    def _diff(self, prev: Snapshot, cur: Snapshot) -> DeltaSummary:
        d = DeltaSummary(requests=max(0, cur.requests - prev.requests),
                         errors=max(0, cur.errors - prev.errors),
                         duration=cur.duration)

        for (ill, model, choice), cnt in cur.detected.items():
            p = prev.detected.get((ill, model, choice), 0)
            delta = cnt - p
            if delta <= 0:
                continue
            name = ill_type_name(ill)
            for _ in range(delta):
                eid = self._alloc_id()
                d.events.append(
                    AnomalyEvent(id=eid, ts=cur.ts, instance=cur.instance,
                                 model=model, ill_type=name, choice_index=str(choice))
                )
            d.anomalies_total += delta
            d.by_type[name] += delta
            d.by_model[model] = d.by_model.get(model, 0) + delta
            pm = d.per_model.setdefault(model, {v: 0 for v in ILL_TYPE_NAMES.values()})
            pm[name] += delta
        return d

    def _alloc_id(self) -> int:
        allocer = getattr(self, "_id_allocer", None)
        if allocer is not None:
            return allocer()
        eid = self._next_id
        self._next_id += 1
        return eid