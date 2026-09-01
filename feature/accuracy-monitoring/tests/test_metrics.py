"""metrics 单元测试：独立 registry + per-HTTP-request + choice_index + 四 gauge（spec §2.10）。"""
from __future__ import annotations

from anomaly_middleware.metrics import (
    ILL_GARBLED,
    ILL_NAN,
    ILL_NORMAL,
    ILL_RARE,
    ILL_REPETITION,
    METRICS_CONTENT_TYPE,
    Metrics,
)


def test_metrics_content_type():
    assert METRICS_CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


def test_record_detection_normal_only_requests():
    m = Metrics()
    m.record_detection([[False, ILL_NORMAL], [False, ILL_NORMAL]], "glm-4-7")
    text = m.render_metrics().decode()
    assert "vllm_anomaly_requests_total" in text
    # normal 不计 detected
    assert "vllm_anomaly_detected_total" in text
    # requests_total 应为 1（按 HTTP 请求计数，不是按 choice）
    assert 'vllm_anomaly_requests_total 1.0' in text or 'vllm_anomaly_requests_total 1' in text


def test_record_detection_anomaly_choice_index():
    m = Metrics()
    # n=3：choice0 正常，choice1 生僻字，choice2 乱码
    m.record_detection(
        [[False, ILL_NORMAL], [True, ILL_RARE], [True, ILL_GARBLED]], "glm-4-7"
    )
    text = m.render_metrics().decode()
    # 每请求 +1
    assert "vllm_anomaly_requests_total 1" in text
    # 两个异常分别上报，choice_index 区分
    assert 'ill_type="1"' in text and 'choice_index="1"' in text
    assert 'ill_type="2"' in text and 'choice_index="2"' in text
    # 四 gauge：rare=1, garbled=1, repetition=0, nan=0
    assert 'vllm_anomaly_last_rare_character{model="glm-4-7"} 1.0' in text
    assert 'vllm_anomaly_last_garbled{model="glm-4-7"} 1.0' in text
    assert 'vllm_anomaly_last_repetition{model="glm-4-7"} 0.0' in text
    assert 'vllm_anomaly_last_nan_value{model="glm-4-7"} 0.0' in text


def test_record_detection_nan_type():
    m = Metrics()
    m.record_detection([[True, ILL_NAN]], "m")
    text = m.render_metrics().decode()
    assert 'vllm_anomaly_last_nan_value{model="m"} 1.0' in text
    assert 'ill_type="4"' in text


def test_record_detection_repetition_type():
    m = Metrics()
    m.record_detection([[True, ILL_REPETITION]], "m")
    text = m.render_metrics().decode()
    assert 'vllm_anomaly_last_repetition{model="m"} 1.0' in text
    assert 'ill_type="3"' in text


def test_record_error():
    m = Metrics()
    m.record_error()
    m.record_error()
    text = m.render_metrics().decode()
    assert "vllm_anomaly_detection_errors_total 2" in text


def test_record_detection_accumulates_requests_per_request():
    m = Metrics()
    m.record_detection([[False, ILL_NORMAL]], "m")  # 请求1
    m.record_detection([[False, ILL_NORMAL]], "m")  # 请求2
    text = m.render_metrics().decode()
    assert "vllm_anomaly_requests_total 2" in text


def test_record_detection_unknown_model_label():
    m = Metrics()
    # record_detection 用传入的 model；middleware 传 "unknown" 缺失时
    m.record_detection([[True, ILL_RARE]], "unknown")
    text = m.render_metrics().decode()
    assert 'model="unknown"' in text


def test_registry_isolated_from_default():
    m = Metrics()
    # 独立 registry：不应包含 vllm 默认 /metrics 内容（仅含本中间件指标）
    text = m.render_metrics().decode()
    assert "vllm_anomaly_" in text
    # 确认独立 registry 实例
    assert m.registry is not Metrics().registry


def test_record_detection_does_not_raise_on_bad_input():
    m = Metrics()
    m.record_detection([], "m")  # 空结果
    m.record_detection([[]], "m")  # 空 choice 结果
    m.record_detection("garbage", "m")  # 非法
    # 不抛异常即通过
    assert b"vllm_anomaly" in m.render_metrics()


def test_monitor_rate_gauge_exists():
    m = Metrics()
    text = m.render_metrics().decode()
    assert "vllm_anomaly_monitor_rate" in text


def test_set_monitor_rate():
    m = Metrics()
    m.set_monitor_rate(0.3)
    text = m.render_metrics().decode()
    assert "vllm_anomaly_monitor_rate 0.3" in text


def test_set_monitor_rate_default_zero():
    m = Metrics()
    text = m.render_metrics().decode()
    assert "vllm_anomaly_monitor_rate 0.0" in text
