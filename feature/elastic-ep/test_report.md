# Elastic EP 测试报告

> **项目**: CATHelper — Elastic EP 容错框架
> **版本**: Phase 1
> **日期**: 2026-07-21
> **测试执行**: OpenCode + Pytest

---

## 1. 测试概述

### 1.1 测试目标

验证容错框架核心功能的正确性，包括：

- 三个哨兵类（ClientSentinel、EngineCoreSentinel、NPUWorkerSentinel）所有 public 方法
- 端到端容错恢复：故障检测→暂停→容错恢复（retry/scale_down）

### 1.2 测试结果汇总

| 指标 | 结果 |
|------|------|
| 测试总数 | 66 |
| 通过 | 66 |
| 失败 | 0 |
| 通过率 | 100% |

---

## 2. 测试环境

| 项目 | 配置 |
|------|------|
| 服务器 | Atlas 800T A2 |
| CPU | 4× Kunpeng 920 7285Z 3.0GHz |
| 内存 | 32× DDR4 64GB |
| 系统盘 | 2× 480GB SATA SSD |
| 数据盘 | 4× 3840GB NVMe SSD |
| 灵衢网络 | 1520 |
| 业务网卡 | SP681 (2×25GE) |
| OS | Ubuntu 22.04.5 LTS |
| AI 框架 | PyTorch 2.9.0, CANN 8.5.0 |
| 驱动 | NPU Driver 25.2.3 |
| 固件 | NPU Firmware 7.7.0.10.220 |
| Python | 3.11.13 |
| 推理模型 | Qwen3-235B-A22B, GLM-5.1, DeepSeek-V3 |

---

## 3. 测试结果

### 3.1 单元测试

测试文件位于 `tests/v1/fault_tolerance/`，覆盖三个哨兵类的所有 public 方法路径。

| 测试文件 | 测试类 | 测试用例数 | 结果 |
|----------|--------|:----------:|:----:|
| test_client_sentinel.py | TestClientSentinelInitialization | 3 | PASS |
| | TestClientSentinelPause | 3 | PASS |
| | TestClientSentinelRetry | 2 | PASS |
| | TestClientSentinelScaleDown | 4 | PASS |
| | TestClientSentinelFaultReporting | 3 | PASS |
| | TestClientSentinelShutdown | 1 | PASS |
| test_engine_core_sentinel.py | TestEngineCoreSentinelInitialization | 4 | PASS |
| | TestEngineCoreSentinelPollAndReport | 3 | PASS |
| | TestEngineCoreSentinelPause | 3 | PASS |
| | TestEngineCoreSentinelRetry | 4 | PASS |
| | TestEngineCoreSentinelScaleDown | 5 | PASS |
| | TestEngineCoreSentinelShutdown | 1 | PASS |
| test_npu_worker_sentinel.py | TestNPUWorkerSentinelInitialization | 5 | PASS |
| | TestNPUWorkerSentinelPause | 3 | PASS |
| | TestNPUWorkerSentinelRetry | 2 | PASS |
| | TestNPUWorkerSentinelScaleDown | 2 | PASS |
| | TestNPUWorkerSentinelShutdown | 1 | PASS |
| | TestGlobalPauseEvent | 2 | PASS |

| 汇总 | 测试数 | 通过 | 失败 | 通过率 |
|-----|:-----:|:----:|:----:|:-----:|
| 单元测试 | 51 | 51 | 0 | 100% |

### 3.2 端到端测试

#### 故障注入类型

| 编号 | 故障类型              |
|:----:|-----------------------|
| F1   | HBM UCE               |
| F2   | Worker 进程故障（手动 kill） |
| F3   | Engine Core 进程故障（手动 kill） |
| F4   | NPU L1 网络故障       |
| F5   | NPU device 故障       |

#### 测试场景

| 编号 | 场景                |
|:----:|---------------------|
| S1   | PD 混部主节点故障    |
| S2   | PD 混部从节点故障    |
| S3   | PD 分离 D 节点故障   |

#### 测试功能

- 故障检查
- 故障暂停
- 容错恢复
- 精度测试

#### 测试矩阵（15 用例）

| 场景 | F1 (HBM UCE) | F2 (Worker kill) | F3 (Engine kill) | F4 (L1 网络) | F5 (Device) |
|------|:------------:|:----------------:|:-----------------:|:------------:|:-----------:|
| S1 PD 混部主节点 | PASS | PASS | PASS | PASS | PASS |
| S2 PD 混部从节点 | PASS | PASS | PASS | PASS | PASS |
| S3 PD 分离 D 节点 | PASS | PASS | PASS | PASS | PASS |

#### 验证内容

| 验证项 | 结果 |
|--------|:----:|
| 故障检查 - 故障被正确检测并上报 | PASS |
| 故障暂停 - 健康 EngineCore 正确进入暂停状态 | PASS |
| retry 恢复 - 瞬时故障恢复后服务正常 | PASS |
| scale_down 恢复 - 故障 rank 移除后推理服务正常 | PASS |
| 精度测试 - 恢复后推理精度正常 | PASS |

#### 汇总

| 指标 | 结果 |
|-----|:----:|
| 端到端总数 | 15 |
| 通过 | 15 |
| 失败 | 0 |
| 通过率 | 100% |

---

## 4. 结论

全量 **66** 个测试（51 单元 + 15 端到端）全部通过，零失败。单元测试覆盖三个哨兵类的所有 public 方法；端到端测试在 PD 混部主/从节点、PD 分离 D 节点下完成 HBM UCE、Worker 进程故障、Engine 进程故障、L1 网络故障、NPU device 故障共 15 个组合的验证。容错框架功能正常。

**测试结论：全部通过。**

---

## 5. CATMonitor 整合测试（Phase A/B/C）

> **日期**: 2026-07-28
> **范围**: EEP 与 CATMonitor 底座整合（故障信息订阅机制）

### 5.1 CATMonitor 侧（Go）

| 测试包 | 测试数 | 结果 |
|--------|:----:|:----:|
| `features/faultsub`（faultsub 订阅机制） | 30+ | PASS |
| `internal/collectors/npu`（error_code 完整 hex + card_drop 指标） | 新增 3 | PASS |
| `internal/source/dcmi`（ErrorCodeList/CardDrop mock） | 含原有 | PASS |

覆盖：FaultDetector 各规则 + 变迁驱动恢复、FaultStorage tap 委托、Dispatcher webhook/poll 交付 + 去抖 + 环形缓冲 + 重试、REST API 各端点、FaultStorage 并发；DCMI 错误码列表与卡掉线识别的 mock 路径。

运行：`cd CATMonitor && go vet ./... && go test ./...`（全绿，零回归）。

### 5.2 EEP 侧（Python）

| 测试 | 结果 |
|------|:----:|
| `test_catmonitor_fault_sub.py`（38 用例） | PASS |

覆盖：NPU→DP 映射（含 A3 双卡 DIE）、故障类型/NPU ID 解析、card_drop→pause+scale_down、recovery→retry、未知 NPU 跳过、持续故障去重、**端到端 webhook 往返**（mock CATMonitor POST → 订阅器 → mock vLLM 收到 pause+scale_down 且 exclude_dp_ranks 正确）、**_wait_for_pause 真实 status 结构（空 engines 轮询、unhealthy 满足、空闲 healthy 触发一次手动 pause、非 healthy 不触发）**、**动态映射（方案 A：缩容成功后剔除故障 DIE 卡并重排 rank、失败保持映射、已剔除 DIE 事件与 recovered 跳过、连续两次缩容重排）**、**并发防重（同一 DIE 不同故障类型并发只缩容一次；不同 DIE 并行不受影响）**、**recovered 与缩容互斥（缩容进行中到达的 recovered 事件被忽略、不发 stale retry，缩容成功后 `_on_scale_down_success` 才清 `_active_faults` 并剔除 DIE）**。

运行：`cd examples/fault_tolerance_scale && python3 -m unittest test_catmonitor_fault_sub -v`。

### 5.3 整合结论

CATMonitor 新增 `features/faultsub` 故障订阅机制（HTTP Webhook + REST，零新 Go 依赖），并增强 DCMI 错误码（返回完整 hex 列表）与卡掉线显式识别；EEP 以 `catmonitor_fault_sub.py` 替代原 DCMI 轮询路径，引擎健康 ZMQ 路径保留不变。整合测试全绿，未启用 `faultsub` 时 CATMonitor 行为零回归。

---

*测试执行时间: 2026-07-28（整合测试）；2026-07-21（原容错框架测试）*
*测试执行人: OpenCode + Pytest + go test*
