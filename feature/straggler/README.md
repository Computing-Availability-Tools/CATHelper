# CATHelper — 慢节点（Straggler）检测

AI 智算集群中识别性能劣化 NPU 卡的两道防线检测体系。第一道 **KPI 资源检测**（轻量、常态化）基于 NPU 资源指标做空间 peer 对比（最后一个聚合点）；第二道 **Profiling 深查**（按需触发）基于 Ascend PyTorch Profiler 数据从计算/通信/CPU/Bubble 四个维度精查。两道结果合并输出为**一个 JSON 文件**。既支持一次性手动运行，也支持**守护进程模式**（`--daemon`）常驻运行：周期性自动完成采集→检测，结果通过 HTTP 查询与运维控制（见[五、守护进程模式](#五守护进程模式常驻检测)）。

---

## 目录

- [一、快速开始](#一快速开始)
- [二、目录结构](#二目录结构)
- [三、输入数据](#三输入数据)
- [四、CLI 参数详解](#四cli-参数详解)
- [五、守护进程模式（常驻检测）](#五守护进程模式常驻检测)
- [六、检测原理](#六检测原理)
- [七、输出与解读](#七输出与解读)
- [八、边界情况](#八边界情况)
- [九、构建与部署](#九构建与部署)
- [十、设计文档](#十设计文档)

---

## 一、快速开始

### 运行前提

- Go ≥ 1.23（构建时需能访问模块代理拉取 `modernc.org/sqlite`）
- Profiler 模式需要目标目录含 `ascend_pytorch_profiler_{N}.db` 文件

### 模式 1：仅 KPI 检测

```bash
cd feature/straggler
# 遗留 CSV 目录模式（每节点 CSV + node_config.json）
go run . --kpi-path=/data/kpi_csv_dir

# 整合模式（CATMonitor straggler_output JSONL，优先）
go run . --kpi-jsonl-dir=/var/lib/catmonitor/straggler
```

### 模式 2：仅 Profiler 检测

```bash
go run . path=/data/profiler_output degradation=0.3
```

### 模式 3：KPI + Profiler 联合检测

```bash
go run . path=/data/profiler_output --kpi-path=/data/kpi_csv_dir degradation=0.3
```

**检测顺序**：先跑 KPI（轻量、无侵入）→ KPI 发现异常 → 有 `path` 时继续跑 Profiler 做交叉验证；KPI 无异常 → 自动 fallback 到 Profiler 精查；仅 KPI 无 `path` → KPI 结果即为最终输出。两道结果都会合并进 `straggler_output.json`（只跑哪个维度就只有哪个键）。

---

## 二、目录结构

```
straggler/
├── main.go                 # 统一入口：CLI 解析、双模式编排、合并 JSON 输出、--daemon 入口（PATH 解析 dyno/dynolog）
├── daemon/                 # 守护进程：dynolog/dyno 采集 + 周期检测 + HTTP 查询/控制
│   ├── daemon.go           #   运行循环（周期调度、生命周期、优雅退出）
│   ├── dyno.go             #   dynolog 拉起 + dyno 触发校验 + python analyse 转 .db
│   ├── store.go            #   会话历史 + 周期计数
│   ├── server.go           #   HTTP 路由（/status /straggler/* /daemon/*）
│   └── types.go            #   Config / CycleResult / HTTP 响应类型
├── README.md               # 本文件
├── go.mod / go.sum         # 独立 Go module（依赖 modernc.org/sqlite）
├── build.sh                # 构建：架构检查 + 安装 dyno/dynolog（.deb）+ Python 版本检查 + wheel 安装 + go build
├── clustering/             # 共享 kmeans 比例检测算法（KPI 空间检测与 Profiler 均质化聚类共用）
│   └── kmeans.go
├── resource/               # 第一道防线：资源指标检测（KPI）
│   ├── types.go            #   数据结构 & 指标注册表 & 配置
│   ├── parser.go           #   CSV / KPI 目录解析（node 感知全局卡号）
│   ├── json_reader.go      #   CATMonitor straggler_kpi JSONL 读取（含多节点子目录布局）
│   ├── aggregator.go       #   10 秒聚合（裁剪均值 / 计数器增量）
│   ├── space_detector.go   #   空间维度检测（peer 对比，最后一点）
│   ├── report.go           #   管线编排 + 文本报告（stdout）
├── profiling/              # 第二道防线：Profiling 检测
│   ├── dataparse/          #   数据清洗（SQLite → CSV/JSON 中间件）
│   │   ├── data_process.go
│   │   ├── scenario_segregate.go
│   │   └── utils.go
│   └── detector/           #   检测算法
│       ├── constants.go    #   并行域 / 列名常量
│       ├── data_parser.go  #   并行域拓扑 + 单步快照
│       ├── detection.go    #   主流水线（4 类检测编排）
│       ├── data_handler.go #   慢计算/慢通信/慢CPU/Bubble 实现
│       ├── clustering.go   #   HomogenizationComparisonFunc 包装（kmeans）
│       └── debug.go        #   --debug-output 诊断分数
├── config/                 # Profiler 共享配置
│   └── config.go
├── utils/                  # 结果聚合（节点级）+ 工具
│   ├── node_result.go
│   └── tools.go
├── report/                 # Profiler 文本报告生成
│   └── report.go
├── DESIGN.md               # Profiling 检测设计
├── DESIGN_NPU_RESOURCE.md  # KPI 资源检测设计
├── SPEC.md                 # 检测技术规范
└── straggler_combination_DESIGN.md  # 与 CATMonitor 底座的整合设计
```
> 各包的测试文件（`*_test.go`）未在树中列出。

---

## 三、输入数据

### 3.1 KPI 输入（二选一）

#### `--kpi-path`：每节点 CSV 目录（遗留模式）

传一个**目录**，内含多个每节点 CSV 文件 + 一个固定的 `node_config.json`：

```
/data/kpi_csv_dir/
├── node1.csv              # 节点 node-1 的卡数据
├── node2.csv              # 节点 node-2 的卡数据
└── node_config.json
```

**CSV 格式**：每行一个时间戳，指标列值为 JSON dict（`{cardID: value}`）：

```csv
timestamp,NPU_CARD_TEMP,NPU_CARD_POWER,NPU_CARD_AICORE_FREQ,NPU_CARD_AICORE_UTIL,NPU_CARD_HBM_BANDWIDTH_UTIL,NPU_CARD_HBM_UTIL,NPU_TX_BANDWIDTH,NPU_RX_PFC_PKT,NPU_ROCE_TX_ERR_PKT,NPU_ROCE_OUT_OF_ORDER,NPU_ROCE_NEW_PKT_RTY,NPU_NIC_RX_ALL_PKG,CPU_average
1784547926,"{""0"":55,""1"":56}","{""0"":1628}","{""0"":1800}","{""0"":90}","{""0"":80}","{""0"":50}","{""0"":102400}","{""0"":0}","{""0"":0}","{""0"":0}","{""0"":0}","{""0"":100}","{""cpu1"":""4.26""}"
```

> `timestamp` 列必填；其余指标列缺失只告警不阻断（对应 metric dict 为空）。列名不区分顺序。

**`node_config.json` 格式**：把每个 CSV 文件映射到物理节点及其卡号（0 起始）：
```json
{
  "node1.csv": { "node": "node-1", "cards": [0, 1] },
  "node2.csv": { "node": "node-2", "cards": [0, 1] }
}
```
- 校验：每个 CSV 必须在 config 里有条目；config 引用的 CSV 必须存在。
- 配置的卡在 CSV 中无数据 → 告警。

#### `--kpi-jsonl-dir`：CATMonitor straggler_output JSONL（整合模式）

传一个**目录**，读取目录内全部 `straggler_kpi_{date}.jsonl` 文件（空间检测只取最后一个聚合点，历史数据用于 10 秒聚合）。**优先于 `--kpi-path`**。

**单节点布局（向后兼容）**——文件直接放目录下：
```
{dir}/
├── straggler_kpi_2026-08-13.jsonl
└── straggler_kpi_2026-08-12.jsonl
```

**多节点布局（推荐）**——每节点一个子目录 + `node_config.json` 声明每个子目录对应的节点和实际使用的卡号：
```
{dir}/
├── node-a/
│   └── straggler_kpi_2026-08-13.jsonl
├── node-b/
│   └── straggler_kpi_2026-08-13.jsonl
└── node_config.json
```

`node_config.json` 格式（key = **子目录名**；`node` = 节点名；`cards` = 该节点**实际使用的卡号**，节点内 0 起始）：
```json
{
  "node-a": { "node": "node-1", "cards": [0, 1] },
  "node-b": { "node": "node-2", "cards": [0, 1] }
}
```
- 有 `node_config.json` → 按多节点子目录读取；无 → 按单目录读取（兼容旧布局：样本 `vals` 平铺 → 单节点 `"none"`；`vals` 外层为节点名的嵌套形态 → 按节点解析）。
- 子目录名只是文件系统 key，`node` 才是参与 peer 对比的节点名（可与子目录名不同）。
- `cards` 之外的数据会被过滤；配置引用的子目录缺失 → 报错。
- 每个节点的样本为平铺形态 `{card: {field: value}}`，卡号节点内 0 起始（与 `--kpi-path` 的每节点 CSV 一致）。

**JSONL 单行记录格式**（每行一个 JSON）：
```json
{ "ts": 1784547926, "vals": { "0": { "temp": 55, "power": 1628 } }, "cpu_avg": { "cpu1": "4.26" } }
```
- `ts`：Unix 秒；`vals`：平铺 `{cardID: {字段: 值}}`（多节点时每个文件里仍是平铺，节点由子目录名/配置决定）；`cpu_avg` 可选。
- 字段名用小写下划线（`temp`/`power`/`aicore_freq`/`aicore_util`/`hbm_bandwidth_util`/`hbm_util`/`tx_bandwidth`/`rx_pfc_pkt`/`roce_tx_err_pkt`/`roce_out_of_order`/`roce_new_pkt_rty`/`nic_rx_all_pkg`），与 CSV 的 `NPU_CARD_*` 列名不同。其中 `nic_rx_all_pkg`（以及 CSV 的 `NPU_NIC_RX_ALL_PKG` 列）会被解析，但**不在 11 个检测指标内，只采集不参与判定**。

### 3.2 Profiler 输入

`path=` 传含每卡一个 SQLite 文件的目录：

```
/data/profiler_output/
├── ascend_pytorch_profiler_0.db
├── ascend_pytorch_profiler_1.db
└── ...
```

---

## 四、CLI 参数详解

### 顶层入口参数

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `path` | string | 否* | — | Profiler `.db` 目录（*与 KPI 输入至少提供一个） |
| `degradation` | float64 | 否 | 0.3 | 灵敏度。`< 0` 重置为 0.3；`> 1` 允许但告警。联动 Profiler 阈值 |
| `--kpi-path` | string | 否* | — | KPI 模式：每节点 CSV + `node_config.json` 的**目录** |
| `--kpi-jsonl-dir` | string | 否* | — | KPI 模式：CATMonitor `straggler_kpi_{date}.jsonl` 目录（优先于 `--kpi-path`） |
| `--space-ratio-threshold` | float64 | 否 | 2.0 | 空间 kmeans 簇比例阈值（独立旋钮，不随 degradation 变化） |
| `--debug-output` | bool | 否 | 假 | 输出全量数据排查未检出（仍在 `straggler_output.json`，不额外生成文件）：KPI 每个指标的 `cards` 列出全部卡（含正常的 score，`abnormal` 区分）；Profiler `node_result` 包含所有节点（含正常节点及其诊断 score） |

\* `path` 与 KPI 输入（`--kpi-path` / `--kpi-jsonl-dir`）至少提供一个；都没有则打印用法并退出。

### 守护进程（`--daemon`）参数

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `--daemon` | bool | 否 | 假 | 进入常驻守护进程模式（周期自动采集+检测，HTTP 查询/控制） |
| `--profiler-dir` | string | 是* | — | 采集落盘根目录（传给 dyno 的 `--log-file`）；每轮 dump 目录建在其下（*`--daemon` 时必填） |
| `--kpi-dir` | string | 否 | — | KPI 数据目录（CATMonitor JSONL；可选，缺省则每轮只跑 Profiler 检测） |
| `--daemon-port` | int | 否 | 8080 | HTTP 端口 |
| `--interval` | int | 否 | 600 | 检测周期（秒，≥60，非法回退默认） |
| `--collect-wait` | int | 否 | 60 | dyno 触发成功后的等待秒数 |
| `--profiler-iterations` | int | 否 | 5 | dyno nputrace 采集迭代数（传给 dyno 的 `--iterations`） |

`--daemon` 模式用法与 HTTP 接口详见[五、守护进程模式](#五守护进程模式常驻检测)。`degradation`、`--debug-output` 在该模式下语义不变（作用于每轮检测）。

### 阈值计算

```
KPI 模式:
  SpaceRatioThreshold = --space-ratio-threshold  # 空间簇比例阈值（默认 2.0，独立旋钮）

Profiler 模式:
  CalThreshold  = 1 + degradation              # 慢计算/慢CPU 阈值（默认 1.3）
  CommThreshold = 1 + degradation × 5          # 慢通信阈值（默认 2.5）
```

### KPI 内部配置（代码内默认值，非 CLI）

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `AggregationWindowSec` | 10 | 10 秒聚合窗口 |
| `TrimRatio` | 0.25 | 裁剪比例（每端 25%，中间 50%） |
| `MinSamplesForTrim` | 4 | 桶内原始样本 < 4 时降级为普通均值 |
| `SpaceRatioThreshold` | 2.0 | 空间 kmeans 簇比例阈值（CLI `--space-ratio-threshold` 覆盖） |

---

## 五、守护进程模式（常驻检测）

一次性模式按需手动运行；**守护进程模式（`--daemon`）常驻运行**，周期性自动完成「触发采集 → 转换 → 解析 → 分析」全链路，检测结果通过 HTTP 查询与运维控制，适合接入手管或调度系统持续巡检。

### 5.1 工作原理

每到一个周期，daemon 自动执行一次完整的检测循环（每周期数据为 **`--profiler-dir` 根目录下的全部 rank 子目录**——dyno 每个 rank 写一个 `master_<pid>_<ts>_ascend_pt`，互不共享状态）：

```
dyno 触发采集 → 校验生效(commandStatus=effective + 命中 vllm 进程) → 等待 collect-wait →
对整个 --profiler-dir 根目录 python analyse 转 .db（覆盖所有 rank）→ dataparse 解析 →
KPI 检测(读 --kpi-dir) + Profiler 检测(整个根目录) → 合并 JSON + daemon_meta.json 直接落盘 daemon_results/<start>/ → 周期结束删除整个 profiler-dir
```

同时检测 **KPI 资源**与 **Profiler 深查**（未提供 `--kpi-dir` 时 KPI 段跳过，仅跑 Profiler），两者合并为一份 `straggler_output.json`（只跑到的维度才有对应键，与一次性模式同形状）。启动后等待一个周期（`--interval`）再开始循环；`POST /daemon/trigger` 可随时手动补跑一轮。每轮周期把结果（合并 JSON、检测报告）直接落盘到 `--profiler-dir` 之外的 `daemon_results/<start>/`，周期结束时删除整个 `--profiler-dir`（dyno 下次采集自动重建）——存结果与删数据互不影响，防止 profiler 数据堆积影响后续检测。`Ctrl-C` / `SIGTERM` 优雅退出：停 HTTP、等当轮周期结束（≤10 分钟）、杀掉自己拉起的 dynolog、清理临时目录。

### 5.2 前置条件与启动

| 条件 | 说明 |
|------|------|
| 硬件 | aarch64 Linux + Ascend NPU + CANN |
| Python | 3.9–3.12，装有 `torch_npu`（`build.sh` 自动安装 mindstudio_monitor wheel） |
| 采集链路 | 训练进程以 `MSMONITOR_USE_DAEMON=1` 启动（dyno 才能命中并触发采集） |
| 构建 | 先跑 `bash build.sh`（架构检查 + 下载 dyno/dynolog + 装 Python 依赖 + go build） |

```bash
cd feature/straggler
bash build.sh          # 首次构建（见九、构建与部署）

./slowNodeDetection --daemon \
    --profiler-dir=/data/profiler \
    --kpi-dir=/data/kpi \
    --interval=600 \
    --collect-wait=60 \
    --profiler-iterations=5 \
    --daemon-port=8080 \
    --degradation=0.3
```

参数说明：

| 参数 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `--profiler-dir` | 是 | — | 采集落盘根目录（传给 dyno 的 `--log-file`） |
| `--kpi-dir` | 否 | — | KPI 数据目录（CATMonitor JSONL；缺省则每轮只跑 Profiler） |
| `--interval` | 否 | 600 | 检测周期（秒，≥60） |
| `--collect-wait` | 否 | 60 | 触发成功后等待采集完成的秒数 |
| `--profiler-iterations` | 否 | 5 | dyno 采集迭代数（默认 5） |
| `--daemon-port` | 否 | 8080 | HTTP 端口 |
| `--degradation` | 否 | 0.3 | 灵敏度（与一次性模式同义） |

> 注意：上方命令是**可直接复制执行**的写法（续行 `\` 后不留注释/空格）。若需给参数加注释，请写到注释区或参数表里，不要插在命令续行内——`\` 后跟注释会让续行失效，参数会被 shell 拆分。

`--profiler-dir` 必填；`--kpi-dir` 可选（缺省时每轮只跑 Profiler 检测，合并 JSON 不含 `kpi` 键）。日志打到 stderr。

> `--kpi-dir` 与单次调用的 `--kpi-jsonl-dir` 共用同一读取逻辑（`resource.ReadKPIFiles`），支持两种目录布局：
> 1. **平铺**：目录下直接放 `straggler_kpi_{date}.jsonl`（无 `node_config.json`）；
> 2. **多节点**：目录下有 `node_config.json`（`{"<folder>": {"node": "节点名", "cards": [...]}}`），jsonl 放在各 `<folder>/` 子目录内，按 per-node 卡号过滤。
>
> 注意：目录里**一旦存在 `node_config.json`，就按多节点布局读，顶层散放的 jsonl 会被忽略**。daemon 的 `--kpi-dir` 应指向 CATMonitor 的 `straggler_output.data_dir`（默认 `/var/lib/catmonitor/straggler`）且 CATMonitor 侧启用 `straggler_output` 插件。守护进程启动时会打印该目录可读取的 jsonl 文件数（两种布局都统计）；若为 0 会输出明确 WARNING。每轮周期的 KPI 执行结果（`ok` / `skipped: ...` / `failed: ...`）记录在 history 与 `/status` 的 `last_cycle.kpi_status` 中，KPI 未生效时原因一目了然。

### 5.3 HTTP 接口

路由**无 `/api/v1` 前缀**。查询类只读，控制类需 POST。

| 方法 & 路径 | 作用 | 请求体 |
|-------------|------|--------|
| `GET /healthz` | 存活探针 | — |
| `GET /status` | 状态总览：state / interval_sec / 两个数据目录 / cycles_total / cycles_failed / last_cycle / next_run_at | — |
| `GET /straggler/results/latest` | 最近一轮合并结果 JSON（数据源 = `daemon_results` 归档文件） | — |
| `GET /straggler/results/history?limit=N` | 本次会话全部周期摘要（含失败的 error），按时间倒序；`?limit=N` 可选，限制返回条数 | — |
| `GET /straggler/results/{id}` | 指定周期 id 的合并结果 JSON | — |
| `GET /straggler/report/latest` | 最近一轮 Profiler 文本报告（text/plain） | — |
| `POST /daemon/start` | 恢复运行（paused → running） | — |
| `POST /daemon/pause` | 暂停（在跑的周期跑完，不再排新的） | — |
| `POST /daemon/stop` | 优雅关闭守护进程（停 HTTP、等周期结束、杀 dynolog、删除全部落盘结果） | — |
| `POST /daemon/interval` | 修改检测周期 | `{"interval_sec": 300}`（60–86400） |
| `POST /daemon/trigger` | 立即补跑一轮（已有周期在跑 → 409） | — |

**curl 示例**：

```bash
# 查询
curl -s localhost:8080/status | jq
curl -s localhost:8080/straggler/results/latest | jq
curl -s localhost:8080/straggler/results/history | jq        # 全部历史；可用 ?limit=N 截断
curl -s localhost:8080/straggler/results/2 | jq
curl -s localhost:8080/straggler/report/latest

# 控制
curl -s -X POST localhost:8080/daemon/pause
curl -s -X POST localhost:8080/daemon/trigger
curl -s -X POST localhost:8080/daemon/interval -d '{"interval_sec": 300}'
curl -s -X POST localhost:8080/daemon/start
curl -s -X POST localhost:8080/daemon/stop
```

**`GET /status` 响应示例**：

```json
{
  "state": "running",
  "interval_sec": 600,
  "profiler_dir": "/data/profiler",
  "kpi_dir": "/data/kpi",
  "cycles_total": 3,
  "cycles_failed": 0,
  "last_cycle": {
    "id": 3,
    "started_at": "2026-08-20T10:00:00+08:00",
    "finished_at": "2026-08-20T10:02:00+08:00",
    "duration_ms": 120000,
    "dbs": 8,
    "dump_dir": "daemon_results/20260820-100000",
    "summary": { "cal": 1, "comm": 0, "cpu": 0, "npu_bubble": 0 }
  },
  "next_run_at": "2026-08-20T10:10:00+08:00"
}
```

### 5.4 数据落盘与重启

每轮周期的采集产物落在 `--profiler-dir` 根目录下（dyno 每个 rank 写一个 `master_<pid>_<ts>_ascend_pt` 子目录）；结果 JSON 与 meta 直接落盘到运行目录下的 `daemon_results/<start>/`（`--profiler-dir` 之外），整个 `--profiler-dir` 在周期结束时删除（dyno 下次采集自动重建，防堆积）：

```
<profiler-dir>/                    # 采集根目录；整个 profiler-dir 周期结束删除
├── master_<pid>_<ts>_ascend_pt/     # 每个 rank 一个子目录（dyno 落盘）
├── ascend_pytorch_profiler_*.db     # python analyse 转出的 SQLite（findDBs 递归发现）
├── op_metric/                       # dataparse 中间产物（写根下）
└── analysis_result/detection_report.log

daemon_results/<start>/           # 每轮结果直接落盘于此（归档记录；查询走内存 store）
├── straggler_output.json          # 本轮合并结果（latest/{id} 经 JSONPath 读）
├── daemon_meta.json               # 周期元数据（归档记录，查询不读）
└── analysis_result/detection_report.log   # 文本报告（归档记录，report/latest 走内存）
```

运行目录另有一份最新的 `straggler_output.json`（与一次性模式同形状，覆盖写）。

**查询只看本次会话**：所有查询接口（latest/history/{id}/report）都从进程内 store 读，daemon 重启后清空，不读磁盘历史；本次会话的历史无条数上限，`/straggler/results/history` 默认返回全部周期（可用 `?limit=N` 截断）；`/status` 的 `cycles_total`/`cycles_failed` 同样是本进程内累计（重启归零）。

### 5.5 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 每轮周期都失败，`error` 含 `processesMatched empty` | 训练进程未设 `MSMONITOR_USE_DAEMON=1`，dyno 没命中 → 检查训练启动参数 |
| 周期失败，`error` 含 `python analyse` | `torch_npu` 未装或版本不匹配 → 重跑 `build.sh` 装 mindstudio_monitor wheel |
| `dynolog exited` 日志但能检测 | IPC 端口已被占用，daemon 复用现有实例，属正常 |
| `trigger` 返回 409 | 已有周期在跑（周期内再触发被 single-flight 挡住），稍后再试 |

---

## 六、检测原理

### 6.1 KPI 检测（resource/）

```
CSV/JSONL 解析 → 10 秒聚合 → 空间检测(最后一点 peer 对比) →
按指标分组异常卡(含空间劣化程度) → 合并 JSON
```

**指标注册表**（方法 = 空间检测方法；cluster 方向自适应，由双方向标记数对比决定，无需预判）：

| 指标 | 分类 | 空间方法 | 说明 |
|------|------|---------|------|
| `temp` | 计算 | cluster | 温度 (°C) |
| `power` | 计算 | cluster | 功耗 (W) |
| `aicore_freq` | 计算 | cluster | AI Core 频率 (MHz)，离散档位 |
| `aicore_util` | 计算 | cluster | AI Core 利用率 (%) |
| `hbm_bandwidth_util` | 计算 | cluster | HBM 带宽使用率 (%) |
| `hbm_util` | 计算 | cluster | HBM 内存使用率 (%) |
| `tx_bandwidth` | 通信 | cluster | TX 带宽 |
| `rx_pfc_pkt` | 通信 | absolute | PFC 暂停帧（计数） |
| `roce_tx_err_pkt` | 通信 | absolute | RoCE 发送错误包（计数） |
| `roce_out_of_order` | 通信 | absolute | RoCE 乱序包（计数） |
| `roce_new_pkt_rty` | 通信 | absolute | RoCE 重传包（计数） |

**空间维度（peer 对比）**：只取全部数据的**最后一个聚合点**（时间维度与基线/检测窗口已移除，是否异常完全由空间维度判定）；peer 组 = 同一节点内的在场卡（跨节点不互比）。
- **cluster（kmeans 比例）**：共享 `clustering` 包。≤0 读数（含真实 0）钳制到极小值 `zeroFloor=1e-3`——真实 0 是空闲/关闭读数，参与聚类而非丢弃 → z-score 标准化（std≈0 强制 1）→ 肘部法选 k → kmeans++ + Lloyd 迭代（固定种子，结果确定）→ **双方向各检一次**（max：基线 = 最小均值簇；min：基线 = 最大均值簇），各得标记集 α1 / α2 → **标记数少的方向为异常，个数相等不上报** → 对选中方向异常簇递归精化。参与聚类的卡都输出**真实簇比值**：基线簇成员恰为 1.0，其他未标记簇保留真实比值（如 1.2），被标记卡为其比值（> 阈值）；判定用选中方向递归 `Detect` 的标记（不随比值变化）。多卡同档异常会一起标记。方向无需预判：单卡降频、升温、冷却都能检出。
- **absolute**：错误计数类指标，值 `> 0` 即异常（sentinel 999）。

**判定与输出**：某指标某卡空间异常 → 该卡异常。输出按**指标分组**：每个异常指标下列出异常的卡及其 `score`（劣化程度）。

### 6.2 Profiler 检测（profiling/）

```
SQLite .db → 并行域拓扑解析 → 单步快照 → 4 类检测 → 节点聚合 → 合并 JSON
```

| 类别 | 数据 | 阈值/方向 | 说明 |
|------|------|-----------|------|
| 慢计算 `cal` | ZP_Kernel（优先）/ ZP_Duration（降级） | `CalThreshold`(1+deg) | kmeans，方向 max/min |
| 慢通信 `comm` | `{域}_Duration` | `CommThreshold`(1+deg×5) | 每组取通信时长最小的卡为代表，按 PP stage 分桶后 kmeans，方向 max |
| 慢CPU `cpu` | ZP_Host（hostUid 平滑） | `CalThreshold` | 同主机卡取去 min/max 均值消除节点内差异 |
| Bubble `npu_bubble` | ZP_Bubble | `< 5000 ns` | 固定阈值直接判定 |

> cal / comm / cpu 三类检测统一走共享 `clustering` 包（kmeans 比例检测），与 KPI 空间 cluster 同一算法；Bubble 走固定阈值直接判定。

---

## 七、输出与解读

### 7.1 合并 JSON：`straggler_output.json`（运行目录）

两道检测结果合并为**一个文件**，写在**运行目录**（启动命令所在目录）：

```json
{
  "kpi": {
    "summary": { "total_cards": 8, "total_nodes": 2, "anomalies": 1, "normal": 7, "source": "...", "data_points": 129600, "space_ratio_threshold": 2.0 },
    "anomaly_metrics": [ { "metric": "aicore_freq", "method": "cluster", "cards": [ { "node": "node-1", "card_id": 0, "score": 2.25 } ] } ]
  },
  "profiler": {
    "node_result": [
      { "hostname": "<hostName>", "npu": [ { "id": 0, "cal": { "score": 1.5 }, "npu_bubble": { "score": 3200.0 } } ], "cpu": { "score": 1.4 } }
    ],
    "comm_domain_result": { "tp": { "0,1,2,3": 3.2 } }
  }
}
```

- **只跑 KPI** → 只有 `"kpi"` 键；**只跑 Profiler** → 只有 `"profiler"` 键；KPI 检测失败且无 Profiler → 不写文件。
- `kpi` 段 = KPI 检测结果（summary / anomaly_metrics，指标优先：每个异常指标下列异常卡及空间 score）。
- `profiler` 段 = 节点聚合结果：`node_result[]` 按物理节点（hostname，缺失回退 hostUid）分组，`npu[]` 只含异常 NPU（cal/npu_bubble），`cpu` 节点级；`comm_domain_result` 按通信域分组（组内 rank 逗号连接 → score）。

**`--debug-output` 调试输出**（不加额外键，直接在现有结果里展示所有数据，仍在 `straggler_output.json`）：
- **KPI**：`anomaly_metrics` 对**全部 11 个指标**列出其 `cards`（含正常的，`abnormal` 区分是否标异常），正常卡的 `score` 约 1.0，可对照看"为什么某指标没标"。
- **Profiler**：`node_result[]` 包含**所有节点**（异常+正常），正常节点的 `npu[]` 也列出其 `cal`/`npu_bubble`/`cpu` 的诊断 score（比值，正常 rank 约 1.0；无数据/被 ≤0 过滤的 rank 不出现该键）；`comm_domain_result` 也包含**所有通信组**（异常+正常），每组的 score 为代表卡比值（正常组约 1.0）：
  ```json
  { "hostname": "node-1", "npu": [ { "id": 0, "cal": { "score": 1.02 }, "npu_bubble": { "score": 8000 } } ], "cpu": { "score": 1.05 } }
  ```
  对照阈值即可看出"为什么某 rank/组 值不低却没被标"（比如 ratio 1.02 < CalThreshold 1.3）。

### 7.2 文本报告

| 报告 | 路径 | 内容 |
|------|------|------|
| Profiler 报告 | `path/analysis_result/detection_report.log` | 检测摘要表（4 类状态）、ZP_Kernel 跨 rank 排序柱状图、ZP_Host 跨节点对比（≥2 节点）、通信域分组对比 |

> KPI 已无文本报告文件（`npu_resource_detection_report.log` 已移除），KPI 文本仅打印到 stdout。

### 7.3 stdout

- KPI 文本报告（仅 stdout，不落盘）
- Profiler 逐类摘要（有异常才列出详情）：
  ```
  慢计算 (cal): 无异常 / 异常 (2) 0: 1.50x; 3: 1.60x
  慢通信 (comm): 无异常
  慢CPU (cpu): 无异常            ← 物理节点数 < 2 时整行不显示
  Bubble (npu_bubble): 无异常
  ```

### 7.4 结果字段解读

| 字段 | 含义 |
|------|------|
| `score` | 空间簇比例（cluster 方法）= 劣化程度；异常条件 `> SpaceRatioThreshold` |
| `anomaly_metrics[].cards[].abnormal` | 该指标下该卡是否空间异常（debug 模式才出现） |

---

## 八、边界情况

| 场景 | 处理 |
|------|------|
| 空间维度同行点 < 2 卡 | 该节点 score=0（无法 peer 对比） |
| 某节点在场卡 < 2 | 该节点 score=0，其他节点不受影响 |
| ≤0 读数（含真实 0） | 钳制到 `zeroFloor=1e-3` 参与聚类（空闲/关闭读数不丢弃）；NaN 排除 |
| 缺失 / NaN 卡 | 该卡该指标 score=0（无读数，不参与聚类） |
| 裁尾后数据不足 | 降级为普通均值（桶内样本 < 4）；截尾后不足 2 点 → 中位数兜底 |
| 计数器回绕 | 自动加 `MaxUint64` 修正 |
| JSONL 某天文件不存在 | 天然跳过（只读存在的文件） |
| CSV 列不完整 | 缺失列告警但不阻断，对应 metric dict 为空 |
| 仅 KPI 无 `path` | 只输出 KPI 结果（`straggler_output.json` 只有 `kpi` 键），不执行 Profiler |
| KPI 检测失败（有 `path`） | 告警后继续执行 Profiler |
| Profiler 单节点 | 慢CPU 无法检测，stdout 不显示该行 |
| `aicore_freq` 轻度降频（<2×） | 簇比例未超阈值 → 空间不标记（时间维度已移除，无其他兜底） |

---

## 九、构建与部署

### 标准构建（`build.sh`，推荐）

`build.sh` 在 **aarch64 Linux** 主机上一次完成依赖与编译（仅支持 aarch64，其他架构直接报错退出）：

```bash
cd feature/straggler
bash build.sh
```

它依次做 6 件事：
1. **架构检查**：`uname -m` != `aarch64` → 报错退出
2. **安装 dyno / dynolog**：wget 直接下载 `dynolog_0.3.2_1.aarch64.deb`（msmonitor daily bucket）→ 检测系统包管理器（dpkg 原生安装 .deb；rpm 系需 `alien` 转换）→ 安装，使 `dyno` / `dynolog` 直接可从 PATH 调用（已安装则跳过）
3. **Python 版本检查**：须 3.9 / 3.10 / 3.11 / 3.12，否则报错退出
4. **装依赖**：下载并 `pip install` 对应的 `mindstudio_monitor-26.2.0-cp<xx>-cp<xx>-linux_aarch64.whl`（cp 标签随 Python 版本）
5. **Go 工具链**：需 >= go.mod 版本；缺失/过旧时从阿里云镜像下载并持久化 PATH（`/usr/local/go`，不可写时 `~/.local/go`）
6. **编译**：`CGO_ENABLED=0 go build -o slowNodeDetection .`

产物 `./slowNodeDetection`。dyno/dynolog 由第 2 步安装到**系统**（不进仓库，也不再使用 `3rdparty/`）；下载的中间文件在临时目录，退出即清理。改动 Go 代码后只需重跑第 6 步（或直接 `go build`）。

### 手动编译 / 跨平台

Go 编译**不再依赖** dyno/dynolog 二进制（已无 embed），任何平台都能出包；daemon 模式只在运行时要求目标 aarch64 主机装好 dyno/dynolog（跑一次 `build.sh` 即可）：

```bash
cd feature/straggler
go mod tidy                      # 首次拉取 modernc.org/sqlite 依赖（需网络）
CGO_ENABLED=0 go build -o slowNodeDetection .                                   # aarch64（本机，daemon + 一次性都可用）
CGO_ENABLED=0 GOOS=linux  GOARCH=amd64 go build -o slownode_linux_amd64 .       # 跨平台，仅一次性模式
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -o slownode_win_amd64.exe .    # 跨平台，仅一次性模式
```

全静态二进制，无 CGo（Profiler 用纯 Go SQLite 驱动 `modernc.org/sqlite`）。

**测试**：
```bash
go build ./... && go test ./...
```

---

## 十、设计文档

- [DESIGN_NPU_RESOURCE.md](./DESIGN_NPU_RESOURCE.md) — KPI 资源指标检测设计
- [DESIGN.md](./DESIGN.md) — Profiling 检测设计
- [SPEC.md](./SPEC.md) — 检测技术规范
- [straggler_combination_DESIGN.md](./straggler_combination_DESIGN.md) — 与 CATMonitor 底座的整合设计
