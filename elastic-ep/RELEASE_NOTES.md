# 发布说明

## v0.1.0

- **版本:** v0.1.0
- **发布日期:** 2026-07-16
- **发布人员:** a798347923

### 功能特性

- 核心容错框架，采用三级哨兵层级架构（ClientSentinel、EngineCoreSentinel、WorkerSentinel）
- 基于ZMQ的通信机制，用于故障报告和指令分发
- REST API，用于外部容错控制（`/fault_tolerance/apply`、`/fault_tolerance/status`）
- 优雅缩容：暂停受影响的等级、重新分配专家、重载权重、重新初始化通信组
- 动态EPLB集成，用于故障后专家负载均衡
- 通过DCMI轮询实现外部NPU硬件故障监控
- `--enforce-eager` 模式支持
- PIECEWISE ACL Graph 模式支持
- MTP（多令牌预测）支持

### 测试模型

- DeepSeek-V3 (DSv3)
- Qwen3-235B-A22B
- GLM5

### 已知问题

在**第二次缩容**期间，偶尔可能出现以下问题：

1. 故障权重加载时间显著增加
2. `stop device` 无法停止，阻塞缩容流程
3. 恢复后工作进程卡在 `input_event` 同步中

### 补丁

- `patches/vllm_scale_down.patch`：vLLM v0.18.0核心容错框架
- `patches/vllm_ascend_scale_down.patch`：vllm-ascend v0.18.0昇腾特定适配
