# CATHelper Release Notes

> 本文档按时间倒序记录 CATHelper 每次发布的版本信息。每次发布在顶部追加，不删除历史记录。

---

## v0.2.4

| 项目 | 说明 |
|------|------|
| 版本号 | v0.2.4 |
| 发布时间 | 2026-09-08 |
| 发布人 | sunnytao |
| 平台支持 | Linux (x86_64)；NPU 容错/检测特性需华为昇腾 A3 服务器（daemon 模式需 aarch64 + Ascend NPU + CANN） |
| 组成 | 底座 CATMonitor v0.3.3（**版本号不变**）+ 上层特性 Elastic EP v0.1.0（**版本号不变**）+ Straggler 慢节点检测 v0.2.3 + Accuracy-Monitoring 推理精度异常检测 v0.2.0 |
| 许可证 | Apache-2.0 |

### 版本定位

在 v0.2.3 基础上，合入 `straggler-detection` 与 `feature/accuracy-monitoring` 分支更新（约 29 个提交）：Straggler 守护进程运维能力增强（优雅关闭、采集步数参数、op_metric 归档与聚合查询）并修复慢通信检测链路多处缺陷；Accuracy-Monitoring 新增**运行时动态配置端点**、异常落盘增强（输出 token id + 思维链）与 WebUI 优化。**底座 CATMonitor 与 EEP 版本号不变**，Straggler 由 v0.2.2 升至 v0.2.3，Accuracy-Monitoring 由 v0.1.0 升至 v0.2.0。

### 主要变更

#### 上层特性 — Straggler 慢节点检测 v0.2.3

- **新增 `POST /daemon/stop`**：优雅关闭守护进程（幂等，重复调用为 no-op）；关闭时删除全部 `daemon_results/` 归档落盘结果（`dump_dir`），HTTP server 关闭、在跑周期等待结束、dynolog 子进程清理。
- **新增 `--profiler-iterations` 参数**：透传 dyno `nputrace` 采集迭代数，默认 **1**（较 v0.2.3 初版的 5 调低，缩短单周期采集耗时）。
- **op_metric 归档与聚合查询**：每周期结束时将 `op_metric/` 中间产物复制归档至 `daemon_results/<start>/`（周期结束 `--profiler-dir` 整目录删除后仍可查），并新增聚合视图接口 `GET /straggler/op_metric/latest`、`GET /straggler/op_metric/{id}`、`GET /straggler/op_metric/{id}/{file}`。
- **检测算法重构**：`clustering` score 统一为**簇均值/基线均值**（双方向各检一次取标记少的簇，min 侧阈值取倒数），KPI 空间检测与 Profiler 均质化聚类语义一致。
- **移除 faultsub 回注（代码落地）**：正式移除 `--faultsub-url` 参数及向 faultsub 回注 `straggler_detected` 事件的逻辑（v0.2.3 已文档化预告，本次代码移除）。命中慢卡由 daemon HTTP 接口或结果文件消费。
- **Bug 修复**：修复并行拓扑去重失效（tp 重复组）、`idToXp` 键空间错误致慢通信不可检测、dataparse 通信组 Duration 反向映射键空间错位、daemon `fileWriteOnce` 未按周期重置致后续周期拓扑丢失、Summary 扁平化残留旧结构体引用与降频检测断言、`build.sh` `ver_ge` 版本比较死循环（并禁用 GOTOOLCHAIN 自动下载、为 Go 下载加超时）。
- **文档同步**：`feature/straggler/README.md` 按用户执行顺序重写（安装构建前置 + 最新接口与默认值），补充 C++/Rust 依赖与 `mindstudio_monitor` 手动编译说明。

#### 上层特性 — Accuracy-Monitoring 推理精度异常检测 v0.2.0

- **新增运行时动态配置端点**：`GET/POST <VLLM_ANOMALY_CONFIG_PATH>`（默认 `/anomaly/config`，路径可配）内联响应——GET 返回当前 `{"monitor_rate": <float>}`；POST 校验 ∈ [0.0, 1.0] 后即时更新请求监控概率（下一请求生效），校验失败 400 且当前值不变；不持久化（重启回退 env 初始值），无认证（同 metrics 端点），`enabled=False` 时端点仍可达。
- **新增 Prometheus 指标**：`vllm_anomaly_monitor_rate` Gauge（当前监控概率，运行时可更新）。
- **异常落盘增强**：落盘异常记录新增 `text_tokenid`（per-choice 实际生成 token_id 序列，非流式与流式 SSE 累积均可）与 `reasoning_content`（chat 思维链文本）字段。
- **WebUI 优化**：多实例聚合页面与 store/前端增强；`webui.yaml` 默认容量调整（异常事件环形缓冲 10000→1000、告警 500→200、趋势保留 24h→30 天）。
- **测试补充**：新增异常落盘、动态配置、e2e 采样指标降级等用例；测试用例修复。

#### 底座 — CATMonitor / 上层特性 — EEP

- **无变更**：底座 CATMonitor（v0.3.3）与 EEP（v0.1.0）代码与版本号均不变。

### 测试

- **CATMonitor（Go 1.23.4）**：`go test ./...` 全部通过（collectors/source/features 约 30 个包全绿）。
- **Straggler（独立 Go module，Go 1.23.4）**：`go test ./...` 全绿（`clustering`/`resource` 包通过，其余包无测试文件）。
- **Accuracy-Monitoring（Python 3.12，pytest）**：单元 + 集成 **374 passed / 12 skipped**；e2e 42 例标记 `nightly`/`full`，需真实 vLLM 服务与模型文件（本机未装 vllm、无模型路径），未能执行——失败/报错均为 `FileNotFoundError: 'vllm'` 环境缺失所致，非代码缺陷，待带 vLLM 环境的机器完整验证。
- **构建注意**：两个 go.mod 均声明 `go 1.23.4`，需 Go ≥ 1.21 工具链（Go 1.20 报 "invalid go version" 无法解析，实测复现）；本机默认 `go` 为 1.20.10 时需改用 1.23.4。

### 已知限制

1. **daemon 模式需 aarch64 真机**（继承 v0.2.3）：依赖 Ascend NPU + CANN + `torch_npu` + `mindstudio_monitor` wheel，dyno/dynolog 须由 `build.sh` 装到系统 PATH；跨平台编译产物仅支持一次性模式。
2. **`POST /daemon/stop` 会删除全部归档结果**：优雅关闭时清空 `daemon_results/`，需保留历史请先自行备份或另行消费。
3. **monitor_rate 动态配置不持久化**：经 `/anomaly/config` 更新的监控概率重启后回退 env 初始值。
4. **e2e 测试需 vLLM 环境**：42 个 e2e 用例（`nightly`/`full` 标记）依赖真实 vLLM 服务与本地模型文件，CI/本机需预装后执行。
5. **Go 工具链要求 ≥1.21**：`go 1.23.4` 格式的 go.mod 无法被 Go 1.20 及更早版本解析。

---

## v0.2.3

| 项目 | 说明 |
|------|------|
| 版本号 | v0.2.3 |
| 发布时间 | 2026-08-26 |
| 发布人 | sunnytao |
| 平台支持 | Linux (x86_64)；NPU 容错/检测特性需华为昇腾 A3 服务器（daemon 模式需 aarch64 + Ascend NPU + CANN） |
| 组成 | 底座 CATMonitor v0.3.3（**版本号不变**）+ 上层特性 Elastic EP v0.1.0（**版本号不变**）+ Straggler 慢节点检测 v0.2.2 |
| 许可证 | Apache-2.0 |

### 版本定位

在 v0.2.2 基础上，合入 `straggler-detection` 分支（30+ 提交）对 Straggler 特性做重大升级：新增**常驻守护进程模式（daemon）**、KPI 检测算法重构为纯空间维度、新增 `build.sh` 一键构建、引入 msmonitor 子模块。**底座 CATMonitor 与 EEP 版本号不变**，仅 Straggler 由 v0.2.1 升至 v0.2.2。

### 主要变更

#### 上层特性 — Straggler 慢节点检测 v0.2.2

- **常驻守护进程模式（`--daemon`）**：新增 `daemon/` 包（`daemon.go`/`dyno.go`/`server.go`/`store.go`/`types.go`），周期性自动完成「触发采集（dynolog/dyno）→ 转 .db（python analyse）→ 解析 → KPI+Profiler 检测」全链路，结果通过 HTTP 查询与运维控制，适合接入手管/调度系统持续巡检。HTTP 接口：`GET /healthz`/`/status`/`/straggler/results/{latest,history,{id}}`/`/straggler/report/{latest,{id}}`、`POST /daemon/{start,pause,interval,trigger}`（默认端口 `:8080`）。每周期结果落盘 `daemon_results/<start>/`，周期结束删除整个 `--profiler-dir` 防堆积；查询只看本次会话内存 store（重启归零）。
- **KPI 检测算法重构（resource 模块）**：移除时间维度、历史基线、检测窗口、根因定界（删除 `emit.go`/`fusion.go`/`rootcause.go`/`time_detector.go`/`baseline.go`），异常**完全由空间维度 peer 对比**（取最后一个聚合点，同节点卡互比）判定；空间检测统一走共享 `clustering` 包的 kmeans 比例检测（双方向各检一次，标记数少的为异常簇）；新增 `utils/node_result.go` 节点级结果聚合。
- **共享聚类包 `clustering/kmeans.go`**：KPI 空间检测与 Profiler 均质化聚类共用同一 kmeans 比例算法（z-score 标准化 + 肘部法选 k + kmeans++ Lloyd 迭代，固定种子结果确定）。
- **移除 faultsub 回注**：移除 `--faultsub-url` 参数及向 faultsub 回注 `straggler_detected` 事件的逻辑（与守护进程结果上报重复）。命中慢卡不再回注 faultsub，改由 daemon 的 HTTP 接口或结果文件消费。
- **CLI 参数变更（破坏性）**：移除 `--faultsub-url`/`--baseline-hours`/`--detection-hours`/`--space-method`/`--space-z-threshold`/`--time-z-threshold`/`--time-weight`/`--no-trend`/`--no-fallback`/`--always-profiling` 等旧 flag；新增 `--space-ratio-threshold`（空间簇比例阈值，默认 2.0，独立旋钮）、`--debug-output`（全量数据诊断）、守护进程参数 `--profiler-dir`/`--kpi-dir`/`--daemon-port`/`--interval`/`--collect-wait`。
- **一键构建 `build.sh`**（aarch64 必需）：架构检查 → 安装 dyno/dynolog（`.deb`，系统包管理器）→ Python 版本检查（3.9–3.12）→ 安装 `mindstudio_monitor` wheel → Go 工具链（缺失/过旧从阿里云镜像安装）→ `CGO_ENABLED=0 go build`。Go 编译不再依赖 dyno/dynolog 二进制（已无 embed），任何平台可出包；daemon 模式运行时才要求 aarch64 主机装好 dyno/dynolog。
- **msmonitor 子模块**：新增 `feature/straggler/3rdparty/msmonitor`（Ascend/msmonitor，`build.sh` 安装流程引用）。
- **Bug 修复与测试修正**：修复并行拓扑去重失效（tp 重复组）、`idToXp` 键空间错误导致慢通信不可检测、dataparse 通信组 Duration 反向映射键空间错位、node_result 为空与报告排序重复、dyno 采集判定与 dump 目录定位、守护进程暂停期间定时器继续走导致恢复后提前触发（`f0d456e`）；修正 `TestSpaceFreqSingleDownclock` 期望为原始比 0.444（`0169472`）。
- **文档同步**：`feature/straggler/` 下 README/DESIGN/DESIGN_NPU_RESOURCE/SPEC/straggler_combination_DESIGN 全面重写；根目录 `README.md`/`SPEC.md`/`User_Manual.md` 同步新算法、daemon 模式、新 CLI 与构建流程。

#### 底座 — CATMonitor / 上层特性 — EEP

- **无变更**：底座 CATMonitor（v0.3.3）与 EEP（v0.1.0）代码与版本号均不变，仅根目录文档同步 Straggler 新特性描述。

### 测试

- **Straggler（独立 Go module，Go 1.23.4）**：`go build`（CGO_ENABLED=0，产物 14.9MB）/`go vet ./...`/`go test ./...` 全绿，clustering + resource 包测试通过（含此前失败的 `TestSpaceFreqSingleDownclock`，已随 `0169472` 修正）。
- **合并验证**：本地 `main` ← `origin/straggler-detection` 试合并，16 处冲突全部位于 `feature/straggler/`（10 内容冲突 + 6 modify/delete），按"以 straggler-detection 为准"对齐解决；elastic-ep 与 accuracy-monitoring 零冲突。验证未推送至远端前已完成。

### 已知限制

1. **daemon 模式需 aarch64 真机**：依赖 Ascend NPU + CANN + `torch_npu` + `mindstudio_monitor` wheel，dyno/dynolog 须由 `build.sh` 装到系统 PATH；跨平台（amd64/windows）编译产物仅支持一次性模式。
2. **KPI 时间维度已移除**：仅靠空间 peer 对比判定异常，无历史基线兜底；单节点在场卡 < 2 时该节点 score=0；`aicore_freq` 轻度降频（< 空间簇比例阈值 2.0×）不被标记。
3. **回注 faultsub 已移除**：命中慢卡不再经 faultsub 闭环，需通过 daemon HTTP 接口或 `straggler_output.json` 自行消费。
4. **msmonitor 子模块需初始化**：克隆仓库后需 `git submodule update --init feature/straggler/3rdparty/msmonitor`（`build.sh` 引用），Go 编译本身不依赖。
5. **未推送到远端**：本次发布暂在本地完成。

---

## v0.2.2

| 项目 | 说明 |
|------|------|
| 版本号 | v0.2.2 |
| 发布时间 | 2026-07-31 |
| 发布人 | sunnytao |
| 平台支持 | Linux (x86_64)；NPU 容错/检测特性需华为昇腾 A3 服务器 |
| 组成 | 底座 CATMonitor v0.3.3（**版本号不变**）+ 上层特性 Elastic EP v0.1.0 + Straggler 慢节点检测 v0.2.1 |
| 许可证 | Apache-2.0 |

### 版本定位

在 v0.2.1 基础上，合入 `straggler-detection` 分支，修复 Straggler 第二道（Profiler）慢 CPU 检测的节点分组逻辑：由硬编码"每 4 张连续 rank 卡为一台机器"改为从 Profiler `.db` 的 `HOST_INFO` 表读取 `hostUid` 进行动态分组。**底座 CATMonitor 与 EEP 版本号不变**，仅 Straggler 由 v0.2.0 升至 v0.2.1。

### 主要变更

- **慢 CPU 检测动态分组**：`profiling/dataparse` 新增 `queryHostUid`（`SELECT hostUid FROM HOST_INFO LIMIT 1`）识别每张卡所属物理节点，新增 `writeHostInfo` 写入 `op_metric/host_info_{N}.json`（rank→hostUid 映射），`PerformanceMetrics` 增加 `HostUid` 字段；`profiling/detector` 新增 `GetHostUidMapping` 读取映射，`getSlowHostRanksByHomogenize` 用 `smoothByHostUid` 替换 `processCPUData`——相同 hostUid 的卡归为同节点，节点内去 min/max 截尾均值预处理后均质化聚类（方向 max），无 hostUid 的卡保持原值不变。
- **消除硬编码假设**：旧版假定每 4 张连续 rank 卡属同一物理机，在非 4 卡/节点拓扑下误分组；新版按真实 hostUid 分组，适配任意节点卡数。
- **文档同步**：`feature/straggler/DESIGN.md`、`SPEC.md` 更新数据解析流程、慢 CPU 算法说明与关键 SQL；根目录 `README.md`/`SPEC.md`/`User_Manual.md` 同步慢 CPU 分组描述。

### 测试

- `go build`/`go vet`/`go test` 全绿；二进制构建通过；gofmt 无问题（合并提交已验证）。

### 已知限制

1. `HOST_INFO` 表或 `hostUid` 列缺失的 `.db`，对应卡跳过节点预处理，保持原始 ZP_Host 值参与聚类（降级兼容）。
2. 未推送到远端：本次发布暂在本地完成。

---

## v0.2.1

| 项目 | 说明 |
|------|------|
| 版本号 | v0.2.1 |
| 发布时间 | 2026-07-31 |
| 发布人 | sunnytao |
| 平台支持 | Linux (x86_64)；NPU 容错/检测特性需华为昇腾 A3 服务器 |
| 组成 | 底座 CATMonitor v0.3.3（**版本号不变**，合入 snapshot 统一生产重构）+ 上层特性 Elastic EP v0.1.0 + Straggler 慢节点检测 v0.2.0 |
| 许可证 | Apache-2.0 |

### 版本定位

在 v0.2.0 基础上，合入 `feature/catmonitor` 分支，对底座 CATMonitor 进行 snapshot 统一生产重构 + feature-scoped 采集增强。**底座 CATMonitor 版本号维持 v0.3.3 不变**，仅刷新底座文档内容以反映重构。

### 主要变更

- **Snapshot 统一生产**：新增 `features/snapshot` 包，daemon 作为唯一 snapshot 生产者，产出 per-component `snapshot_<comp>.json` + 全局 `snapshot.json`（health/collectors/intervals/system_specs）；web/dfee 转为**只读消费者**，不再各自采集，避免重复跑硬件。
- **Feature-scoped 采集**：`features` 配置 + `internal/metrics` 的 `SetFeatureScope` 白名单机制；非空时只采各 feature `metrics.yaml` 并集内且 `priority ≥ min_priority` 的指标，`AnyWanted` 跳过全 out-of-scope 子方法；并派生 per-component cadence `C_comp = min(feature interval)`、`C_global = min(C_comp)`。
- **dfee 独立二进制化**：`features/dfee` 转为 `package main`（`catmonitor-dfee`，:9528），补全 69 项 `metrics.yaml`，只读消费 snapshot。
- **web 瘦身**：删除 `DataCollector`/`config.go`/`config.yaml`，改 `-addr`/`-snapshot-dir` flag，REST API 只读化（删 `/api/refresh`、`POST /api/config`），`/dfee/` 路由转独立二进制。
- **Makefile**：`make all/web/dfee` + CANN DCMI 头自动探测（`-tags dcmi`）。

### 测试

- `go vet`/`go build`/`go test` 全绿；三二进制（daemon/web/dfee）独立构建通过。
- 系统测试（无 NPU/GPU 环境）：version/list/collect/health + daemon:9100/metrics（Prometheus 格式）+ web:9527（/api/snapshot 等）+ dfee:9528（/dfee/ + /api/dfee）+ 无硬件采集器优雅降级，全部通过。详见 `CATMonitor/docs/test_report.md`。
- 已知限制：5 个新增文件存在 gofmt 格式瑕疵（struct 字段对齐，非阻塞）；`-race` 需 cgo 未覆盖。

### 文档

刷新底座 5 个文档反映重构：`CATMonitor/README.md`、`SPEC.md`、`DESIGN.md`、`docs/User_Manual.md`、`docs/CATMonitor_indi_list.md`（底座版本号维持 v0.3.3，indi_list 更新日期 2026-07-31）。

---

## v0.2.0

| 项目 | 说明 |
|------|------|
| 版本号 | v0.2.0 |
| 发布时间 | 2026-07-28 |
| 发布人 | sunnytao |
| 平台支持 | Linux (x86_64)；NPU 容错/检测特性需华为昇腾 A3 服务器 |
| 组成 | 底座 CATMonitor v0.3.3 + 上层特性 Elastic EP v0.1.0 + Straggler 慢节点检测 v0.2.0 |
| 许可证 | Apache-2.0 |

### 版本定位

在 v0.1.0（底座 + EEP 弹性容错）基础上，新增上层特性 **Straggler 慢节点（慢卡）检测**，并与 CATMonitor 底座有机整合。至此 CATHelper 形成"采集 → 故障容错（EEP） + 性能劣化检测（straggler）"的双特性上层，二者共享底座的指标采集与故障订阅能力。

### 变更摘要

#### 上层特性 — Straggler 慢节点检测 v0.2.0

- **两道防线检测体系**：第一道（KPI 资源指标检测）基于 15 天历史基线 + 1h 检测窗，时间×空间双维 Z-score + 二维交叉验证 + 根因定界；第二道（Profiler 检测）读 Ascend PyTorch Profiler `.db`，均质化聚类检测慢计算/慢通信/慢CPU/NPU Bubble
- **独立 Go module**：`feature/straggler/`（自带 `go.mod`，依赖纯 Go `modernc.org/sqlite`，无 CGo），import 路径重构为 `.../CATHelper/feature/straggler/*`
- **与 CATMonitor 底座整合**：第一道接入 CATMonitor——新增 opt-in 的 `stragglerout` KPI 文件输出（替代自带 `kpi_collect.sh`）；第二道（Profiler `.db`）保留独立
- **检测命中回注 faultsub**：straggler 把慢卡作为 `straggler_detected` 事件 POST 给 CATMonitor faultsub（经 `POST /faultsub/events` ingest 端点），由 faultsub 推送给订阅者（EEP/运维）触发卡隔离/排查，闭环"采集→检测→响应"
- 详见 [feature/straggler/straggler_combination_DESIGN.md](feature/straggler/straggler_combination_DESIGN.md)

#### 底座 — CATMonitor 增强

- **新增 `features/stragglerout` 模块**（opt-in `collector.Storage` 插件）：抽 11 项 NPU KPI（temp/power/aicore_freq/aicore_util/hbm_util/tx_bandwidth/rx_pfc_pkt/roce_tx_err_pkt/roce_out_of_order/roce_new_pkt_rty + cpu_avg），按时刻×按卡聚合追加写日级 `straggler_kpi_{date}.jsonl`（保留 15 天，60s flush），默认关闭、零回归
- **faultsub 新增事件 ingest**：`POST /faultsub/events` 端点接收外部检测器（straggler）回注的 FaultEvent，经同一 Dispatch 管道推送给订阅者；新增 `straggler_detected` 故障类型
- **指标补充**：`metrics.yaml` 登记 `roce_new_pkt_rty`（RoCE 重传报文数，Medium），补齐 straggler 第 11 项 KPI；hccn_tool 统计解析器本为通用 key:value，无需改代码

#### 根目录文档

- README/SPEC/User_Manual 同步新增 straggler 特性说明、用法与配置；路线图更新（straggler 由"规划中"转为"已交付"）

### 测试

- **CATMonitor（Go）**：`go vet`/`go build`/`go test` 全绿，27 包（+1 stragglerout），含 stragglerout 6 子测试、faultsub ingest 3 子测试；`straggler_output`/`faultsub` 未启用时行为零回归
- **straggler（独立 Go module）**：`go vet`/`go build`/`go test` 全绿，resource 包 9 子测试（json_reader 6 + emit 3）；修复既存编译错误（report.go 字段/位置混用）

### 已知限制

1. **DCMI/真机未验证**：NPU KPI 真实采集、Profiler `.db` 解析、faultsub webhook 端到端推送均需在昇腾 A3 真机复测（单测由 mock 驱动）
2. **roce_new_pkt_rty 字段名待真机确认**：mapper 用别名表（roce_new_pkt_rty / roce_retrans_pkt_num / roce_rx_retrans_pkt_num）兼容；若 hccn_tool 无该字段将缺省为 0
3. **KPI 文件量**：3s 采样 × 8 卡 ≈ 5.7MB/天、15 天≈86MB，定时检测读取可接受
4. **EEP 既有已知问题**：缩容后再次缩容存在偶现问题（详见 [feature/elastic-ep/Release_Notes.md](feature/elastic-ep/Release_Notes.md)）
5. **未推送到远端**：本次发布暂在本地完成
6. **后续**：SGLang 框架支持待后续版本交付

---

## v0.1.0

| 项目 | 说明 |
|------|------|
| 版本号 | v0.1.0 |
| 发布时间 | 2026-07-28 |
| 发布人 | sunnytao |
| 平台支持 | Linux (x86_64)；NPU 容错特性需华为昇腾 A3 服务器 |
| 组成 | 底座 CATMonitor v0.3.3 + 上层特性 Elastic EP v0.1.0 |
| 许可证 | Apache-2.0 |

### 版本定位

CATHelper 的初始版本（v0.1.0）。确立"**底座 + 上层特性**"的分层架构：以 CATMonitor 作为全栈指标采集与健康度评估底座，向上层推理高可用特性提供故障信息采集、判定与推送能力；首个上层特性 Elastic EP（推理卡级弹性容错）完成开发并与底座有机整合。CATHelper 是 CAT 技术架构的主体部分，服务于鲲鹏和昇腾服务器，提供全栈故障指标采集、分析和容错恢复能力，方便被集成，以及使能大型生产环境的高可用特性开发。

### 变更摘要

#### 底座 — CATMonitor v0.3.3

- **全栈指标采集**：7 个部件（CPU / 内存 / 硬盘 / GPU / NPU / 网卡 / 机箱）、204 个指标；14 个来源包抽象数据获取与解析，无硬件时优雅降级；NPU 指标按设备并行采集
- **采集粒度控制**：`collection.min_priority`（low/medium/high）按优先级阈值预过滤采集，采集器经 `AnyWanted` DI 在执行前跳过无需采集的指标组
- **健康度评估**：0-100 健康分，自动检测 GPU/NPU 切换权重方案（Excellent/Good/Warning/Critical）
- **Prometheus 导出**：daemon 内置 `/metrics` 端点（`:9100`），`CachingStorage` 复用采集管道，一次采集同时落盘 JSONL + 缓存导出，零额外进程；含 `/-/healthy`、`/-/ready`
- **Web 仪表盘与能效监控**：独立二进制 `catmonitor-web`（`:9527`），可视化单机健康度与各部件指标；`/dfee/` 能效指标实时图表 SPA
- **数据输出**：JSONL 落盘（按天轮转）+ Prometheus + Web；跨平台（Linux/Windows，NPU/GPU 部分指标 Linux 专有）
- **外部依赖**：仅 `gopkg.in/yaml.v3`，默认构建无 CGo；NPU DCMI 采集在 `-tags dcmi` 后

#### 底座 — 故障订阅推送机制（faultsub，承上启下的新特性）

- **新增 `features/faultsub` 模块**：作为 daemon 的 `collector.Storage` 插件（与 exporter 的 `CachingStorage` 同模式），零侵入 tap 进采集管道，对采集到的 NPU 指标做故障判定并向订阅者推送事件
- **故障判定规则**：7 类——卡掉线（`card_drop`）/ 健康状态（`npu_health`）/ 错误码（`npu_error_code`）/ HBM UCE（`hbm_uce`）/ DDR UCE（`ddr_uce`）/ RoCE 链路异常（`roce_link_down`）/ 驱动异常（`driver_unhealthy`）；规则可配置开关，未配置默认启用（fail-open）
- **变迁驱动事件语义**：仅故障出现/恢复时推送，持续故障不重复推送，事件流安静
- **HTTP Webhook 推送**：经 `net/http` 主动 POST `FaultEvent`（JSON）到订阅者回调 URL，异步不阻塞采集管道，失败重试；**零新依赖**，CATMonitor 保持"仅 yaml.v3"
- **订阅 REST API**（`:9101`）：注册/查询/注销订阅（声明回调 URL / 故障类型 / 关注 NPU / 去抖窗口 / 严重级别）+ `/faultsub/snapshot`（最新故障快照）+ `/faultsub/events`（事件回补）+ `/faultsub/types`（能力发现）
- **订阅级去抖**：`SubscriptionManager` 持有每订阅去抖状态，`Subscription` 保持值类型便于序列化
- **默认关闭**：`faultsub.enabled` 默认 false，不启用时 daemon 行为与底座原版完全一致（零回归）
- **DCMI 故障信息增强**：`ErrorCodeList` 返回完整 hex 错误码列表（原仅返回计数，EEP 靠 `0x40f84e00` 判卡掉线）；`CardDrop` 显式识别 `DeviceNotReady(-8012)`；NPU 采集器新增 `npu/card_drop` 指标，`error_code` 指标升为 High 并输出完整错误码 labels

#### 上层特性 — Elastic EP v0.1.0

- **推理卡级弹性容错**：DP+EP 部署模式下卡故障后推理实例不退出，隔离故障卡所在 DP 域、重排专家（EPLB）后剩余 DP 继续提供推理服务；支持网络闪断故障后请求重推恢复
- **三级哨兵容错框架**：ClientSentinel / EngineCoreSentinel / NPUWorkerSentinel，基于 ZMQ 通信，故障上报 + 自动暂停 + 重试/缩容；vLLM/vLLM-Ascend v0.18.0 补丁形态
- **对外 REST API**：`/fault_tolerance/apply`（pause/retry/scale_down）、`/fault_tolerance/status`
- **外部故障管理中心**：`scale_down_demo.py` + `catmonitor_fault_sub.py`，双路径故障检测（**CATMonitor webhook 订阅 NPU 故障** + ZMQ 引擎健康订阅），映射 NPU→DP rank 后下发容错指令
- **模型支持**：已在 DeepSeek-V3、Qwen3-235B-A22B、GLM5.1（W8A8）完成验证

#### 底座与特性整合

- **EEP 故障信息输入与 CATMonitor 真实衔接**：原 EEP 自带的 DCMI 轮询 Demo 替换为订阅 CATMonitor 的 faultsub 机制；CATMonitor 采集并判定 NPU 故障后经 HTTP Webhook 推送 `FaultEvent` 给 EEP 故障管理中心，由其映射 NPU→DP rank 后下发 pause/scale_down/retry；引擎健康 ZMQ 路径（EEP 内部边界）保留不变
- **整合设计文档**：[feature/elastic-ep/EEP_combination_DESIGN.md](feature/elastic-ep/EEP_combination_DESIGN.md)
- **跨机支持**：EEP 注册时声明可达回调 URL，CATMonitor 反向 POST 推送，支持分机部署

#### 根目录文档体系

- 新增根目录 [README.md](README.md)（项目简介）、[SPEC.md](SPEC.md)（功能规格）、[User_Manual.md](User_Manual.md)（使用手册）、[Release_Notes.md](Release_Notes.md)（本文档），作为 CATHelper 整体入口

### 测试

- **CATMonitor（Go）**：`go vet ./...` 零警告，全量测试通过（含新增 `features/faultsub` 46 个子测试、NPU 采集器 error_code/card_drop 用例、DCMI ErrorCodeList/CardDrop mock 路径）；未启用 faultsub 时行为零回归
- **EEP（Python）**：`test_catmonitor_fault_sub.py` 10 用例全过（含端到端 webhook 往返：mock CATMonitor POST → 订阅器 → mock vLLM 收到 pause+scale_down 且 `exclude_dp_ranks` 正确）
- **既有容错框架**：66 用例（51 单元 + 15 端到端）保持通过

### 已知限制

1. **DCMI CGo 未真机验证**：`dcmi_cgo.go`（含新增 `ErrorCodeList`/`CardDrop` wrapper）在 `dcmi` 构建标签后，本机无 CANN SDK 无法编译，需在真 NPU 服务器 `go build -tags dcmi` 验证
2. **NPU/GPU/Chassis 无真机**：系统测试仅验证优雅降级路径与 mock 驱动路径，真实故障指标采集与端到端容错需在配备昇腾 A3 硬件的机器复测
3. **EEP 容错已知问题**：缩容后再次缩容存在偶现问题会导致缩容不成功（详见 [feature/elastic-ep/Release_Notes.md](feature/elastic-ep/Release_Notes.md)）
4. **FULL Graph 模式未兼容**：EEP 暂不支持大模型整图捕获
5. **未推送到远端**：本次发布暂在本地完成
6. **后续特性待开发**：推理慢节点满卡检测特性、SGLang 支持待后续版本交付

---

*本文档仅追加新版本记录，不删除历史。*
