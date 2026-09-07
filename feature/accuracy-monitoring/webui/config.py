"""配置模型 + 校验 + ConfigManager（yaml 热重载/原子写盘/mtime 检测/最小化应用）。

设计原则（§3 / §7.2）：
- `configs/webui.yaml` 为唯一事实源。
- 所有实例变更（Web 界面操作或外部编辑 yaml）收敛到统一「读取 → diff → 应用」路径；
  外部编辑由 mtime 检测触发，界面操作先原子写回再由统一入口读取应用。
- 动态段：`instances`、`auth`（凭据热重载即时生效）。
- 非动态段：`poll` / `store` 容量 / `webhooks` / `alerts`，变更记日志
  「requires restart」并忽略。
- 监听 host/port 不再放配置（原 `server` 段从未生效）：统一由 uvicorn 启动命令配置。
- 运行中热重载校验失败 → 保留上次有效配置并记警告，服务不中断。
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import stat
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

logger = logging.getLogger("webui.config")

CONFIG_PATH_DEFAULT = "configs/webui.yaml"

# 与中间件 parse_ill_type_name 一致的字符串名（§5.1）
ILL_TYPES: Tuple[str, ...] = ("rare_character", "garbled", "repetition", "nan_value")

# 非动态配置段：变更仅记日志，需要重启服务
NON_DYNAMIC_SECTIONS = ("poll", "store", "webhooks", "email", "alerts")


class ConfigError(Exception):
    """配置非法（启动时报错退出 / 热重载时拒绝）。"""


def _require(d: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] is None:
        raise ConfigError(f"{where}: 缺少必填字段 '{key}'")
    return d[key]


def _positive_number(d: Mapping[str, Any], key: str, where: str) -> int:
    v = _require(d, key, where)
    try:
        ival = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: 字段 '{key}' 必须是正整数，得到 {v!r}")
    if ival <= 0:
        raise ConfigError(f"{where}: 字段 '{key}' 必须是正整数，得到 {ival}")
    return ival


def _validate_url(url: str, where: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{where}: 非法 URL（需 http/https 且带主机）: {url!r}")


def _validate_ill_type(value: Optional[str], where: str) -> None:
    if value is None:
        return
    if value not in ILL_TYPES:
        raise ConfigError(
            f"{where}: 未知 ill_type {value!r}（可选 {ILL_TYPES} 或缺省=任一）"
        )


@dataclass(frozen=True)
class AuthConfig:
    username: str
    secret: str = ""            # 明文密码（当配置了 password 时）
    password_hash: str = ""     # sha256 hex（当配置了 password_hash 时）
    token_ttl_hours: int = 24

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "AuthConfig":
        username = str(_require(d, "username", "auth"))
        if not username.strip():
            raise ConfigError("auth.username 不能为空")
        secret = str(d.get("password", "") or "")
        hash_ = str(d.get("password_hash", "") or "").lower()
        if not secret and not hash_:
            raise ConfigError("auth: 必须配置 password 或 password_hash 之一")
        if hash_:
            if len(hash_) != 64 or any(c not in "0123456789abcdef" for c in hash_):
                raise ConfigError("auth.password_hash 必须是 64 位 sha256 hex")
        ttl = int(d.get("token_ttl_hours", 24))
        if ttl <= 0:
            raise ConfigError("auth.token_ttl_hours 必须为正整数")
        return cls(username=username, secret=secret, password_hash=hash_, token_ttl_hours=ttl)

    def verify(self, username: str, password: str) -> bool:
        """校验凭据；用户名不匹配返回 False（不泄露存在性）。"""
        if username != self.username:
            return False
        if self.password_hash:
            got = hashlib.sha256((password or "").encode("utf-8")).hexdigest().lower()
            return got == self.password_hash
        return bool(self.secret) and (password or "") == self.secret


@dataclass(frozen=True)
class PollConfig:
    interval_seconds: float = 3.0
    http_timeout_seconds: float = 2.0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PollConfig":
        interval = float(d.get("interval_seconds", 3.0))
        if not (2.0 <= interval <= 5.0):
            raise ConfigError(f"poll.interval_seconds 需在 2-5s 之间，得到 {interval}")
        timeout = float(d.get("http_timeout_seconds", 2.0))
        if timeout <= 0:
            raise ConfigError(f"poll.http_timeout_seconds 必须为正数: {timeout}")
        return cls(interval_seconds=interval, http_timeout_seconds=timeout)


@dataclass(frozen=True)
class InstanceConfig:
    name: str
    url: str
    paused: bool = False

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], where: str) -> "InstanceConfig":
        name = str(_require(d, "name", where)).strip()
        if not name:
            raise ConfigError(f"{where}: 实例 name 不能为空")
        url = str(_require(d, "url", where)).strip()
        _validate_url(url, f"{where}({name})")
        paused = bool(d.get("paused", False))
        return cls(name=name, url=url, paused=paused)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": self.name, "url": self.url}
        if self.paused:
            d["paused"] = True
        return d


@dataclass(frozen=True)
class StoreConfig:
    event_capacity: int = 10000
    alert_capacity: int = 500
    raw_trend_window_seconds: int = 3600
    trend_bucket_seconds: int = 60
    trend_horizon_seconds: int = 2592000  # 30 天（阶梯趋势事件保留上限）

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StoreConfig":
        event = _positive_number(d, "event_capacity", "store") if "event_capacity" in d else 10000
        alert = _positive_number(d, "alert_capacity", "store") if "alert_capacity" in d else 500
        raw = _positive_number(d, "raw_trend_window_seconds", "store") if "raw_trend_window_seconds" in d else 3600
        bucket = _positive_number(d, "trend_bucket_seconds", "store") if "trend_bucket_seconds" in d else 60
        horizon = _positive_number(d, "trend_horizon_seconds", "store") if "trend_horizon_seconds" in d else 2592000
        if raw >= horizon:
            raise ConfigError(
                f"store: raw_trend_window_seconds({raw}) 必须小于 trend_horizon_seconds({horizon})"
            )
        if bucket > raw:
            raise ConfigError(
                f"store: trend_bucket_seconds({bucket}) 不能大于 raw_trend_window_seconds({raw})"
            )
        return cls(
            event_capacity=event,
            alert_capacity=alert,
            raw_trend_window_seconds=raw,
            trend_bucket_seconds=bucket,
            trend_horizon_seconds=horizon,
        )


@dataclass(frozen=True)
class WebhooksConfig:
    default: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "WebhooksConfig":
        url = str(d.get("default", "") or "").strip()
        if url:
            _validate_url(url, "webhooks.default")
        return cls(default=url)


@dataclass(frozen=True)
class EmailConfig:
    """邮件告警 SMTP 配置（可选，enabled=false 时不发送）。"""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    use_ssl: bool = True     # True=SMTP_SSL(465)，False=SMTP+STARTTLS(587)
    from_addr: str = ""
    to_addrs: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EmailConfig":
        d = d or {}
        enabled = bool(d.get("enabled", False))
        smtp_host = str(d.get("smtp_host", "") or "").strip()
        smtp_port = int(d.get("smtp_port", 465))
        smtp_user = str(d.get("smtp_user", "") or "").strip()
        smtp_password = str(d.get("smtp_password", "") or "").strip()
        use_ssl = bool(d.get("use_ssl", True))
        from_addr = str(d.get("from_addr", "") or smtp_user).strip()
        to_raw = d.get("to_addrs", []) or []
        if isinstance(to_raw, str):
            to_raw = [to_raw]
        to_addrs = tuple(str(x).strip() for x in to_raw if str(x).strip())
        if smtp_port <= 0:
            raise ConfigError("email.smtp_port 必须为正整数")
        if enabled:
            if not smtp_host:
                raise ConfigError("email: enabled 时需配置 smtp_host")
            if not from_addr:
                raise ConfigError("email: enabled 时需配置 from_addr（缺省用 smtp_user）")
            if not to_addrs:
                raise ConfigError("email: enabled 时需配置 to_addrs（收件人，可列表）")
        return cls(
            enabled=enabled,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            use_ssl=use_ssl,
            from_addr=from_addr,
            to_addrs=to_addrs,
        )

    def is_ready(self) -> bool:
        return self.enabled and bool(self.smtp_host) and bool(self.from_addr) and bool(self.to_addrs)


@dataclass(frozen=True)
class AlertRule:
    name: str
    instance: str = "*"
    model: str = "*"
    ill_type: Optional[str] = None   # None = 任一异常
    window_seconds: int = 300
    threshold: int = 3
    webhook_url: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], where: str) -> "AlertRule":
        name = str(_require(d, "name", where)).strip()
        if not name:
            raise ConfigError(f"{where}: 规则 name 不能为空")
        instance = str(d.get("instance", "*"))
        model = str(d.get("model", "*"))
        ill = d.get("ill_type")
        ill_str = str(ill) if ill is not None else None
        _validate_ill_type(ill_str, f"{where}({name})")
        window = int(d.get("window_seconds", 300))
        threshold = int(d.get("threshold", 3))
        if window <= 0:
            raise ConfigError(f"{where}({name}): window_seconds 必须为正整数")
        if threshold < 1:
            raise ConfigError(f"{where}({name}): threshold 必须 >= 1")
        webhook = str(d.get("webhook_url", "") or "")
        if webhook:
            _validate_url(webhook, f"{where}({name}).webhook_url")
        enabled = bool(d.get("enabled", True))
        return cls(
            name=name,
            instance=instance,
            model=model,
            ill_type=ill_str,
            window_seconds=window,
            threshold=threshold,
            webhook_url=webhook,
            enabled=enabled,
        )

    def matches(self, instance: str, model: str, ill_type: str) -> bool:
        if self.instance != "*" and self.instance != instance:
            return False
        if self.model != "*" and self.model != model:
            return False
        if self.ill_type is not None and self.ill_type != ill_type:
            return False
        return True


@dataclass(frozen=True)
class WebUIConfig:
    auth: AuthConfig
    poll: PollConfig
    store: StoreConfig
    instances: Tuple[InstanceConfig, ...]
    webhooks: WebhooksConfig
    email: EmailConfig
    alerts: Tuple[AlertRule, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WebUIConfig":
        auth_dict = data.get("auth", {}) or {}
        auth = AuthConfig.from_dict(auth_dict) if "username" in auth_dict or "password" in auth_dict or "password_hash" in auth_dict else AuthConfig(username="", secret="")
        poll = PollConfig.from_dict(data.get("poll", {}) or {})
        store = StoreConfig.from_dict(data.get("store", {}) or {})
        webhooks = WebhooksConfig.from_dict(data.get("webhooks", {}) or {})
        email = EmailConfig.from_dict(data.get("email", {}) or {})

        instances = tuple(
            InstanceConfig.from_dict(d, "instances") for d in (data.get("instances", []) or [])
        )
        names = [i.name for i in instances]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ConfigError(f"instances: 实例名冲突，重复: {dupes}")

        alerts = tuple(
            AlertRule.from_dict(d, "alerts") for d in (data.get("alerts", []) or [])
        )
        rule_names = [r.name for r in alerts]
        if len(rule_names) != len(set(rule_names)):
            dupes = sorted({n for n in rule_names if rule_names.count(n) > 1})
            raise ConfigError(f"alerts: 规则名冲突，重复: {dupes}")

        return cls(
            auth=auth,
            poll=poll,
            store=store,
            instances=instances,
            webhooks=webhooks,
            email=email,
            alerts=alerts,
        )

    @classmethod
    def from_yaml(cls, path: str) -> "WebUIConfig":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: yaml 语法错误: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{path}: 读取失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: 必须是 yaml 映射")
        return cls.from_dict(data)


def _section_dict(data: Mapping[str, Any], key: str) -> Optional[Dict[str, Any]]:
    v = data.get(key)
    if isinstance(v, dict):
        return v
    if isinstance(v, list):  # alerts 是列表
        return {"_list": list(v)}
    return v


class ConfigManager:
    """yaml 唯一事实源管理：启动加载 / mtime 热重载 / 原子写盘 / diff 日志。

    - 非并发要求：调用方（应用层）用 `asyncio.Lock` 串行所有读/写入口。
    - `last_config` 即「上次应用的配置快照」；重复应用相同配置为幂等 no-op。
    """

    def __init__(self, path: str):
        self.path = path
        self.last_config: Optional[WebUIConfig] = None
        self._last_mtime_ns: Optional[int] = None

    # ------------------------------------------------------------------ #
    # 启动加载
    # ------------------------------------------------------------------ #
    def load(self) -> WebUIConfig:
        cfg = self._read_and_validate()
        self._record_applied(cfg, initial=True)
        return cfg

    # ------------------------------------------------------------------ #
    # 统一「读取 → diff → 应用」
    # ------------------------------------------------------------------ #
    def reload_if_changed(self) -> Optional[WebUIConfig]:
        """mtime 变化才重读；未变化返回 None。非法文件保留旧配置记警告。"""
        mtime_ns = self._mtime_ns()
        if mtime_ns is None or mtime_ns == self._last_mtime_ns:
            return None
        try:
            cfg = self._read_and_validate()
        except ConfigError as exc:
            logger.warning("configs: yaml 热重载被拒绝，保留上次有效配置: %s", exc)
            self._last_mtime_ns = mtime_ns
            return None
        self._record_applied(cfg, initial=False)
        return cfg

    def reload_now(self) -> WebUIConfig:
        """忽略 mtime，强制重读并应用（供界面操作写盘后调用）。写盘前已校验 → 必成功。"""
        cfg = self._read_and_validate()
        self._record_applied(cfg, initial=False)
        return cfg

    # ------------------------------------------------------------------ #
    # 原子写盘（界面实例管理）
    # ------------------------------------------------------------------ #
    def write_instances(self, instances: Sequence[InstanceConfig]) -> None:
        """把 instances 段原子写回 yaml（临时文件 + os.replace）。写失败抛 IOError。

        调用方保证新列表已通过校验（校验先于写盘，写盘后应用必成功）。
        """
        with open(self.path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            data = {}
        data["instances"] = [i.to_dict() for i in instances]
        tmp = f"{self.path}.tmp-{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _read_and_validate(self) -> WebUIConfig:
        if not os.path.exists(self.path):
            raise ConfigError(f"配置文件不存在: {self.path}")
        cfg = WebUIConfig.from_yaml(self.path)
        return cfg

    def _mtime_ns(self) -> Optional[int]:
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def _record_applied(self, cfg: WebUIConfig, initial: bool) -> None:
        old = self.last_config
        if not initial and old is not None:
            self._log_changes(old, cfg)
        elif initial:
            logger.info("configs: 已加载 %s (%d 实例, %d 规则)", self.path, len(cfg.instances), len(cfg.alerts))
        self.last_config = cfg
        self._last_mtime_ns = self._mtime_ns()

    def _log_changes(self, old: WebUIConfig, new: WebUIConfig) -> None:
        for section in NON_DYNAMIC_SECTIONS:
            old_v = getattr(old, section)
            new_v = getattr(new, section)
            if old_v != new_v:
                logger.warning(
                    "configs: 配置段 '%s' 变更需重启服务（requires restart），本次忽略",
                    section,
                )
        # 实例 diff（动态段，供状态追踪/日志）
        old_map = {i.name: i for i in old.instances}
        new_map = {i.name: i for i in new.instances}
        for name, inst in new_map.items():
            prev = old_map.get(name)
            if prev is None:
                logger.info("configs: 实例新增 %s (%s)", name, inst.url)
            elif prev != inst:
                logger.info("configs: 实例变更 %s -> %s", name, inst.url)
        for name in sorted(set(old_map) - set(new_map)):
            logger.info("configs: 实例删除 %s", name)
        # auth 凭据热重载即时生效
        if old.auth != new.auth:
            username_changed = old.auth.username != new.auth.username
            logger.info(
                "configs: auth 凭据热重载生效 (username%s)",
                " 已变更" if username_changed else " 保持不变, 仅密码变更",
            )