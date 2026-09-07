# 推理精度异常检测

## 1 简介
推理精度异常检测基于模型输出的 token 和 logprobs 序列，在无侵入、零参照知识的条件下，实时检测推理过程中可能出现的异常响应，对企业级GenAI推理服务中出现的生僻字、乱码、重复输出等输出崩溃类故障进行在线实时、高准确率的异常检测。

- 生僻字：偶发性输出无意义字符，且不符合上下文语境。
- 乱码：模型持续输出生僻字，明显胡言乱语，文本无意义，无法正常对话。
- 重复：重复输出相同内容。
- NaN Value：logprobs 出现 nan/inf 值


当前检测功能是通过 vLLM `--middleware` 插件部署，透明拦截推理请求，
强制采集 logprobs 与 token_id，后台运行异常检测算法，并通过独立 Prometheus 端点暴露检测结果。
全过程对客户端无感知——不影响响应状态、不阻塞响应返回、不泄漏内部参数。



## 2 快速开始

### 2.1 安装

```powershell
# 进入项目路径
cd accuracy-monitoring/

# 安装包
pip install -e .
```

依赖：`prometheus_client`、`pyyaml`、`numpy`、`httpx`、`colorlog`

### 2.2 部署

```powershell
vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware
```

请确保当前使用的 vLLM 支持 `--middleware`。


### 2.3 发送推理请求

```powershell
# 发送推理请求（中间件自动拦截注入检测，用户无感知）
curl http://localhost:8000/v1/chat/completions -d '{"model":"...","messages":[...]}'
```

### 2.4 异常指标监控

用户可查看端点 anomaly/metrics，查看推理异常检测情况。
```powershell
# 查看检测指标，端点：anomaly/metrics
curl http://localhost:8000/anomaly/metrics
```

### 2.5 推理精度异常监控 Web 界面
[ Web 推理精度异常监控 ](./webui_README.md)，独立的 Web 服务，支持多 vLLM 实例聚合可视化推理精度异常检测现象，并支持可配置的阈值告警和多渠道告警（界面告警 + Webhook(钉钉、飞书、企业微信) + 邮箱通知）。


## 3 可选环境变量

### 3.1 环境变量说明

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `VLLM_ANOMALY_ENABLED` | `1` | 总开关，`0`/`false` → 纯透传不检测 |
| `VLLM_ANOMALY_MONITOR_RATE` | `1.0` | 请求被异常监控的概率，范围 0.0-1.0，`0` 不检测 |
| `VLLM_ANOMALY_TOP_LOGPROBS` | `20` | 注入的 top-logprobs 数量，范围 1-20 |
| `VLLM_ANOMALY_METRICS_PATH` | `/anomaly/metrics` | 指标端点路径 |
| `VLLM_ANOMALY_TOKENIZER_MODEL` | None | 显式指定模型绝对路径 |
| `VLLM_ANOMALY_SAVE_PATH` | None | 异常详细数据保存到本地，如需开启，请配置保存的路径。默认不开启 |


### 3.2 配置方式
#### (1) 配置示例
以 VLLM_ANOMALY_TOP_LOGPROBS 为例，当需要显式设置 VLLM_ANOMALY_TOP_LOGPROBS 数量时，可在拉起服务前配置全局环境变量。
```powershell
export VLLM_ANOMALY_TOP_LOGPROBS=10  # 配置 VLLM_ANOMALY_TOP_LOGPROBS 为 10
vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware
```

#### (2) VLLM_ANOMALY_SAVE_PATH 详细介绍
a) 当开启此功能时，请配置存储文件的绝对路径，文件存储为 pkl 格式。
```powershell
export VLLM_ANOMALY_SAVE_PATH=/xxx/qwen3.pkl   # 方法1，指定存储的文件
# export VLLM_ANOMALY_SAVE_PATH=/xxx/ # 方法2，指定存储的文件夹，系统默认以模型名作为文件名
vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware
```

b) 当开启此功能时，可将异常详细信息保存到本地，便于后续做故障复现。每条异常信息内容如下：
| 字段 |  说明 |
|---|---|
| `time` | 时间戳 | 
| `prompt` | 请求 prompt | 
| `ill_type` | 异常类型 | 
| `topk_logprobs` | topk logprobs |
| `tokens_ids` | token ids | 
| `text` | 推理输出文本 | 
| `model_name` | 模型名称 |

#### (3) VLLM_ANOMALY_MONITOR_RATE 详细介绍
VLLM_ANOMALY_MONITOR_RATE 表示请求被异常监控的概率，取值范围在 0.0-1.0。有静态和动态两种配置方式。

a) 静态配置

在拉起服务时配置全局参数
```
export VLLM_ANOMALY_MONITOR_RATE=0.3  # 表示每个请求有 30% 的概率会被监控
vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware
```

b) 动态配置

当需要根据当前请求量和精度异常检出数量来动态配置 VLLM_ANOMALY_MONITOR_RATE 时，可以使用下述命令修改当前请求被异常监控的概率。
```
curl -X POST http://localhost:${port}/anomaly/config \
  -H "Content-Type: application/json" \
  -d '{"monitor_rate": 0.2}'
```

## 4 检测算法阈值配置

检测器算法默认参数在 `configs/detector.yaml`，包含窗口大小、各类异常阈值等，用户可根据需要进行配置：

```yaml
window_size: 128    # 检测窗口大小
stride: 64          # 滑窗步长

rare_character:     # 生僻字检测
  explogp_sum_thresh: 0.4
  category_thresh: 2
  top1_logp_thresh: -6

garbled:            # 乱码检测
  top1_logp_thresh: -5
  window_ratio: 0.2
  window_thresh: 0

repetition:         # 重复检测
  trajectory:
    n: 3
    distinct_n_thresh: 0.2
    logp_thresh: -0.2
  acf:
    acf_threshold: 0.65
    logp_thresh: -0.2
  single_window_thresh: 14
  multi_window_thresh: 2
```


## 5 Prometheus 指标

访问 `GET /anomaly/metrics`（默认路径），Content-Type: `text/plain; version=0.0.4; charset=utf-8`。

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `vllm_anomaly_requests_total` | Counter | — | 被检测请求计数 |
| `vllm_anomaly_detected_total` | Counter | `ill_type`, `model` | 检出异常计数 |
| `vllm_anomaly_detection_errors_total` | Counter | — | 检测失败计数 |
| `vllm_anomaly_detection_duration_seconds` | Histogram | — | 检测耗时 |
| `vllm_anomaly_last_rare_character` | Gauge | `model` | 最近生僻字结果（ill_type=1） |
| `vllm_anomaly_last_garbled` | Gauge | `model` | 最近乱码结果（ill_type=2） |
| `vllm_anomaly_last_repetition` | Gauge | `model` | 最近重复结果（ill_type=3） |
| `vllm_anomaly_last_nan_value` | Gauge | `model` | 最近 NaN 结果（ill_type=4） |

`ill_type` 取值：`0`=normal, `1`=rare_character, `2`=garbled, `3`=repetition, `4`=nan_value。
`model` 标签来自请求体 `model` 字段，缺失用 `"unknown"`。

