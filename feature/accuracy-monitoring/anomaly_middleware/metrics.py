"""Prometheus 指标（design §3.7 / spec §2.10）。

独立 CollectorRegistry，与 vLLM 默认 /metrics 隔离。
- requests_total：按 HTTP 请求计数（每被检测请求 +1）。
- detected_total：每个异常候选 +1；labels ill_type, model, choice_index（第几个候选异常）。
- detection_errors_total：每次检测失败 +1。
- detection_duration_seconds：每次检测耗时直方图。
- 四种异常分别独立 gauge（rare_character / garbled / repetition / nan_value），
  按 model 标签，值=该模型最近一次请求是否检出该类异常（1/0）。
"""
from __future__ import annotations

from typing import Sequence

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# ill_type 取值：0=normal,1=rare_character,2=garbled,3=repetition,4=nan_value
ILL_NORMAL = 0
ILL_RARE = 1
ILL_GARBLED = 2
ILL_REPETITION = 3
ILL_NAN = 4


class Metrics:
    """独立 registry + 指标记录/渲染。"""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests_total = Counter(
            "vllm_anomaly_requests_total",
            "被检测的推理请求数（按 HTTP 请求计数）",
            registry=self.registry,
        )
        self.detected_total = Counter(
            "vllm_anomaly_detected_total",
            "检出异常的候选数（按 choice 计数；choice_index 表示第几个候选）",
            ["ill_type", "model", "choice_index"],
            registry=self.registry,
        )
        self.detection_errors_total = Counter(
            "vllm_anomaly_detection_errors_total",
            "检测失败次数",
            registry=self.registry,
        )
        self.detection_duration = Histogram(
            "vllm_anomaly_detection_duration_seconds",
            "检测耗时（秒）",
            registry=self.registry,
        )
        # 四种异常分别独立 gauge
        self.last_rare_character = Gauge(
            "vllm_anomaly_last_rare_character",
            "该模型最近一次请求是否检出生僻字（1=是,0=否）",
            ["model"],
            registry=self.registry,
        )
        self.last_garbled = Gauge(
            "vllm_anomaly_last_garbled",
            "该模型最近一次请求是否检出乱码（1=是,0=否）",
            ["model"],
            registry=self.registry,
        )
        self.last_repetition = Gauge(
            "vllm_anomaly_last_repetition",
            "该模型最近一次请求是否检出重复（1=是,0=否）",
            ["model"],
            registry=self.registry,
        )
        self.last_nan_value = Gauge(
            "vllm_anomaly_last_nan_value",
            "该模型最近一次请求是否检出 NaN（1=是,0=否）",
            ["model"],
            registry=self.registry,
        )
        # 异常编号 / 时间戳（与本地保存文件对应；不改动现有指标/标签）
        self.last_anomaly_id = Gauge(
            "vllm_anomaly_last_id",
            "该模型最近一次检出异常的编号（与本地保存文件 key 对应）",
            ["model"],
            registry=self.registry,
        )
        self.last_anomaly_timestamp = Gauge(
            "vllm_anomaly_last_timestamp_seconds",
            "该模型最近一次检出异常的时间戳（Unix 秒）",
            ["model"],
            registry=self.registry,
        )
        self._gauge_by_ill_type = {
            ILL_RARE: self.last_rare_character,
            ILL_GARBLED: self.last_garbled,
            ILL_REPETITION: self.last_repetition,
            ILL_NAN: self.last_nan_value,
        }
        self.monitor_rate_gauge = Gauge(
            "vllm_anomaly_monitor_rate",
            "当前异常监控概率（运行时可通过 POST /anomaly/config 更新）",
            registry=self.registry,
        )

    def record_detection(
        self,
        results: Sequence[Sequence],
        model: str,
    ) -> None:
        """记录一次检测（对应一个 HTTP 请求的 n 个候选结果）。

        results: list[[is_ill, ill_type]]，与 choice 平行。
        - 按请求计数：requests_total +1（无论正常/异常）。
        - 每个异常候选：detected_total{ill_type,model,choice_index} +1。
        - 正常（ill_type=0）只增 requests，不计 detected（§3.7）。
        - 四个 gauge 按 model：该请求是否出现对应异常类型（1/0）。
        异常全捕获：记录失败不得影响客户端。
        """
        try:
            self.requests_total.inc()
            seen_types = set()
            for idx, res in enumerate(results):
                if not res or len(res) < 2:
                    continue
                is_ill = bool(res[0])
                ill_type = int(res[1])
                if is_ill and ill_type != ILL_NORMAL:
                    self.detected_total.labels(
                        ill_type=str(ill_type),
                        model=model,
                        choice_index=str(idx),
                    ).inc()
                    seen_types.add(ill_type)
            # 四个 gauge：出现对应类型=1，否则=0
            for ill_type, gauge in self._gauge_by_ill_type.items():
                gauge.labels(model=model).set(1 if ill_type in seen_types else 0)
        except Exception:
            # 指标记录失败不得影响客户端
            pass

    def record_error(self) -> None:
        try:
            self.detection_errors_total.inc()
        except Exception:
            pass

    def record_anomaly(
        self,
        anomaly_id: int,
        timestamp: float,
        model: str,
    ) -> None:
        """记录最近一次异常的编号与时间戳（异常全捕获，不影响客户端）。"""
        try:
            self.last_anomaly_id.labels(model=model).set(int(anomaly_id))
            self.last_anomaly_timestamp.labels(model=model).set(str(timestamp*1000).split('.')[0]) # float(timestamp)
        except Exception:
            pass

    def set_monitor_rate(self, rate: float) -> None:
        try:
            self.monitor_rate_gauge.set(rate)
        except Exception:
            pass

    def render_metrics(self) -> bytes:
        return generate_latest(self.registry)


def parse_ill_type_name(ill_type: int) -> str:
    names = {
        ILL_NORMAL: "normal",
        ILL_RARE: "rare_character",
        ILL_GARBLED: "garbled",
        ILL_REPETITION: "repetition",
        ILL_NAN: "nan_value",
    }
    return names.get(int(ill_type), "unknown")
