"""env 单元测试：PluginConfig.from_env 校验 + resolve_config_path（spec §2.11 §2.12）。

路径解析固定返回 configs/detector.yaml（不可 env 覆盖）。
"""
from __future__ import annotations

import os

import pytest

from anomaly_middleware.env import PluginConfig, resolve_config_path


def test_config_defaults(monkeypatch):
    for k in list(os.environ):
        if k.startswith("VLLM_ANOMALY"):
            monkeypatch.delenv(k, raising=False)
    c = PluginConfig.from_env()
    assert c.enabled is True
    assert c.top_logprobs == 20
    assert c.metrics_path == "/anomaly/metrics"
    assert c.monitor_rate == 1.0
    assert c.detector_workers == 4


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_ENABLED", "0")
    monkeypatch.setenv("VLLM_ANOMALY_TOP_LOGPROBS", "5")
    monkeypatch.setenv("VLLM_ANOMALY_MONITOR_RATE", "0.3")
    monkeypatch.setenv("VLLM_ANOMALY_METRICS_PATH", "/x/m")
    monkeypatch.setenv("VLLM_ANOMALY_DETECTOR_WORKERS", "2")
    c = PluginConfig.from_env()
    assert c.enabled is False
    assert c.top_logprobs == 5
    assert c.monitor_rate == 0.3
    assert c.metrics_path == "/x/m"
    assert c.detector_workers == 2


def test_config_invalid_top_logprobs(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_TOP_LOGPROBS", "0")
    with pytest.raises(ValueError):
        PluginConfig.from_env()


def test_config_invalid_top_logprobs_high(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_TOP_LOGPROBS", "21")
    with pytest.raises(ValueError):
        PluginConfig.from_env()


def test_config_invalid_monitor_rate(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_MONITOR_RATE", "1.5")
    with pytest.raises(ValueError):
        PluginConfig.from_env()


def test_resolve_config_path_default():
    path = resolve_config_path()
    assert path is not None
    assert os.path.isfile(path) and path.endswith("detector.yaml")


def test_resolve_config_path_missing_raises(monkeypatch):
    import anomaly_middleware.env as env_mod
    monkeypatch.setattr(env_mod.os.path, "isfile", lambda _p: False)
    with pytest.raises(FileNotFoundError):
        resolve_config_path()


# --------------------------- tokenizer_model --------------------------- #
def test_tokenizer_model_default_none(monkeypatch):
    monkeypatch.delenv("VLLM_ANOMALY_TOKENIZER_MODEL", raising=False)
    cfg = PluginConfig.from_env()
    assert cfg.tokenizer_model is None


def test_tokenizer_model_env(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_TOKENIZER_MODEL", "/data/Qwen3.0.6B")
    cfg = PluginConfig.from_env()
    assert cfg.tokenizer_model == "/data/Qwen3.0.6B"


# --------------------------- 边界/非法值回退（spec §2.11） --------------------------- #
def test_config_workers_zero_raises(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_DETECTOR_WORKERS", "0")
    with pytest.raises(ValueError):
        PluginConfig.from_env()  # <1 -> 启动期 raise（fail-fast, spec §2.11）


def test_config_enabled_invalid_string_defaults_true(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_ENABLED", "banana")  # 非 0/false -> 默认 True
    c = PluginConfig.from_env()
    assert c.enabled is True


def test_config_monitor_rate_boundaries_valid(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_MONITOR_RATE", "0.0")
    assert PluginConfig.from_env().monitor_rate == 0.0
    monkeypatch.setenv("VLLM_ANOMALY_MONITOR_RATE", "1.0")
    assert PluginConfig.from_env().monitor_rate == 1.0


def test_config_metrics_path_empty_uses_default(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_METRICS_PATH", "   ")
    c = PluginConfig.from_env()
    assert c.metrics_path == "/anomaly/metrics"


# --------------------------- config_path --------------------------- #
def test_config_path_default(monkeypatch):
    for k in list(os.environ):
        if k.startswith("VLLM_ANOMALY"):
            monkeypatch.delenv(k, raising=False)
    c = PluginConfig.from_env()
    assert c.config_path == "/anomaly/config"


def test_config_path_env_override(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_CONFIG_PATH", "/custom/cfg")
    c = PluginConfig.from_env()
    assert c.config_path == "/custom/cfg"


def test_config_path_empty_uses_default(monkeypatch):
    monkeypatch.setenv("VLLM_ANOMALY_CONFIG_PATH", "   ")
    c = PluginConfig.from_env()
    assert c.config_path == "/anomaly/config"
