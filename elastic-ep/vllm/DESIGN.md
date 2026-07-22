# vLLM Elastic EP 设计文档 (DESIGN)

> 本文档描述 vLLM Elastic EP 的架构设计、模块设计、容错工作流设计。
> 规格与需求见 [SPEC.md](SPEC.md)。

---

## 1. 架构设计

### 1.1 分层架构

```
                        外部监控器
                       (scale_down.py)
                             |
              +-----------+-----------+
              |                       |
    ZMQ SUB (故障)        HTTP POST /fault_tolerance/apply
              |                       |
              v                       v
      +-------+-------+     +--------+--------+
      |   API服务器   |     |    API服务器    |
      |   (FastAPI)  |     |    (FastAPI)    |
      +-------+-------+     +--------+--------+
              |                       |
              v                       v
      +-------+-----------------------+--------+
      |          ClientSentinel                 |
      |  （每个 vLLM 实例一个）                  |
      |  - 通过 ZMQ 接收故障报告                |
      |  - 发布引擎健康状态                     |
      |  - 分发暂停/重试/缩容指令               |
      +--+------------+------------+------------+
         |            |            |
+--------+--+  +-----+------+  +--+--------+
| EngineCore |  | EngineCore |  | EngineCore|
|  Sentinel  |  |  Sentinel  |  |  Sentinel |
| (DP rank 0)|  | (DP rank 1)|  | (DP rank 2)|
+-----+------+  +-----+------+  +-----+-----+
      |                |               |
+-----+----------------+---------------+-----+
|           EngineCore（run_busy_loop）        |
|   使用 @fault_tolerant_wrapper 包装         |
+-----+----------------+---------------+-----+
      |                |               |
+-----+------+  +-----+------+  +-----+------+
|   工作进程  |  |   工作进程  |  |   工作进程  |
|  Sentinel  |  |  Sentinel  |  |  Sentinel  |
|   (NPU)    |  |   (NPU)    |  |   (NPU)    |
+------------+  +------------+  +------------+
```

### 1.2 哨兵层级架构

容错框架采用**三级哨兵架构**：

#### 1.2.1 ClientSentinel（顶层）

- 每个 vLLM 实例一个，运行在 API 服务器进程中
- 通过 ZMQ ROUTER 接收所有 EngineCoreSentinel 的故障报告（故障报告含哨兵标识符、进程ID、DP rank id、错误类型、错误追踪堆栈等信息）
- 接收 EngineCore 启动时的哨兵注册请求（注册消息，含哨兵标识符、进程ID、DP rank id、心跳次数）
- 向外部系统发布引擎健康状态（ZMQ 发布，健康状态消息）
- 向引擎分发容错指令（暂停/重试/缩容）
- 处理外部 REST API 请求（`/fault_tolerance/apply`, `/fault_tolerance/status`）

#### 1.2.2 EngineCoreSentinel（中间层）

- 每个数据并行 Rank 一个EngineCoreSentinel，运行在 EngineCore 进程中
- 通过故障信号队列监控引擎异常
- 通过 ZMQ 将故障信息转发给 ClientSentinel
- 接收并执行 ClientSentinel 的指令（暂停/重试/缩容）
- 与 WorkerSentinels 通信，执行工作进程级操作
- 执行重试清理流程（状态重置、Gloo 通信组重建）

#### 1.2.3 WorkerSentinel（底层）

- 每个工作进程（NPU 设备）一个，运行在工作进程中
- 通过 ZMQ 接收 EngineCoreSentinel 的命令
- 在 NPU 级别执行暂停/重试/缩容操作
- 在缩容中执行专家分布重算、专家权重重载、专家路由重建、并行参数更新、CPU Gloo通信组重建、MC2 Mask参数更新、MoE配置更新等操作

### 1.3 容错工作流

```
                          故障事件
                             │
                             v
                      ┌──────┴──────┐
                      │  故障上报    │
                      │  哨兵注册    │
                      └──────┬──────┘
                             │
                             v
                      ┌──────┴──────┐
                      │  暂停操作    │
                      │  等待指令    │
                      └──────┬──────┘
                             │
                      ┌──────┴──────┐
                      │  指令决策    │
                      └──────┬──────┘
                             │
                ┌────────────┼────────────┐
                v            v            v
        ┌───────┴──┐  ┌──────┴─────────┐  ┌─┴───────────┐
        │  重试    │  │    缩容        │  │   超时退出   │
        │ (清理)   │  │ ①专家分布重算  │  │  (抛异常)    │
        │ (重置)   │  │ ②专家权重重载  │  └─────────────┘
        │ (重建)   │  │ ③专家路由重建  │
        └────┬─────┘  │ ④并行参数更新  │
             │        │ ⑤CPU Gloo通信组重建 │
             │        │ ⑥MC2 Mask参数更新   │
             │        │ ⑦MoE配置更新        │
             │        └──────────┬─────────┘
             └───────────────────┘
                             │
                             v
                      ┌──────┴──────┐
                      │  恢复服务    │
                      │  发布新状态  │
                      └─────────────┘
```

#### 带外部监控器（NPU 硬件故障）

监控器（`scale_down.py`）使用 DCMI 轮询 NPU 健康状态。当检测到卡故障时：
1. 监控器发送 暂停 指令，暂停所有健康的 DP Rank
2. 监控器发送 缩容 指令，移除故障 DP Rank
3. vLLM 在剩余健康 NPU 上重新分配专家恢复服务

#### 不带监控器（任何引擎异常）

容错框架在引擎繁忙循环中捕获异常：
1. 容错包装器捕获异常
2. 引擎通过 ZMQ 上报故障（故障报告消息）
3. ClientSentinel 获悉后健康 rank 进入暂停状态
4. 引擎暂停并等待指令（最多等待 `engine_recovery_timeout_sec`）
5. 用户通过 REST API 手动发送重试或缩容指令

### 1.4 数据流

```
NPU卡掉线/引擎崩溃
         │
         v
  ┌──────┴──────┐
  │  故障检测    │
  │ (DCMI轮询 / │
  │  异常捕获)   │
  └──────┬──────┘
         │
         v
  ┌──────┴──────┐
  │  故障上报    │
  │  ZMQ 故障   │
  │  报告消息    │
  └──────┬──────┘
         │
         ├──→ ClientSentinel ──→ 外部ZMQ发布（健康状态）
         │                              │
         v                              v
   EngineCoreSentinel              scale_down.py
         │                         （外部监控器）
         v
  ┌──────┴──────┐
  │  指令分发    │
  │  暂停/重试  │
  │  /缩容      │
  └──────┬──────┘
         │
         v
  ┌──────┴──────────────┐
  │  NPUWorkerSentinel  │
  │  暂停: 停止设备     │
  │  重试: 清理状态     │
  │    + 重建DP通信组   │
  │  缩容:              │
  │    →缩容助手        │
  └──────┬──────────────┘
         │
         v
  ┌──────┴──────────────┐
  │  缩容助手（7阶段）  │
  │  ①专家分布重算      │
  │  ②专家权重重载      │
  │  ③专家路由重建      │
  │  ④并行参数更新      │
  │  ⑤CPU Gloo通信组重建│
  │  ⑥MC2 Mask参数更新  │
  │  ⑦MoE配置更新       │
  └──────┬──────────────┘
         │
         v
  ┌──────┴──────┐
  │  恢复服务    │
  │ + 健康状态  │
  └─────────────┘
```

### 1.5 两种操作模式

```
┌─────────────────────────────────────────────────────┐
│                  操作模式                            │
├─────────────────────────────┬───────────────────────┤
│ 带外部监控器                 │ 不带监控器             │
├─────────────────────────────┼───────────────────────┤
│ DCMI 轮询 NPU 健康状态      │    捕获异常             │
│ 自动检测卡故障              │    进程暂停             │
│ 自动发送暂停/缩容指令       │ 手动通过 REST API 操作  │
│ 完全自动化                  │ 灵活性更高              │
└─────────────────────────────┴───────────────────────┘
```

### 1.6 关键设计决策

#### 基于 ZMQ 的通信

所有组件间通信使用 ZMQ 套接字，原因：
- 低延迟和高吞吐量
- 解耦的生产者-消费者模式
- 支持异步操作

#### 有状态健康跟踪

ClientSentinel 维护引擎状态字典，跟踪每个引擎的状态：
- 健康 - 引擎正常运行
- 已暂停 - 引擎暂停，等待指令
- 不健康 - 引擎遇到错误
- 已终止 - 引擎进程已退出

#### 指令工作流模型

ClientSentinel 将暂停/重试/缩容指令作为 ZMQ 消息发送给 EngineCoreSentinel。指令消息格式：
```python
@dataclass
class FaultToleranceInstruction:
    instruction: str          # "pause" | "retry" | "scale_down"
    instruction_id: str       # 全局唯一指令 ID
    params: dict              # 指令参数（timeout, exclude_engine_index 等）
```

EngineCoreSentinel 收到指令后，通过指令处理器分发到对应执行函数：
- 暂停处理器：冻结请求处理
- 重试处理器：清理工作进程状态，重建通信
- 缩容处理器：触发缩容助手 7 阶段工作流：专家分布重算 → 专家权重重载 → 专家路由重建 → 并行参数更新 → CPU Gloo 通信组重建 → MC2 算子 Mask 参数更新 → MoE 专家配置更新

#### 故障上报机制

引擎通过 ZMQ 发送故障报告消息到 ClientSentinel，格式：
```python
@dataclass
class FaultReport:
    sentinel_id: str
    pid: int
    rank: int
    err_type: str             # "engine_crash" | "worker_failure" | "npu_hang"
    err_msg: str
    traceback: str
```

ClientSentinel 记录故障后通过 ZMQ 发布广播健康状态消息给所有外部订阅者。

#### 重试清理机制

针对瞬时性错误（transient error），重试指令执行以下恢复步骤：
1. 清理状态：清除暂停事件，停止设备 + 重启设备重新初始化 NPU 设备，重置进程组重置分发通信组，清理 worker 状态（模型状态、KV 连接器输出、输入批次等）
2. 重建 Gloo 通信组：重新初始化 DP cpu_group
3. 恢复请求处理

#### 优雅降级

当 `engine_recovery_timeout_sec` 超时且未收到指令时，会重新抛出原始异常进行标准错误处理，确保系统可预测地失败，而不是无限期挂起。

---

## 2. 模块详细设计

### 2.1 目录结构

```
elastic-ep/vllm/
├── examples/
│   └── fault_tolerance_scale/
│       ├── serve_qwen.sh              # 启动带容错功能的 vLLM 服务
│       └── scale_down.py              # NPU 硬件故障监控和处理程序
├── patches/
│   ├── vllm_scale_down.patch          # vLLM v0.18.0 核心容错框架补丁
│   └── vllm_ascend_scale_down.patch   # vllm-ascend v0.18.0 昇腾特定适配补丁
├── tests/
│   └── v1/
│       └── fault_tolerance/
│           ├── test_client_sentinel.py        # ClientSentinel 单元测试
│           ├── test_engine_core_sentinel.py    # EngineCoreSentinel 单元测试
│           └── test_npu_worker_sentinel.py     # NPUWorkerSentinel 单元测试
├── README.md                          # 项目说明
├── SPEC.md                            # 技术规格说明书
├── DESIGN.md                          # 架构与系统设计
├── RELEASE_NOTES.md                   # 版本发布记录
└── TEST_REPORT.md                     # 系统测试报告
```

### 2.2 模块职责

| 模块 | 职责 |
|------|------|
| ClientSentinel | 故障接收、状态管理、指令分发 |
| EngineCoreSentinel | 引擎异常捕获、故障上报、指令执行 |
| NPUWorkerSentinel | NPU级操作、状态清理、资源重建 |
| scale_down.py | 外部硬件监控、自动故障响应 |

### 2.3 通信通道

| 通道 | 协议 | 方向 | 用途 |
|------|------|------|------|
| 引擎故障套接字 | ZMQ DEALER/ROUTER | 引擎 -> ClientSentinel | 报告引擎异常（故障报告消息） |
| 哨兵注册 | ZMQ DEALER/ROUTER | EngineCore -> ClientSentinel | 启动时注册哨兵身份（注册消息） |
| 故障状态 发布/订阅 | ZMQ 发布/订阅 | ClientSentinel -> 外部 | 广播引擎健康状态（健康状态消息） |
| 容错请求/结果 | ZMQ DEALER/PUSH | ClientSentinel -> 引擎 | 分发暂停/重试/缩容指令 |
| 工作进程命令 | ZMQ ROUTER/DEALER | EngineCore -> 工作进程 | 工作进程级控制（rank mask 等） |
| HTTP API | REST | 外部 -> API 服务器 | 外部容错控制 |


## 3. 外部监控模块设计

### 3.1 scale_down.py

| 项目 | 说明 |
|------|------|
| 路径 | `examples/fault_tolerance_scale/scale_down.py` |
| 功能 | NPU 硬件故障监控和处理（外部监控器） |
| 依赖 | DCMI (`libdcmi.so`), requests, ZMQ |
| 工作方式 | 定时轮询 DCMI 获取 NPU 健康状态，同时通过 ZMQ SUB 订阅引擎健康状态 |

**工作流程：**
1. 初始化 DCMI，获取所有 NPU 设备列表
2. 通过 ZMQ 订阅 ClientSentinel 发布的健康状态消息
3. 每 `interval_time` 秒轮询一次 NPU 健康状态
4. 检测到 NPU 故障时，通过 REST API 发送暂停指令
5. 确认引擎暂停后，发送缩容指令
6. 监控缩容完成状态
7. 通过健康状态确认恢复

### 3.2 serve_qwen.sh

| 项目 | 说明 |
|------|------|
| 路径 | `examples/fault_tolerance_scale/serve_qwen.sh` |
| 功能 | 启动带容错功能的 vLLM 服务 |
| 参数 | dp/re/host/port/fault-port/recovery-timeout/gloo-timeout-seconds |

---

## 4. CLI 与使用场景

### 4.1 启动命令

```bash
# 启动 vLLM 服务（带容错）
bash examples/fault_tolerance_scale/serve_qwen.sh \
    --dp 4 --re 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

### 4.2 启动监控（可选）

```bash
python examples/fault_tolerance_scale/scale_down.py \
    --npu-ids 0,1,2,3 --interval-time 3 \
    --external-fault-notify-port 22867 --port 8006
```

### 4.3 使用场景

#### 场景一：查询当前容错状态

```bash
curl http://localhost:8006/fault_tolerance/status
```

#### 场景二：重试（重启所有 DP rank）

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"retry","params":{"timeout":30}}'
```

#### 场景三：缩容（排除指定 DP rank）

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"scale_down","params":{"timeout":30,"exclude_dp_ranks":[2]}}'
```
