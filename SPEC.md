# CATHelper 功能规格说明书 (SPEC)

> **文档定位**：本文档是 CATHelper 项目的面向使用者的功能规格介绍，作为 [README.md](README.md) 的补充。
> 详细技术设计与架构见各子项目文档：[CATMonitor/DESIGN.md](CATMonitor/DESIGN.md)、[feature/elastic-ep/DESIGN.md](feature/elastic-ep/DESIGN.md)、[feature/straggler/straggler_combination_DESIGN.md](feature/straggler/straggler_combination_DESIGN.md)。
> 构建与操作步骤见 [User_Manual.md](User_Manual.md)。

---

## 1. 定位与整体架构

### 1.1 项目定位

CATHelper 是 CAT 技术架构的主体部分，服务于鲲鹏和昇腾服务器，提供全栈故障指标采集、分析和容错恢复能力，方便被集成，以及使能大型生产环境的高可用特性开发。

### 1.2 分层架构

CATHelper 采用"**底座 + 上层特性**"的分层架构：

```
┌─────────────────────────────────────────────────────────────┐
│                    上层特性 (feature/)                        │
│  ┌────────────────────┐ ┌─────────────────┐ ┌────────────┐  │
│  │  Elastic EP (EEP)  │ │ Straggler 慢节点 │ │ Accuracy-  │  │
│  │  推理卡级弹性容错  │ │ 检测            │ │ Monitoring │  │
│  └─────────┬──────────┘ └────────┬────────┘ └────────────┘  │
│            │ 故障订阅(Webhook)   │ KPI 文件 + 守护进程 HTTP    │
├────────────┼─────────────────────┼──────────────────────────┤
│            ▼                     ▼                          │
│   底座 — CATMonitor (CATMonitor/)                            │
│   全栈指标采集 · 健康度评估 · Prometheus 导出 · 故障订阅推送 │
│   ┌────────┬────────┬────────┬────────┬──────────┬──────────┐ │
│   │ 健康度 │Web仪表盘│ 能效监控│Prometheus│ faultsub │stragglerout│ │
│   │ 评估   │         │        │  导出   │ 故障订阅 │ KPI 输出  │ │
│   └────────┴────────┴────────┴────────┴──────────┴──────────┘ │
│   7 部件采集器 · 204 指标 · 14 来源层                         │
└─────────────────────────────────────────────────────────────┘
```

> Accuracy-Monitoring 经 vLLM `--middleware` 以进程内 ASGI 中间件运行，**不依赖底座**（无上图衔接线），见 §5。

- **底座（CATMonitor）**：成熟的全栈指标采集守护进程，提供故障信息的采集、判定与对外推送能力，供上层特性消费。
- **上层特性**：基于底座的指标/故障信息，面向特定高可用场景实现容错恢复与性能劣化检测逻辑。当前已交付 **EEP**（推理卡级弹性容错，v0.1.0）、**Straggler 慢节点检测**（v0.2.3）与 **Accuracy-Monitoring 推理精度异常检测**（v0.2.0）。

### 1.3 版本

| 项目 | 说明 |
|------|------|
| 当前版本 | v0.2.4 |
| 底座版本 | CATMonitor v0.3.3 |
| EEP 版本 | Elastic EP v0.1.0 |
| Straggler 版本 | Straggler 慢节点检测 v0.2.3 |
| Accuracy-Monitoring 版本 | anomaly_middleware v0.2.0（vLLM ASGI 中间件） |
| 平台支持 | Linux (x86_64)，NPU 容错/检测特性需华为昇腾 A3 服务器（straggler daemon 模式需 aarch64 + Ascend NPU + CANN） |
| 许可证 | Apache-2.0 |

---

## 2. 底座功能 — CATMonitor

CATMonitor 是 CATHelper 的底座，可独立运行。详细功能规格见 [CATMonitor/SPEC.md](CATMonitor/SPEC.md)。

### 2.1 指标采集

| 能力 | 说明 |
|------|------|
| 多部件采集 | CPU / 内存 / 硬盘 / GPU / NPU / 网卡 / 机箱 共 7 个部件 |
| 指标规模 | 204 个指标（High 24 / Medium 121 / Low 59），详见 [指标清单](CATMonitor/docs/CATMonitor_indi_list.md) |
| 来源层架构 | 14 个来源包抽象数据获取与解析，采集器不直接读文件/执行命令，无硬件时优雅降级 |
| 采集粒度控制 | `collection.min_priority`（low/medium/high）按优先级阈值预过滤采集，降低开销 |
| 设备并行采集 | NPU 指标按设备并行采集（单卡失败不影响其他卡） |
| 跨平台 | Linux / Windows 双平台（NPU/GPU 部分指标 Linux 专有） |

### 2.2 健康度评估

- 基于 0-100 健康分评估服务器整体健康度，自动检测 GPU/NPU 切换权重方案
- 等级：Excellent (90-100) / Good (75-89) / Warning (60-74) / Critical (0-59)
- 按 High/Medium 指标阈值扣分，多卡取最差卡

### 2.3 数据输出

| 方式 | 端口 | 说明 |
|------|------|------|
| JSONL 落盘 | — | `{data_dir}/{component}_{date}.jsonl`，按天轮转 |
| Prometheus 导出 | `:9100` | `/metrics` 端点（`catmonitor_{component}_{name}` 前缀），含 `/-/healthy`、`/-/ready` |
| Web 仪表盘 | `:9527` | 独立二进制 `catmonitor-web`，**只读消费** daemon snapshot，可视化单机健康度与各部件指标 |
| 能效监控 | `:9528` | 独立二进制 `catmonitor-dfee`，**只读消费** daemon snapshot，能效指标实时图表 SPA |

### 2.4 故障订阅推送（faultsub）— 承上启下

> faultsub 是底座与上层特性衔接的关键模块。它作为 daemon 的 Storage 插件，复用采集管道，对采集到的指标做故障判定并推送事件。

| 能力 | 说明 |
|------|------|
| 故障判定 | 对 NPU 指标判定 7 类故障：卡掉线 / 健康状态 / 错误码 / HBM UCE / DDR UCE / RoCE 链路异常 / 驱动异常 |
| 推送方式 | HTTP Webhook（`net/http`，零新依赖）主动 POST `FaultEvent` 到订阅者回调 URL；支持跨机 |
| 订阅配置 | REST API（`:9101`）注册订阅：故障类型 / 关注 NPU / 去抖窗口 / 严重级别 / 回调 URL |
| 事件语义 | 变迁驱动——仅故障出现/恢复时推送，持续故障不重复推送 |
| 拉取兜底 | REST `/faultsub/snapshot`（各 NPU 最新故障快照）、`/faultsub/events`（近期事件回补） |
| 默认关闭 | `faultsub.enabled` 默认 false，不启用时 daemon 行为零回归 |

故障类型与上层 EEP 恢复动作的对应见 [§3.3](#33-故障信息接入)。

详见 [CATMonitor/features/faultsub/faultsub_SPEC.md](CATMonitor/features/faultsub/faultsub_SPEC.md)。

---

## 3. 上层特性 — Elastic EP

EEP（Elastic EP）是 CATHelper 的首个上层特性，实现推理大 EP 卡级弹性容错。详见 [feature/elastic-ep/SPEC.md](feature/elastic-ep/SPEC.md)。

### 3.1 功能能力

| 能力 | 说明 |
|------|------|
| 故障上报 | vLLM 内的容错框架捕获故障后不立即退出，通过 ZMQ 向外报告异常详情与引擎健康状态 |
| 自动暂停 | 故障发生时自动暂停健康 DP rank，防止级联失败（健康→不健康→pause→已暂停） |
| 弹性缩容 | 故障不可恢复时移除故障 DP rank，重新分配专家（EPLB）、重载权重、重建通信组，在剩余健康 NPU 上恢复服务 |
| 重试恢复 | 瞬时性可恢复故障时，清理工作进程状态、重建 Gloo 通信组，恢复推理服务 |
| 网络闪断重推 | 支持 RoCE 链路短暂中断后请求重推恢复 |

### 3.2 适用场景与限制

| 项 | 说明 |
|----|------|
| 部署模式 | DP（数据并行）+ EP（专家并行），仅支持 TP=1，不支持 Pipeline Parallel |
| 硬件 | 当前版本仅支持华为昇腾 A3 服务器 |
| 框架 | 当前支持 vLLM，后续计划支持 SGLang |
| 量化模型 | 仅兼容 W8A8（ModelSlim 格式），W4A8/W4A16 暂不支持 |
| 冗余专家数 | 健康卡上的冗余专家总数必须大于故障卡上的逻辑专家数量 |
| FULL Graph 模式 | 暂未兼容，不支持大模型整图捕获 |

### 3.3 故障信息接入

EEP 的外部故障管理中心通过两条路径获取故障，并据此决策容错动作：

| 路径 | 来源 | 内容 | 处理 |
|------|------|------|------|
| ① 硬件故障 | **CATMonitor 订阅**（HTTP Webhook） | NPU 卡掉线 / HBM UCE / 错误码 / RoCE 链路等 `FaultEvent` | 映射 NPU→DP rank，下发 pause→scale_down（不可恢复）或 retry（恢复） |
| ② 引擎故障 | vLLM ZMQ PUB（`:22867`） | 引擎健康状态、dead 引擎 | 下发 scale_down |

故障类型与恢复动作对应：

| FaultEvent 类型 | EEP 恢复动作 |
|----------------|-------------|
| `card_drop` / `npu_health`(Critical) / `hbm_uce` / `ddr_uce` | pause → scale_down（移除故障 DP rank） |
| `roce_link_down`（recovered=true） | retry（网络闪断重推恢复） |
| `roce_link_down`（持续） | pause → 等待/人工 |
| `npu_error_code`（非卡掉线） | pause → 查状态 → scale_down 或 retry |

整合设计详见 [feature/elastic-ep/EEP_combination_DESIGN.md](feature/elastic-ep/EEP_combination_DESIGN.md)。

### 3.4 容错工作流

```
NPU 故障 → CATMonitor 采集判定 → FaultEvent(webhook) → EEP 故障管理中心
                                                          │ NPU→DP 映射
                                                          ▼
                                          pause 暂停健康 DP → scale_down 移除故障 rank
                                                          │
                                                          ▼
                                          重排专家(EPLB) → 重载权重 → 重建通信组 → 恢复推理
```

完整容错工作流图见 [feature/elastic-ep/DESIGN.md §1.3](feature/elastic-ep/DESIGN.md)。

---

## 4. 上层特性 — Straggler 慢节点检测

Straggler 是 CATHelper 的第二个上层特性，检测 AI 集群中性能劣化的 NPU 卡。详见 [feature/straggler/README.md](feature/straggler/README.md) 与 [straggler_combination_DESIGN.md](feature/straggler/straggler_combination_DESIGN.md)。

### 4.1 功能能力

两道防线检测体系，既支持一次性手动运行，也支持**常驻守护进程模式**（`--daemon`）周期自动采集+检测，结果经 HTTP 查询与控制：

| 防线 | 输入 | 方法 | 输出 |
|------|------|------|------|
| 第一道（KPI 资源检测） | NPU KPI 时序（取最后一个聚合点） | 空间 peer 对比（同节点卡互比，共享 kmeans 比例检测，无历史基线/检测窗口） | JSON + stdout 报告 |
| 第二道（Profiler 检测） | Ascend PyTorch Profiler `.db`（按需） | 均质化聚类：慢计算/慢通信/慢CPU（按物理节点 hostUid 分组）/NPU Bubble | JSON + 文本报告 |

两道结果合并为一份 `straggler_output.json`（只跑到的维度才有对应键）。守护进程模式默认端口 `:8080`，提供 `GET /status`/`/straggler/results/{latest,history,{id}}`/`/straggler/report/{latest,{id}}`/`/straggler/op_metric/{latest,{id},{id}/{file}}` 与 `POST /daemon/{start,pause,interval,trigger,stop}`。

### 4.2 检测指标与底座覆盖

第一道所需 11 项 KPI 中，10 项 CATMonitor 已采集（temp/power/aicore_freq/aicore_util/hbm_util/tx_bandwidth/rx_pfc_pkt/roce_tx_err_pkt/roce_out_of_order + cpu_avg），v0.2.0 补齐第 11 项 `roce_new_pkt_rty`（RoCE 重传）。

### 4.3 与底座的整合

- **数据接入（第一道）**：CATMonitor 经 opt-in 的 `stragglerout` 模块输出专用 KPI 时序文件 `straggler_kpi_{date}.jsonl`（按时刻×按卡聚合，保留 15 天），straggler 读该文件做空间 peer 对比。
- **守护进程模式**：straggler 以 `--daemon` 常驻运行，周期性自动完成「dyno 触发采集 → python analyse 转 .db → 解析 → KPI+Profiler 检测」，结果落盘 `daemon_results/<start>/` 并经 HTTP 查询/控制，适合运维/调度系统持续巡检。每周期 KPI 与 Profiler 同时检测（未提供 `--kpi-dir` 时只跑 Profiler）。
- **结果消费**：v0.2.3 起不再向 faultsub 回注 `straggler_detected` 事件，命中慢卡由 daemon HTTP 接口或 `straggler_output.json` 自行消费。
- **第二道独立**：Profiler `.db` 属应用级数据，CATMonitor 不采集，straggler 保留独立读取。

### 4.4 异常指标 → 建议动作映射

| straggler 命中指标/类别 | 建议动作 |
|---|---|
| `temp`（温度异常） / `power`（功耗异常） | 排查散热/风道 |
| `aicore_freq`（频率降档） | 排查驱动/固件频率策略 |
| `tx_bandwidth` / `rx_pfc_pkt` / `roce_*`（通信类异常） | 排查光模块/光纤/CRC |
| `hbm_*`（HBM 带宽/使用率异常） | 排查 HBM 负载/容量 |
| 慢计算 `cal` / 慢CPU `cpu` / Bubble `npu_bubble` | 触发 Profiler 精查 或 卡隔离 |
| 慢通信 `comm` | 排查通信拓扑/链路 |

整合设计详见 [feature/straggler/straggler_combination_DESIGN.md](feature/straggler/straggler_combination_DESIGN.md)。

---

## 5. 上层特性 — 推理精度异常检测（Accuracy-Monitoring）

Accuracy-Monitoring 是 CATHelper 的第三个上层特性：基于模型输出的 token 与 logprobs 序列，在**无侵入、零参照知识**条件下实时检测推理过程中的输出崩溃类故障。详见 [feature/accuracy-monitoring/README.md](feature/accuracy-monitoring/README.md) 与 [spec.md](feature/accuracy-monitoring/spec.md)。

### 5.1 功能能力

| 能力 | 说明 |
|------|------|
| 四类异常检测 | 生僻字（rare_character）/ 乱码（garbled）/ 重复（repetition）/ NaN Value（nan_value），检测算法随包分发（`configs/detector.yaml` 阈值） |
| 透明拦截 | vLLM `--middleware anomaly_middleware.AnomalyMiddleware` 进程内纯 ASGI 中间件：不影响响应状态、不阻塞返回、不泄漏内部参数 |
| 参数注入与恢复 | 强制注入 logprobs / top_logprobs / return_tokens_as_token_ids 供检测（top_logprobs 跨请求恒定，默认 20），响应侧恢复客户端原始参数语义 |
| 异常监控概率 | `VLLM_ANOMALY_MONITOR_RATE`（默认 1.0，范围 0-1）按概率采样；运行时可经 `GET/POST /anomaly/config` 动态查询/调整（不持久化） |
| Prometheus 指标 | 独立 `/anomaly/metrics` 端点：请求 / 检出（按 ill_type、model）/ 检测错误 / 耗时 / 最近异常 / 当前监控概率 |
| 异常落盘 | 检出异常现场保存本地 pickle（prompt / topk_logprobs / 输出 token id / 思维链文本等，`VLLM_ANOMALY_SAVE_PATH`） |
| WebUI | 多 vLLM 实例聚合可视化 + 滑动窗口阈值告警（界面 / Webhook 钉钉飞书企业微信 / 邮箱）+ 历史数据导入 |

### 5.2 适用场景与限制

| 项 | 说明 |
|----|------|
| 部署形态 | 随 vLLM 进程内运行（`--middleware`），**不依赖 CATMonitor 底座**；WebUI 可独立部署聚合多实例 |
| 硬件/框架 | 华为昇腾 A3 服务器 + vLLM-Ascend；其他 vLLM 部署形态亦可运行（检测与硬件解耦） |
| 依赖 | Python ≥3.8；`prometheus_client`/`pyyaml`/`numpy`/`httpx`/`colorlog`；tokenizer 启动期加载（env 显式指定 / argv 解析 / HF 缓存扫描） |
| 拦截范围 | 仅 `/v1/chat/completions` 与 `/v1/completions`，其余请求原样透传；空响应不检测 |
| 硬依赖 fail-fast | detector.yaml / env 校验 / tokenizer 加载失败 → 终止启动；推理期检测异常仅记录不影响服务 |

---

## 6. 路线图

| 特性 | 状态 | 说明 |
|------|------|------|
| CATMonitor 底座 | 已交付 (v0.3.3) | 全栈采集 + 健康度 + Prometheus + 故障订阅 + KPI 输出 |
| Elastic EP | 已交付 (v0.1.0) | 推理卡级弹性容错，已与 CATMonitor 整合 |
| Straggler 慢节点检测 | 已交付 (v0.2.3) | 两道防线检测，第一道接入 CATMonitor + 守护进程模式（--daemon） |
| Accuracy-Monitoring 推理精度异常检测 | 已交付 (v0.2.0) | vLLM ASGI 中间件四类输出崩溃检测 + 多实例聚合 WebUI |
| SGLang 支持 | 规划中 | EEP 后续计划支持 SGLang 框架 |
| 真机验证 | 进行中 | NPU 真实采集 / Profiler 解析 / 端到端链路在昇腾 A3 复测 |

---

## 7. 集成方式

CATHelper 设计为"方便被集成"：

- **作为整体部署**：底座 daemon + EEP 容错框架 + 外部故障管理中心 + straggler 检测器协同运行（见 [User_Manual.md](User_Manual.md)）。
- **底座独立集成**：CATMonitor 可作为独立指标采集组件被任意监控系统通过 Prometheus `/metrics` 或 JSONL 集成。
- **故障信息集成**：第三方故障管理者可按 faultsub 订阅契约（REST + Webhook）接入 CATMonitor 的故障事件流；`POST /faultsub/events` ingest 端点仍可接收外部检测器回注的命中事件（v0.2.3 起 straggler 自身不再使用该端点）。
- **KPI 数据集成**：第三方检测器可消费 `stragglerout` 输出的 KPI 时序文件，或经 straggler daemon 的 HTTP 接口（`/straggler/results/*`）查询检测结果。
- **推理服务集成**：accuracy-monitoring 经 vLLM `--middleware` 参数进程内接入任意 vLLM 服务（不依赖底座），其 WebUI 亦可独立部署聚合多实例。
- **特性定制**：上层特性可基于底座的故障订阅/KPI 输出能力开发专用检测/容错逻辑。

---

*文档版本：v2.2 · 对应 CATHelper v0.2.4*
