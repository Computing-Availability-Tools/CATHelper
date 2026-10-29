# Prometheus 指标参考（C3/C4）

## 端点

- 指标端点：`GET <metrics_path>`（默认 `/anomaly/metrics`，可由 `VLLM_ANOMALY_METRICS_PATH` 配置）；
  由中间件**内联响应**，不依赖下游路由；Content-Type：`text/plain; version=0.0.4; charset=utf-8`；
  与 vLLM 默认 `/metrics` 使用隔离的 registry，互不影响；
- 动态配置端点：`GET/POST <config_path>`（默认 `/anomaly/config`），仅 `monitor_rate` 可热更
  （校验 ∈ [0,1]，非法返回 400；不持久化，重启回退 env）；
- 关联头：每个被拦截请求的响应含 `x-anomaly-request-id`（唯一，用于端到端追踪）。

## 指标清单

| 指标 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `vllm_anomaly_requests_total` | Counter | — | 已被异常监控处理（注入+检测链路）的请求数 |
| `vllm_anomaly_detected_total` | Counter | `ill_type`, `model` | 异常检出计数；ill_type：0=normal（不计入 detected）1=生僻字 2=乱码 3=重复 4=NaN |
| `vllm_anomaly_detection_errors_total` | Counter | — | 检测执行错误计数（不影响客户端与后续检测） |
| `vllm_anomaly_detection_duration_seconds` | Histogram | — | 单次检测耗时分布 |
| `vllm_anomaly_last_result` | Gauge | `ill_type`, `model` | 最近一次检测结果 |
| `vllm_anomaly_last_id` | Gauge | `model` | 最近落盘的 pkl 异常编号 |
| `vllm_anomaly_last_timestamp_seconds` | Gauge | `model` | 最近异常检出时间（Unix 秒） |
| `vllm_anomaly_monitor_rate` | Gauge | — | 当前监控采样率（运行时热更后同步） |

注：`model` 标签取请求体 `model` 字段（缺失用 `"unknown"`）。

## 常用查询

```bash
# 当前检出分布（按类型）
curl -s http://<host>:<port>/anomaly/metrics | grep '^vllm_anomaly_detected_total'

# 采样率与最近异常
curl -s http://<host>:<port>/anomaly/metrics | grep -E 'monitor_rate|last_id|last_timestamp'
```

```promql
# Prometheus 服务端（已接入抓取时）
sum by (ill_type) (increase(vllm_anomaly_detected_total[24h]))
rate(vllm_anomaly_detected_total{ill_type="3"}[1h])
sum by (instance) (vllm_anomaly_detected_total)
```

## 三通道检视决策表（"想知道 X，看通道 Y"）

| 你想知道 | 通道 | 怎么看 | 特性 |
|---|---|---|---|
| 现在有没有异常在发生（秒级） | webui 看板 | 累计统计/异常趋势/类型分布/实例状态卡 | 实时轮询（2-5s），重启清零 |
| 某类异常最近 1h/24h 是否变密 | Agent 读数回答 | 数据源优先级：webui `/api/trends` → pkl 分桶 → PromQL | 见下方回答协议 |
| 接入公司监控告警体系 | 本端点 | Prometheus 抓取 + PromQL | 标准文本格式，机器读 |
| 阈值告警（钉钉/飞书/企微/邮件） | webui | `configs/webui.yaml` alerts 滑动窗口规则 | 触发后窗口去抖 |
| 某次异常的完整现场 | pkl 落盘 | 按 pkl-data-contract.md 读取 | 唯一全量现场，按编号 |
| 昨天/上周/任意区间检出情况 | 报告 | anomaly_report.py | 离线聚合，可回查编号 |

## 密度/趋势类问题的回答协议（"某类异常最近有没有变密"）

Agent **必须读数据、给结论**，不得以"请查看 webui 看板"推诿；看板路径仅作回答末尾的复核指引。

数据源优先级：

1. webui 在运行：`POST /api/login` 取 token → `GET /api/trends`（1h/4h/8h/16h/24h 窗口）；
2. pkl 已落盘：按小时/日分桶计数对比——**任意历史窗口**（含 webui 窗口外）；
3. 已接入 Prometheus 历史抓取：`increase(vllm_anomaly_detected_total[...])`；
4. 均不可用 → **如实回答无法判定**（历史数据已丢失），给出补救（拉起 webui / 开启落盘）。

结论口径（防误判，逐条遵守）：

- **密度 ≠ 计数**：基于 Δ检出 / Δ请求数（剔除流量涨落），并附绝对计数；
- **对比窗口等长**（如最近 24h vs 前 24h）；
- **monitor_rate 陷阱**：窗口内若调整过采样率（不留历史），检出数突变可能只是采样率变化——
  查当前值并声明该风险；
- **存活窗口声明**：webui 重启清零、pkl 自开启日起才有——结论只覆盖数据源实际存活区间。

结论格式：一句结论（变密/变稀/持平/无法判定）→ 证据（两窗口对照 + 原文摘录）→ 看板路径（指明看哪张图）。
