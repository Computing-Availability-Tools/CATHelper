# 慢节点检测算法 — 设计文档

## 模块接口

### config
```go
var FilePath string                                  // CLI path= 设置
var CalThreshold float64                             // = 1 + degradation（默认 1.3）
var CommThreshold float64                            // = 1 + degradation × 5（默认 2.5）

type DegradationData map[string]map[string]float64   // 类别 → (key → 劣化分数)
func NewDegradationData() DegradationData
func (d DegradationData) AddSingle(category string, rank int, degradation float64)
func (d DegradationData) AddGroup(category string, ranks []int, degradation float64)
```
- **Key 编码**：单卡 `strconv.Itoa(rank)`（如 `"0"`），组 `sort + strings.Join(ranks, ",")`（如 `"0,2,4"`）
- **AddGroup 去重**：已存在 key 时保留**最大**劣化值

### dataparse（profiling/dataparse）
```go
func DataParsing(folderPath string)                          // 入口：遍历 .db → StartProcess
func StartProcess(dbFiles []string, outDir string) error     // 信号量（cap=4）+ WaitGroup
func ProcessDatabase(dbPath string, outDir string) error     // 单文件完整管线
func GetAllStepTimes(db *sql.DB) ([]StepTime, error)         // 3 级降级
func GetStepTimesFromSTEP_TIME(db *sql.DB) ([]StepTime, error)
func GetStepTimesFromTASK(db *sql.DB) ([]StepTime, error)
func TimeDiffForStep(db, xpToGroupName, stepTime) (PerformanceMetrics, error)
func GetAvgKernelTaskDuration(db *sql.DB, stepTime StepTime) (int, error)
func WriteResultsToCSV(outputFile string, pMS []PerformanceMetrics) error
func queryHostInfo(db *sql.DB) (hostUid, hostName string, err error) // 查询 HOST_INFO
func queryNpuID(db *sql.DB) (int, error)                     // 查询 NPU_INFO.id
func readGroupInfo(db, rankStr, outputDir) (map[string]interface{}, map[string]string, error) // META_DATA → group_info JSON + xpToGroupName
func writeHostInfo(outputDir, rankStr, hostUid, hostName string) // 写入 host_info_{N}.json
func writeNpuInfo(outputDir, rankStr string, npuID int)      // 写入 npu_info_{N}.json
func CalculateMean(values []int) (int, error)
func CalculateMidMeanPair(stats []OpStat) (meanDuration, meanCount int, err error)
```

**ProcessDatabase 执行顺序**：
1. `sql.Open("sqlite", path+"?mode=ro")` + WAL 模式
2. 创建 3 个索引（IF NOT EXISTS，幂等）
3. `extractGlobalRankFromFilename` → rank 字符串
4. `queryHostInfo` → `SELECT hostUid, hostName FROM HOST_INFO LIMIT 1`（识别卡所属物理节点与节点名）
5. `queryNpuID` → `SELECT id FROM NPU_INFO LIMIT 1`（NPU 编号，节点聚合输出用）
6. `readGroupInfo` → META_DATA `parallel_group_info` → group_info JSON（sync.Once 写入）+ xpToGroupName 映射（短名 → STRING_IDS 组名字符串，如 `"tp" → "group_name_3"`）
7. `GetAllStepTimes` → 合并为单 step（minStart → maxEnd）
8. `TimeDiffForStep` → 计算所有指标
9. `WriteResultsToCSV` → 单行 CSV
10. `writeHostInfo` → 写入 `op_metric/host_info_{N}.json`（rank → hostUid/hostName 映射）
11. `writeNpuInfo` → 写入 `op_metric/npu_info_{N}.json`（rank → NPU id）

**Step 时间降级链**：
1. `STEP_TIME` 表 → `SELECT id, startNs, endNs ORDER BY id DESC` → 反转升序
2. `TASK` + `STRING_IDS` + `MSTX_EVENTS` → 正则匹配 `^step\s+\d+$` → 查 connectionId → 查 TASK 时间
3. 哨兵：`{ID: -1, StartNs: math.MinInt, EndNs: math.MaxInt}`

**指标计算（TimeDiffForStep）**：
| 指标 | 计算方式 |
|------|---------|
| ZP_Host | 所有通信算子和 KERNEL_AICORE 的 `HEndNs - HStartNs` 均值（HStartNs > 0 && HEndNs ≥ HStartNs）；空 → -99999 |
| ZP_Bubble | 所有 `OpStartNs - HostEndNs > 0` 的正值均值（HEndNs > 0）；空 → -99999 |
| ZP_Duration | 收集所有通信区间 → `mergeIntervalsSimple` 合并重叠 → 总跨度 |
| ZP_Device | `stepDuration - ZP_Duration`（钳位到 0） |
| ZP_Kernel | `SELECT AVG(endNs - startNs) FROM TASK ... WHERE KERNEL_AICORE` |
| 各域 Duration/Count | 域内算子 → `CalculateMidMeanPair`（去 min/max 后均值） |

**通信组 Duration 契约（{domain}_Duration 列）**：
- COMMUNICATION_OP.groupName 是 STRING_IDS 中组名字符串的 id；`parallel_group_info` 的**顶层 key**（长名，如 `"group_name_3"`）即 STRING_IDS 里的组名字符串，而每项的 `group_name` 字段是**短名**（如 `"tp"`）
- `xpToGroupName` 以短名为键、长名为值；`idToXp` 反向映射为 **STRING_IDS id → 短名**，CSV 的域列以短名命名（`tp_Duration, tp_Count`），与 detector 的域常量一致
- 某域在 STRING_IDS/COMMUNICATION_OP 中无算子 → 该域无列（正常，无数据可测）

**三种数据缺失场景**：
- `xpToGroupName` 为空 → 全部填充 -99999，ZP_Kernel/DataLoader 独立查询
- `groupNameIds` 为空（组名不在 STRING_IDS）→ 通信指标填充 -99999
- `deviceOps` 为空 → 除 ZP_Host 外的指标填充 -99999，ZP_Host 回退用 KERNEL_AICORE Host 耗时

**区间合并（mergeIntervalsSimple）**：按 Start 排序 → 遍历合并重叠区间 → 累加非重叠部分总长。

**并发控制**：
```go
var csvMutex sync.Mutex                    // CSV 写入全局锁
var fileWriteOnce map[string]*sync.Once    // group_info/host_info/npu_info JSON 去重
var fileWriteOnceMu sync.Mutex             // 保护 fileWriteOnce map
```
- DB 并发：`make(chan struct{}, 4)` 信号量
- CSV：全局 Mutex（每 goroutine 写不同文件，但保留锁保安全）
- group_info/host_info/npu_info JSON：`sync.Once` 每文件名（同机卡拓扑/hostUid/NPU id 相同，只需写一次）

### detector（profiling/detector）
```go
func GetCurDetectionInfo(jobPath string) (map[string][][]int, []int)   // parallels + validRanks
func GetCurJobLastStepData(ranks []int) map[string]map[int]float64    // CSV → 单快照
func GetHostUidMapping(jobPath string, ranks []int) map[int]string    // 读取 host_info_*.json
func DelimitDetection(StepData map[string]map[int]float64, parallels map[string][][]int, validRanks []int) config.DegradationData
func GetCalDetectionGroup(parallels map[string][][]int, curNpus []int) (string, [][]int)
func DebugRankScores(stepData map[string]map[int]float64, validRanks []int) map[int]map[string]float64   // --debug-output 用
func DebugCommScores(stepData map[string]map[int]float64, parallels map[string][][]int) map[string]map[string]float64
```

**GetCurDetectionInfo**：遍历 `op_metric/group_info_*.json`，收集所有 rank ID 和域名称（`group_name` 字段，短名），对每个域调用 `getDetectionJobParallelInfo` 提取组，过滤 < 2 卡的组，返回 parallels 映射和排序 validRanks。**无 group_info 文件（组名未注册）时**：回退从 `global_rank_*.csv` 文件名收集 rank（该文件无条件写），返回空 parallels → 主流程降级 cal-only。

**GetCurJobLastStepData**：对每个 rank 读 CSV → `map[列名][]float64` → 取倒数第二行（n > 1 时 n-2）→ 跳过 -99999 → 返回 `map[指标名]map[rank]值`。

**主检测组优先级**：`tp → exp → ep → tp_exp → cp → cp2 → cp_ulysses → cp_ring → dp → dp_cp → dp_modulo_exp_cp`

**并行域去重**：每个 rank 的 `group_info_{N}.json` 都可能列出同一组（如 tp 组 [0..7]），用 `assigned map[int]bool` 追踪本域内已归属的 rank：组内任一 rank 已归属 → 跳过该组。修复前内层 map 从未被填充导致去重失效，8 个 rank 文件会产出 8 个完全相同的组。

## 检测逻辑

#### 慢计算（getSlowCalculateRanks → detCalForOneGroup）
```
对主检测组每个子组：
  1. 检查 ZP_Kernel 可用性（组内所有卡 > 0）
     ✓ → 指标 = ZP_Kernel，方向 = "max"
     ✗ → 指标 = ZP_Duration，方向 = "min"
  2. 收集非零值，要求 >= minRanksInGroup(2)
  3. kmeans 比例检测 → AddSingle("cal", rank, degradation)
```
主检测组无可用并行域（组名未注册/仅未知域如 mc2）时，GetCalDetectionGroup 降级为**全体 rank 一组**（default_group），cal 仍可检测；comm/CPU/Bubble 无数据保持静默。

#### 慢通信（detectionAllCommunicationParallel → HomogenizationForSlowCommunication）
```
对每个非 PP/非 embd 域：
  ppStageNum = len(parallels["pp"][0])（若无 PP 则为 1）
  1. 每个子组内部排序，组间字典序排序
  2. 每组取 {domain}_Duration 最小的卡为代表
  3. detectionCards 按 ppStageNum 均分桶
  4. 每桶内对代表卡做 kmeans 比例检测（方向 "max"）
  5. 异常代表卡通过 rank2Group 映射回完整组
  → AddGroup("comm", fullGroup, degradation)
```
PP=1 时所有代表卡在同一桶，算法天然降级为普通聚类。

#### 慢CPU（getSlowHostRanksByHomogenize）
```
1. 收集所有 validRanks 的 ZP_Host 值
2. smoothByHostUid(values, ranks, rankToHostUid) — 原地修改：
   从 host_info_{N}.json 读取 hostUid 映射（数据解析阶段从 HOST_INFO 表查询）
   相同 hostUid 的卡归为同一物理节点：
     > 2 个 → 去掉节点内 min/max，计算剩余均值
     ≤ 2 个 → 普通均值
   用均值覆盖节点内所有卡的值
   无 hostUid 的卡保持原始值不变
3. kmeans 比例检测（方向 "max"）→ AddSingle("cpu", rank, degradation)
```

注：旧版本硬编码每 4 个连续 rank 为一台机器，现已改为从 profiler 数据库 `HOST_INFO` 表读取实际 `hostUid` 进行分组。若 `HOST_INFO` 表不存在则对应卡跳过预处理。

#### NPU Bubble（detectionZpBubbleData）
```go
for npuID, value := range ZP_Bubble:
    if value > 0 && value < 5000:
        AddSingle("npu_bubble", npuID, value)
```
注：阈值硬编码 `< 5000`；config 中无对应参数（`zpBubbleAbnormalBoundary` 已移除）。

### clustering（共享 kmeans 比例检测）
```go
func HomogenizationComparisonFunc(fileRanks []int, alignedData []float64,
    degradationPercent float64, abnormalType string) ([]int, []float64)
```
`profiling/detector/clustering.go` 只是包装：把参数透传给共享包
`feature/straggler/clustering` 的 `Detect(values, threshold, abnormalType != "min")`，
再把 `Result.Index` 映射回 `fileRanks`、`Result.Ratio` 作为退化值。

**核心流程**（与 KPI 资源检测的空间 cluster 共享同一算法）：
```
1. 过滤值 <= 0；不足 2 个 → 无异常退出
2. Z-score 标准化（std≈0 → 强制 1）
3. 肘部法选 k（K=2..min(n,10)，取 inertia 二阶差分最大）
4. kmeans++ 初始化（首个质心 = data[0]，后续 D² 加权采样，固定种子 kmeansSeed=42）
5. Lloyd 迭代（≤300 轮，空簇处理，收敛 1e-9）
6. 基线簇 = 方向极值簇（"max"→最小均值簇，"min"→最大均值簇）
7. 簇均值比 > threshold → 异常簇
   "max": 簇均值 / 基线均值 > threshold
   "min": 基线均值 / 簇均值 > threshold
8. 对异常簇递归（深度 ≤10）：更深层异常替换父层，更深层无异常保持父层
9. 返回最深异常簇；degradation = 对应簇比例
```

固定种子（kmeansSeed=42）：kmeans++ 采样确定性，同一数据多次运行结果一致。

时间复杂度：kmeans O(n·k·iter)（n ≤ 64，k ≤ 10，iter ≤ 300），递归深度 ≤ 10；空间复杂度 O(n)。

### utils
```go
func BuildNodeResult(finalResult map[string]map[string]float64, parallels map[string][][]int, debug *DebugInfo) (*NodeOutput, error)
func CheckFileOrDirectoryReadMode(path string) bool
func CheckFileOrDirectoryIsSoftLink(path string) bool
func TransferFloatArrayToInt(ids []interface{}) []int
func ReadFile(filePath string) ([]byte, error)
```

**BuildNodeResult 逻辑**（节点聚合输出，不写文件，返回 `NodeOutput` 供 main 合并进 `straggler_output.json` 的 `profiler` 段）：
1. 读 `op_metric/host_info_{N}.json`（hostName）+ `npu_info_{N}.json`（NPU id）作为每 rank 元数据
2. cal / npu_bubble（逐 rank）→ 按 hostname 分组、按 NPU id 聚合 → `node_result[].npu[]`，只含有异常的节点/NPU
3. cpu（逐 rank，节点级）→ `node_result[].cpu`（节点内 rank 值相同，取共享值）
4. comm → 用 `findDomainForRanks` 解析域名 → `comm_domain_result[域名][组key] = score`
5. stdout 逐类摘要（慢计算/慢通信/慢CPU/Bubble，单节点跳过慢CPU）；JSON 由 main.go 合并写入运行目录 `straggler_output.json`（`{"kpi": ..., "profiler": ...}`）。`--debug-output` 时传入 `DebugInfo{ValidRanks, RankScores, CommScores}`，输出全部节点/NPU（含正常）及其诊断分

### report
```go
func WriteReport(stepData, parallels, validRanks, outputDir, detectionResult, inputPath, degradation) string
func GenerateReport(stepData, parallels, validRanks, detectionResult, inputPath, degradation) string
```

**报告章节**：头部 → 并行域拓扑 → 检测摘要表 → ZP_Kernel 柱状图（Top 30 + Bottom 5，跨 rank）→ ZP_Host 节点对比（≥2 物理节点才显示，跨节点聚合）→ 各域通信分组对比（min/mean/max + 柱状图）。通信以通信组为单位比较，不输出逐 rank 的总通信时间排序。

**常量**：柱状图 `█`，最大宽度 40，Top N = 30，Bottom N = 5。

**时间格式化**：`≥1e9` → s，`≥1e6` → ms，`≥1e3` → µs，其余 → ns。

## 数据结构

```go
type StepTime struct {
    ID      int   // Step 编号（合并后 = 0）
    StartNs int   // 开始时间（ns）
    EndNs   int   // 结束时间（ns）
}

type CommunicationOp struct {
    OpStreamIndex int   // COMMUNICATION_OP._rowid_
    OpName        int   // 算子名称（STRING_IDS ID）
    StartNs       int   // 设备侧开始时间
    EndNs         int   // 设备侧结束时间
    HStartNs      int   // Host 侧开始时间（由 CANN_API/MSTX_EVENTS 关联填充）
    HEndNs        int   // Host 侧结束时间
    Count         int
    ConnectionID  int   // 设备-主机关联键
    DomainID      int   // 并行域 ID（COMMUNICATION_OP.groupName，即 STRING_IDS 中组名字符串的 id）
}

type PerformanceMetrics struct {
    HostUid      string         // 物理节点标识（从 HOST_INFO 表读取）
    StepIndex    int            // 合并后 = 0
    StepDuration int            // maxEndNs - minStartNs
    ZPDevice     int            // 非通信时间 = stepDuration - ZP_Duration（钳位到 0）
    ZPDuration   int            // 总通信时间（合并区间后）
    ZPHost       int            // 平均 Host 耗时（-99999 表示缺失）
    ZPBubble     int            // 平均 Bubble 时间（-99999 表示缺失）
    ZPCount      int            // 未使用（恒为 -99999）
    ZPKernel     int            // 平均 KERNEL_AICORE 耗时
    DataLoader   int            // DataLoader 耗时
    Durations    map[string]int // 各域通信耗时（按短名，如 "tp"）
    Counts       map[string]int // 各域通信计数
}

type OpStat struct { Duration, Count int }
type Interval struct { Start, End int }
type HostOp struct  { StartNs, EndNs int }
```

## 关键 SQL

```sql
-- 物理节点标识
SELECT hostUid, hostName FROM HOST_INFO LIMIT 1

-- NPU 编号（节点聚合输出）
SELECT id FROM NPU_INFO LIMIT 1

-- 并行域配置
SELECT value FROM META_DATA WHERE name = 'parallel_group_info'

-- 组名字符串 → STRING_IDS id
SELECT value, id FROM STRING_IDS WHERE value IN (?, ...)

-- 通信算子（step 时间窗口内 + 指定 groupName ID）
SELECT opName, startNs, endNs, connectionId, count, _rowid_, groupName
FROM COMMUNICATION_OP
WHERE groupName IN (?, ...) AND startNs >= ? AND endNs <= ?
ORDER BY startNs ASC

-- Host 时序（批量查 connectionId）
SELECT startNs, endNs, connectionId
FROM {CANN_API|MSTX_EVENTS}
WHERE connectionId IN (?, ...)

-- KERNEL_AICORE connectionId
SELECT t.connectionId FROM TASK t
INNER JOIN STRING_IDS s ON t.taskType = s.id
WHERE s.value IN ('KERNEL_AICORE') AND t.startNs >= ? AND t.endNs <= ? AND t.connectionId > 0

-- 平均 Kernel 耗时
SELECT AVG(t.endNs - t.startNs) FROM TASK t
INNER JOIN STRING_IDS s ON t.taskType = s.id
WHERE s.value IN ('KERNEL_AICORE') AND t.startNs >= ? AND t.endNs <= ?

-- DataLoader
SELECT startNs, endNs FROM MSTX_EVENTS
WHERE message = ? AND startNs >= ? AND endNs <= ? LIMIT 1
```

## 错误处理

**致命（程序终止）**：
- 无 CLI 参数 / 缺 path / path 非目录
- 目录下无 `.db` 文件
- 获取并行域/有效 rank 失败
- 无有效 step data
- 检测返回空

**非致命（跳过/降级，继续执行）**：
| 场景 | 处理 |
|------|------|
| 单个 .db 处理失败 | 日志 + 继续下一个 |
| CSV 为空 | 跳过该 rank |
| group_info JSON 无效 | 跳过该 rank 的拓扑 |
| CANN_API/MSTX_EVENTS 表不存在 | 跳过 Host 时间查询 |
| DataLoader 查询失败 | DataLoader = 0 |
| Kernel 查询无数据 | ZP_Kernel = 0 |
| 通信耗时 > step 总耗时 | ZP_Device 钳位到 0 + 警告 |
| 某域在 STRING_IDS/COMMUNICATION_OP 无算子 | 该域无 Durations 列（不参与慢通信检测） |
| 组名未注册（无并行拓扑） | 降级 cal-only：validRanks 从 global_rank_*.csv 收集，全体 rank 一组检测慢计算；comm/CPU/Bubble 无数据不检测 |

## 日志前缀

`[SLOWNODE ALGO]` 算法通用 | `[DATA PROCESS]` 数据解析 | `[WARN]` 警告 | `[REPORT]` 报告生成 | `[DAEMON]` 守护进程

---

## 守护进程模式（daemon）

### 概述

一次性模式（`path=...` 单次运行后退出）之外，提供常驻守护进程：周期性**触发采集（dyno）-> 转换（python analyse）-> 解析 -> 分析** profiler 数据，并同时**读取 KPI 数据联合检测**，合并结果 JSON 落盘并通过 HTTP 查询；守护进程本身通过 HTTP 控制（启动/暂停/改周期）。

```
┌────────────────────────── daemon 包 ──────────────────────────┐
│                                                               │
│  HTTP Server (net/http)          Runner (goroutine)           │
│  ├ GET  /status                  │  ticker ──> runCycle       │
│  ├ GET  /straggler/results/latest │  ├ dyno 触发采集           │
│  ├ GET  /straggler/results/history│  ├ wait 采集落盘           │
│  ├ GET  /straggler/report/latest  │  ├ python analyse -> .db  │
│  ├ POST /daemon/start             │  ├ StartProcess 解析       │
│  ├ POST /daemon/pause             │  ├ KPI 读取 + 检测         │
│  ├ POST /daemon/interval          │  └ detect + report         │
│  ├ POST /daemon/trigger           │        │                   │
│  └ POST /daemon/stop              │        v                   │
│         │                         │  结果 JSON 落盘            │
│         └──── 控制命令 ───────────>│  （查询接口的数据源）       │
└───────────────────────────────────────────────────────────────┘
```

### CLI 与启动

```bash
# 一次性模式（现状，不变）
go run . path=/data/dir [degradation=0.3] ...

# 守护进程模式（无需 path=，数据目录来自每周期采集）
go run . --daemon \
    --profiler-dir=/home/nf/data \
    --kpi-dir=/home/nf/kpi \
    --interval=600 \
    --collect-wait=60 \
    --profiler-iterations=5 \
    --daemon-port=8080
```

参数说明：

| 参数 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `--profiler-dir` | 是 | — | profiler 采集落盘根目录（即传给 dyno 的 `--log-file`） |
| `--kpi-dir` | 否 | — | KPI 数据目录（CATMonitor JSONL，同 `--kpi-jsonl-dir` 语义；缺省只跑 Profiler） |
| `--interval` | 否 | 600 | 循环周期（秒），默认 600 |
| `--collect-wait` | 否 | 60 | dyno 触发成功后的等待秒数，默认 60 |
| `--profiler-iterations` | 否 | 5 | dyno nputrace 采集迭代数（传给 dyno 的 `--iterations`） |
| `--daemon-port` | 否 | 8080 | HTTP 监听端口 |

> 注意：命令为可直接执行写法（续行 `\` 后不留注释/空格）；参数语义见上表。

`--daemon` 进入常驻模式：从 PATH 解析 dyno/dynolog 并拉起 dynolog、启动 HTTP 服务，随后等待一个 interval 再开始周期循环（首个周期不在启动时立即执行）。`degradation` 等其余参数语义不变；每周期**同时检测 profiler 与 KPI**，二者结果合并为一份 JSON 落盘。

### 采集链路（dynolog / dyno）

profiler 数据由 dynolog（NPU 版）采集；vllm 服务进程需 `export MSMONITOR_USE_DAEMON=1` 接入（该环境变量由**服务侧**设置，守护进程不负责）。守护进程负责完整链路：

```
1. 启动 dynolog（daemon 启动时执行一次，见「dyno / dynolog 安装」）：
   dynolog --enable-ipc-monitor --certs-dir NO_CERTS

2. 发起采集（每周期）：
   dyno --certs-dir NO_CERTS nputrace \
        --start-step -1 --iterations <N> \
        --activities NPU,CPU --profiler-level Level0 \
        --msprof-tx --export-type Db --log-file <profiler-dir>
   # N = 守护进程 CLI 的 --profiler-iterations，默认 5
   # dyno 自身的参数名就是 --log-file；守护进程 CLI 用 --profiler-dir 指同一路径

3. 解析 dyno stdout 中的 JSON（形如 "response = {...}"；stdout 还带前导
   "Security Warning: ..." / "NpuTrace config = ..." 与尾随 "Matched N processes"
   等文本，解析取第一个 `{` 到最后一个 `}` 之间的 JSON 对象）：
   {"activityProfilersBusy":0,
    "activityProfilersTriggered":[2503,124212],
    "commandStatus":"effective",
    "eventProfilersBusy":0,
    "eventProfilersTriggered":[],
    "processesMatched":[2503,124212]}

   commandStatus == "effective"   -> 采集已触发，继续（判定只看这里；
                                      dyno 进程退出码非零不影响，只要 effective 即继续）
   commandStatus == "ineffective" -> 本周期失败
   processesMatched 为空          -> 目标进程未匹配（vllm 未运行或未设
                                      MSMONITOR_USE_DAEMON=1），周期失败并提示

4. 固定等待 --collect-wait（默认 60s），让 --iterations（默认 5，--profiler-iterations 可调）个迭代完成落盘

5. 数据目录 = **整个 --profiler-dir 根目录**（不是某个 rank 子目录）。dyno 在根下
   **每个 rank 写一个 master_<pid>_<ts>_ascend_pt 子目录**，所以根目录就是本轮全部
   数据（含所有 rank），与一次性模式 `path=` 语义一致。analyse / parse / detect
   都对根目录递归操作。

6. 转换为 .db（依赖 PATH 上的 python 已安装 torch_npu）：
   python -c "from torch_npu.profiler.profiler import analyse; \
              analyse(profiler_path='<--profiler-dir>', export_type=['db'])"

7. 对该根目录执行现有检测管线（递归发现所有 rank 的 .db）；结果 JSON 落盘，
   作为查询接口的数据源
```

### dyno / dynolog 安装（build.sh，系统包管理器 .deb）

dyno / dynolog 二进制**不进版本库**，也不随 Go 二进制 embed——由 build.sh 用系统包管理器把 `dynolog_*.deb` 装到系统，二者直接可从 PATH 调用。

**build.sh 构建流程**（aarch64 主机，一次性执行）：

1. **架构检查**：`uname -m` != `aarch64` -> 报错退出
2. **安装 dyno / dynolog**：wget 直接下载
   `https://ascend-package.obs.cn-north-4.myhuaweicloud.com/msmonitor/daily/2026040207/deb/aarch64/dynolog_0.3.2_1.aarch64.deb`
   -> 检测包管理器安装（dpkg 原生安装 .deb；rpm 系需 `alien` 转换；权限不足经 sudo）-> 使 `dyno` / `dynolog` 直接可调用；二者已在 PATH 上则跳过
3. **Python 版本检查**：须为 3.9 / 3.10 / 3.11 / 3.12，否则报错退出
4. **安装 mindstudio_monitor**：wget 下载
   `https://mindstudio-pkg.obs.cn-north-4.myhuaweicloud.com/tag/26.2.0/B025/aarch64/mindstudio_monitor-26.2.0-cp<python>-cp<python>-linux_aarch64.whl`
   （`<python>` 按第 3 步版本映射为 cp39/cp310/cp311/cp312）-> `pip install` -> 清理中间文件
5. **Go 工具链**：需 >= go.mod 版本；缺失/过旧时从阿里云镜像下载（`/usr/local/go`，不可写则 `~/.local/go`）并持久化 PATH
6. **go build**：`CGO_ENABLED=0 go build -o slowNodeDetection .`

**运行期**：Go 编译完全不依赖 dyno/dynolog（已无 embed）；daemon 启动时用 `exec.LookPath("dyno")` / `exec.LookPath("dynolog")` 从 PATH 解析出二者路径，注入 `Config.DynoBin / DynologBin`（解析失败 -> 报错退出，提示先跑 build.sh）：
- spawn dynolog（`--enable-ipc-monitor --certs-dir NO_CERTS`）作为子进程并持有；启动失败（端口/IPC 已被占用）-> 记日志复用现有实例，dyno 经 IPC 复用其连通
- dyno 每周期经 `exec.Command` 调用
- 优雅退出：终止自己拉起的 dynolog

注意：daemon 机器上先跑一次 `build.sh`（或等价地 `dpkg -i dynolog_*.deb`）装好 dyno/dynolog，否则 daemon 启动即报错。

### 检测循环（runCycle）

每周期的数据是 **--profiler-dir 根目录下的全部 rank 子目录**（每个 rank 一个
master_<pid>_<ts>_ascend_pt），周期之间互不共享状态：

```
1. 执行采集链路步骤 2-4：dyno 触发 -> 校验 commandStatus -> 等待落盘
2. python analyse 转 .db：对整个 --profiler-dir 根目录递归 analyse（覆盖所有 rank）
3. walk 根目录发现 ascend_pytorch_profiler_*.db（所有 rank 的 .db）
   为空 -> 周期失败（collect 成功但无 .db = analyse 失败或落盘结构变化，
   错误信息附根目录顶层清单）
4. dataparse.StartProcess(dbFiles, root)   ← 不走 DataParsing
   （DataParsing 零文件时 os.Exit 会杀死 daemon；根目录每周期删了重建，
   也无需增量状态）
5. KPI 检测：读 --kpi-dir 最新数据（resource.RunDetectionFromData，
   与一次性模式 --kpi-jsonl-dir 同源）；目录为空 -> 该维度本轮跳过
6. detectFromParsedData(root, ...)（与一次性模式共用，见下节）
7. 合并结果 JSON（{"kpi": ..., "profiler": ...}）与 daemon_meta.json 直接落盘到
   ./daemon_results/<start>/（--profiler-dir 之外；文本报告拷入其 analysis_result/）
8. 周期结束时删除整个 --profiler-dir（dyno 每次采集自动重建根）——
   profiler 数据每周期清理不堆积；存结果与删数据是两步独立的事，
   删除不依赖结果写入是否成功
```

`config.FilePath` / `CalThreshold` / `CommThreshold` 全局量按周期设置（FilePath 每周期 = --profiler-dir 根目录）。

### 与一次性模式的代码复用（main.go 重构点）

main.go 第 3-8 步抽取为共用函数，一次性模式与 daemon 调用同一实现：

```go
// detectFromParsedData 在 op_metric 中间产物就绪后执行检测阶段
// （原 main.go 步骤 4-8：拓扑 -> step data -> 检测 -> 节点聚合 -> 报告）。
// 解析阶段（步骤 3）不在此函数内：一次性模式调 DataParsing（全量+os.Exit），
// daemon 调 StartProcess（错误返回，不退出进程）。
func detectFromParsedData(inputPath string, degradation float64, debugOutput bool) (*utils.NodeOutput, error)
```

错误处理差异：一次性模式检测失败 -> `os.Exit(1)`；daemon 检测失败 -> 周期错误记入历史，**守护进程继续运行**。

KPI 检测无需重构：一次性模式与 daemon 都走 `resource.RunDetectionFromData`；差别只在输入——一次性读整个目录一次，daemon 每周期重读 `--kpi-dir` 取最新数据，无额外状态。

### 状态机与并发控制

```
状态: running | paused（初始 running）

ticker 触发 ──> if paused: 跳过
             └─> if 上个周期仍在运行: 跳过本 tick（single-flight，不排队）

POST /daemon/pause  -> paused = true（进行中的周期自然跑完，不再调度新周期）
POST /daemon/start  -> paused = false + 重置 ticker（interval 后触发下一周期）
POST /daemon/interval -> 校验 [60, 86400] 秒 -> 更新 interval + 重置 ticker
POST /daemon/trigger -> 立即执行一个周期；若正在运行返回 409
POST /daemon/stop   -> 关闭 stopCh -> Run 走 shutdown（停 HTTP + 等周期结束 + 杀 dynolog + 删除 daemon_results/ 全部落盘结果）
```

- 所有状态变更经同一把 mutex；周期执行本身不持锁（长任务不阻塞 HTTP）
- 优雅退出：SIGINT/SIGTERM -> `http.Server.Shutdown` + 停止 ticker + 等待进行中周期结束（超时 10 分钟）+ 杀掉拉起的 dynolog（保留落盘结果）
- `POST /daemon/stop`：在上述基础上再删除 `daemon_results/`（所有周期 dump_dir 的落盘结果一并清掉）

### HTTP API

路径不带 `/api/v1` 版本前缀（内网诊断工具，按资源直白命名）。JSON 响应，无鉴权（内网工具，绑定地址即边界）。控制接口均为幂等。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/healthz` | 存活检查，恒 200 |
| GET | `/status` | 守护进程状态 |
| GET | `/straggler/results/latest` | 最近周期的落盘结果 JSON |
| GET | `/straggler/results/history?limit=N` | 本次会话全部周期摘要（元数据，倒序；`?limit=N` 可选，限制条数） |
| GET | `/straggler/results/{id}` | 指定周期的落盘结果 JSON |
| GET | `/straggler/report/latest` | 最近周期文本报告（`text/plain`） |
| GET | `/straggler/report/{id}` | 指定周期的文本报告（`text/plain`） |
| POST | `/daemon/start` | 恢复循环 |
| POST | `/daemon/pause` | 暂停循环 |
| POST | `/daemon/stop` | 优雅关闭守护进程（停 HTTP + 等周期结束 + 杀 dynolog + 删除 daemon_results/ 全部落盘结果） |
| POST | `/daemon/interval` | 修改循环周期 |
| POST | `/daemon/trigger` | 立即触发一个周期 |

查询接口以**本次会话的进程内记录为数据源**：`/straggler/results/latest` 与 `/straggler/results/{id}` 返回内存 store 里对应周期的结果 JSON（经 `JSONPath` 指向的 `daemon_results/<start>/straggler_output.json` 读取）；`/straggler/results/history` 列出本次会话全部周期摘要（无条数上限，daemon 重启即空，不读磁盘历史）。

**GET /status 响应**：
```json
{
  "state": "running",
  "interval_sec": 600,
  "profiler_dir": "/home/nf/data",
  "kpi_dir": "/home/nf/kpi",
  "cycles_total": 12,
  "cycles_failed": 1,
  "last_cycle": {
    "id": 12,
    "started_at": "2026-08-18T10:00:00+08:00",
    "finished_at": "2026-08-18T10:00:08+08:00",
    "duration_ms": 8123,
    "dbs": 8,
    "summary": {"profiler": {"cal": 0, "comm": 1, "cpu": 0, "npu_bubble": 0}, "kpi": {"temp": 1}},
    "error": null
  },
  "next_run_at": "2026-08-18T10:10:00+08:00"
}
```

**GET /straggler/results/history 响应**（周期元数据摘要）：
```json
{
  "cycles": [
    {
      "id": 12,
      "started_at": "2026-08-18T10:00:00+08:00",
      "finished_at": "2026-08-18T10:01:30+08:00",
      "dbs": 8,
      "dump_dir": "daemon_results/20260818-100000",
      "summary": {"profiler": {"cal": 0, "comm": 1, "cpu": 0, "npu_bubble": 0}, "kpi": {"temp": 1}},
      "error": null
    }
  ]
}
```

**POST /daemon/interval 请求**：`{"interval_sec": 300}`；越界（<60 或 >86400）返回 400。响应：`{"interval_sec": 300}`。

**周期失败示例**（history 中 error 非 null）：
```json
{ "id": 11, ..., "dbs": 0,
  "error": "dyno commandStatus=ineffective, processesMatched=[]" }
```

### 数据结构

```go
// daemon/types.go
type DaemonConfig struct {
    ProfilerDir string        // profiler 采集落盘根目录（CLI --profiler-dir=，必填；传给 dyno 的 --log-file）
    KpiDir      string        // KPI 数据目录（CLI --kpi-dir=，可选；空 = 每轮只跑 Profiler）
    Interval    time.Duration // 循环周期，默认 600s
    Port        int           // HTTP 端口，默认 8080
    CollectWait time.Duration // dyno 触发成功后的等待秒数，默认 60s
    DynoBin     string        // dyno 可执行路径（build.sh 用 .deb 装到系统，启动时 PATH 解析）
    DynologBin  string        // dynolog 可执行路径（build.sh 用 .deb 装到系统，启动时 PATH 解析）
    Degradation float64       // 阈值参数透传
    DebugOutput bool
}

// CycleResult 单个周期的元数据 + 结果。本周期数据源是独立的 dump 目录，
// 周期之间无跨周期的增量状态，故不需要 daemonState 之类的游标结构。
type CycleResult struct {
    ID         int                   // 自增周期号（进程内从 1 起）
    StartedAt  time.Time
    FinishedAt time.Time
    DurationMs int64
    DBs        int                   // 本周期解析的 .db 数
    DumpDir    string                // 本周期结果落盘目录 daemon_results/<start>/（含结果 JSON / meta / 报告；--profiler-dir 只是临时采集输入，周期结束即删，不作为 dump_dir）
    JSONPath   string                // 本周期结果 JSON 落盘路径（查询接口的数据源）
    KPI        *resource.DetectionResult // KPI 检测结果（nil = 本轮无 KPI 数据）
    Result     *utils.NodeOutput     // profiler 检测结果（nil = 周期失败或无新数据）
    Summary    CycleSummary          // {kpi: {指标->异常卡数}, profiler: {cal=卡/comm=通信组/cpu=节点/npu_bubble=卡}}
    Report     string                // 文本报告（供 /report/latest）
    Error      string                // 空 = 成功
}

// dynoResponse 是 dyno 触发命令响应中内嵌的 JSON 片段（形如 "response = {...}"），
// 据此判断本次采集是否生效。processesMatched 是命中的 vllm 进程 PID（数字），
// 空 = 没有进程接入（未设置 MSMONITOR_USE_DAEMON=1）。
type dynoResponse struct {
    CommandStatus    string `json:"commandStatus"`    // "effective" / "ineffective"
    ProcessesMatched []int  `json:"processesMatched"` // 命中的 vllm 进程 PID
}
```

### 文件布局与产物

```
daemon/
├── daemon.go    // Daemon 结构：生命周期、状态机、runCycle
├── dyno.go      // dynolog 拉起 + dyno 触发采集 + 等待 + python analyse 调用
├── store.go     // 会话历史（mutex 保护，无条数上限）
├── server.go    // HTTP handlers（net/http 标准库，不引第三方框架）
└── types.go     // DaemonConfig / CycleResult / dynoResponse / API 载荷

（dyno / dynolog 由 build.sh 装到系统 PATH，仓库不携带、不 embed）

运行期产物：
├── <kpi-dir>/                # KPI 数据目录（外部 CATMonitor 写入，daemon 只读）
├── <profiler-dir>/           # profiler 采集落盘根目录（--profiler-dir=）
│                             # 整个 profiler-dir 在周期结束后被删除(dyno 采集时重建)
├── daemon_results/<start>/   # 每周期结果直接落盘于此（归档记录；查询走内存 store）
│   ├── straggler_output.json # 本周期结果 JSON（含 kpi + profiler；latest/{id} 经 JSONPath 读）
│   ├── daemon_meta.json      # 周期元数据（归档记录，查询不读）
│   └── analysis_result/detection_report.log  # 文本报告（归档记录，report/latest 走内存）
└── straggler_output.json     # 运行目录副本 = 最近周期结果（与一次性模式输出同构）
```

### 错误处理

| 场景 | 处理 |
|------|------|
| dyno 触发失败（命令执行错误/超时） | 记 error，本周期失败，daemon 存活 |
| dyno commandStatus=ineffective（无 vllm 进程接入） | 记 error（processesMatched 为空），本周期失败，daemon 存活 |
| python analyse 转换失败 | 记 error，本周期失败，daemon 存活（下周期重新触发） |
| dynolog 已被占用（端口/IPC 冲突） | 复用现有实例不重启，dyno 经 IPC 复用其连通 |
| 新 .db 单文件解析失败 | 沿用 StartProcess 语义：日志 + 继续其余文件；全部失败 -> 周期失败 |
| 检测阶段失败（拓扑/step data 为空等） | 周期失败，daemon 存活（对比一次性模式的 os.Exit） |
| 周期超时 | 无强制超时（周期长度 = 解析+检测自然时长）；下个 tick 被 single-flight 跳过 |
| HTTP 请求 body 非法 | 400 + 错误信息 |
| 端口占用 | 启动失败，退出码 1 |

### 设计取舍

- **不引 Web 框架**：接口少且无中间件需求，`net/http` 标准库足够，与项目零额外依赖的风格一致
- **查询以本次会话内存记录为数据源**：`/straggler/results/*` 从进程内无上限 store 读（本次会话全部周期均可查，重启即空，看不到历史）；每周期结果 JSON 落盘到 `daemon_results/<start>/`（`--profiler-dir` 之外）供 latest/{id} 经 `JSONPath` ServeFile 读取，`daemon_meta.json` 仅作归档记录
- **每周期清理 profiler 数据**：周期结束时删除整个 `--profiler-dir`（重量的原始 profiler / .db / 中间产物都在其下），成功与失败都删（防半截数据干扰后续周期定位）——只留 `--profiler-dir` 之外的小结果文件；存结果与删数据相互独立
- **采集走 dynolog/dyno 而非 watch/exec 插件**：vllm 经 dynolog IPC 接入（`MSMONITOR_USE_DAEMON=1` 由服务侧设置，守护进程不管）；工具侧统一以 `dyno nputrace` 触发，不绑定部署侧脚本
- **dyno/dynolog 用系统包管理器安装而非 embed/仓库分发**：仓库不携带第三方制品；build.sh 从 msmonitor 包取 `dynolog_*.deb` 用系统包管理器安装（dpkg 原生 / rpm 系 alien 转换），二者走 PATH 调用。代价是 daemon 运行机器必须先装好（否则启动即报错），换来编译与交付简单：Go 产物与采集工具解耦，任何平台都能出包
- **固定等待而非轮询就绪**：dyno 响应不含落盘路径，采集完成时间无法获知；按联调实测默认等待 60s（`--collect-wait` 可调）
- **无鉴权**：内网诊断工具，控制接口的风险面与绑定地址相同；如需暴露再补 token

