# vLLM Elastic EP Release Notes

> 本文档按时间倒序记录每次发布的版本信息。每次发布在顶部追加，不删除历史记录。

---

## v0.1.0

| 项目 | 说明 |
|------|------|
| 版本号 | v0.1.0 |
| 发布时间 | 2026-07-16 |
| 发布人 | sunnytao-blue |
| 平台支持 | Linux (ARM, Ascend NPU) |

### 变更摘要

- **容错框架**：采用三级哨兵层级架构（ClientSentinel、EngineCoreSentinel、WorkerSentinel），支持通过 REST API 与外部的实例故障管理中心协同
- **故障上报**： 提供主动（外部实例故障管理中心通过 REST API）和被动（vLLM内部通过 ZMQ）2种方式上报故障到Client层
- **优雅缩容**： 故障发生时暂停实例，通过执行重试、缩容恢复指令实现快速自愈
- **ZMQ 通信机制**：基于 ZMQ DEALER/ROUTER/PUB/SUB 的故障报告和指令分发通道
- **REST API**：提供 `/fault_tolerance/apply`（pause/retry/scale_down）和 `/fault_tolerance/status` 外部控制接口
- **动态 EPLB 集成**：故障后通过 EPLB 框架重新平衡专家放置
- **外部 NPU 硬件故障监控**：scale_down.py 通过 DCMI 轮询 NPU 健康状态
- **量化模型支持**：W8A8 量化模型适配（ModelSlim 格式）
- **MTP 支持**：多 Token 预测适配，在 GLM5.1 上完成测试
- **图模式支持**：`--enforce-eager` 模式 + PIECEWISE ACL Graph 模式

### 已知限制

1. **第二次缩容存在一些偶现问题**
2. **W4A8 量化模型**：暂未适配
3. **FULL Graph 模式**：暂不支持
4. **扩容**：当前版本不支持扩容
