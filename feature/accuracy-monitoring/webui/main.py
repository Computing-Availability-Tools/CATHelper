"""WebUI 服务入口：FastAPI app + REST API + 静态托管 + 生命周期管理。

- 查询端点：/api/summary /api/instances /api/events /api/alerts /api/trends（短轮询）。
- 实例管理：POST/DELETE /api/instances...（先校验 → 原子写盘 → 统一重载应用）。
- 认证：除 /api/login 与静态页面外，均需 `Authorization: Bearer <token>`。
- 动态配置段热重载由后台任务轮询 yaml mtime 触发；所有变更经 asyncio.Lock 串行。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import pickle
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .alerts import AlertEngine
from .auth import AuthManager
from .collector import Collector
from .config import (
    CONFIG_PATH_DEFAULT,
    ConfigError,
    ConfigManager,
    ILL_TYPES,
    InstanceConfig,
    WebUIConfig,
)
from .events import AnomalyEvent, EventSynthesizer, ill_type_name
from .store import Store, TrendEvent

logger = logging.getLogger("webui.main")

# 趋势时间窗白名单：1day / 7day / 30day（天计数，最多近一个月）
TREND_WINDOWS: Dict[str, int] = {
    "1day": 86400,
    "7day": 604800,
    "30day": 2592000,
}

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 自定义区间时钟容差（秒）：允许 end 略超当前时间，吸收客户端/服务端时钟偏差
CUSTOM_RANGE_CLOCK_TOLERANCE = 60.0


def _trend_payload(
    c: "AppContext",
    window: str,
    start: Optional[str],
    end: Optional[str],
    instance: Optional[str] = None,
) -> Dict[str, Any]:
    """统一趋势查询：预设窗口（1day/7day/30day）与自定义区间（custom + start/end）。

    校验失败抛 HTTPException 400 + 可读 detail；可查询范围以实际保留数据为准
    （data_start/partial），不设固定跨度上限。
    """
    now = time.time()
    if window == "custom":
        if start is None or end is None:
            raise HTTPException(
                status_code=400,
                detail="自定义区间需同时提供 start 与 end（unix 秒）",
            )
        try:
            s = float(start)
            e = float(end)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="start/end 必须为 unix 秒数值"
            ) from exc
        if s < 0 or e < 0:
            raise HTTPException(status_code=400, detail="start/end 不能为负数")
        if s >= e:
            raise HTTPException(status_code=400, detail="开始时间必须早于结束时间")
        if e > now + CUSTOM_RANGE_CLOCK_TOLERANCE:
            raise HTTPException(
                status_code=400, detail="结束时间不能晚于当前时间"
            )
        result = c.store.query_trend_window(s, e, instance)
        result["window"] = "custom"
        result["window_seconds"] = None
    else:
        secs = TREND_WINDOWS.get(window)
        if secs is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"非法 window: {window!r}"
                    f"（可选 {'/'.join(TREND_WINDOWS)} 或 custom）"
                ),
            )
        result = c.store.query_trend_window(now - secs, now, instance)
        result["window"] = window
        result["window_seconds"] = secs
    return result


class _SafeUnpickler(pickle.Unpickler):
    """受限反序列化：拒绝任何全局对象（防 pickle 代码执行）。

    中间件落盘的异常数据为纯 dict/int/str/list/float，不触发 find_class；
    文件被篡改注入 GLOBAL/INST 操作码时直接拒绝。
    """

    def find_class(self, module, name):
        raise pickle.UnpicklingError(f"不允许的全局对象: {module}.{name}")


def parse_anomaly_pickle(data: bytes) -> "tuple[List[Dict[str, Any]], int]":
    """解析异常 pickle 字节为记录列表 + 跳过条数。

    每条记录取 time/ill_type/model_name 三字段；缺字段或转换失败跳过。
    ill_type=0(normal) 与越界值跳过（非异常，不入趋势）。
    返回的记录为 dict: {ts, ill_type(字符串名), model}。
    """
    try:
        raw = _SafeUnpickler(io.BytesIO(data))
        d = raw.load()
    except pickle.UnpicklingError:
        raise
    except Exception as exc:
        raise ValueError(f"pickle 解析失败: {exc}") from exc
    if not isinstance(d, dict):
        raise ValueError("文件内容不是异常数据字典")

    records: List[Dict[str, Any]] = []
    skipped = 0
    for _key, rec in d.items():
        if not isinstance(rec, dict):
            skipped += 1
            continue
        try:
            ts = float(rec.get("time"))
            ill = int(rec.get("ill_type"))
            model = str(rec.get("model_name") or "unknown")
        except (TypeError, ValueError):
            skipped += 1
            continue
        if ill < 1 or ill > 4:
            skipped += 1
            continue
        name = ill_type_name(ill)
        if name == "unknown":
            skipped += 1
            continue
        records.append({"ts": ts, "ill_type": name, "model": model})
    records.sort(key=lambda r: r["ts"])
    return records, skipped


class LoginRequest(BaseModel):
    username: str
    password: str


class InstanceCreate(BaseModel):
    name: str
    url: str


def _resolve_config_path(override: Optional[str] = None) -> str:
    """解析配置文件路径：env WEBUI_CONFIG > 显式参数 > CWD > 包上级目录。"""
    if override:
        return override
    env = os.environ.get("WEBUI_CONFIG")
    if env:
        return env
    cwd = Path("configs/webui.yaml")
    if cwd.exists():
        return str(cwd)
    pkg = Path(__file__).resolve().parent.parent / "configs" / "webui.yaml"
    if pkg.exists():
        return str(pkg)
    return CONFIG_PATH_DEFAULT


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return ""


async def require_user(request: Request) -> str:
    """Bearer token 校验依赖（§7.1）：401 → 前端跳登录页。"""
    ctx = request.app.state.ctx
    token = _bearer_token(request)
    if not token or not ctx.auth.validate(token):
        raise HTTPException(status_code=401, detail="未登录或 token 已过期")
    return ctx.auth.user_for(token) or ""


class AppContext:
    """应用运行时上下文：配置、认证、存储、事件/告警引擎、采集器与生命周期。"""

    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.cm = ConfigManager(config_path)
        self.cfg: WebUIConfig = self.cm.load()

        self.auth = AuthManager(self.cfg.auth)
        self.store = Store(self.cfg.store)

        self.synth = EventSynthesizer()
        self.synth.set_id_source(self.store.alloc_event_id)
        self.alert_engine = AlertEngine(
            self.store.alerts,
            id_allocer=self.store.alloc_alert_id,
            default_webhook_url=self.cfg.webhooks.default,
            email_cfg=self.cfg.email,
        )
        self.alert_engine.set_rules(list(self.cfg.alerts), self.cfg.webhooks.default)

        self.collector = Collector(
            store=self.store,
            synthesizer=self.synth,
            alerts=self.alert_engine,
            poll_cfg=self.cfg.poll,
        )
        self.lock = asyncio.Lock()
        self.reload_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._apply_config(self.cfg)
        self.reload_task = asyncio.create_task(self._reload_loop())

    async def stop(self) -> None:
        if self.reload_task is not None:
            self.reload_task.cancel()
            await asyncio.gather(self.reload_task, return_exceptions=True)
            self.reload_task = None
        await self.collector.shutdown()

    async def _reload_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            async with self.lock:
                cfg = self.cm.reload_if_changed()
                if cfg is not None:
                    self._apply_config(cfg)

    # ------------------------------------------------------------------ #
    # 统一「读取 → diff → 最小化应用」路径（§3）
    # ------------------------------------------------------------------ #
    def _apply_config(self, cfg: WebUIConfig) -> None:
        """应用一份配置快照：auth 凭据 + 实例 desired-state（幂等）。"""
        self.auth.configure(cfg.auth)
        self.collector.apply_instances(list(cfg.instances))

    # ------------------------------------------------------------------ #
    # 实例管理（先校验 → 原子写盘 → 重读应用）
    # ------------------------------------------------------------------ #
    def _commit_instances(self, new_instances: List[InstanceConfig]) -> WebUIConfig:
        """写盘 + 重读 + 最小化应用；失败抛 HTTPException 500（内存配置不动）。"""
        try:
            self.cm.write_instances(new_instances)
        except Exception as exc:  # noqa: BLE001 —— IO/yaml 错误统一 500
            logger.error("写 yaml 失败: %s", exc)
            raise HTTPException(status_code=500, detail="写配置文件失败")
        try:
            cfg = self.cm.reload_now()
        except ConfigError as exc:
            logger.error("写盘后重读校验失败: %s", exc)
            raise HTTPException(status_code=500, detail="配置文件重读失败") from exc
        self._apply_config(cfg)
        return cfg

    def current_instances(self) -> Dict[str, InstanceConfig]:
        return {i.name: i for i in self.cm.last_config.instances}

    def instance_response(self, inst: InstanceConfig) -> Dict[str, Any]:
        st = self.store.instance_stats(inst.name)
        if st is not None:
            state = st.state
            last_event = (
                {"ill_type": st.last_event[0], "ts": st.last_event[1]}
                if st.last_event
                else None
            )
            by_type = dict(st.by_type)
            by_model = dict(st.by_model)
            # 模型名 = 异常事件观测 ∪ metrics 观测（零异常的在线实例也能显示模型名）
            models = sorted(set(by_model) | st.models_seen)
        else:
            state = "paused" if inst.paused else "offline"
            last_event = None
            by_type = {t: 0 for t in ILL_TYPES}
            by_model = {}
            models = []
        return {
            "name": inst.name,
            "url": inst.url,
            "paused": inst.paused,
            "state": state,
            "requests": st.requests if st else 0,
            "anomalies": st.anomalies if st else 0,
            "errors": st.errors if st else 0,
            "last_event": last_event,
            "by_type": by_type,
            "by_model": by_model,
            "models": models,
        }


def create_app(config_path: Optional[str] = None) -> FastAPI:
    path = _resolve_config_path(config_path)
    ctx = AppContext(config_path=path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.start()
        yield
        await ctx.stop()

    app = FastAPI(title="推理精度异常监控 Web 界面", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx

    # ------------------------------------------------------------------ #
    # 认证
    # ------------------------------------------------------------------ #
    @app.post("/api/login", tags=["auth"])
    async def login(body: LoginRequest, request: Request) -> Dict[str, str]:
        c: AppContext = request.app.state.ctx
        token = c.auth.authenticate(body.username, body.password)
        if token is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        return {"token": token, "user": body.username}

    # ------------------------------------------------------------------ #
    # 查询端点（短轮询，需登录）
    # ------------------------------------------------------------------ #
    @app.get("/api/summary", tags=["query"])
    async def api_summary(
        request: Request, user: str = Depends(require_user)
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        data = c.store.summary()
        data["user"] = user
        return data

    @app.get("/api/instances", tags=["query"])
    async def api_instances(
        request: Request, user: str = Depends(require_user)
    ) -> List[Dict[str, Any]]:
        c: AppContext = request.app.state.ctx
        return [
            c.instance_response(i) for i in c.cm.last_config.instances
        ]

    @app.get("/api/events", tags=["query"])
    async def api_events(
        request: Request, limit: int = 50, user: str = Depends(require_user)
    ) -> List[Dict[str, Any]]:
        c: AppContext = request.app.state.ctx
        n = max(0, min(limit, 1000))
        return [
            {
                "id": e.id,
                "ts": e.ts,
                "instance": e.instance,
                "model": e.model,
                "ill_type": e.ill_type,
                "choice_index": e.choice_index,
                "source": getattr(e, "source", "live"),
            }
            for e in c.store.recent_events(n)
        ]

    @app.get("/api/alerts", tags=["query"])
    async def api_alerts(
        request: Request, limit: int = 50, user: str = Depends(require_user)
    ) -> List[Dict[str, Any]]:
        c: AppContext = request.app.state.ctx
        n = max(0, min(limit, 1000))
        return [a.to_dict() for a in c.store.recent_alerts(n)]

    @app.get("/api/trends", tags=["query"])
    async def api_trends(
        request: Request,
        window: str = "1day",
        start: Optional[str] = None,
        end: Optional[str] = None,
        user: str = Depends(require_user),
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        return _trend_payload(c, window, start, end)

    @app.get("/api/instances/{name}/summary", tags=["query"])
    async def api_instance_summary(
        name: str, request: Request, user: str = Depends(require_user)
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        cur = c.current_instances()
        if name not in cur:
            raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
        return c.instance_response(cur[name])

    @app.get("/api/instances/{name}/trends", tags=["query"])
    async def api_instance_trends(
        name: str,
        request: Request,
        user: str = Depends(require_user),
        window: str = "1day",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        if name not in c.current_instances():
            raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
        return _trend_payload(c, window, start, end, instance=name)

    @app.get("/api/instances/{name}/events", tags=["query"])
    async def api_instance_events(
        name: str,
        request: Request,
        user: str = Depends(require_user),
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        c: AppContext = request.app.state.ctx
        if name not in c.current_instances():
            raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
        n = max(0, min(limit, 1000))
        return [
            {
                "id": e.id,
                "ts": e.ts,
                "instance": e.instance,
                "model": e.model,
                "ill_type": e.ill_type,
                "choice_index": e.choice_index,
                "source": getattr(e, "source", "live"),
            }
            for e in c.store.events_for(name, n)
        ]

    # ------------------------------------------------------------------ #
    # 实例管理端点（§8）
    # ------------------------------------------------------------------ #
    @app.post("/api/instances", status_code=201, tags=["admin"])
    async def add_instance(
        body: InstanceCreate, request: Request, user: str = Depends(require_user)
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        try:
            inst = InstanceConfig.from_dict(
                {"name": body.name, "url": body.url}, "instances"
            )
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with c.lock:
            if inst.name in c.current_instances():
                raise HTTPException(
                    status_code=409, detail=f"实例 {inst.name} 已存在"
                )
            new_list = [i for i in c.cm.last_config.instances] + [inst]
            c._commit_instances(new_list)
            return c.instance_response(inst)

    @app.delete("/api/instances/{name}", status_code=204, tags=["admin"])
    async def delete_instance(
        name: str, request: Request, user: str = Depends(require_user)
    ) -> Response:
        c: AppContext = request.app.state.ctx
        async with c.lock:
            if name not in c.current_instances():
                raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
            new_list = [i for i in c.cm.last_config.instances if i.name != name]
            c._commit_instances(new_list)
            return Response(status_code=204)

    @app.post("/api/instances/{name}/pause", tags=["admin"])
    async def pause_instance(
        name: str, request: Request, user: str = Depends(require_user)
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        async with c.lock:
            cur = c.current_instances()
            if name not in cur:
                raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
            old = cur[name]
            if old.paused:
                raise HTTPException(status_code=409, detail=f"实例 {name} 已暂停")
            target = InstanceConfig(name=old.name, url=old.url, paused=True)
            new_list = [target if i.name == name else i for i in c.cm.last_config.instances]
            c._commit_instances(new_list)
            return c.instance_response(target)

    @app.post("/api/instances/{name}/resume", tags=["admin"])
    async def resume_instance(
        name: str, request: Request, user: str = Depends(require_user)
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        async with c.lock:
            cur = c.current_instances()
            if name not in cur:
                raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
            old = cur[name]
            if not old.paused:
                raise HTTPException(status_code=409, detail=f"实例 {name} 未暂停")
            target = InstanceConfig(name=old.name, url=old.url, paused=False)
            new_list = [target if i.name == name else i for i in c.cm.last_config.instances]
            c._commit_instances(new_list)
            return c.instance_response(target)

    # ------------------------------------------------------------------ #
    # 历史数据导入（异常 pickle → 阶梯趋势事件）
    # ------------------------------------------------------------------ #
    @app.post("/api/import", tags=["admin"])
    async def api_import(
        request: Request,
        instance: str = "",
        user: str = Depends(require_user),
    ) -> Dict[str, Any]:
        c: AppContext = request.app.state.ctx
        instance = (instance or "").strip()
        if not instance:
            raise HTTPException(status_code=400, detail="请指定实例名")
        if instance not in c.current_instances():
            raise HTTPException(status_code=404, detail=f"实例 {instance} 不存在")
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="未收到文件内容")
        try:
            records, skipped = parse_anomaly_pickle(body)
        except pickle.UnpicklingError:
            raise HTTPException(status_code=400, detail="文件包含不允许的对象（安全拒绝）")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not records:
            raise HTTPException(status_code=400, detail="文件中未解析到有效异常记录")
        # 模型对应校验（严格子集，失败不合入）：文件 model_name 与实例观测模型
        # 同源于拉起服务时的 served-model-name（请求体 model 字段）。观测来源 =
        # 异常事件（by_model）∪ metrics 带 model 标签的序列（models_seen，含正常请求）。
        file_models = sorted({r["model"] for r in records})
        st = c.store.instance_stats(instance)
        known_models = sorted(
            (set(st.by_model.keys()) | st.models_seen) if st else set()
        )
        warning: Optional[str] = None
        if known_models:
            known_set = set(known_models)
            unmatched = [m for m in file_models if m not in known_set]
            if unmatched:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"导入文件与实例 {instance} 的模型不匹配，已拒绝合入："
                        f"文件内模型 {file_models}，实例观测模型 {known_models}，"
                        f"未匹配 {unmatched}"
                    ),
                )
        else:
            warning = "实例尚无观测模型，未做模型对应校验"
        # 构造 AnomalyEvent + TrendEvent（source=imported）
        anomaly_events = [
            AnomalyEvent(
                id=c.store.alloc_event_id(),
                ts=r["ts"], instance=instance, model=r["model"],
                ill_type=r["ill_type"], choice_index="0", source="imported",
            )
            for r in records
        ]
        trend_events = [
            TrendEvent(
                ts=r["ts"], instance=instance, model=r["model"],
                ill_type=r["ill_type"], source="imported",
            )
            for r in records
        ]
        cleared = c.store.apply_import(instance, anomaly_events, trend_events)
        logger.info(
            "导入历史异常: instance=%s, 导入=%d, 跳过=%d, 覆盖旧=%d",
            instance, len(anomaly_events), skipped, cleared,
        )
        return {
            "instance": instance,
            "imported": len(anomaly_events),
            "skipped": skipped,
            "cleared": cleared,
            "warning": warning,
        }

    # ------------------------------------------------------------------ #
    # 静态前端（公开访问，登录页由前端控制）
    # ------------------------------------------------------------------ #
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def _build_module_app() -> FastAPI:
    """模块级 app（uvicorn webui.main:app）；配置加载失败会在此抛错并拒绝启动。"""
    return create_app()


app = _build_module_app()