"""store 单元测试：环形缓冲、purge_instance、分层趋势（原始点/分钟桶）与全局聚合。"""
from __future__ import annotations

import time

from webui.config import StoreConfig
from webui.events import AnomalyEvent
from webui.store import RingBuffer, Store, TrendPoint


def _store(**kw) -> Store:
    kw.setdefault("event_capacity", 1000)
    kw.setdefault("alert_capacity", 200)
    cfg = StoreConfig(**kw)
    return Store(cfg)


def test_ring_buffer_capacity_eviction():
    buf = RingBuffer(3)
    for i in range(5):
        buf.append(i)
    assert list(buf) == [2, 3, 4]
    assert buf.latest(2) == [4, 3]
    assert buf.latest(10) == [4, 3, 2]


def test_ring_buffer_remove_where():
    buf = RingBuffer(10)
    for i in range(6):
        buf.append({"v": i, "instance": "a" if i % 2 == 0 else "b"})
    buf.remove_where(lambda x: x["instance"] == "a")
    assert all(x["instance"] == "b" for x in buf)


def test_purge_instance_clears_only_that_instance():
    st = _store()
    now = time.time()
    st.record_delta("a", _delta(1, [("garbled", "a", "m", "0")]))
    st.record_delta("b", _delta(1, [("rare_character", "b", "m", "1")]))
    st.record_trend("a", "m", TrendPoint(ts=now, anomalies=1.0), now=now)
    st.record_trend("b", "m", TrendPoint(ts=now, anomalies=1.0), now=now)
    st.add_alert(_alert(1, "r", "a", "m", "garbled"))

    st.purge_instance("a")
    assert st.recent_events(10) == [] or all(e.instance == "b" for e in st.recent_events(10))
    assert all(a.instance == "b" for a in st.recent_alerts(10))
    assert st.raw_points_for("a", "m") == [] and st.bucket_points_for("a", "m") == []
    assert st.raw_points_for("b", "m") or st.bucket_points_for("b", "m")
    assert st.instance_stats("a") is None
    assert st.instance_stats("b") is not None


def _delta(requests, event_specs):
    from webui.events import DeltaSummary

    d = DeltaSummary(requests=requests)
    eid = 0
    for ill, instance, model, choice in event_specs:
        eid += 1
        e = AnomalyEvent(id=eid, ts=time.time(), instance=instance, model=model,
                         ill_type=ill, choice_index=choice)
        d.events.append(e)
        d.anomalies_total += 1
        d.by_type[ill] = d.by_type.get(ill, 0) + 1
        d.by_model[model] = d.by_model.get(model, 0) + 1
    return d


def _alert(aid, rule, instance, model, ill, ts: float = None):
    from webui.alerts import Alert

    return Alert(id=aid, rule_name=rule, ts=ts if ts is not None else time.time(),
                 instance=instance, model=model, ill_type=ill, count=1)


def test_global_aggregation():
    st = _store()
    st.record_delta("a", _delta(5, [("garbled", "a", "m1", "0"), ("nan_value", "a", "m2", "0")]))
    st.record_delta("b", _delta(3, [("garbled", "b", "m1", "0")]))
    s = st.summary()
    assert s["requests"] == 8
    assert s["anomalies"] == 3
    assert s["by_type"]["garbled"] == 2
    assert s["by_type"]["nan_value"] == 1
    assert s["by_instance"] == {"a": 2, "b": 1}
    assert s["anomaly_rate"] == round(3 / 8, 6)


def test_purge_updates_global_aggregates():
    st = _store()
    st.record_delta("a", _delta(1, [("garbled", "a", "m", "0")]))
    st.record_delta("b", _delta(1, [("rare_character", "b", "m", "1")]))

    st.purge_instance("a")
    s = st.summary()
    assert s["requests"] == 1
    assert s["anomalies"] == 1
    assert s["by_type"]["garbled"] == 0
    assert s["by_type"]["rare_character"] == 1
    assert s["by_instance"] == {"b": 1}
    assert "a" not in s["by_instance"]


def test_summary_by_instance_filters_zero():
    st = _store()
    st.set_state("a", "online")
    st.set_state("b", "online")
    st.record_delta("b", _delta(1, [("garbled", "b", "m", "0")]))
    s = st.summary()
    assert s["by_instance"] == {"b": 1}


def test_import_then_purge_summary():
    st = _store()
    from webui.events import AnomalyEvent
    from webui.store import TrendEvent

    events = [AnomalyEvent(id=1, ts=1.0, instance="a", model="m",
                           ill_type="garbled", choice_index="0", source="imported")]
    trend_events = [TrendEvent(ts=1.0, instance="a", model="m",
                               ill_type="garbled", source="imported")]
    st.apply_import("a", events, trend_events)
    assert st.summary()["anomalies"] == 1

    st.purge_instance("a")
    assert st.summary()["anomalies"] == 0
    assert st.summary()["by_instance"] == {}


def test_trend_raw_pruning_by_time():
    st = _store(raw_trend_window_seconds=60, trend_bucket_seconds=60, trend_horizon_seconds=300)
    now = 10_000.0
    st.record_trend("a", "m", TrendPoint(ts=9_930.0, anomalies=1.0), now=now)
    st.record_trend("a", "m", TrendPoint(ts=9_960.0, anomalies=2.0), now=now)
    pts = st.raw_points_for("a", "m")
    # 9_930 < now-60=9_940 → 被淘汰；9_960 保留
    assert [p.ts for p in pts] == [9_960.0]
    assert [p.anomalies for p in pts] == [2.0]

def test_trend_bucket_sum_and_prune():
    st = _store(raw_trend_window_seconds=120, trend_bucket_seconds=60, trend_horizon_seconds=300)
    now = 10_000.0
    # 同一个 60s 桶内三个原始点求和
    st.record_trend("a", "m", TrendPoint(ts=9_900.0, anomalies=1.0), now=now)
    st.record_trend("a", "m", TrendPoint(ts=9_920.0, anomalies=2.0), now=now)
    st.record_trend("a", "m", TrendPoint(ts=9_940.0, anomalies=3.0), now=now)
    buckets = st.bucket_points_for("a", "m")
    b = buckets[0]  # ts=9_900 （桶起点）
    assert b.ts == 9_900.0
    assert b.anomalies == 6.0
    assert b.requests == 0.0


def test_query_trends_boundary_no_double_count():
    """原始区边界：桶整体落在边界之前才取，避免与原始点重复计数。"""
    cfg = StoreConfig(
        event_capacity=100, alert_capacity=50,
        raw_trend_window_seconds=120, trend_bucket_seconds=60, trend_horizon_seconds=300,
    )
    st = Store(cfg)
    now = 10_000.0
    # 9_800 / 9_860 落在同一分钟桶 ts=9_780（区间 [9780,9840) 整体在 boundary=9880 前）
    st.record_trend("a", "m", TrendPoint(ts=9_800.0, anomalies=1.0), now=now)
    st.record_trend("a", "m", TrendPoint(ts=9_860.0, anomalies=1.0), now=now)
    # 9_900 原始点在 boundary=9880 之后 → 原始点区
    st.record_trend("a", "m", TrendPoint(ts=9_900.0, anomalies=5.0), now=now)
    pts = st.query_trends(300, now=now)
    ts_map = {p["ts"]: p for p in pts}
    # 分钟桶 9_780 合计 anomalies=2，且不与原始点重复计数（原始点 9_900 单独一点）
    assert ts_map[9_780.0]["rare_character"] == 0.0
    assert ts_map[9_900.0]["rare_character"] == 0.0
    assert len([p for p in pts if p["ts"] == 9_900.0]) == 1


def test_query_trends_window_filter():
    st = _store(raw_trend_window_seconds=3600, trend_bucket_seconds=60, trend_horizon_seconds=86400)
    now = 10_000.0
    st.record_trend("a", "m", TrendPoint(ts=9_000.0, anomalies=1.0), now=now)  # 窗口外
    st.record_trend("a", "m", TrendPoint(ts=9_800.0, anomalies=2.0), now=now)  # 窗口内原始区
    pts = st.query_trends(60, now=now)
    assert all(9_900.0 <= p["ts"] <= 10_000.0 for p in pts)


def test_alloc_ids_monotonic():
    st = _store()
    ids = [st.alloc_event_id() for _ in range(5)]
    assert ids == list(range(1, 6))
    aid = [st.alloc_alert_id() for _ in range(3)]
    assert aid == [1, 2, 3]