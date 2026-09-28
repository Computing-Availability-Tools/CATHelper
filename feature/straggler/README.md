# CATHelper — 慢节点（Straggler）检测

AI 智算集群中识别性能劣化 NPU 卡的两道独立检测。**Profiling 深查**（优先执行）基于 Ascend PyTorch Profiler 数据从计算/通信/CPU/Bubble 四个维度精查；**KPI 资源检测**（轻量）基于 NPU 资源指标做空间 peer 对比（最后一个聚合点）。两道检查互不阻塞，结果合并输出为**一个 JSON 文件**。既支持一次性手动运行，也支持**守护进程模式**（`--daemon`）常驻运行：周期性自动完成采集→检测，结果通过 HTTP 查询与运维控制。

---

## 目录

- [一、安装与构建](#一安装与构建)
- [二、基于 Profiling 算子数据的慢卡检测](#二基于-profiling-算子数据的慢卡检测)
- [三、基于资源 KPI 指标数据的慢卡检测](#三基于资源-kpi-指标数据的慢卡检测)
- [四、HTTP 接口（守护进程）](#四http-接口守护进程)
- [五、中心节点模式](#五中心节点模式)
- [六、输出与解读](#六输出与解读)
- [七、CLI 参数参考](#七cli-参数参考)
- [八、边界情况](#八边界情况)
- [九、目录结构](#九目录结构)
- [十、设计文档](#十设计文档)

---

## 一、安装与构建

> 依赖一次装齐，后续所有模式共用。仅改了 Go 代码时，重跑 `CGO_ENABLED=0 go build -o slowNodeDetection .` 即可，不必再走完整 `build.sh`。

### 1.1 环境要求

| 项 | 要求 |
|----|------|
| 硬件 | **aarch64（ARM64）** Linux + Ascend NPU + CANN（守护进程模式需要） |
| Python | 3.9 / 3.10 / 3.11 / 3.12 之一，带 `pip` |
| C++ | gcc 8.5.0+（msmonitor wheel 编译用；缺失则回退下载预编译 wheel） |
| Rust | Rust ≥ 1.81（dyno/dynolog 编译用；缺失则回退下载预编译安装包） |
| 包管理器 | `dpkg`（Debian/Ubuntu，推荐）或 `rpm + alien` |
| 下载工具 | `wget`（Go / dynolog / wheel 兜底下载需要） |
| 网络 | 能访问模块代理（`modernc.org/sqlite`）、OBS / Aliyun 镜像 |

### 1.2 构建

```bash
cd feature/straggler
bash build.sh
# TLS 证书校验失败时：bash build.sh --insecure
```

`build.sh` 依次做 6 件事：

1. **架构检查**：`uname -m` 必须为 `aarch64`，否则报错退出。
2. **Python 版本检查**：必须是 3.9 / 3.10 / 3.11 / 3.12（决定 wheel 的 `cp` 标签）。
3. **安装 dyno / dynolog**（守护进程采集用）：优先 `git submodule update` 后在 `3rdparty/msmonitor` 内 `build.py -e dynolog=true` 构建并 `dpkg`/`rpm` 安装；失败则回退到 OBS 下载 `dynolog_0.3.2_1.aarch64.deb`。若 `dyno`/`dynolog` 已在 PATH 则跳过。
4. **安装 mindstudio_monitor wheel**（`python analyse` 转 `.db` 用）：同样优先子模块构建、回退下载 `mindstudio_monitor-26.2.0-cp<xx>-cp<xx>-linux_aarch64.whl` 并 `pip install`。若已装 26.2.0 则跳过。
5. **确保 Go 工具链** ≥ `go.mod` 要求（1.23.4）：缺失/过旧时从阿里云镜像下载到 `/usr/local/go`（不可写则 `~/.local/go`）并持久化 PATH。
6. **编译**：`CGO_ENABLED=0 go build -o slowNodeDetection .`。

产物 `./slowNodeDetection`。dyno/dynolog 装到系统，可从 PATH 直接调用；下载的中间文件在临时目录，退出即清理。

### 1.3 只手动编译（不改采集依赖，1.2 的替代方案）

若只想出包、不装 dyno/dynolog 和 mindstudio_monitor，可跳过 `build.sh` 直接编译（Go 编译不依赖这些）：

```bash
cd feature/straggler
go mod tidy                      # 首次拉取 modernc.org/sqlite（需网络）
CGO_ENABLED=0 go build -o slowNodeDetection .
```

- aarch64 本机构建：一次性模式 + 守护进程模式都可用（前提：目标机已装 dyno/dynolog）。
- 跨平台（仅一次性模式，无守护进程采集）：`GOOS=linux GOARCH=amd64` / `GOOS=windows GOARCH=amd64` 同理。
- 产物全静态、无 CGo（Profiler 用纯 Go SQLite 驱动）。

### 1.4 验证

```bash
./slowNodeDetection path=/nonexistent    # 应报"Invalid directory"而非"dyno not found"
dyno --help >/dev/null && echo "dyno OK"
dynolog --help >/dev/null && echo "dynolog OK"
python3 -c "import msmonitor; print('mindstudio_monitor OK')"
```

---

## 二、基于 Profiling 算子数据的慢卡检测

基于 Ascend PyTorch Profiler 算子数据识别慢卡，覆盖慢计算/慢通信/慢CPU/Bubble 四个维度，深查定位。跑完即退出（一次性检测），产物是运行目录下的 `straggler_output.json`（只跑本维度时仅含 `profiler` 键）。

### 2.1 在容器/hostOS 内拉起慢节点的守护进程

在训练容器内或 hostOS 上常驻拉起守护进程，周期性自动完成 Profiler 采集→检测（配 `--kpi-dir` 时同时做 KPI 检测），结果经 HTTP 查询（见[四、HTTP 接口（守护进程）](#四http-接口守护进程)）。前置：已跑 `bash build.sh`（见 1.2）；训练进程以 `MSMONITOR_USE_DAEMON=1` 启动（见 2.2）；准备 `--profiler-dir`（采集落盘根，可空目录）：

```bash
cd feature/straggler
./slowNodeDetection --daemon \
    --profiler-dir=/data/profiler \
    --kpi-dir=/data/kpi \
    --interval=600 \
    --collect-wait=60 \
    --profiler-iterations=1 \
    --daemon-port=8080 \
    --degradation=0.3
```

启动参数说明：

| 参数 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `--profiler-dir` | 是 | — | dyno 采集落盘根目录（传给 dyno 的 `--log-file`） |
| `--kpi-dir` | 否 | — | KPI 数据目录（CATMonitor JSONL）；缺省则每轮只跑 Profiler |
| `--interval` | 否 | 600 | 检测周期（秒，≥60） |
| `--collect-wait` | 否 | 60 | dyno 触发成功后的等待秒数 |
| `--profiler-iterations` | 否 | 1 | dyno nputrace 每轮采集迭代数 |
| `--daemon-port` | 否 | 8080 | HTTP 端口 |
| `--degradation` | 否 | 0.3 | 灵敏度（与一次性模式同义） |

> 命令为**可直接复制执行**写法：续行 `\` 后不留注释/空格，否则 shell 会把反斜杠当成普通字符导致参数被拆散。

**启动后行为**：
- 拉起 dynolog、启动 HTTP 服务；首个周期在 `--interval` 之后运行（想立即跑一轮用 `POST /daemon/trigger`）。
- 周期结束删除整个 `--profiler-dir`（dyno 下次采集自动重建），防止数据堆积影响后续定位。
- `Ctrl-C` / `SIGTERM` 优雅退出：停 HTTP、等当轮周期结束（≤10 分钟）、杀掉自己拉起的 dynolog。

**HTTP 查询与控制**（完整接口见[四、HTTP 接口（守护进程）](#四http-接口守护进程)）：

| 方法 & 路径 | 作用 |
|-------------|------|
| `GET /status` | 状态总览（state / 周期 / cycles_total / last_cycle 等） |
| `GET /straggler/results/latest` | 最近一轮合并结果 JSON |
| `GET /straggler/results/history?limit=N` | 本次会话周期摘要（倒序） |
| `GET /straggler/report/latest` | 最近一轮 Profiler 文本报告 |
| `POST /daemon/pause` | 暂停（在跑周期跑完，不再排新的） |
| `POST /daemon/trigger` | 立即补跑一轮（已有周期在跑 → 409） |
| `POST /daemon/stop` | 优雅关闭守护进程（删除全部落盘结果） |

```bash
curl -s localhost:8080/status | jq
curl -s localhost:8080/straggler/results/latest | jq
curl -s -X POST localhost:8080/daemon/trigger
```

### 2.2 拉起训练/推理业务

训练或推理进程在启动时，需要在 CLI 启动命令前加上 `MSMONITOR_USE_DAEMON=1`，dyno 才能命中进程并触发采集。例如：

```bash
MSMONITOR_USE_DAEMON=1 vllm serve /path/to/model ...
```

### 2.3 Profiler 数据准备与检测

守护进程模式下，通过 curl 触发常驻进程开启一轮数据采集（`POST /daemon/trigger`；也可等待 `--interval` 周期自动触发）：

```bash
curl -s -X POST localhost:8080/daemon/trigger
```

触发后整条链路**自动完成，无需人工干预**：

```
dyno 触发采集 → 校验生效(commandStatus=effective + 命中 vllm) → 等待 collect-wait →
python analyse 转 .db（覆盖根下所有 rank）→ dataparse 解析 →
Profiler 检测(整个根目录) → KPI 检测(读 --kpi-dir) → 合并 JSON + meta 落盘 daemon_results/<start>/ →
周期结束删除整个 profiler-dir
```

- **采集转换**：每个 NPU 卡获得一个 Ascend PyTorch Profiler Level0 SQLite 文件，位于 `--profiler-dir` 下；
- **检测**：守护进程内部自动执行，即一次性检测管线（等价于 `./slowNodeDetection path=/data/profiler_output degradation=0.3`），无需手动运行；
- **归档与查询**：结果落盘 `daemon_results/<start>/` 并经 HTTP 查询（见[四、HTTP 接口（守护进程）](#四http-接口守护进程)）；周期结束时 daemon 自动清理 `--profiler-dir`，`.db` 文件无需手动处理。

> **不依赖守护进程的一次性检测**：把自行准备好的 `.db` 目录（每卡一个 Level0 SQLite 文件）直接喂给检测器，跑完即退出：
>
> ```
> /data/profiler_output/
> ├── ascend_pytorch_profiler_0.db
> ├── ascend_pytorch_profiler_1.db
> └── ...
> ```
>
> ```bash
> ./slowNodeDetection path=/data/profiler_output degradation=0.3
> ```

### 2.4 检测原理

```
SQLite .db → 并行域拓扑解析 → 单步快照 → 4 类检测 → 节点聚合 → 合并 JSON
```

| 类别 | 数据 | 阈值/方向 | 说明 |
|------|------|-----------|------|
| 慢计算 `cal` | ZP_Kernel（优先）/ ZP_Duration（降级） | `CalThreshold`(1+deg) | kmeans，方向自适应 |
| 慢通信 `comm` | `{域}_<opType>_<count>`（带宽回填列） | `SlowCommRatio`(默认 1.3) | 按算子类型分类，每组取 count 最大带宽作代表（带宽=count/组内单次算子最小时间的升序前10%均值），仅保留 count ≥ 最大 count×50% 且 count > 10240 的组后做 kmeans 递归聚类（带宽小=慢）；一个组须在**所有算子类型都异常**才上报，劣化数值取各算子中的最大值 |
| 慢CPU `cpu` | ZP_Host（hostUid 平滑） | `CPUThreshold`(1+deg×5) | 同主机卡取去 min/max 均值消除节点内差异 |
| Bubble `npu_bubble` | ZP_Bubble | `< 5000 ns` | 固定阈值直接判定 |

> cal / cpu 走共享 `clustering` 包（kmeans 比例检测，与 3.3 的 KPI 空间 cluster 同一算法）；慢通信走带宽比较（先回填 `{域}_<opType>_<count>` 带宽列，再按 `SlowCommRatio` 判劣化）；Bubble 走固定阈值。

**中间产物（`op_metric/`）**：解析阶段在每个数据目录下生成每 rank 三件套——`group_info_{N}.json`（并行拓扑）、`host_info_{N}.json`（rank→hostUid）、`global_rank_{N}.csv`（各域通信耗时/计数 + ZP_* 指标）。守护进程会把每轮 `op_metric/` 归档到 `daemon_results/<start>/op_metric/` 供复查。

---

## 三、基于资源 KPI 指标数据的慢卡检测

基于 NPU 资源指标的空间 peer 对比（最后一个聚合点）识别慢卡，轻量、无侵入。跑完即退出（一次性检测），产物是运行目录下的 `straggler_output.json`（只跑本维度时仅含 `kpi` 键）。

### 3.1 KPI 数据准备（二选一）

#### 选项 A：CATMonitor JSONL 目录（`--kpi-jsonl-dir`，推荐）

一个目录，内含 `straggler_kpi_{date}.jsonl`。支持两种布局：

- **平铺**：文件直接放目录下。
  ```
  {dir}/
  ├── straggler_kpi_2026-08-13.jsonl
  └── straggler_kpi_2026-08-12.jsonl
  ```
- **多节点**：每节点一个子目录 + 顶层 `node_config.json`。
  ```
  {dir}/
  ├── node-a/straggler_kpi_2026-08-13.jsonl
  ├── node-b/straggler_kpi_2026-08-13.jsonl
  └── node_config.json
  ```
  `node_config.json`（key = 子目录名；`node` = 参与 peer 对比的节点名；`cards` = 该节点实际使用卡号，0 起始）：
  ```json
  { "node-a": { "node": "node-1", "cards": [0, 1] }, "node-b": { "node": "node-2", "cards": [0, 1] } }
  ```
  目录一旦存在 `node_config.json` 就按多节点布局读，顶层散放 jsonl 会被忽略。

每行 JSON 记录格式（字段名小写下划线）：

```json
{ "ts": 1784547926, "vals": { "0": { "temp": 55, "power": 1628 } }, "cpu_avg": { "cpu1": "4.26" } }
```

支持字段：`temp` / `power` / `aicore_freq` / `aicore_util` / `hbm_bandwidth_util` / `hbm_util` / `tx_bandwidth` / `rx_pfc_pkt` / `roce_tx_err_pkt` / `roce_out_of_order` / `roce_new_pkt_rty` / `nic_rx_all_pkg`。其中 `nic_rx_all_pkg` 只采集、不参与判定。

#### 选项 B：每节点 CSV 目录（`--kpi-path`，遗留模式）

一个目录：每个节点一个 CSV + 固定 `node_config.json`。

```
/data/kpi_csv_dir/
├── node1.csv
├── node2.csv
└── node_config.json
```

CSV 每行一个时间戳，指标列为 `{cardID: value}` 的 JSON dict：

```csv
timestamp,NPU_CARD_TEMP,NPU_CARD_POWER,NPU_CARD_AICORE_FREQ,...
1784547926,"{""0"":55,""1"":56}","{""0"":1628}","{""0"":1800}",...
```

`node_config.json`（key = CSV 文件名）：
```json
{ "node1.csv": { "node": "node-1", "cards": [0, 1] }, "node2.csv": { "node": "node-2", "cards": [0, 1] } }
```

### 3.2 一次性检测

```bash
cd feature/straggler
./slowNodeDetection --kpi-jsonl-dir=/var/lib/catmonitor/straggler
# 或遗留 CSV 目录：
./slowNodeDetection --kpi-path=/data/kpi_csv_dir
```

> **联合 Profiler 检测**：同时提供 `path`（Profiler `.db` 目录）时自动先跑 Profiler 深查、再跑 KPI（两道检查相互独立、互不阻塞，见 2.3）：
>
> ```bash
> ./slowNodeDetection path=/data/profiler_output --kpi-jsonl-dir=/var/lib/catmonitor/straggler degradation=0.3
> ```

### 3.3 检测原理

```
CSV/JSONL 解析 → 10 秒聚合 → 空间检测(最后一点 peer 对比) →
按指标分组异常卡(含空间劣化程度) → 合并 JSON
```

**指标注册表**（cluster 方向自适应，双方向标记数少者为异常）：

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

**空间维度（peer 对比）**：只取全部数据的最后一个聚合点（时间维度/基线/检测窗口已移除）；peer 组 = 同一节点内的在场卡（跨节点不互比）。
- **cluster（kmeans 比例）**：≤0 读数钳制到极小值 `zeroFloor=1e-3` 参与聚类 → z-score 标准化（std≈0 强制 1）→ 肘部法选 k → kmeans++ + Lloyd 迭代（固定种子，结果确定）→ 双方向各检一次（max：基线=最小均值簇；min：基线=最大均值簇）→ 标记数少的方向为异常、相等不上报 → 对选中方向异常簇递归精化。score = **簇均值 / 基线均值**（统一 min/max 两侧：max 侧 `> 阈值` 判异常，min 侧 `< 1/阈值` 判异常）；判定用递归 `Detect` 的标记，不随比值变化。
- **absolute**：错误计数类指标，值 `> 0` 即异常。

---

## 四、HTTP 接口（守护进程）

路由无 `/api/v1` 前缀。查询类只读，控制类需 POST。以下假设端口 8080（`--daemon-port` 可改）。

| 方法 & 路径 | 作用 | 请求体 |
|-------------|------|--------|
| `GET /` | Web 控制台页面（HTML，纯前端） | — |
| `GET /healthz` | 存活探针 | — |
| `GET /status` | 状态总览（state / interval_sec / degradation / 数据目录 / cycles_total / cycles_failed / last_cycle / next_run_at） | — |
| `GET /straggler/results/latest` | 最近一轮合并结果 JSON | — |
| `GET /straggler/results/history?limit=N` | 本次会话全部周期摘要（倒序；`?limit=N` 可选限制条数） | — |
| `GET /straggler/results/{id}` | 指定周期 id 的合并结果 JSON | — |
| `GET /straggler/report/latest` | 最近一轮 Profiler 文本报告（text/plain） | — |
| `GET /straggler/report/{id}` | 指定周期 id 的 Profiler 文本报告（text/plain） | — |
| `GET /straggler/op_metric/latest` | 最近一轮 `op_metric` 聚合视图（rank → group_info/host_info/global_rank） | — |
| `GET /straggler/op_metric/{id}` | 指定周期 id 的 `op_metric` 聚合视图 | — |
| `GET /straggler/op_metric/{id}/{file}` | 下载该周期 `op_metric` 原始文件（如 `global_rank_0.csv`） | — |
| `POST /daemon/start` | 恢复运行（paused → running） | — |
| `POST /daemon/pause` | 暂停（在跑周期跑完，不再排新的） | — |
| `POST /daemon/stop` | 优雅关闭守护进程（停 HTTP、等周期结束、杀 dynolog、删除全部落盘结果） | — |
| `POST /daemon/interval` | 修改检测周期 | `{"interval_sec": 300}`（60–86400） |
| `GET /daemon/degradation` | 读取阈值基数（当前灵敏度） | — |
| `POST /daemon/degradation` | 修改阈值基数（只影响后续检测，不改历史结果） | `{"degradation": 0.3}`（[0,1)） |
| `POST /daemon/trigger` | 立即补跑一轮（已有周期在跑 → 409） | — |
| `POST /daemon/match` | 被中心节点匹配（进入 managed 托管） | `{"center_addr","key","business","daemon"}` |
| `POST /daemon/unmatch` | 解除托管（回到 running，需 `X-Match-Key`） | — |
| `GET /daemon/match_status` | 查询匹配状态（`X-Match-Key` 匹配则确认归属） | — |

**curl 示例**：

```bash
# 查询
curl -s localhost:8080/status | jq
curl -s localhost:8080/straggler/results/latest | jq
curl -s localhost:8080/straggler/results/history | jq
curl -s localhost:8080/straggler/results/2 | jq
curl -s localhost:8080/straggler/report/latest
curl -s localhost:8080/straggler/op_metric/latest | jq
curl -s localhost:8080/straggler/op_metric/1/global_rank_0.csv

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

**`GET /straggler/op_metric/latest` 聚合视图**：一级键为 rank，二级为 `group_info` / `host_info` / `global_rank`（CSV 已转 JSON 对象，多行时为数组）：

```json
{
  "cycle": 3,
  "dir": "daemon_results/20260820-100000/op_metric",
  "ranks": {
    "0": {
      "group_info": { "group_name_3": { "group_name": "tp", "global_ranks": [0,1,2,3,4,5,6,7] } },
      "host_info":  { "rank": "0", "hostUid": "..." },
      "global_rank": { "StepIndex": 0, "ZP_Kernel": 20.87, "tp_Duration": 51.88 }
    }
  }
}
```

#### 常见启动/周期问题

| 现象 | 原因与处理 |
|------|-----------|
| 每轮周期失败，`error` 含 `processesMatched empty` | 训练进程未设 `MSMONITOR_USE_DAEMON=1`，dyno 没命中 → 检查训练启动参数 |
| 周期失败，`error` 含 `python analyse` | `torch_npu`/`mindstudio_monitor` 未装或版本不符 → 重跑 `bash build.sh` |
| `dynolog exited` 日志但能检测 | IPC 端口已被占用，daemon 复用现有实例，属正常 |
| `trigger` 返回 409 | 已有周期在跑（single-flight），稍后再试 |
| 传 `--daemon-port` 却不生效 | 多半是启动命令续行 `\` 后带了注释/空格把参数拆散（见 2.1 注意） |

---

## 五、中心节点模式

中心节点（`--center`）统一管理多个**业务**（business）——每个业务是一个真实训练任务，对应 1..N 个守护进程（一个任务跨节点、全局唯一 rank）。中心按周期触发业务内所有守护进程「采集→检测→上报」，接收它们上报的 op_metric，合并后**再检测一遍**（多节点数据放一起，可触发跨节点/主机级指标），落盘结果并对外提供业务级 Web 控制台。

### 5.1 启动

```bash
./slowNodeDetection --center \
    --center-port=8080 \
    --center-data-dir=center_data \
    --center-interval=600
```

| 参数 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `--center` | — | — | 进入中心节点模式 |
| `--center-port` | 否 | 8080 | 中心节点 HTTP 端口 |
| `--center-data-dir` | 否 | center_data | 持久化根目录（业务列表 + 每业务每周期结果） |
| `--center-interval` | 否 | 600 | 新业务默认触发周期（秒，≥60） |

### 5.2 核心概念

- **业务（business）**：一个真实训练任务；含名称、触发周期、守护进程列表、阈值基数、vllm `/metrics` 端点。
- **守护进程（daemon）**：业务内的一个 `--daemon`（ip:port）。
- **匹配（match）**：中心为每个守护进程生成**独立密钥**并下发，守护进程进入 `managed` 托管状态。密钥持久化落盘，中心重启后带同一密钥自证身份，无需守护进程重新匹配。
- **阈值基数（degradation）**：每个业务独立，初始值继承节点启动时的 `degradation`，可在业务控制台动态调整。

### 5.3 托管（managed）语义

- 进入 managed 后，守护进程**停止自主调度**：`/daemon/start`、`/daemon/pause`、`/daemon/interval` 被拒绝；`/daemon/trigger` 必须带中心密钥（`X-Match-Key`）才能触发。
- 中心按业务周期**并发 trigger** 业务内所有守护进程：各守护进程「采集 → 分析 → 本地检测 → 上报 op_metric」，中心收齐（或 `max(各守护进程 collect-wait)+60s` 超时）后合并检测。
- **双向心跳**：中心每 5s 探守护进程 `/healthz` + `/daemon/match_status`（带密钥）；守护进程反向探中心 `/center/healthz`，中心挂则自动解除匹配回 `running`。

### 5.4 守护进程状态（中心视角）

| 状态 | 判定 | 说明 |
|------|------|------|
| 已匹配 | healthz 通 + 匹配当前中心 | 可触发 |
| 断连 | 连续 3 次 healthz 失败 | 业务冻结，恢复即解冻 |
| 未匹配 | healthz 通但不在 managed | 需手动「匹配」 |
| 匹配他人 | managed 但密钥非本中心 | 需到对方中心解除 |
| 上报失败 | trigger 后超时未上报 | 下次探测自动恢复 |

业务**任一**守护进程非「已匹配」即整体冻结（跳过本轮触发）。

### 5.5 HTTP 接口（中心节点）

| 方法 & 路径 | 作用 |
|-------------|------|
| `GET /` | 业务管理 Web 控制台 |
| `GET /center/healthz` | 中心存活探针（守护进程反向心跳用） |
| `GET /center/businesses` | 业务列表（含每守护进程状态） |
| `POST /center/business` | 添加业务 `{"name","interval_sec","daemons":[{"ip","port"}]}` |
| `DELETE /center/business/{name}` | 删除业务 |
| `POST /center/business/{name}/daemon` | 添加守护进程 `{"ip","port"}` |
| `DELETE /center/business/{name}/daemon?ip=&port=` | 删除守护进程 |
| `POST /center/business/{name}/daemon/match` | 手动匹配（重连）`{"ip","port"}` |
| `POST /center/business/{name}/daemon/unmatch` | 手动解除匹配 `{"ip","port"}` |
| `POST /center/business/{name}/trigger` | 立即触发业务检测 |
| `POST /center/business/{name}/pause` | 暂停业务调度 |
| `POST /center/business/{name}/start` | 恢复业务调度 |
| `POST /center/business/{name}/interval` | 修改业务触发周期 `{"interval_sec":600}` |
| `POST /center/business/{name}/degradation` | 修改业务阈值基数 `{"degradation":0.3}` |
| `POST /center/business/{name}/vllm` | 设置业务 vllm `/metrics` 端点 `{"url"}` |
| `DELETE /center/business/{name}/vllm` | 清除 vllm 端点 |
| `GET /center/business/{name}/metrics` | 业务 TPOT/TTFT 延迟时序（按 engine） |
| `POST /center/op_metric/{business}/{daemon}` | 守护进程上报 op_metric（`X-Match-Key` 鉴权） |
| `GET /center/business/{name}/history` | 业务检测历史 |
| `GET /center/business/{name}/report` | 业务最新合并检测报告（text/plain） |
| `GET /center/business/{name}/result` | 业务最新合并结果 JSON |
| `GET /center/business/{name}/op_metric` | 业务最新合并 op_metric JSON |

### 5.6 Web 控制台 / 业务控制台

浏览器访问 `http://<host>:<center-port>/` 打开**业务管理控制台**：业务列表（名称/周期/守护进程/健康汇总）、守护进程状态徽章、添加业务与守护进程、匹配/解除/删除，以及每业务的「进入控制台」。

每个业务有独立的**业务控制台**（`/center/business/{name}/console`）：运行状态、控制（立即触发/暂停/启动/改周期/改阈值基数）、节点列表（守护进程 ip:port）、检测历史/报告/结果/op_metric，以及顶部的 **vllm 延迟指标图**（TPOT / TTFT，按 engine 分曲线，点击图例可显示/隐藏单条 engine，20s 采样一个点）。

---

## 六、输出与解读

### 6.1 合并 JSON：`straggler_output.json`

一次性模式写在**运行目录**；守护进程模式额外归档到 `daemon_results/<start>/straggler_output.json`：

```json
{
  "profiler": {
    "node_result": [
      { "hostname": "<hostName>", "npu": [ { "id": 0, "cal": { "score": 1.5 }, "npu_bubble": { "score": 3200.0 } } ], "cpu": { "score": 1.4 } }
    ],
    "comm_domain_result": { "tp": { "0,1,2,3": 3.2 } }
  },
  "kpi": {
    "summary": { "total_cards": 8, "total_nodes": 2, "anomalies": 1, "normal": 7, "source": "...", "data_points": 129600, "space_ratio_threshold": 2.0 },
    "anomaly_metrics": [ { "metric": "aicore_freq", "method": "cluster", "cards": [ { "node": "node-1", "card_id": 0, "score": 0.44 } ] } ]
  }
}
```

- **只跑 Profiler** → 只有 `"profiler"` 键；**只跑 KPI** → 只有 `"kpi"` 键。
- `profiler` 段 = `node_result[]` 按物理节点分组（hostname，缺失回退 hostUid），`npu[]` 只含异常 NPU（cal / npu_bubble），`cpu` 节点级；`comm_domain_result` 按通信域分组（组内 rank 逗号连接 → score）。
- `kpi` 段 = summary + `anomaly_metrics`（指标优先：每个异常指标下列异常卡及其空间 score）。

**守护进程模式的数据落盘**（`--profiler-dir` 之外，不受周期清理影响）：

```
daemon_results/<start>/
├── straggler_output.json          # 本轮合并结果（HTTP 读取的数据源）
├── daemon_meta.json               # 周期元数据（归档记录）
├── op_metric/                     # 检测输入快照（group_info_*.json / host_info_*.json / global_rank_*.csv）
└── analysis_result/
    └── detection_report.log       # 本轮 Profiler 文本报告
```

运行目录另有一份最新的 `straggler_output.json`（覆盖写，与一次性模式同形状）。

- 查询接口读**进程内 store**（本次会话），daemon 重启后清空，不读磁盘历史；历史无条数上限，可用 `?limit=N` 截断。
- `POST /daemon/stop` 会在优雅关闭后**删除整个 `daemon_results/`**（所有落盘结果一并清掉）。

**`--debug-output` 调试输出**（排查"某卡为什么没被判异常"时使用；不额外生成文件，直接在现有结果里展示全量）：
- KPI：`anomaly_metrics` 对全部 11 个指标列出其 `cards`（含正常的，`abnormal` 区分），正常卡 `score` 约 1.0。
- Profiler：`node_result[]` 含所有节点、`comm_domain_result` 含所有通信组，正常卡/组 `score` 约 1.0，对照阈值可看出"为什么没被标"。

### 6.2 文本报告

| 报告 | 路径 | 内容 |
|------|------|------|
| Profiler 报告 | `path/analysis_result/detection_report.log` | 头部（数据来源 / 生成时间 / 有效 rank 数）、检测摘要表（4 类状态，含计算类/通信类阈值）、计算类算子耗时排序（带劣化指数）、ZP_Host 跨节点对比（≥2 节点，慢CPU）、通信域分组对比（耗时 + 劣化指数，按耗时降序） |

守护进程会把该报告归档到 `daemon_results/<start>/analysis_result/` 并经 `/straggler/report/{id}` 提供。KPI 无文本报告文件，文本仅打印到 stdout。报告头部「数据来源」：守护进程/一次性模式为数据目录，中心节点模式为业务内所有守护进程的 `ip:port`（英文逗号分隔）。

### 6.3 stdout 摘要

一次性模式与守护进程日志均打到 stderr；KPI 文本报告仅 stdout。Profiler 逐类摘要示例：

```
慢计算 (cal): 异常 (2) 0: 1.50x; 3: 1.60x
慢通信 (comm): 无异常
慢CPU (cpu): 无异常            ← 物理节点数 < 2 时整行不显示
Bubble (npu_bubble): 无异常
```

---

## 七、CLI 参数参考

### 顶层参数（一次性模式）

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `path` | string | 否* | — | Profiler `.db` 目录（*与 KPI 输入至少提供一个） |
| `degradation` | float64 | 否 | 0.3 | 灵敏度；`<0` 重置 0.3，`>1` 允许但告警。联动 Profiler 阈值 |
| `--kpi-path` | string | 否* | — | KPI 模式：每节点 CSV + `node_config.json` 的目录 |
| `--kpi-jsonl-dir` | string | 否* | — | KPI 模式：CATMonitor `straggler_kpi_*.jsonl` 目录（优先于 `--kpi-path`） |
| `--space-ratio-threshold` | float64 | 否 | 2.0 | 空间 kmeans 簇比例阈值（独立旋钮） |
| `--comm-slow-ratio` | float64 | 否 | 1.3 | 慢通信带宽劣化阈值（max/min ≥ 该值判劣化，须 >1） |
| `--comm-min-count` | int | 否 | 1000 | 参与带宽统计的最小 op 计数（更小视为延迟主导，不计入） |
| `--debug-output` | bool | 否 | 假 | 结果含全部正常/异常数据便于排查（见 6.1） |

\* `path` 与 KPI 输入至少提供一个；都没有则打印用法并退出。

### 守护进程参数（`--daemon`）

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `--daemon` | bool | 否 | 假 | 进入常驻守护进程模式 |
| `--profiler-dir` | string | 是* | — | dyno 采集落盘根目录（传给 dyno 的 `--log-file`） |
| `--kpi-dir` | string | 否 | — | KPI 数据目录（CATMonitor JSONL）；缺省则每轮只跑 Profiler |
| `--daemon-port` | int | 否 | 8080 | HTTP 端口 |
| `--interval` | int | 否 | 600 | 检测周期（秒，≥60） |
| `--collect-wait` | int | 否 | 60 | dyno 触发成功后的等待秒数 |
| `--profiler-iterations` | int | 否 | 1 | dyno nputrace 每轮采集迭代数 |

### 中心节点参数（`--center`）

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `--center` | bool | 否 | 假 | 进入中心节点模式（管理多个业务/守护进程） |
| `--center-port` | int | 否 | 8080 | 中心节点 HTTP 端口 |
| `--center-data-dir` | string | 否 | center_data | 持久化根目录（业务列表 + 每业务每周期结果） |
| `--center-interval` | int | 否 | 600 | 新业务默认触发周期（秒，≥60） |

> 中心节点模式的 `degradation`（阈值基数）**每个业务独立**，新业务继承节点启动时的初始值，之后可在业务控制台动态调整（见[五、中心节点模式](#五中心节点模式)）。

### 阈值计算

```
KPI 模式:
  SpaceRatioThreshold = --space-ratio-threshold      # 默认 2.0（独立旋钮）

Profiler 模式:
  CalThreshold  = 1 + degradation                    # 慢计算阈值（默认 1.3）
  CPUThreshold  = 1 + degradation × 5                # 慢CPU 阈值（默认 2.5）
  SlowCommRatio = --comm-slow-ratio                  # 慢通信带宽劣化阈值（默认 1.3，独立旋钮）
  SlowCommMinCount = --comm-min-count                # 带宽统计最小 op 计数（默认 1000）
```

> `degradation`（阈值基数）除了启动时用 CLI 设置初始值外，运行期还可通过守护进程/业务控制台或 API 动态调整（见[四、HTTP 接口（守护进程）](#四http-接口守护进程)与[五、中心节点模式](#五中心节点模式)），只影响后续检测，不改写历史结果。

### KPI 内部配置（代码内默认值，非 CLI）

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `AggregationWindowSec` | 10 | 10 秒聚合窗口 |
| `TrimRatio` | 0.25 | 裁剪比例（每端 25%，中间 50%） |
| `MinSamplesForTrim` | 4 | 桶内原始样本 < 4 时降级为普通均值 |
| `SpaceRatioThreshold` | 2.0 | 空间 kmeans 簇比例阈值（CLI `--space-ratio-threshold` 覆盖） |

---

## 八、边界情况

| 场景 | 处理 |
|------|------|
| 空间维度同行点 < 2 卡 | 该节点 score=0（无法 peer 对比） |
| 某节点在场卡 < 2 | 该节点 score=0，其他节点不受影响 |
| ≤0 读数（含真实 0） | 钳制到 `zeroFloor=1e-3` 参与聚类；NaN 排除 |
| 缺失 / NaN 卡 | 该卡该指标 score=0（不参与聚类） |
| 裁尾后数据不足 | 桶内样本 < 4 降级为普通均值；截尾后不足 2 点 → 中位数兜底 |
| 计数器回绕 | 自动加 `MaxUint64` 修正 |
| JSONL 某天文件不存在 | 天然跳过（只读存在的文件） |
| CSV 列不完整 | 缺失列告警但不阻断，对应 metric dict 为空 |
| 仅 KPI 无 `path` | 只输出 KPI 结果（JSON 只有 `kpi` 键） |
| KPI 检测失败 | 只记录告警，不影响 Profiler 检查（互不阻塞） |
| Profiler 检测失败 | 只记录错误，KPI 检查照常执行（互不阻塞） |
| Profiler 单节点 | 慢CPU 无法检测，stdout 不显示该行 |
| Profiler 无并行拓扑 | 降级为仅慢计算（cal-only）检测 |
| `aicore_freq` 轻度降频 | 簇比例未超阈值 → 空间不标记（时间维度已移除，无其他兜底） |

---

## 九、目录结构

```
straggler/
├── main.go                 # 统一入口：CLI 解析、三模式编排（一次性/守护进程/中心节点）、合并 JSON
├── daemon/                 # 守护进程：dynolog/dyno 采集 + 周期检测 + HTTP 查询/控制
│   ├── daemon.go           #   运行循环（周期调度、生命周期、优雅退出）
│   ├── dyno.go             #   dynolog 拉起 + dyno 触发校验 + python analyse 转 .db
│   ├── store.go            #   会话历史 + 周期计数
│   ├── server.go           #   HTTP 路由（/status /straggler/* /daemon/*）
│   └── types.go            #   Config / CycleResult / HTTP 响应类型
├── center/                 # 中心节点：业务管理 + 匹配/心跳 + 合并检测 + Web 控制台
│   ├── center.go           #   Center 结构、持久化、Run 循环
│   ├── manage.go           #   匹配、探测、触发、合并检测、上报接收
│   ├── server.go           #   HTTP 路由（业务 CRUD / op_metric / results / vllm）
│   ├── result.go           #   每业务检测结果目录定义
│   ├── types.go            #   Business / Daemon / Config
│   ├── metrics.go          #   vllm /metrics 抓取（TPOT/TTFT 时序，按 engine）
│   ├── console.go          #   go:embed 控制台页面
│   ├── console.html        #   业务管理 Web 控制台（自包含）
│   ├── business.html       #   业务控制台（自包含）
│   └── chart.umd.min.js    #   本地嵌入的 Chart.js（延迟指标图）
├── README.md               # 本文件
├── go.mod / go.sum         # 独立 Go module（依赖 modernc.org/sqlite）
├── build.sh                # 一键构建：架构/版本检查 + 装 dyno/dynolog + wheel + go build
├── 3rdparty/msmonitor/     # msmonitor 子模块（build.sh 优先从中构建 dynolog/wheel）
├── clustering/             # 共享 kmeans 比例检测算法
│   └── kmeans.go
├── resource/               # KPI 资源指标检测
│   ├── types.go            #   数据结构 & 指标注册表 & 配置
│   ├── parser.go           #   CSV / KPI 目录解析（node 感知全局卡号）
│   ├── json_reader.go      #   CATMonitor straggler_kpi JSONL 读取
│   ├── aggregator.go       #   10 秒聚合（裁剪均值 / 计数器增量）
│   ├── space_detector.go   #   空间维度检测（peer 对比，最后一点）
│   └── report.go           #   管线编排 + 文本报告（stdout）
├── profiling/              # Profiling 检测
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
├── utils/                  # 结果聚合（节点级）+ 工具
├── report/                 # Profiler 文本报告生成
├── DESIGN.md               # Profiling 检测设计
├── DESIGN_NPU_RESOURCE.md  # KPI 资源检测设计
├── SPEC.md                 # 检测技术规范
└── straggler_combination_DESIGN.md  # 与 CATMonitor 底座整合设计
```
> 各包的测试文件（`*_test.go`）未在树中列出。

---

## 十、设计文档

- [DESIGN_NPU_RESOURCE.md](./DESIGN_NPU_RESOURCE.md) — KPI 资源指标检测设计
- [DESIGN.md](./DESIGN.md) — Profiling 检测设计
- [SPEC.md](./SPEC.md) — 检测技术规范
- [straggler_combination_DESIGN.md](./straggler_combination_DESIGN.md) — 与 CATMonitor 底座的整合设计
