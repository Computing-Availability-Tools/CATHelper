"""API 集成测试：认证、查询端点结构、实例管理端点、trends 白名单、yaml 写回。"""
from __future__ import annotations

import yaml
import pytest
import httpx
from fastapi import FastAPI

from webui.main import create_app
from tests._webui_helpers import build_webui_config_dict, write_yaml


@pytest.fixture
async def app_client(tmp_path):
    cfg_path = str(tmp_path / "webui.yaml")
    write_yaml(cfg_path, build_webui_config_dict())
    app = create_app(cfg_path)
    ctx = app.state.ctx
    await ctx.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, ctx, cfg_path
    await ctx.stop()


async def _login(client):
    r = await client.post("/api/login", json={"username": "admin", "password": "test123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ------------------------------------------------------------------ #
# 认证
# ------------------------------------------------------------------ #
async def test_apis_require_auth(app_client):
    client, _, _ = app_client
    for path in ("/api/summary", "/api/instances", "/api/events", "/api/alerts",
                 "/api/trends?window=1h"):
        r = await client.get(path)
        assert r.status_code == 401, path


async def test_login_wrong_credentials(app_client):
    client, _, _ = app_client
    r = await client.post("/api/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


async def test_login_success(app_client):
    client, _, _ = app_client
    r = await client.post("/api/login", json={"username": "admin", "password": "test123"})
    assert r.status_code == 200
    assert "token" in r.json()


async def test_login_does_not_leak_user_existence(app_client):
    client, _, _ = app_client
    r1 = await client.post("/api/login", json={"username": "ghost", "password": "x"})
    r2 = await client.post("/api/login", json={"username": "admin", "password": "x"})
    assert r1.status_code == r2.status_code == 401


# ------------------------------------------------------------------ #
# 查询端点结构
# ------------------------------------------------------------------ #
async def test_summary_structure(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    r = await client.get("/api/summary", headers=h)
    assert r.status_code == 200
    data = r.json()
    for k in ("requests", "anomalies", "anomaly_rate", "errors",
              "by_type", "by_instance", "instances", "updated_at"):
        assert k in data
    assert set(data["by_type"].keys()) == {"rare_character", "garbled", "repetition", "nan_value"}


async def test_summary_reflects_store(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    from webui.events import AnomalyEvent, DeltaSummary

    d = DeltaSummary(requests=5)
    for i in range(2):
        d.events.append(AnomalyEvent(id=i + 1, ts=1.0, instance="a", model="m",
                                     ill_type="garbled", choice_index="0"))
    d.anomalies_total = 2
    d.by_type["garbled"] = 2
    d.by_model["m"] = 2
    ctx.store.record_delta("a", d)
    r = await client.get("/api/summary", headers=h)
    data = r.json()
    assert data["requests"] == 5
    assert data["anomalies"] == 2
    assert data["by_type"]["garbled"] == 2
    assert data["by_instance"] == {"a": 2}


async def test_events_and_alerts_require_auth_then_return_lists(app_client):
    client, ctx, _ = app_client
    r = await client.get("/api/events")
    assert r.status_code == 401
    h = await _login(client)
    r = await client.get("/api/events", headers=h)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    r = await client.get("/api/alerts", headers=h)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_events_limit_clamped(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    from webui.events import AnomalyEvent

    for i in range(30):
        ctx.store.add_event(AnomalyEvent(id=i + 1, ts=float(i), instance="a", model="m",
                                         ill_type="garbled", choice_index="0"))
    r = await client.get("/api/events?limit=10", headers=h)
    assert len(r.json()) == 10
    r = await client.get("/api/events?limit=99999", headers=h)
    assert len(r.json()) == 30  # 上限 1000 以内


async def test_trends_window_whitelist(app_client):
    client, _, _ = app_client
    h = await _login(client)
    for w in ("1h", "4h", "8h", "16h", "24h"):
        r = await client.get(f"/api/trends?window={w}", headers=h)
        assert r.status_code == 200, w
    r = await client.get("/api/trends?window=3h", headers=h)
    assert r.status_code == 400
    r = await client.get("/api/trends?window=bogus", headers=h)
    assert r.status_code == 400


async def test_instances_structure(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    ctx.cm.last_config  # ensure loaded
    ctx.store.set_state("x", "online")
    r = await client.get("/api/instances", headers=h)
    assert r.status_code == 200
    for item in r.json():
        for k in ("name", "url", "paused", "state", "requests", "anomalies", "last_event"):
            assert k in item


# ------------------------------------------------------------------ #
# 实例详情端点（summary / trends / events）
# ------------------------------------------------------------------ #
async def test_instance_summary_requires_auth_then_returns_detail(app_client):
    client, ctx, _ = app_client
    r = await client.get("/api/instances/x/summary")
    assert r.status_code == 401
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    from webui.events import AnomalyEvent, DeltaSummary

    d = DeltaSummary(requests=7)
    d.events.append(AnomalyEvent(id=1, ts=1.0, instance="x", model="m",
                                 ill_type="garbled", choice_index="0"))
    d.anomalies_total = 1
    d.by_type["garbled"] = 1
    d.by_model["m"] = 1
    ctx.store.record_delta("x", d)
    ctx.store.set_state("x", "online")
    r = await client.get("/api/instances/x/summary", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "x"
    assert data["requests"] == 7
    assert data["anomalies"] == 1
    assert data["by_type"]["garbled"] == 1
    assert data["by_model"] == {"m": 1}
    assert data["models"] == ["m"]
    r = await client.get("/api/instances/nope/summary", headers=h)
    assert r.status_code == 404


async def test_instance_trends(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    import time

    ts = time.time() - 10  # 落在 1h 窗口内的最近点
    ctx.store.record_trend("x", "m", _trend_point(ts=ts, garbled=2))
    r = await client.get("/api/instances/x/trends?window=1h", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["window"] == "1h"
    assert data["points"][0]["garbled"] == 2
    # 其他实例的数据不被混入
    ctx.store.record_trend("other", "m", _trend_point(ts=ts, nan_value=9))
    r = await client.get("/api/instances/x/trends?window=1h", headers=h)
    assert r.json()["points"][0]["nan_value"] == 0
    # window 白名单 + 实例不存在
    r = await client.get("/api/instances/x/trends?window=3h", headers=h)
    assert r.status_code == 400
    r = await client.get("/api/instances/nope/trends?window=1h", headers=h)
    assert r.status_code == 404


async def test_instance_events_filtered(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    from webui.events import AnomalyEvent

    ctx.store.add_event(AnomalyEvent(id=1, ts=1.0, instance="x", model="m",
                                     ill_type="garbled", choice_index="0"))
    ctx.store.add_event(AnomalyEvent(id=2, ts=2.0, instance="y", model="m",
                                     ill_type="nan_value", choice_index="0"))
    r = await client.get("/api/instances/x/events?limit=10", headers=h)
    assert r.status_code == 200
    items = r.json()
    assert [e["instance"] for e in items] == ["x"]
    r = await client.get("/api/instances/nope/events", headers=h)
    assert r.status_code == 404


def _trend_point(ts, **kw):
    from webui.store import TrendPoint

    p = TrendPoint(ts=ts)
    for t, v in kw.items():
        p.by_type[t] = v
    return p


# ------------------------------------------------------------------ #
# 实例管理端点
# ------------------------------------------------------------------ #
async def test_add_instance(app_client):
    client, ctx, cfg_path = app_client
    h = await _login(client)
    r = await client.post("/api/instances", json={"name": "prod-1", "url": "http://10.0.0.5:8000"}, headers=h)
    assert r.status_code == 201
    assert r.json()["name"] == "prod-1"
    # yaml 已写回
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert [i["name"] for i in data["instances"]] == ["prod-1"]
    # 内存配置同步
    assert "prod-1" in ctx.current_instances()


async def test_add_instance_duplicate_409(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "p", "url": "http://x:1"}, headers=h)
    r = await client.post("/api/instances", json={"name": "p", "url": "http://y:2"}, headers=h)
    assert r.status_code == 409


async def test_add_instance_invalid_400(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.post("/api/instances", json={"name": "", "url": "http://x:1"}, headers=h)
    assert r.status_code == 400
    r = await client.post("/api/instances", json={"name": "q", "url": "not-a-url"}, headers=h)
    assert r.status_code == 400


async def test_delete_instance(app_client):
    client, ctx, cfg_path = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "del-me", "url": "http://x:1"}, headers=h)
    r = await client.delete("/api/instances/del-me", headers=h)
    assert r.status_code == 204
    assert "del-me" not in ctx.current_instances()
    r = await client.get("/api/instances", headers=h)
    assert all(i["name"] != "del-me" for i in r.json())


async def test_delete_instance_404(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.delete("/api/instances/missing", headers=h)
    assert r.status_code == 404


async def test_pause_resume_flow(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "p", "url": "http://x:1"}, headers=h)
    r = await client.post("/api/instances/p/pause", headers=h)
    assert r.status_code == 200
    assert r.json()["paused"] is True
    r = await client.post("/api/instances/p/pause", headers=h)
    assert r.status_code == 409  # 已暂停
    r = await client.post("/api/instances/p/resume", headers=h)
    assert r.status_code == 200
    assert r.json()["paused"] is False
    r = await client.post("/api/instances/p/resume", headers=h)
    assert r.status_code == 409  # 未暂停


async def test_pause_missing_404(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.post("/api/instances/nope/pause", headers=h)
    assert r.status_code == 404


async def test_delete_purges_instance_data(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "p", "url": "http://x:1"}, headers=h)
    from webui.events import AnomalyEvent, DeltaSummary

    d = DeltaSummary(requests=1)
    d.events.append(AnomalyEvent(id=1, ts=1.0, instance="p", model="m",
                                 ill_type="garbled", choice_index="0"))
    d.anomalies_total = 1
    d.by_type["garbled"] = 1
    d.by_model["m"] = 1
    ctx.store.record_delta("p", d)
    assert ctx.store.summary()["anomalies"] == 1

    await client.delete("/api/instances/p", headers=h)
    assert ctx.store.instance_stats("p") is None
    assert all(e.instance != "p" for e in ctx.store.recent_events(10))
    assert ctx.store.summary()["anomalies"] == 0
    assert ctx.store.summary()["by_instance"] == {}


async def test_add_then_instance_visible_in_list(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.get("/api/instances", headers=h)
    assert r.json() == []
    await client.post("/api/instances", json={"name": "n", "url": "http://x:1"}, headers=h)
    r = await client.get("/api/instances", headers=h)
    assert [i["name"] for i in r.json()] == ["n"]


async def test_static_pages_public(app_client):
    client, _, _ = app_client
    r = await client.get("/")
    assert r.status_code == 200
    assert "推理精度异常监控" in r.text
    r = await client.get("/js/app.js")
    assert r.status_code == 200
    r = await client.get("/css/style.css")
    assert r.status_code == 200