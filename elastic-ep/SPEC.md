# vLLM 弹性容错规格说明

## 系统要求

| 组件 | 要求 |
|------|------|
| 硬件 | 华为昇腾 910C NPU |
| 操作系统 | Linux（昇腾兼容） |
| Docker镜像 | `quay.io/ascend/vllm-ascend:v0.18.0-a3` |
| vLLM | v0.18.0（带昇腾后端 `vllm_ascend`） |
| DCMI库 | `/usr/local/dcmi/libdcmi.so`（可选，用于NPU监控） |
| Python包 | `zmq`, `msgspec`, `requests` |

## 补丁

| 补丁 | 目标 | 描述 |
|------|------|------|
| `patches/vllm_scale_down.patch` | `vllm-project/vllm` (v0.18.0) | 核心容错框架：哨兵层级、ZMQ通信、HTTP API、引擎健康监控、缩容编排 |
| `patches/vllm_ascend_scale_down.patch` | `vllm-project/vllm-ascend` (v0.18.0) | 昇腾特定：NPU工作进程哨兵、ScaleDownHelper（7阶段工作流）、DCMI硬件监控、EPLB故障重排策略 |

## CLI参数

### vLLM Serve参数

| 参数 | 默认值 | 描述 |
|------|--------|------|
| `--enable-fault-tolerance` | `False` | 启用容错框架 |
| `--enable-expert-parallel` | `False` | 启用专家并行（容错必需） |
| `--fault-tolerance-config` | `None` | 容错配置JSON字典（自动启用容错） |
| `--gloo-timeout-seconds` | `None`（回退到600） | Gloo进程组超时时间（秒） |

### FaultToleranceConfig

| 字段 | 默认值 | 描述 |
|------|--------|------|
| `engine_recovery_timeout_sec` | `120` | 等待恢复指令的秒数，超时后重新抛出原始错误 |
| `enable_fault_tolerance_rebalance` | `False` | 故障后重新调用EPLB进行专家负载均衡 |
| `internal_fault_report_port` | `22866` | 引擎向ClientSentinel报告故障的端口（内部） |
| `external_fault_notify_port` | `22867` | ClientSentinel发布故障通知的端口（外部ZMQ PUB） |

### serve_qwen.sh脚本参数

| 参数 | 默认值 | 描述 |
|------|--------|------|
| `--dp` | `4` | 数据并行大小 |
| `--re` | `0` | 冗余专家数量 |
| `--host` | `0.0.0.0` | 服务器主机地址 |
| `--port` | `8006` | 服务器端口 |
| `--fault-port` | `22867` | 外部故障通知端口 |
| `--model-name` | `/qwen-ai/Qwen3-30B-A3B-W8A8` | 模型名称或路径 |
| `--local-model` | `nytopop/Qwen3-30B-A3B.w8a8` | 本地模型路径 |
| `--recovery-timeout` | `120` | 引擎恢复超时时间（秒） |
| `--gloo-timeout-seconds` | `30` | Gloo通信组超时时间（秒） |

## REST API

### POST /fault_tolerance/apply

向运行中的vLLM实例发送容错指令。

**请求体：**

```json
{
    "instruction": "pause | retry | scale_down",
    "params": {
        "timeout": 30
    }
}
```

**指令：**

| 指令 | 必需参数 | 可选参数 | 描述 |
|------|----------|----------|------|
| `pause` | `timeout` | `exclude_engine_index` | 暂停指定或所有数据并行等级的请求处理 |
| `retry` | `timeout` | — | 清理工作进程状态并重新初始化通信 |
| `scale_down` | `timeout`, `exclude_dp_ranks` | — | 移除指定的数据并行等级并重新分配专家 |

### GET /fault_tolerance/status

返回当前引擎健康状态。

**响应：**

```json
{
    "total_engines": 4,
    "engines": [
        {"id": 0, "status": "healthy"},
        {"id": 1, "status": "healthy"},
        {"id": 2, "status": "dead"},
        {"id": 3, "status": "healthy"}
    ]
}
```

## 功能状态

| 功能 | 状态 | 备注 |
|------|------|------|
| 动态EPLB | 完全支持 | 故障后通过EPLB框架重新平衡专家放置 |
| 量化模型（W8A8） | 支持 | 适配昇腾格式W8A8量化 |
| 量化模型（W4A8） | 暂不支持 | W4A8量化尚未适配 |
| MTP（多令牌预测） | 支持 | 在GLM5上适配并测试 |
| `--enforce-eager` 模式 | 支持 | 禁用图捕获，在急切模式下运行 |
| PIECEWISE ACL Graph 模式 | 支持 | 大模型分块图捕获 |

## 通信通道

| 通道 | 协议 | 方向 | 用途 |
|------|------|------|------|
| 引擎故障套接字 | ZMQ DEALER/ROUTER | 引擎 -> ClientSentinel | 报告引擎异常 |
| 故障状态PUB/SUB | ZMQ PUB/SUB | ClientSentinel -> 外部 | 广播引擎健康状态 |
| 容错请求/结果 | ZMQ DEALER/PUSH | ClientSentinel -> 引擎 | 分发暂停/重试/缩容指令 |
| 工作进程命令 | ZMQ ROUTER/DEALER | EngineCore -> 工作进程 | 工作进程级控制 |
| HTTP API | REST | 外部 -> API服务器 | 外部容错控制 |
