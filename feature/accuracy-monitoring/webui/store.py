"""内存环形缓冲 + 分层趋势时序点（纯数据，无 IO，单事件循环内访问无需加锁）。

- events / alerts：环形缓冲（满按时间淘汰最旧）。
- trends：原始点（轮询粒度，保留 raw_trend_window_seconds）+ 分钟聚合桶
  （保留 trend_horizon_seconds）；新原始点落盘同步累加进当前分钟桶；均按时间淘汰。
- `purge_instance(name)`：清除该实例的事件/告警/趋势/统计，不影响其它实例。
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Deque, List, Optional, Tuple

from .config import ILL_TYPES, StoreConfig
from .events import AnomalyEvent

# 时间单位：秒
NOW = time.time


@dataclass
class TrendPoint:
    """一个时序点（增量口径）。by_type 为 4 类异常名 → 事件数。"""

    ts: float
    requests: float = 0.0
    anomalies: float = 0.0
    errors: float = 0.0
    by_type: Dict[str, float] = field(default_factory=lambda: {t: 0.0 for t in ILL_TYPES})

    def add(self, other: "TrendPoint") -> None:
        self.requests += other.requests
        self.anomalies += other.anomalies
        self.errors += other.errors
        for t in ILL_TYPES:
            self.by_type[t] += other.by_type[t]

    def to_series(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ts": self.ts, "anomalies": self.anomalies}
        out.update(self.by_type)
        return out


@dataclass
class TrendEvent:
    """单个异常事件的趋势记录（阶梯点来源）。

    source="live" 来自轮询增量；source="imported" 来自历史 pickle 导入。
    """

    ts: float
    instance: str
    model: str
    ill_type: str
    source: str = "live"


@dataclass
class ImportedSummary:
    """单实例导入的异常计数快照（用于再次导入时回滚旧值）。"""

    anomalies: int = 0
    by_type: Dict[str, int] = field(default_factory=lambda: {t: 0 for t in ILL_TYPES})
    by_model: Dict[str, int] = field(default_factory=dict)


class TrendSeries:
    """单实例序列的原始点 + 分钟桶双层结构，按时间淘汰。"""

    def __init__(self, cfg: StoreConfig) -> None:
        self._cfg = cfg
        self._raw: Deque[TrendPoint] = deque()
        self._buckets: Dict[float, TrendPoint] = {}

    def append(self, point: TrendPoint, now: Optional[float] = None) -> None:
        now = now if now is not None else NOW()
        self._raw.append(point)
        self._prune_raw(now)
        bkey = math.floor(point.ts / self._cfg.trend_bucket_seconds) * self._cfg.trend_bucket_seconds
        b = self._buckets.get(bkey)
        if b is None:
            b = TrendPoint(ts=float(bkey))
            self._buckets[bkey] = b
        b.add(point)
        self._prune_buckets(now)

    def _prune_raw(self, now: float) -> None:
        cutoff = now - self._cfg.raw_trend_window_seconds
        while self._raw and self._raw[0].ts < cutoff:
            self._raw.popleft()

    def _prune_buckets(self, now: float) -> None:
        cutoff = now - self._cfg.trend_horizon_seconds
        for k in [k for k in self._buckets if k < cutoff]:
            del self._buckets[k]

    def raw_points(self) -> List[TrendPoint]:
        return list(self._raw)

    def bucket_points(self) -> List[TrendPoint]:
        return list(self._buckets.values())


@dataclass
class InstanceStats:
    name: str
    state: str = "paused"  # online | offline | paused
    url: str = ""
    requests: int = 0
    errors: int = 0
    anomalies: int = 0
    by_type: Dict[str, int] = field(default_factory=lambda: {t: 0 for t in ILL_TYPES})
    by_model: Dict[str, int] = field(default_factory=dict)
    last_event: Optional[Tuple[str, float]] = None  # (ill_type, ts)
    detection_duration: Optional[Dict[str, float]] = None  # mean/p50/p95


class RingBuffer:
    """按容量淘汰最旧的环形缓冲。"""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._items: Deque[Any] = deque(maxlen=capacity)

    def append(self, item: Any) -> None:
        self._items.append(item)

    def latest(self, limit: int) -> List[Any]:
        """返回最近 limit 条，时间倒序（最新在前）。"""
        items = list(self._items)
        return items[::-1][:limit]

    def remove_where(self, pred) -> None:
        keep = [it for it in self._items if not pred(it)]
        self._items = deque(keep, maxlen=self.capacity)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


class Store:
    """监控数据中枢：事件/告警环形缓冲、分层趋势、实例统计与全局聚合。"""

    def __init__(self, cfg: StoreConfig) -> None:
        self.cfg = cfg
        self.events = RingBuffer(cfg.event_capacity)
        self.alerts = RingBuffer(cfg.alert_capacity)
        self._trends: Dict[str, TrendSeries] = {}  # key=(instance, model)
        self._trend_events: Deque[TrendEvent] = deque()  # 阶梯趋势事件（按时间裁剪）
        self._imported: Dict[str, ImportedSummary] = {}  # 导入计数快照（回滚用）
        self._stats: Dict[str, InstanceStats] = {}
        self._next_event_id = 1
        self._next_alert_id = 1
        self.detection_duration: Optional[Dict[str, float]] = None

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def alloc_event_id(self) -> int:
        eid = self._next_event_id
        self._next_event_id += 1
        return eid

    def alloc_alert_id(self) -> int:
        aid = self._next_alert_id
        self._next_alert_id += 1
        return aid

    def add_event(self, event: AnomalyEvent) -> None:
        self.events.append(event)

    def add_alert(self, alert: Any) -> None:
        self.alerts.append(alert)

    def recent_events(self, limit: int = 50) -> List[AnomalyEvent]:
        return self.events.latest(limit)

    def recent_alerts(self, limit: int = 50) -> List[Any]:
        return self.alerts.latest(limit)

    def events_for(self, instance: str, limit: int = 50) -> List[AnomalyEvent]:
        """按实例过滤的最近事件（时间倒序，最新在前）。"""
        out: List[AnomalyEvent] = []
        for e in reversed(list(self.events)):
            if e.instance == instance:
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------------------------------ #
    # 实例统计 + 全局聚合
    # ------------------------------------------------------------------ #
    def _stats_for(self, name: str, url: str = "") -> InstanceStats:
        st = self._stats.get(name)
        if st is None:
            st = InstanceStats(name=name, url=url)
            self._stats[name] = st
        return st

    def set_state(self, name: str, state: str) -> None:
        st = self._stats_for(name)
        st.state = state

    def set_url(self, name: str, url: str) -> None:
        st = self._stats_for(name)
        st.url = url

    def record_delta(self, instance: str, delta, now: Optional[float] = None) -> None:
        """按 DeltaSummary 更新实例统计、事件环形缓冲与全局聚合。"""
        st = self._stats_for(instance)
        st.requests += delta.requests
        st.errors += delta.errors
        for e in delta.events:
            self.events.append(e)
            st.anomalies += 1
            st.by_type[e.ill_type] += 1
            st.by_model[e.model] = st.by_model.get(e.model, 0) + 1
            st.last_event = (e.ill_type, e.ts)
            # 同步追加到阶梯趋势事件（source=live）
            self._trend_events.append(
                TrendEvent(ts=e.ts, instance=instance, model=e.model,
                           ill_type=e.ill_type, source="live")
            )
        self._prune_trend_events(now)

        if delta.duration is not None:
            self.detection_duration = delta.duration

    def set_detection_duration(self, instance: str, dur: Optional[Dict[str, float]]) -> None:
        st = self._stats_for(instance)
        st.detection_duration = dur

    def instance_stats(self, name: str) -> Optional[InstanceStats]:
        return self._stats.get(name)

    def all_instance_stats(self) -> List[InstanceStats]:
        return list(self._stats.values())

    # ------------------------------------------------------------------ #
    # 分层趋势
    # ------------------------------------------------------------------ #
    def record_trend(
        self, instance: str, model: str, point: TrendPoint, now: Optional[float] = None
    ) -> None:
        key = (instance, model)
        s = self._trends.get(key)
        if s is None:
            s = TrendSeries(self.cfg)
            self._trends[key] = s
        s.append(point, now)

    def raw_points_for(self, instance: str, model: str) -> List[TrendPoint]:
        s = self._trends.get((instance, model))
        return s.raw_points() if s is not None else []

    def bucket_points_for(self, instance: str, model: str) -> List[TrendPoint]:
        s = self._trends.get((instance, model))
        return s.bucket_points() if s is not None else []

    def trend_series_count(self) -> int:
        return len(self._trends)

    def query_trends(
        self,
        window_seconds: int,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """聚合所有 (instance, model) 序列为全局 4 类异常时序。

        窗口规则：最近 raw_trend_window_seconds 内用原始点；更早用分钟桶
        （桶整体落在原始区边界之前，避免重复计数）。按 ts 升序。
        """
        now = now if now is not None else NOW()
        boundary = now - self.cfg.raw_trend_window_seconds
        start = now - window_seconds
        bucket_sec = self.cfg.trend_bucket_seconds

        agg: Dict[float, Dict[str, float]] = {}
        for key, s in self._trends.items():
            for p in s.raw_points():
                if p.ts >= start and p.ts >= boundary:
                    self._merge(agg, p)
            for p in s.bucket_points():
                if (p.ts + bucket_sec) <= boundary and p.ts >= start:
                    self._merge(agg, p)
        if not agg:
            return []
        return [self._make_point(ts, v) for ts, v in sorted(agg.items())]

    @staticmethod
    def _merge(agg: Dict[float, Dict[str, float]], p: TrendPoint) -> None:
        entry = agg.get(p.ts)
        if entry is None:
            entry = {"requests": 0.0, "anomalies": 0.0}
            for t in ILL_TYPES:
                entry[t] = 0.0
            agg[p.ts] = entry
        entry["requests"] += p.requests
        entry["anomalies"] += p.anomalies
        for t in ILL_TYPES:
            entry[t] += p.by_type[t]

    @staticmethod
    def _make_point(ts: float, v: Dict[str, float]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ts": ts}
        for t in ILL_TYPES:
            out[t] = v[t]
        return out

    def query_trends_for_instance(
        self,
        instance: str,
        window_seconds: int,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """聚合单实例所有模型的 4 类异常时序（规则同 query_trends）。"""
        now = now if now is not None else NOW()
        boundary = now - self.cfg.raw_trend_window_seconds
        start = now - window_seconds
        bucket_sec = self.cfg.trend_bucket_seconds

        agg: Dict[float, Dict[str, float]] = {}
        for (inst, _model), s in self._trends.items():
            if inst != instance:
                continue
            for p in s.raw_points():
                if p.ts >= start and p.ts >= boundary:
                    self._merge(agg, p)
            for p in s.bucket_points():
                if (p.ts + bucket_sec) <= boundary and p.ts >= start:
                    self._merge(agg, p)
        if not agg:
            return []
        return [self._make_point(ts, v) for ts, v in sorted(agg.items())]

    # ------------------------------------------------------------------ #
    # 阶梯趋势事件（异常事件级，供 /api/trends 阶梯图消费）
    # ------------------------------------------------------------------ #
    def _prune_trend_events(self, now: Optional[float] = None) -> None:
        """按 trend_horizon_seconds 淘汰过旧的阶梯趋势事件。"""
        now = now if now is not None else NOW()
        cutoff = now - self.cfg.trend_horizon_seconds
        while self._trend_events and self._trend_events[0].ts < cutoff:
            self._trend_events.popleft()

    def append_trend_events(
        self, events: List["TrendEvent"], now: Optional[float] = None
    ) -> None:
        """批量追加阶梯趋势事件（导入用，source 应为 "imported"）。"""
        for e in events:
            self._trend_events.append(e)
        self._prune_trend_events(now)

    def clear_imported_trend_events(self, instance: str) -> int:
        """删除指定实例的导入趋势事件（覆盖语义）；返回清除条数。"""
        before = len(self._trend_events)
        keep = deque(
            (e for e in self._trend_events
             if not (e.instance == instance and e.source == "imported")),
            maxlen=None,
        )
        self._trend_events = keep
        return before - len(self._trend_events)

    def query_trend_events(
        self,
        window_seconds: int,
        instance: Optional[str] = None,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """查询窗口内异常事件，按 ts 升序累计计数，返回阶梯点列表。

        每点含 ts/cumulative/model/ill_type/instance/source。累计从 1 开始（窗口内从 0 增长）。
        instance=None 表示所有实例（看板）；指定则过滤该实例（详情页）。
        """
        now = now if now is not None else NOW()
        start = now - window_seconds
        evs = [e for e in self._trend_events if e.ts >= start]
        if instance is not None:
            evs = [e for e in evs if e.instance == instance]
        evs.sort(key=lambda e: e.ts)
        points: List[Dict[str, Any]] = []
        cum = 0
        for e in evs:
            cum += 1
            points.append({
                "ts": e.ts,
                "cumulative": cum,
                "model": e.model,
                "ill_type": e.ill_type,
                "instance": e.instance,
                "source": e.source,
            })
        return points

    # ------------------------------------------------------------------ #
    # 历史导入：同步到事件/统计/趋势/全局 KPI（覆盖语义）
    # ------------------------------------------------------------------ #
    def apply_import(
        self,
        instance: str,
        anomaly_events: List[AnomalyEvent],
        trend_events: List["TrendEvent"],
        now: Optional[float] = None,
    ) -> int:
        """将导入的异常事件应用到所有模块（事件列表、实例统计、趋势、全局 KPI）。

        覆盖语义：先回滚该实例上一次导入的计数与事件，再追加新数据。
        返回被覆盖清除的旧趋势事件条数。
        """
        # 1. 回滚上一次导入
        old = self._imported.get(instance)
        if old is not None:
            st = self._stats.get(instance)
            if st is not None:
                st.anomalies = max(0, st.anomalies - old.anomalies)
                for t, n in old.by_type.items():
                    st.by_type[t] = max(0, st.by_type[t] - n)
                for m, n in old.by_model.items():
                    base = st.by_model.get(m, 0)
                    if base <= n:
                        st.by_model.pop(m, None)
                    else:
                        st.by_model[m] = base - n
            # 移除旧导入事件
            self.events.remove_where(
                lambda e: e.instance == instance
                and getattr(e, "source", "live") == "imported"
            )

        # 2. 清旧导入趋势事件
        cleared = self.clear_imported_trend_events(instance)

        # 3. 追加新事件到环形缓冲
        for e in anomaly_events:
            self.events.append(e)

        # 4. 追加新趋势事件
        self.append_trend_events(trend_events, now=now)

        # 5. 更新实例统计与全局 KPI
        st = self._stats_for(instance)
        imp = ImportedSummary()
        for e in anomaly_events:
            st.anomalies += 1
            st.by_type[e.ill_type] += 1
            st.by_model[e.model] = st.by_model.get(e.model, 0) + 1
            st.last_event = (e.ill_type, e.ts)
            imp.anomalies += 1
            imp.by_type[e.ill_type] += 1
            imp.by_model[e.model] = imp.by_model.get(e.model, 0) + 1

        self._imported[instance] = imp
        return cleared

    # ------------------------------------------------------------------ #
    # purge
    # ------------------------------------------------------------------ #
    def purge_instance(self, name: str) -> None:
        """删除实例 → 清除该实例事件/告警/趋势/统计/导入快照；不影响其它实例。"""
        self.events.remove_where(lambda e: e.instance == name)
        self.alerts.remove_where(lambda a: getattr(a, "instance", None) == name)
        for key in [k for k in self._trends if k[0] == name]:
            del self._trends[key]
        self._trend_events = deque(
            (e for e in self._trend_events if e.instance != name)
        )
        self._imported.pop(name, None)
        self._stats.pop(name, None)

    def summary(self) -> Dict[str, Any]:
        stats = list(self._stats.values())
        requests = sum(st.requests for st in stats)
        anomalies = sum(st.anomalies for st in stats)
        errors = sum(st.errors for st in stats)
        by_type: Dict[str, int] = {t: 0 for t in ILL_TYPES}
        for st in stats:
            for t, n in st.by_type.items():
                by_type[t] += n
        by_instance: Dict[str, int] = {
            st.name: st.anomalies for st in stats if st.anomalies > 0
        }
        online = sum(1 for st in stats if st.state == "online")
        offline = sum(1 for st in stats if st.state == "offline")
        paused = sum(1 for st in stats if st.state == "paused")
        rate = anomalies / requests if requests else 0.0
        return {
            "requests": requests,
            "anomalies": anomalies,
            "anomaly_rate": round(rate, 6),
            "errors": errors,
            "by_type": by_type,
            "by_instance": by_instance,
            "instances": {"total": len(stats), "online": online, "offline": offline, "paused": paused},
            "detection_duration": self.detection_duration,
            "updated_at": NOW(),
        }