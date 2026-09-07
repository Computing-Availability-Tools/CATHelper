"""配置与检测器路径解析（design §3.8 / spec §2.11 / §2.12）。

检测器仅依赖 detector.yaml（算法阈值）+ 运行时注入的 tk2cat 映射。
配置路径固定为 configs/detector.yaml（项目根目录），不可通过 env 覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

METRICS_PATH_DEFAULT = "/anomaly/metrics"
CONFIG_PATH_DEFAULT = "/anomaly/config"
TOP_LOGPROBS_DEFAULT = 20
MONITOR_RATE_DEFAULT = 1.0
DETECTOR_WORKERS_DEFAULT = 4

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    low = raw.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.strip())


def _env_str(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


@dataclass
class PluginConfig:
    """中间件运行配置（env 读取 + 校验）。不可变快照。"""

    enabled: bool = True
    top_logprobs: int = TOP_LOGPROBS_DEFAULT
    metrics_path: str = METRICS_PATH_DEFAULT
    monitor_rate: float = MONITOR_RATE_DEFAULT
    detector_workers: int = DETECTOR_WORKERS_DEFAULT
    tokenizer_model: Optional[str] = None
    save_path: Optional[str] = None
    config_path: str = CONFIG_PATH_DEFAULT

    @classmethod
    def from_env(cls) -> "PluginConfig":
        top_logprobs = _env_int("VLLM_ANOMALY_TOP_LOGPROBS", TOP_LOGPROBS_DEFAULT)
        monitor_rate = _env_float("VLLM_ANOMALY_MONITOR_RATE", MONITOR_RATE_DEFAULT)
        if not isinstance(top_logprobs, int) or not (1 <= top_logprobs <= 20):
            raise ValueError(
                f"VLLM_ANOMALY_TOP_LOGPROBS 必须为 1-20 整数, 当前值: {top_logprobs}"
            )
        if not (0.0 <= monitor_rate <= 1.0):
            raise ValueError(
                f"VLLM_ANOMALY_MONITOR_RATE 必须为 0.0-1.0, 当前值: {monitor_rate}"
            )
        workers = _env_int("VLLM_ANOMALY_DETECTOR_WORKERS", DETECTOR_WORKERS_DEFAULT)
        if not isinstance(workers, int) or workers < 1:
            raise ValueError(
                f"VLLM_ANOMALY_DETECTOR_WORKERS 必须为正整数, 当前值: {workers}"
            )
        save_path = _env_str("VLLM_ANOMALY_SAVE_PATH")
        if save_path is not None and not os.path.isabs(save_path):
            raise ValueError(
                f"VLLM_ANOMALY_SAVE_PATH 必须为绝对路径, 当前值: {save_path}"
            )
        return cls(
            enabled=_env_bool("VLLM_ANOMALY_ENABLED", True),
            top_logprobs=top_logprobs,
            metrics_path=_env_str("VLLM_ANOMALY_METRICS_PATH") or METRICS_PATH_DEFAULT,
            monitor_rate=monitor_rate,
            detector_workers=workers,
            tokenizer_model=_env_str("VLLM_ANOMALY_TOKENIZER_MODEL"),
            save_path=save_path,
            config_path=_env_str("VLLM_ANOMALY_CONFIG_PATH") or CONFIG_PATH_DEFAULT,
        )


def resolve_config_path() -> str:
    """返回固定的检测器配置路径 configs/detector.yaml。

    路径固定为项目根目录下的 configs/detector.yaml，不可通过 env 覆盖。
    文件不存在 → raise（启动期 fail-fast）。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(os.path.dirname(base_dir), "configs", "detector.yaml")
    if os.path.isfile(cfg):
        return cfg
    raise FileNotFoundError(
        f"检测器配置文件缺失: {cfg} 不存在，请确认部署目录结构完整"
    )
