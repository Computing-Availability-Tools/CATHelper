# Elastic EP 技术规格说明书 (SPEC)

> **Elastic EP** — 推理大EP卡级弹性容错

---

## 1. 概述

### 1.1 软件定位

> 项目概述见 [README.md](README.md#elastic-ep)。

### 1.2 技术栈

| 项目 | 选型 |
|------|------|
| 开发语言 | Python 3.10+ |
| 目标平台 | Linux (ARM, Ascend NPU) |
| 基础框架 | vLLM v0.18.0 + vllm-ascend v0.18.0 |
| 通信协议 | ZMQ (DEALER/ROUTER/PUB/SUB) |
| 外部 API | REST (FastAPI) |
| 配置方式 | CLI 参数 + JSON 字典 |
| 专家均衡 | EPLB 框架 |
| 外部依赖 | zmq, msgspec, requests |
| 补丁方式 | Git patch (vllm + vllm-ascend) |

### 1.3 核心目标

在 vLLM 进程内实现弹性容错，针对局部性、瞬时性或可恢复的故障进行进程级恢复，避免实例重启。

---

## 2. 需求分析

### 2.1 目标故障场景

| 场景 | 说明 |
|------|------|
| 加速器故障 | NPU 设备崩溃、HBM UCE 等硬件错误 |
| 网络通信故障 | NIC/交换机/光模块异常导致的链路中断、丢包、短暂断连 |
| 主机侧故障 | CPU、内存等主机硬件异常 |

### 2.2 恢复方法

| 方法 | 说明 | 适用场景 |
|------|------|----------|
| 弹性缩容（scale down） | 隔离故障 NPU，动态调整资源分配，在剩余健康 NPU 上继续服务 | 不可恢复的硬件故障（设备崩溃、UCE 等） |
| 重试（retry） | 清理工作进程状态、重置 rank mask、重建通信组，重新执行推理 | 短暂性故障（网络抖动、瞬时通信超时等） |


### 2.3 功能需求

1. **故障上报**：故障发生后引擎不再立即退出，通过 ZMQ 向外报告异常详情与引擎健康状态，为上层框架提供故障诊断能力
2. **自动暂停**：故障发生时自动暂停健康 DP rank，防止级联失败（状态流转：健康 → 不健康 → 自动下发 pause → 已暂停）
3. **重试恢复**：针对瞬时性和可恢复故障，清理工作进程状态、重建 Gloo 通信组，恢复推理服务
4. **优雅缩容**：故障不可恢复时，移除故障 DP rank，重新分配专家（EPLB），重载权重，重建通信组，在剩余健康 NPU 上恢复服务

### 2.4 容错工作流

> 详细的容错工作流图与数据流详见 [DESIGN.md §1.3 容错工作流](DESIGN.md#13-容错工作流) 和 [DESIGN.md §1.4 数据流](DESIGN.md#14-数据流)。

---

## 3. 开发阶段划分

### Phase 1 — 核心容错框架

**目标**：从零搭建完整容错框架，包括故障上报、pause、retry、scale down 全部能力。

| 序号 | 任务 | 说明 |
|------|------|------|
| 1.1 | 项目初始化 | 目录结构，补丁框架 |
| 1.2 | ZMQ 通信 | DEALER/ROUTER/PUB/SUB 通道 |
| 1.3 | 故障上报框架 | 哨兵注册、故障消息定义、健康状态发布 |
| 1.4 | pause 指令实现 | 设置全局 pause 事件 → NPU `stop_device`（释放 NPU 设备资源），健康 DP rank 暂停请求处理 |
| 1.5 | retry 实现 | 状态清理、通信组重建、瞬时错误恢复 |
| 1.6 | scale down 工作流 | 暂停 → 重分配专家 → 重载权重 → 重建通信 |
| 1.7 | REST API | `/fault_tolerance/apply`, `/fault_tolerance/status` |
| 1.8 | Phase 1 完整测试 | 输出测试报告 |

### Phase 2 — 外部故障管理中心与完善

**目标**：增加模拟外部故障管理中心，完善量化&MTP模型支持。

| 序号 | 任务 | 说明 |
|------|------|------|
| 2.1 | 外部故障管理中心 | scale_down.py 双路径检测（DCMI 硬件轮询 + ZMQ 引擎健康订阅） |
| 2.2 | W8A8 量化适配 | ModelSlim 格式 W8A8 量化模型适配 |
| 2.3 | MTP 适配 | 多 Token 预测适配 |
| 2.4 | Phase 2 完整测试 | 更新测试报告 |

---

## 4. 测试要求

### 4.1 单元测试

```bash
pytest tests/v1/fault_tolerance/
```

### 4.2 测试流程

1. 提交代码前执行 `pytest tests/v1/fault_tolerance/`，确保所有单元测试通过
2. 每次变更后更新 `test_report.md`，记录单元测试与端到端测试结果
3. 端到端测试在 NPU 物理机上执行，使用 `serve_qwen.sh` 部署后注入故障验证

### 4.3 测试覆盖范围

- **单元测试**：`tests/v1/fault_tolerance/`，覆盖 ClientSentinel、EngineCoreSentinel、NPUWorkerSentinel 三个哨兵类的所有 public 方法（正常路径、异常路径、边界条件）
- **端到端测试**：在DP4/TP1 配置下注入 RuntimeError 和进程杀死两类故障，验证 pause → retry/scale_down 完整容错链路

---

## 5. 配置规格

### 5.1 CLI 参数

#### 5.1.1 vLLM Serve 参数

| 参数 | 默认值 | 描述 |
|------|--------|------|
| `--enable-fault-tolerance` | `False` | 启用容错框架 |
| `--enable-expert-parallel` | `False` | 启用专家并行（容错必需） |
| `--fault-tolerance-config` | `None` | 容错配置 JSON 字典（自动启用容错） |
| `--gloo-timeout-seconds` | `None`（回退到 600） | Gloo 进程组超时时间（秒） |

#### 5.1.2 FaultToleranceConfig

| 字段 | 默认值 | 描述 |
|------|--------|------|
| `engine_recovery_timeout_sec` | `120` | 等待恢复指令的秒数，超时后重新抛出原始错误 |
| `enable_fault_tolerance_rebalance` | `False` | 故障后重新调用 EPLB 进行专家负载均衡 |
| `internal_fault_report_port` | `22866` | 引擎向 ClientSentinel 报告故障的端口（内部 ZMQ） |
| `external_fault_notify_port` | `22867` | ClientSentinel 发布故障通知的端口（外部 ZMQ PUB） |

#### 5.1.3 serve_qwen.sh 脚本参数

| 参数 | 默认值 | 描述 |
|------|--------|------|
| `--dp-size` | `4` | 数据并行大小，即启动的 DP rank 数量 |
| `--redundant-experts` | `0` | 每个 rank 的冗余专家数量，缩容时有限制：全部健康卡上的冗余专家的总数必须大于故障卡上的逻辑专家数量 |
| `--host` | `0.0.0.0` | 服务器主机地址 |
| `--port` | `8006` | 服务器端口 |
| `--fault-port` | `22867` | 外部故障通知端口 |
| `--model-name` | `/qwen-ai/Qwen3-30B-A3B-W8A8` | 模型名称或路径 |
| `--local-model` | `nytopop/Qwen3-30B-A3B.w8a8` | 本地模型路径 |
| `--recovery-timeout` | `120` | 引擎恢复超时时间（秒），故障暂停后等待重试/缩容指令的最长时间，超时则抛异常退出 |
| `--gloo-timeout-seconds` | `30` | DP 域 Gloo CPU 通信组超时（秒）。故障 rank 同步阻塞时健康 rank 会等待此超时，需小于 `--recovery-timeout` 以避免容错指令超时 |

#### 5.1.4 scale_down.py 脚本参数

| 参数 | 默认值 | 描述 |
|------|--------|------|
| `--npu-ids` | `0-15` | 参与推理的 NPU 设备 ID 列表（逗号分隔） |
| `--interval-time` | `3` | NPU 健康状态轮询间隔（秒） |
| `--external-fault-notify-port` | `22867` | 订阅引擎健康状态的 ZMQ SUB 端口（需与 vLLM 的 `--fault-port` 一致） |
| `--port` | `8006` | vLLM API 端口，用于发送暂停/缩容指令 |
| `--host` | `localhost` | vLLM API 主机地址 |
| `--recovery-timeout` | `120` | 容错指令的超时等待时间（秒） |

### 5.2 配置文件

容错配置通过 `--fault-tolerance-config` CLI 参数以 JSON 字典形式传入：

```json
{
  "engine_recovery_timeout_sec": 120,
  "enable_fault_tolerance_rebalance": false,
  "internal_fault_report_port": 22866,
  "external_fault_notify_port": 22867
}
```

### 5.3 REST API

#### POST /fault_tolerance/apply

向运行中的 vLLM 实例发送容错指令。

**请求体：**

```json
{
    "instruction": "pause | retry | scale_down",
    "params": {
        "timeout": 30
    }
}
```

**指令说明：**

| 指令 | 参数 | 描述 |
|------|------|------|
| `pause` | `timeout`, `exclude_engine_index`（可选） | 暂停所有健康 DP rank。 |
| `retry` | `timeout` | 仅针对瞬时性可恢复故障。通过清理工作进程状态（input batch、model state等）、重建 Gloo 通信组等恢复推理服务 |
| `scale_down` | `timeout`, `exclude_dp_ranks` | 永久故障恢复。移除指定的故障DP rank来恢复推理服务 |

#### GET /fault_tolerance/status

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

### 5.4 通信通道

> 通信通道详见 [DESIGN.md §2.3 通信通道](DESIGN.md#23-通信通道)。

---

## 6. 依赖要求与限制

### 6.1 依赖要求

| 依赖 | 用途 | 备注 |
|------|------|------|
| zmq | ZMQ 通信 | 容错框架核心通信通道 |
| msgspec | 消息序列化 | 高性能序列化框架 |
| requests | HTTP 请求 | 外部 REST API 调用 |
| DCMI (`libdcmi.so`) | NPU 硬件状态查询 | 可选，仅模拟外部故障管理中心需要 |

### 6.2 限制

> 完整兼容性与限制详见 [Release_Notes.md §兼容性与限制](Release_Notes.md#兼容性与限制)。

| 限制 | 说明 |
|------|------|
| NPU 支持 | 仅支持华为昇腾 A3 服务器 |
| Expert Parallel | 必须开启专家并行 |
| Pipeline Parallel | 不支持 |
| Tensor Parallel | 仅支持 TP=1 |
| 动态 EPLB | 已兼容，支持故障后通过 EPLB 框架重新平衡专家放置 |
| 量化模型 | 仅兼容 W8A8（ModelSlim 格式），W4A8、W4A16 等暂不支持 |
| FULL Graph 模式 | 暂未兼容，不支持大模型整图捕获 |
| 冗余专家数 | 健康卡上的冗余专家总数必须大于故障卡上的逻辑专家数量 |
