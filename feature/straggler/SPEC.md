# 慢节点检测算法 — 技术规范

基于双入口（KPI 资源指标 / Ascend Profiler Level0），从时间+空间双维度检测 AI 训练集群中性能劣化的 NPU 卡。

---

## 系统概览

```
                    ┌─────────────────────────────┐
                    │       slowNodeDetection      │
                    └─────────────┬───────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                                       ▼
   ┌─────────────────────┐               ┌─────────────────────┐
   │  KPI 资源检测（轻量）│               │ Profiler 深查（重量）│
   │  --kpi-csv 或        │               │  path=/data/dir      │
   │  --kpi-jsonl-dir     │               │  (每卡一个 .db)       │
   └─────────┬───────────┘               └─────────┬───────────┘
             │                                     │
             ▼                                     ▼
   NPU 资源 KPI 异常报告                慢计算/慢通信/慢CPU/Bubble
   (npu_resource_detection_*.json)     (straggler_detection_result.json)
```

- **KPI 模式**：基于 11 个 NPU 资源指标的时序数据，时间+空间双维度交叉验证，轻量快速，适合常态化初筛。有异常时可选触发 Profiler 模式做交叉验证。
- **Profiler 模式**：基于 Ascend PyTorch Profiler Level0 SQLite 数据，从计算/通信/CPU/Bubble 四个维度深入分析单步性能。

**运行策略**：KPI 检测始终优先执行。若 KPI 发现确认异常且有 `path`（Profiler 数据），则继续运行 Profiler 做交叉验证；若 KPI 无异常，降级到 Profiler；若仅 KPI 无 Profiler，KPI 结果即为最终输出。

---

## CLI

```
slowNodeDetection path=/data/dir [degradation=0.3] [--kpi-csv=/path/to/kpi.csv | --kpi-jsonl-dir=/dir] [--faultsub-url=http://host:9101] [--baseline-hours=360] [--detection-hours=1] [--space-cluster-k=3.0]
```

### 参数

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `path` | string | 否* | — | Profiler `.db` 文件目录（*KPI 模式或 Profiler 至少提供一个） |
| `degradation` | float64 | 否 | 0.3 | 灵敏度系数，< 0 重置为 0.3，> 1 允许但警告 |
| `--kpi-csv` | string | 否 | — | KPI 模式：`kpi_collect.sh` 采集的 CSV 文件路径 |
| `--kpi-jsonl-dir` | string | 否 | — | KPI 模式：CATMonitor `straggler_kpi_{date}.jsonl` 目录（优先于 `--kpi-csv`） |
| `--faultsub-url` | string | 否 | — | FaultSub 回调 URL，KPI 发现异常时回传检测结果 |
| `--baseline-hours` | float64 | 否 | 360 | 基线窗口（小时） |
| `--detection-hours` | float64 | 否 | 1 | 检测窗口（小时） |
| `--space-cluster-k` | float64 | 否 | 3.0 | 空间多数簇显著性阈值 k（独立旋钮，不随 degradation 变化） |

### 阈值计算

```
KPI 模式:
  SpaceZThreshold = 1 + degradation
  TimeZThreshold  = 1 + degradation × 0.8

Profiler 模式:
  CalThreshold  = 1 + degradation
  CommThreshold = 1 + degradation × 5
```

---

## 一、KPI 资源检测模式（`--kpi-csv` / `--kpi-jsonl-dir`）

### 1.1 数据流

```
kpi_collect.sh CSV                     CATMonitor JSONL
      │                                      │
      ▼                                      ▼
 ParseCSV()                          ReadKPIFiles()
      │                                      │
      └────────────┬─────────────────────────┘
                   ▼
           TimeSeriesData{Rows, RawRows, CardIDs}
                   │
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
AggregateByMinute  SplitWindows  BuildBaselines
 (1-min trimmed     (baseline +    (Mean/StdDev/
  mean / counter    detection)     Median/Mad/
  delta)                           P50/P95/P99)
     │             │             │
     ▼             ▼             ▼
detectSpaceAnomalies  detectTimeAnomalies  detectTrends
 (peer Z-Score at     (self Z-Score vs     (linear regression
  each time point)     baseline)            on full series)
     │             │             │
     └─────────────┼─────────────┘
                   ▼
           FuseAndSummarize
        (2D cross-validation,
         compute-first ordering)
                   │
                   ▼
           BoundRootCause
        (C1-C10 / N1-N4 rules)
                   │
                   ▼
        CrossCardCorrelation
                   │
                   ▼
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
ExportJSON    WriteReport    EmitToFaultSub
 (JSON)        (text report)   (callback)
```

### 1.2 输入格式

#### CSV 格式（`--kpi-csv`）

由 `kpi_collect.sh` 采集，每分钟一行（~2s 采集频率 → 每分钟约 30 个原始点聚合为一行），列以 JSON dict 编码各卡数值：

```
timestamp,NPU_CARD_POWER,NPU_CARD_TEMP,...,CPU_average
1784547926,"{""0"":1628,""1"":1747}","{""0"":47,""1"":51}",...,"{""cpu1"":""4.26""}"
```

#### JSONL 格式（`--kpi-jsonl-dir`）

由 CATMonitor `stragglerout` 模块写入，按日期分文件 `straggler_kpi_{YYYY-MM-DD}.jsonl`，每行一个采样点：

```json
{"ts":1784547926,"vals":{"0":{"temp":47,"power":1628,"aicore_freq":1800,...},"1":{...}},"cpu_avg":{"cpu1":"4.26"}}
```

`ReadKPIFiles()` 根据 `--baseline-hours` 计算日期范围，读取对应日期的 JSONL 文件并重建 `TimeSeriesData`，与 CSV 路径共享后续全部检测管线。

### 1.3 检测管线（9 步）

#### Step 1: CSV 解析 → `TimeSeriesData`

`ParseCSV()` 按列名映射解析 CSV，每行输出一个 `CSVRow`（各指标以 `map[cardID]float64` 存储）。自动发现所有 card ID。

#### Step 2: 1 分钟聚合

`AggregateByMinute()` 将原始行按分钟分桶（`AggregationWindowSec=60`），每桶产出 1 个聚合行：

| 指标类型 | 聚合方式 | 说明 |
|---------|---------|------|
| 连续型（temp/power/freq/util/hbm_bandwidth_util/hbm_util/tx_bw） | **裁剪均值 (midmean)** | 排序 → trim 两端 25% → 中间 50% 求均值。若样本 < `MinSamplesForTrim`(4) 降级为普通均值 |
| 计数器（error counters / PFC / retry） | **增量 (counter delta)** | `last − first`，处理 64-bit 回绕 |

CPU 取桶内最后一个值。

#### Step 3: 窗口切分

`SplitWindows()` 按时间戳将聚合行分为：

```
基线窗口 = 全部数据 − 最后 DetectionHours
检测窗口 = 最后 DetectionHours
```

#### Step 4: 构建基线

`BuildBaselines()` 对每卡每指标计算统计量：

| 字段 | 含义 | 用途 |
|------|------|------|
| `Mean` / `StdDev` | 经典均值和标准差 | ZScore 指标的时间基线、报告展示 |
| `Median` | 中位数（50th 百分位） | MAD 指标的鲁棒中心 |
| `Mad` | 中位数绝对偏差 | MAD 指标的鲁棒离散度 |
| `P50` / `P95` / `P99` | 百分位数 | 保留，供报告参考 |
| `N` | 样本量 | 不足 2 时基线不可用 |

#### Step 5: 空间维度检测（Peer Comparison）

`detectSpaceAnomalies()` 在检测窗口内逐时间点逐指标计算：

**对每个时间点，取所有卡在该指标上的值作为 peer 组**，按 `SpaceMethod` 判定：

| SpaceMethod | 适用指标 | 机制 | 异常判定 |
|-------------|---------|------|---------|
| `cluster` | temp/power/util/hbm_bandwidth_util/hbm_util/tx_bw | 递归间隙分裂 → 最大簇均值为基线（多数即正常）→ **逐点**单侧 z | 卡被标记的 **mean_z > k**（k=3）|
| `direct` | aicore_freq | 低于 peer 时钟上限 `FreqDownclockGap` | sentinel 999 |
| `absolute` | 4× error counters | > 0 | sentinel 999 |

**cluster（多数簇）机制**（逐时间点）：
1. 递归二分：在最大间隙处切分，两侧都继续，直到子块内无显著间隙（`maxGap ≥ 跨度/2`）——切出完整的簇划分
2. 基线簇 = 成员最多的簇（"谁多谁有理"）；成员数并列时取方向极值簇（DirHigh→最低簇，DirLow→最高簇）；**基线均值 = 多数簇的均值**
3. **基线簇成员豁免**（它们是正常参照本身）；对每个**非基线簇成员**单侧判定（只查异常方向）：`z = |该卡值 − 基线均值| / scale[指标]`
   - `scale[指标]` 从历史基线自我标定：各卡 `1.4826 × baseline.Mad` 的中位数
   - `z > k`（`SpaceClusterK`，默认 3.0）→ 该卡标记
4. 记录每卡每时间点的 z（被标记卡的 z，其余 0）

> 逐点判定相对簇均值判定的优势：异常簇内每张卡按自己的偏离幅度单独评分（严重度精确到卡）。基线成员豁免保住"散布舰队不误报"：无主导间隙的舰队是单簇 → 全员在基线内 → 无人被评分，正常散布的边缘卡不会被误标。

`aggregateSpaceScores()` 汇总：cluster 方法对每卡求 `mean_z`（= 占比 × 平均偏离幅度，持续与幅度互补），`mean_z > k` 判空间异常；absolute/direct 方法取异常占比。

#### Step 6: 时间维度检测（Self Comparison）

`detectTimeAnomalies()` 将检测窗口值与历史基线比较：

按 `TimeMethod` 分两套公式：

| TimeMethod | 适用指标 | 公式 | 零离散度处理 |
|------------|---------|------|-------------|
| **`mad`** | temp / power / aicore_freq / aicore_util / hbm_bandwidth_util | `Z = |currentMedian − baseline.Median| / (1.4826 × baseline.Mad)` | `Mad==0` 且 |Δ|>0.01 → 999 |
| `zscore` | hbm_util / tx_bandwidth / 4× error counters | `Z = |currentMean − baseline.Mean| / baseline.StdDev` | `StdDev==0` 且 |Δ|>0.01 → 999 |

**MAD 鲁棒性原理**：Median 和 MAD 崩溃点为 50%。只要基线窗口内正常数据占多数，少数异常时段不会带偏统计量。`1.4826 = 1/0.6745` 使 MAD 标定至 σ 尺度，阈值语义不变。

**基线防污染前提假设**：基线中大部分数据（>50%）为正常数据。若异常过半，基线已失参考意义，应缩短基线窗口或手动剔除故障时段。

`aggregateTimeScores()` 汇总：MAD 指标报告值为 median / `1.4826×MAD`（与 Z-Score 公式一致），ZScore 指标保持 mean/std。

#### Step 7: 融合与排序

`FuseAndSummarize()` 将空间和时间结果合并，**每指标独立判定象限**：

```
              Space正常     Space异常
Time正常      normal        individual_variance
Time异常    early_degradation  confirmed_anomaly
```

**Compute-First 排序**：
1. 先查计算类指标（temp/power/freq/util/hbm_bandwidth_util/hbm_util）
2. 若计算异常 → category=compute，通信异常标记为 secondary（可能由计算慢导致）
3. 若计算干净 → 查通信类指标 → category=communication
4. 整体象限取所有异常指标中最严重的
5. 复合评分 = `α × mean(TimeZ) + β × mean(SpaceZ)`（α=0.6, β=0.4）

#### Step 8: 根因定界

`BoundRootCause()` 基于异常指标模式匹配规则：

**计算类规则（C1-C10）**：

| 规则 | 指标组合 | 根因 | 置信度 |
|------|---------|------|--------|
| C1 | TEMP↑ + FREQ↓ | thermal_throttle（热降频） | 高 |
| C2 | TEMP↑ + POWER↑ + FREQ 正常 | cooling_insufficient（散热不足） | 高 |
| C3 | FREQ↓ + TEMP 正常 | forced_downclock（强制降频） | 中 |
| C4 | POWER↓ + UTIL↓ + HBM_BANDWIDTH_UTIL↓ | straggler（卡空闲等待） | 高 |
| C5 | UTIL↓ + HBM_BANDWIDTH_UTIL 正常 | load_imbalance（负载不均） | 中 |
| C6 | HBM_BANDWIDTH_UTIL↓ + UTIL 正常 | memory_bottleneck（内存带宽瓶颈） | 低 |
| C7 | TEMP↑ + POWER 正常 + FREQ 正常 | temp_sensor_fault（传感器漂移） | 中 |
| C8 | ≥4 个计算指标异常 | hardware_fault（硬件故障） | 高 |
| C9 | 仅 TEMP↑ | temp_sensor_fault（局部热点） | 低 |
| C10 | 仅 POWER↑（TEMP 正常） | unknown（功率计量偏差） | 低 |

> `hbm_util`（HBM 内存使用率）参与采集/聚合/双维检测/报告展示，但**不参与任何根因规则匹配**。其单独异常时无规则命中，回退为 `unknown` 供人工分析；但异常计数计入 C8 的多指标综合判定。

**通信类规则（N1-N4）**：

| 规则 | 指标组合 | 根因 | 置信度 |
|------|---------|------|--------|
| N1 | ERR_PKT↑ | network_link_issue（物理链路故障） | 高 |
| N2 | PFC_PKT↑ | network_congestion（PFC 风暴） | 高 |
| N3 | OUT_OF_ORDER↑ + RETRY↑ | network_packet_loss（丢包乱序） | 高 |
| N4 | TX_BW↓ + UTIL 正常 | bandwidth_limited（带宽受限） | 中 |

规则按顺序匹配，命中即返回。未命中则 fallback 为 "unknown"。

#### Step 9: 跨卡关联

`CrossCardCorrelation()` 判断异常卡之间的关联：

| 模式 | 条件 | 含义 |
|------|------|------|
| job_level | 所有卡均异常 | 任务级故障（hang/环境问题） |
| card_level | 仅 1 卡异常 | 板卡级故障 |
| card_level | 2-99% 卡异常 | 逐卡排查 |

### 1.4 输出

| 文件 | 位置 | 内容 |
|------|------|------|
| `npu_resource_detection_result.json` | `path/` 或当前目录 | JSON 检测结果 |
| `npu_resource_detection_report.log` | `path/analysis_result/` | 文本报告 |
| stdout | — | 文本报告内容 |
| FaultSub | `--faultsub-url` | 异常卡事件回传 |

### 1.5 NPU 资源指标

| 指标名 | 分类 | 异常方向 | SpaceMethod | TimeMethod | 说明 |
|--------|------|---------|-------------|------------|------|
| `temp` | 计算 | ↑ 偏高 | cluster | **mad** | NPU 温度 (°C)，对称连续 |
| `power` | 计算 | ↑ 偏高 | cluster | **mad** | NPU 功耗 (W)，对称连续 |
| `aicore_freq` | 计算 | ↓ 偏低 | direct | **mad** | AI Core 频率 (MHz)，单点/对称 |
| `aicore_util` | 计算 | ↓ 偏低 | cluster | **mad** | AI Core 利用率 (%)，双峰（80%+ 工作态） |
| `hbm_bandwidth_util` | 计算 | ↓ 偏低 | cluster | **mad** | HBM 带宽使用率 (%)，双峰 |
| `hbm_util` | 计算 | ↓ 偏低 | cluster | zscore | HBM 内存使用率 (%)，仅跟踪不参与规则 |
| `tx_bandwidth` | 通信 | ↓ 偏低 | cluster | zscore | TX 带宽，近似连续 |
| `rx_pfc_pkt` | 通信 | ↑ 偏高 | absolute | zscore | PFC 暂停帧（累积计数器） |
| `roce_tx_err_pkt` | 通信 | ↑ 偏高 | absolute | zscore | RoCE 发送错误包（累积计数器） |
| `roce_out_of_order` | 通信 | ↑ 偏高 | absolute | zscore | RoCE 乱序包（累积计数器） |
| `roce_new_pkt_rty` | 通信 | ↑ 偏高 | absolute | zscore | RoCE 重传包（累积计数器） |

### 1.6 边界情况

| 场景 | 处理 |
|------|------|
| 基线数据不足（N<2） | 时间维度 Z=0，不判定异常 |
| 检测窗口无数据 | `RunDetection` 返回错误 |
| 空间维度同行点 < 2 卡 | Z=0（无法做 peer comparison） |
| MAD=0（历史值恒定）且当前有偏差 | sentinel 999 → 判定异常 |
| 裁尾后数据不足 | 降级为中位数 |
| 计数器回绕 | 自动加 `MaxUint64` 修正 |
| JSONL 某天文件不存在 | 跳过该天（非错误） |
| CSV 列不完整 | 缺失列 warn 但不阻断，对应 metric dict 为空 |
| 仅 `--kpi-csv` 无 `path` | 仅输出 KPI 结果，不执行 Profiler |

### 1.7 配置默认值

```go
AggregationWindowSec: 60      // 1 分钟聚合
TrimRatio:            0.25    // 裁剪比例（每端 25%，中间 50%）
MinSamplesForTrim:    4       // 低于此样本数降级为普通均值
BaselineHours:        360     // 基线窗口（可通过 CLI 覆盖）
DetectionHours:       1       // 检测窗口（可通过 CLI 覆盖）
SpaceClusterK:        3.0                     // 空间多数簇显著性阈值 k（独立旋钮，--space-cluster-k 覆盖，默认 3.0）
SpaceZThreshold:      1 + degradation         // 空间 Z 阈值（保留，zscore 备用）
TimeZThreshold:       1 + degradation × 0.8   // 时间 Z 阈值
TimeWeight:           0.6     // 融合时间权重 α
SpaceWeight:          0.4     // 融合空间权重 β
EnableTrend:          true    // 启用趋势检测
TrendMinRSquared:     0.6     // 趋势最小 R²
```

---

## 二、Profiler 深查模式（`path=/data/dir`）

### 2.1 数据流

```
ascend_pytorch_profiler_{N}.db （每个设备一个）
  │
  ▼
[profiling/dataparse] SQLite 解析
  ├── 读取 META_DATA → parallel_group_info（JSON）→ op_metric/group_info_{N}.json
  ├── 合并所有 step 时间范围为单个聚合 step
  ├── 查询通信算子、Host 时间、Kernel 时间等指标
  └── 输出 op_metric/global_rank_{N}.csv （单行数据）
  │
  ▼
[profiling/detector] 检测引擎
  ├── GetCurDetectionInfo()    → 并行域拓扑 + 有效 rank 列表
  ├── GetCurJobLastStepData()  → 单次快照数据映射
  └── DelimitDetection()       → 执行 4 类检测
  │
  ▼
[utils]  Write_result()       → stdout + straggler_detection_result.json
[report] WriteReport()        → analysis_result/detection_report.log
```

### 2.2 输入目录结构

```
<path>/
  ├── ascend_pytorch_profiler_0.db
  ├── ascend_pytorch_profiler_1.db
  └── ascend_pytorch_profiler_N.db
```

### 2.3 中间产物（op_metric/）

| 文件 | 格式 | 内容 |
|------|------|------|
| `global_rank_{N}.csv` | CSV，单行 | 设备 N 的性能指标 |
| `group_info_{N}.json` | JSON | 并行域拓扑（sync.Once 去重） |
| `host_info_{N}.json` | JSON | 物理节点 hostUid（sync.Once 去重，同机多卡相同） |

### 2.4 CSV 列说明

| 列 | 含义 |
|------|------|
| `StepIndex` | 合并后 step ID（始终为 0） |
| `StepDuration` | 聚合 step 总时长（ns） |
| `ZP_Device` | step 内非通信时间 = stepDuration − 合并后通信总跨度 |
| `ZP_Duration` | 总通信时间（合并重叠区间） |
| `ZP_Host` | 平均 Host 耗时（通信算子 + KERNEL_AICORE 的 Host 端耗时均值） |
| `ZP_Bubble` | 平均 Bubble 时间（OpStartNs − HostEndNs 的正值均值） |
| `ZP_Kernel` | 平均 KERNEL_AICORE 任务耗时 |
| `DataLoader` | MSTX_EVENTS 中 DataLoader 耗时 |
| `{domain}_Duration` | 该并行域内通信算子平均耗时 |
| `{domain}_Count` | 该并行域内通信算子平均计数 |

### 2.5 检测类型

| 类别 | 标签 | 指标 | 方向 | 阈值 | 结果粒度 |
|------|------|------|------|------|---------|
| 慢计算 | `cal` | ZP_Kernel（优先）/ ZP_Duration（降级） | max / min | CalThreshold | 单卡 |
| 慢通信 | `comm` | `{domain}_Duration`（各域独立） | max | CommThreshold | 卡组 |
| 慢CPU | `cpu` | ZP_Host（按 hostUid 截尾均值预处理） | max | CalThreshold | 单卡 |
| NPU Bubble | `npu_bubble` | ZP_Bubble | < 5000ns | 固定 | 单卡 |

#### 检测方法

**慢计算**：对主检测组内每组卡，优先使用 ZP_Kernel（方向 max，值大 = 计算慢）；若组内有卡缺少 ZP_Kernel 则降级为 ZP_Duration（方向 min，值小 = 计算慢导致通信时间短）。

**慢通信**：对每个非 PP/非 embd 并行域，每组取通信时间最小的卡为代表，按 PP stage 分桶后均质化聚类，异常代表映射回完整组。

**慢CPU**：从每张卡的 `.db` 文件读取 `HOST_INFO.hostUid`，将相同 hostUid 的卡视为同一物理节点。每组节点内计算截尾均值（去 min/max 后平均其余值），覆盖原始值后均质化聚类，消除节点内差异暴露节点间差异。旧版 profiler 缺少 HOST_INFO 表时对应卡跳过预处理，保留原始 ZP_Host 参与聚类。

**NPU Bubble**：固定阈值 `< 5000 ns`（5µs），直接判定。

### 2.6 输出

#### straggler_detection_result.json

```json
{
  "cal": [
    {"display_key": "0", "metric_value": 1.5, "is_abnormal": true}
  ],
  "comm": [
    {"display_key": "tp[0, 1, 2, 3]", "metric_value": 3.2, "is_abnormal": true}
  ],
  "cpu": [
    {"display_key": "2", "metric_value": 1.4, "is_abnormal": true}
  ],
  "npu_bubble": [
    {"display_key": "3", "metric_value": 3200.0, "is_abnormal": true}
  ]
}
```

排序：`npu_bubble` 升序（越小越异常），其余降序（越大越异常）。
display_key：`comm` 为 `域名[排序后的 rank 列表]`，其余为 rank 字符串。

#### detection_report.log

带柱状图（`█`，最大 40 字符宽度）的可读文本报告，包含：
- 数据目录、时间、有效 rank 数
- 并行域拓扑摘要
- 四类检测结果表格
- ZP_Kernel / ZP_Host 排序柱状图（Top 30 + Bottom 5）
- 各通信域分组对比（min/mean/max）
- 时间自动单位转换（s / ms / µs / ns）

### 2.7 均质化聚类算法

唯一的异常检测算法，通过方向和阈值参数化适配所有检测场景。

**核心流程**：
1. 按值升序排序（保留原始索引）
2. 计算相邻差值，找最大间隙位置
3. 条件 1：`maxDiff ≥ sum(allDiff) / 2`（最大间隙至少占总跨度一半）
4. 条件 2：`bigMean / littleMean ≥ threshold`（两组均值比达阈值）
5. 按方向取异常组（"max"→大值组, "min"→小值组）
6. 对异常组递归执行，直到无法再分割

**示例**：数据 `[10, 10, 20, 10]`，阈值 1.3，方向 "max"
- 排序：`[(0,10), (1,10), (3,10), (2,20)]`，最大差值 10（位置 2）
- `10 ≥ 10/2 ✓`，`20/10 = 2.0 ≥ 1.3 ✓`
- 返回索引 2（异常），劣化 = 20/10 = 2.0

### 2.8 SQLite 源表

| 表 | 关键列 | 用途 |
|------|---------|------|
| `META_DATA` | `name, value` | 存储 `parallel_group_info` JSON |
| `STRING_IDS` | `id, value` | 名称 ↔ ID 映射 |
| `STEP_TIME` | `id, startNs, endNs` | Step 时间戳（降级链第一级） |
| `COMMUNICATION_OP` | `opName, startNs, endNs, connectionId, count, groupName` | 设备级通信算子 |
| `CANN_API` | `startNs, endNs, connectionId` | Host API 调用时序 |
| `MSTX_EVENTS` | `startNs, endNs, connectionId, message` | Host 事件（DataLoader、Step 标记） |
| `TASK` | `startNs, endNs, taskType, connectionId` | 任务执行（KERNEL_AICORE） |
| `HOST_INFO` | `hostUid` | 卡所属物理节点标识（慢 CPU 分组依据） |

运行时创建索引：`idx_string_ids_value`, `idx_device_op_time`, `idx_task_time_type`

### 2.9 并行域名称

`tp`, `dp_cp`, `dp`, `cp`, `exp`（Expert Parallel，非 "ep"）, `tp_exp`, `pp`, `cp_ring`, `cp_ulysses`, `default_group`

主检测组优先级：`tp → exp → ep → tp_exp → cp → cp2 → cp_ulysses → cp_ring → dp → dp_cp → dp_modulo_exp_cp`

### 2.10 边界情况（Profiler）

| 场景 | 处理 |
|------|------|
| 无 .db 文件 | `log.Fatalf` 退出 |
| ZP_Kernel 数据不全 | 慢计算降级为 ZP_Duration + 方向 "min" |
| 通信算子缺失 | 除 ZP_Host 外所有指标填充 -99999；ZP_Host 回退用 KERNEL_AICORE Host 耗时 |
| 通信耗时 > step 总耗时 | ZP_Device 钳位到 0 |
| 组内有效卡 < 2 | 跳过该组检测 |
| PP = 1（无流水线并行） | ppStageNum=1，所有代表卡放同一桶聚类 |
| 跨节点拓扑 | getDetectionGroups 通过 nodeGlobalRank 集合过滤 |
| group_info 写入竞态 | sync.Once 保证每个文件名只写一次 |
| HOST_INFO 表缺失 | queryHostUid 返回空串，对应卡跳过 hostUid 预处理 |
| DataLoader 不存在 | DataLoader = 0 |
| Kernel 查询无数据 | ZP_Kernel = 0 |

---

## 包结构

| 包 | 文件数 | 职责 |
|------|--------|------|
| `main` | 1 | CLI 参数解析、双模式编排（KPI → Profiler 降级链） |
| `resource` | 14 | KPI 检测引擎：解析 → 聚合 → 基线 → 空间检测 → 时间检测 → 融合 → 根因 → 关联 → 报告 → JSON 导出 → FaultSub 推送 |
| `config` | 1 | Profiler 全局配置（FilePath、阈值）、DegradationData 结果聚合 |
| `profiling/dataparse` | 3 | SQLite `.db` 解析 → CSV + JSON 中间文件（含 host_info） |
| `profiling/detector` | 4 | 并行域拓扑解析、单步快照、四类检测逻辑 |
| `profiling/spacedetector` | 1 | 均质化聚类算法（Profiler 统一异常检测器） |
| `utils` | 1 | Profiler 结果写入（stdout + JSON 文件） |
| `report` | 1 | Profiler 文本报告生成 |

---

## 关键设计决策

- **双模式分离**：KPI（资源指标时序）和 Profiler（单步快照）是完全不同的检测范式和管线，在 `main.go` 中分支，`resource/` 和 `profiling/` 各自独立。
- **KPI: 时间+空间 2D 交叉验证**：空间维度（同时间点 peer 对比）和时间维度（同卡 vs 历史基线）独立检测后融合，减少单维度误报。仅双维确认的异常才打出 `confirmed_anomaly`。
- **KPI: Compute-First 排序**：计算慢必然导致通信慢（卡无法按时参与集合通信），先判定计算再审视通信，避免将计算慢的卡误归因为通信故障。
- **KPI: MAD 鲁棒基线**：5 个连续/双峰指标使用 median + MAD 构造 Z-Score，崩溃点 50%，防止基线窗口中的少数异常数据污染统计量。
- **KPI: 裁剪均值聚合**：原始数据 ~2s 采集，每分钟聚合时使用 25% 裁剪均值，抵抗采集噪声（温度/功耗传感器的瞬时抖动）。
- **KPI: HBM 双指标并存**：`hbm_bandwidth_util`（带宽，参与 C4/C5/C6 根因规则）+ `hbm_util`（内存，仅跟踪展示，不参与规则），语义上带宽更贴合性能瓶颈判断，但内存使用率仍有采集跟踪价值。
- **Profiler: 合并 Step**：所有 step 合并为单聚合 step（minStart → maxEnd），CSV 仅一行。Profiler 时间分辨率低，逐 step 不可靠。
- **Profiler: 倒数第二行**：多行数据取 n-2 行，避免末行不完整。
- **-99999 哨兵**（Profiler）：统一无效数据标记，在 GetCurJobLastStepData、detectionZpBubbleData、report.filterValid 中跳过。
- **Profiler: 单一算法**：均质化聚类是唯一的异常检测器，所有场景通用。
- **Profiler: 不做时序分析**：仅处理单次快照，不进行趋势/移动平均/变点检测。

---

## 构建

```bash
# Linux ARM64（目标平台）
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o slowNodeDetection .

# Linux AMD64
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o slownode_linux_amd64 .

# Windows AMD64
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -o slownode_win_amd64.exe .
```

全静态二进制，SQLite 驱动使用 `modernc.org/sqlite`（纯 Go 实现，无需 CGO）。
