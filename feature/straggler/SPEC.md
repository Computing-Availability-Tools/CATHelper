# 慢节点检测算法 — 技术规范

基于双入口（KPI 资源指标 / Ascend Profiler Level0），从空间维度（KPI 的 peer 对比）检测 AI 训练集群中性能劣化的 NPU 卡。

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
   │  --kpi-path 或        │               │  path=/data/dir      │
   │  --kpi-jsonl-dir     │               │  (每卡一个 .db)       │
   └─────────┬───────────┘               └─────────┬───────────┘
             │                                     │
             ▼                                     ▼
   NPU 资源 KPI 异常报告                慢计算/慢通信/慢CPU/Bubble
        │                                       │
        └──────────────────┬────────────────────┘
                           ▼
            合并输出 straggler_output.json
            {"kpi": ..., "profiler": ...}（运行目录，缺哪个维度就无哪个键）
```

- **KPI 模式**：基于 11 个 NPU 资源指标，空间维度 peer 对比（最后一个聚合点），轻量快速，适合常态化初筛。
- **Profiler 模式**：基于 Ascend PyTorch Profiler Level0 SQLite 数据，从计算/通信/CPU/Bubble 四个维度深入分析单步性能。

**运行策略**：KPI 检测优先执行。有 `path`（Profiler 数据）时：KPI 发现异常 → 继续跑 Profiler 交叉验证；KPI 无异常 → 降级到 Profiler。KPI 失败（有 `path`）→ 告警后仍执行 Profiler。仅 KPI 无 `path` → KPI 结果即为最终输出。

- **守护进程模式**：`--daemon` 常驻运行，周期性自动完成「触发采集 → 转换 → 解析 → 分析」，结果/控制走 HTTP；与一次性模式共用同一检测管线（见[第三章](#三守护进程模式daemon)）。

---

## CLI

```
slowNodeDetection path=/data/dir [degradation=0.3] [--kpi-path=/dir/of/kpi_csvs | --kpi-jsonl-dir=/dir] [--space-ratio-threshold=2.0] [--debug-output]
```

### 参数

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `path` | string | 否* | — | Profiler `.db` 文件目录（*KPI 模式或 Profiler 至少提供一个） |
| `degradation` | float64 | 否 | 0.3 | 灵敏度系数，< 0 重置为 0.3，> 1 允许但警告 |
| `--kpi-path` | string | 否 | — | KPI 模式：包含多个每节点 CSV + `node_config.json` 的目录 |
| `--kpi-jsonl-dir` | string | 否 | — | KPI 模式：CATMonitor `straggler_kpi_{date}.jsonl` 目录（优先于 `--kpi-path`） |
| `--space-ratio-threshold` | float64 | 否 | 2.0 | 空间 kmeans 簇比例阈值（簇均值/基线均值，独立旋钮，不随 degradation 变化） |
| `--debug-output` | bool | 否 | false | 全量输出：KPI 全部指标×全部卡（含正常的）；Profiler 全部节点/全部通信组（含正常的） |

### 守护进程模式（`--daemon`）

```
slowNodeDetection --daemon --profiler-dir=/dir [--kpi-dir=/dir] \
    [--daemon-port=8080] [--interval=600] [--collect-wait=60] \
    [--profiler-iterations=5] [degradation=0.3] [--debug-output]
```

| 参数 | 类型 | 必需 | 默认 | 说明 |
|------|------|------|------|------|
| `--daemon` | bool | 是（本模式） | — | 进入守护进程模式 |
| `--profiler-dir` | string | 是 | — | 采集落盘根目录（传给 dyno 的 `--log-file`） |
| `--kpi-dir` | string | 否 | — | KPI 数据目录（CATMonitor JSONL）；缺省则每轮只跑 Profiler 检测 |
| `--daemon-port` | int | 否 | 8080 | HTTP 端口 |
| `--interval` | int | 否 | 600 | 周期（秒，≥60，非法回退默认） |
| `--collect-wait` | int | 否 | 60 | 触发后等待采集完成的秒数 |
| `--profiler-iterations` | int | 否 | 5 | dyno nputrace 采集迭代数（传给 dyno 的 `--iterations`） |

`--daemon` 未提供 `--profiler-dir` → 打印用法并退出（`--kpi-dir` 可选，缺省只跑 Profiler；见[第三章](#三守护进程模式daemon)）。

### 阈值计算

```
KPI 模式:
  SpaceRatioThreshold = --space-ratio-threshold   # 默认 2.0（独立旋钮）

Profiler 模式:
  CalThreshold  = 1 + degradation                 # 慢计算/慢CPU（默认 1.3）
  CommThreshold = 1 + degradation × 5             # 慢通信（默认 2.5）
```

---

## 一、KPI 资源检测模式（`--kpi-path` / `--kpi-jsonl-dir`）

### 1.1 数据流

```
kpi_collect.sh CSV                     CATMonitor JSONL
      │                                      │
      ▼                                      ▼
 ParseCSV()                          ReadKPIFiles()
      │                                      │
      └────────────┬─────────────────────────┘
                   ▼
           TimeSeriesData{Rows, RawRows, CardIDs,
                          NodeOf, LocalID}
                   │
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
AggregateByMinute
 (10s trimmed mean / counter delta)
     │
     ▼
detectSpaceAnomalies
 (peer comparison at the last
  aggregated point)
     │
     ▼
aggregateScores → buildAnomalyMetrics
        (metric-first grouping:
         metric → anomalous cards + space score)
                   │
                   ▼
    ┌──────────────┼─────────────┐
    ▼              ▼
合并输出JSON   WriteReport
 (straggler_    (stdout text
  output.json)   report)
```

### 1.2 输入格式

card ID 在**每个节点内从 0 开始**编号，身份 = (node, cardID)。节点信息由**目录 + `node_config.json`** 指定。

#### CSV 格式（`--kpi-path` = 目录）

`--kpi-path` 传一个**目录**，内含多个每节点 CSV（平铺 `{cardID: value}`）+ 固定的 `node_config.json`：

```
/dir/
  node_config.json
  node1.csv      # 节点 node-1 的数据（平铺）
  node2.csv      # 节点 node-2 的数据
```

**node_config.json**（按 CSV 文件名 keyed，指定每个文件的节点名和生效的卡）：
```json
{
  "node1.csv": { "node": "node-1", "cards": [0, 1, 2, 3] },
  "node2.csv": { "node": "node-2", "cards": [0, 1, 2, 3] }
}
```

每个 CSV 列以平铺 JSON dict 编码各卡数值：
```
timestamp,NPU_CARD_POWER,NPU_CARD_TEMP,...,CPU_average
1784547926,"{""0"":1628,""1"":1747}","{""0"":47,""1"":51}",...,"{""cpu1"":""4.26""}"
```

`cards` 指定该节点实际使用的卡（CSV 里其他卡被过滤掉）。`ParseKPIDir` 合并所有 CSV 成一个 `TimeSeriesData`，用 `cardIndexer` 分配全局 ID + NodeOf/LocalID。基本校验：每个 CSV 都有配置项、配置引用的 CSV 存在、配置的卡在 CSV 中有数据（缺失 warn）。

> 注：单文件 `ParseCSV`（支持平铺/嵌套 JSON 单元格，平铺 → 节点 `"none"`）保留为内部/测试用，CLI 主路径走目录方式。

#### JSONL 格式（`--kpi-jsonl-dir`）

由 CATMonitor `stragglerout` 模块写入，按日期分文件 `straggler_kpi_{YYYY-MM-DD}.jsonl`，每行一个采样点：

```json
{"ts":1784547926,"vals":{"0":{"temp":47,"power":1628,"aicore_freq":1800,...},"1":{...}},"cpu_avg":{"cpu1":"4.26"}}
```
- `vals` 始终是**平铺**形态 `{cardID: {field: value}}`，卡号在**节点内**从 0 编号；`cpu_avg` 可选。
- **多节点用二级目录 + `node_config.json`**（key = 子目录名；`node` = 节点名；`cards` = 该节点生效卡号，节点内 0 起始），与 `--kpi-path` 一致：
  ```
  {dir}/
  ├── node-a/straggler_kpi_2026-08-13.jsonl   # 每个文件仍是平铺
  ├── node-b/straggler_kpi_2026-08-13.jsonl
  └── node_config.json
  ```
- 无 `node_config.json` 时按单目录读取（旧版兜底）：`vals` 平铺为单节点 `"none"`，或样本内 `vals` 外层为节点名的**嵌套**形态 `{"node-ip-1": {"0": {...}}, "node-ip-2": {...}}`（`sampleToRow` 嗅探第一个字段值是否为对象来区分）。

`ReadKPIFiles()` 读取目录内全部 `straggler_kpi_{date}.jsonl` 文件并重建 `TimeSeriesData`（整个历史都读入，无时间范围窗口；某天文件缺失天然跳过），与 CSV 路径共享后续全部检测管线。

### 1.3 检测管线（4 步）

#### Step 1: CSV/JSONL 解析 → `TimeSeriesData`

`ParseCSV()` / `ReadKPIFiles()` 按列名/字段名映射解析，每行输出一个 `CSVRow`（各指标以 `map[全局卡ID]float64` 存储）。通过 `cardIndexer` 把 `(node, cardID)` 映射为全局整数卡 ID，并记录 `NodeOf`（全局ID→节点名）和 `LocalID`（全局ID→节点内卡ID）；平铺输入（节点 "none"）全局 ID 等于原始卡 ID。自动发现所有卡。

#### Step 2: 10 秒聚合

`AggregateByMinute()` 将原始行按 `AggregationWindowSec`（默认 10 秒）分桶（`timestamp / window * window` 向下取整），每桶产出 1 个聚合行：

| 指标类型 | 聚合方式 | 说明 |
|---------|---------|------|
| 连续型（temp/power/freq/util/hbm_bandwidth_util/hbm_util/tx_bw/nic_rx） | **裁剪均值 (midmean)** | 排序 → trim 两端 25% → 中间 50% 求均值。若样本 < `MinSamplesForTrim`(4) 降级为普通均值；截尾后不足 2 个点 → 中位数兜底 |
| 计数器（error counters / PFC / retry） | **增量 (counter delta)** | `last − first`，处理 64-bit 回绕；桶内样本 < 2 → 0 |

CPU 取桶内最后一个值。

#### Step 3: 空间维度检测（Peer Comparison）

`detectSpaceAnomalies()` **只取全部数据的最后一个聚合点**判定（时间维度与基线/检测窗口已移除）。**peer 组 = 同一节点内的在场卡**（跨节点不互比）；平铺输入（单节点 "none"）时 peer 组 = 全体在场卡。每卡每指标的 score 数组只含 1 个元素（最后一点）。

**对最后一个点、每个节点**，按 `Method` 判定：

| Method | 适用指标 | 机制 | 异常判定 |
|-------------|---------|------|---------|
| `cluster` | temp/power/freq/util/hbm_bandwidth_util/hbm_util/tx_bw | **双方向** kmeans 比例检测（共享 `clustering` 包） | **少数方向标记的卡为异常**（两方向标记数相等 → 不上报）|
| `absolute` | 4× error counters | > 0 | sentinel 999 |

**cluster（kmeans 比例）机制**（共享 `feature/straggler/clustering/kmeans.go`，与 Profiler 均质化聚类同一算法；KPI 层在调用前把 `≤ 0` 读数钳制到极小值 `zeroFloor = 1e-3`——真实 0 是有意义的空闲/关闭读数，参与聚类而非丢弃）：
1. 收集节点内在场卡，把值 `≤ 0` 的读数钳制到 `zeroFloor`（远低于任何真实读数的极小值）后参与聚类（NaN 排除）；不足 2 张 → 该节点全 0 退出
2. Z-score 标准化（std≈0 → 强制 1）
3. 肘部法选 k（K=2..min(n,10)，取 inertia 二阶差分最大）
4. kmeans++ 初始化（首个质心 = `data[0]`，后续 D² 加权采样，**固定种子 seed=42，结果确定**）+ Lloyd 迭代（≤300 轮，空簇处理，收敛 1e-9）
5. **双方向各检一次**：max 方向（基线 = 最小均值簇，标记高于它且比例超阈值的簇）→ α1；min 方向（基线 = 最大均值簇，标记低于它且比例超阈值的簇）→ α2
6. 比较 \|α1\| 与 \|α2\|：**少数者为异常**（单卡偏离多数模式 = 拖后腿；多数整片偏移只是正常模式）；**个数相等 → 不上报**（含 0==0 健康情形与 50/50 歧义情形）
7. 对选中方向的异常簇递归（深度 ≤10）：更深层异常替换父层，更深层无异常保持父层；返回最深异常簇
8. 参与聚类的卡都有 `score = 选中方向的簇比例`（真实比值）：基线簇成员恰为 1.0，其他未标记簇保留真实比值（如 1.2），被标记卡为其比值（> 阈值）；判定用选中方向递归 `Detect` 的标记（Flagged 数组），不随比值变化；缺失 / NaN 的卡为 0（无读数，无法计算比值）

> 双方向聚类免去对"小值为异常还是大值为异常"的预判：同一份数据两个方向各检一次，每方向以自身的方向极值簇为基线标记偏离方，标记数少的方向即偏离同伴的少数派（单卡降频、升温、冷却都能检出）；比例阈值（2.0）保证自然散布（如 54..60°C）不会被当作异常。

`aggregateScores()` 汇总：判定用选中方向递归 Detect 的标记（cluster）或 999 哨兵（absolute，取异常占比）；score 为选中方向的真实簇比值。

#### Step 4: 按指标分组输出

`buildAnomalyMetrics()` 以**纯空间结果**判定：某指标某卡 `abnormal` → 该卡异常。输出为**指标优先**——每个异常指标下列出异常的卡及其 `score`（劣化程度）。卡级不再有 quadrant / 复合评分 / category；根因定界与跨卡关联也已移除。

```
某指标异常 → 该指标下所有空间异常的卡及其 score
```

### 1.4 输出

| 文件 | 位置 | 内容 |
|------|------|------|
| `straggler_output.json` | 运行目录（当前目录） | **合并输出**：`{"kpi": <KPI 结果>, "profiler": <Profiler 结果>}`；只跑了哪个维度就只含哪个键 |
| `detection_report.log` | `path/analysis_result/` | Profiler 文本报告 |
| stdout | — | KPI 文本报告（不落盘）+ Profiler 逐类摘要 |

**JSON 输出结构**（`straggler_output.json` 的 `kpi` 段，即 `{"kpi": {...}}`）：

```json
{
  "summary": { "total_cards": 16, "total_nodes": 2, "anomalies": 1, "normal": 15,
               "source": "/data/kpi_dir", "data_points": 129600, "space_ratio_threshold": 2.0 },
  "anomaly_metrics": [
    {
      "metric": "temp",
      "method": "cluster",
      "cards": [
        { "node": "node-ip-1", "card_id": 0, "score": 2.5, "abnormal": true }
      ]
    }
  ]
}
```
（输出为指标优先：`anomaly_metrics[].cards[]` 列出该指标异常的卡及其空间 score；`abnormal` 仅在 debug 模式出现（列出全部卡时区分）；无 quadrant / composite_score / root_causes / correlations。）

### 1.5 NPU 资源指标

| 指标名 | 分类 | Method | 说明 |
|--------|------|--------|------|
| `temp` | 计算 | cluster | NPU 温度 (°C)，对称连续 |
| `power` | 计算 | cluster | NPU 功耗 (W)，对称连续 |
| `aicore_freq` | 计算 | cluster | AI Core 频率 (MHz)，离散档位，>2× 降频空间判定 |
| `aicore_util` | 计算 | cluster | AI Core 利用率 (%)，双峰（80%+ 工作态） |
| `hbm_bandwidth_util` | 计算 | cluster | HBM 带宽使用率 (%)，双峰 |
| `hbm_util` | 计算 | cluster | HBM 内存使用率 (%) |
| `tx_bandwidth` | 通信 | cluster | TX 带宽，近似连续 |
| `rx_pfc_pkt` | 通信 | absolute | PFC 暂停帧（累积计数器） |
| `roce_tx_err_pkt` | 通信 | absolute | RoCE 发送错误包（累积计数器） |
| `roce_out_of_order` | 通信 | absolute | RoCE 乱序包（累积计数器） |
| `roce_new_pkt_rty` | 通信 | absolute | RoCE 重传包（累积计数器） |

> 异常方向不再预定义：cluster 类指标由**双方向投票**自适应（少数方向为异常），无需逐指标声明 ↑/↓；absolute 类指标 `> 0` 即异常。`nic_rx_all_pkg` 会被解析但**不在 11 个检测指标内**（只采集不判定）。

### 1.6 边界情况

| 场景 | 处理 |
|------|------|
| 空间维度同行点 < 2 卡 | score=0（无法做 peer comparison） |
| 某节点在场卡 < 2 | 该节点 score=0（节点内无法做 peer comparison），其他节点不受影响 |
| ≤0 读数（含真实 0） | 钳制到 `zeroFloor=1e-3` 参与聚类（不丢弃）；NaN 排除 |
| 缺失 / NaN 卡 | 该卡该指标 score=0（无读数，不参与聚类） |
| 裁尾后数据不足 | 桶内样本 < 4 降级为普通均值；截尾后不足 2 点 → 中位数 |
| 计数器回绕 | 自动加 `MaxUint64` 修正 |
| JSONL 某天文件不存在 | 天然跳过（只读存在的文件） |
| CSV 列不完整 | 缺失列 warn 但不阻断，对应 metric dict 为空 |
| 仅 `--kpi-path` 无 `path` | 仅输出 KPI 结果，不执行 Profiler |

### 1.7 配置默认值

```go
AggregationWindowSec: 10      // 10 秒聚合
TrimRatio:            0.25    // 裁剪比例（每端 25%，中间 50%）
MinSamplesForTrim:    4       // 低于此样本数降级为普通均值
SpaceRatioThreshold:  2.0     // 空间 kmeans 簇比例阈值（独立旋钮，--space-ratio-threshold 覆盖）
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
  └── 输出 op_metric/global_rank_{N}.csv （单行数据）+ host_info/npu_info JSON
  │
  ▼
[profiling/detector] 检测引擎
  ├── GetCurDetectionInfo()    → 并行域拓扑 + 有效 rank 列表
  ├── GetCurJobLastStepData()  → 单次快照数据映射
  └── DelimitDetection()       → 执行 4 类检测
  │
  ▼
[utils]  BuildNodeResult()    → stdout 逐类摘要 + 返回节点聚合结果（并入 straggler_output.json 的 "profiler" 段）
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
| `host_info_{N}.json` | JSON | 物理节点 hostUid/hostName（sync.Once 去重，同机多卡相同） |
| `npu_info_{N}.json` | JSON | NPU id（来自 NPU_INFO 表） |

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
| 慢CPU | `cpu` | ZP_Host（按 hostUid 平滑预处理） | max | CalThreshold | 单卡 |
| NPU Bubble | `npu_bubble` | ZP_Bubble | — | 固定 < 5000ns | 单卡 |

#### 检测方法

**慢计算**：对主检测组内每组卡，优先使用 ZP_Kernel（要求组内所有 rank 都有且 > 0；方向 max，值大 = 计算慢）；否则降级为 ZP_Duration（方向 min，值小 = 计算慢导致通信时间短）。组内有效卡 < 2 → 跳过该组。

**慢通信**：对每个非 PP/非 embd 并行域，每组取通信时间最小的卡为代表，按 PP stage 分桶后均质化聚类（方向 max），异常代表卡映射回完整组上报。代表卡 < 2 或桶内 < 2 → 跳过该部分。

**慢CPU**：从每张卡的 `.db` 文件读取 `HOST_INFO.hostUid`，将相同 hostUid 的卡视为同一物理节点。每组节点内计算去 min/max 的修剪均值（≤2 个则普通均值），覆盖原始值后均质化聚类（方向 max），消除节点内差异暴露节点间差异。旧版 profiler 缺少 HOST_INFO 表时对应卡跳过预处理，保留原始 ZP_Host 参与聚类。物理节点数 < 2 时该检测无意义，stdout 摘要整行不显示。

**NPU Bubble**：固定阈值 `< 5000 ns`（5µs）且 > 0，直接判定；上报原始值（非比率）。

### 2.6 输出

#### straggler_output.json 的 "profiler" 段

Profiler 结果写入 `straggler_output.json` 的 `profiler` 键（顶层 `{"profiler": {...}}`），结构为节点聚合：结果按**物理节点**（hostname，来自 HOST_INFO.hostName，缺失回退 hostUid）+ **NPU**（id，来自 NPU_INFO.id）分组；通信结果按**并行域**分组。只含有异常的节点/NPU（`--debug-output` 时含全部）。

```json
{
  "profiler": {
    "node_result": [
      {
        "hostname": "<hostName>",
        "npu": [
          {
            "id": 0,
            "cal":        { "score": 1.5 },
            "npu_bubble": { "score": 3200.0 }
          }
        ],
        "cpu": { "score": 1.4 }
      }
    ],
    "comm_domain_result": {
      "tp": {
        "0,1,2,3": 3.2
      }
    }
  }
}
```

- `node_result[]`：每个异常节点一条，含 `hostname`、`npu[]`（只含异常的 NPU，`cal`/`npu_bubble` score 仅在异常时出现）、`cpu`（节点级，慢节点的共享值）
- `comm_domain_result`：key = 通信域名字（可读域名，如 tp），value = 组内 rank 集（逗号连接）→ score
- 顶层 `straggler_output.json`：KPI 结果在 `kpi` 键，Profiler 结果在 `profiler` 键；只跑 KPI 则只有 `kpi`，只跑 Profiler 则只有 `profiler`

#### detection_report.log

带柱状图（`█`，最大 40 字符宽度）的可读文本报告，包含：
- 数据目录、时间、有效 rank 数
- 并行域拓扑摘要
- 四类检测结果表格（异常详情最多 5 条 + "+N more"）
- ZP_Kernel 跨 rank 排序柱状图（Top 30 + Bottom 5；rank 数 ≤35 时只输出一次完整列表）
- ZP_Host 跨节点对比（≥2 物理节点才出现；逐 rank 排序无意义，不输出）
- 各通信域分组对比（min/mean/max，异常组标 `***`；通信以通信组为单位比较，不输出逐 rank 的总通信时间）
- 时间自动单位转换（s / ms / µs / ns）

### 2.7 均质化聚类算法（kmeans 比例检测）

唯一的异常检测算法，通过方向和阈值参数化适配所有检测场景。**与 KPI 资源检测的空间 cluster 共享同一 `clustering` 包**（`feature/straggler/clustering/kmeans.go`），用簇均值比值作显著性（Profiler 是单快照，无历史噪声可做 z）。

**核心流程**：
1. 过滤值 `≤ 0`；不足 2 个 → 无异常退出
2. Z-score 标准化（std≈0 → 强制 1）
3. 肘部法选 k（K=2..min(n,10)，取 inertia 二阶差分最大）
4. kmeans++ 初始化（首个质心 = `data[0]`，后续 D² 加权采样，**固定种子 seed=42**）+ Lloyd 迭代（≤300 轮，空簇处理，收敛 1e-9）
5. **基线簇 = 方向极值簇**（"max"→最小均值簇，"min"→最大均值簇）
6. 簇均值比 `> threshold` → 异常簇（"max"：`簇均值 / 基线均值`；"min"：`基线均值 / 簇均值`）
7. 对异常簇递归（深度 ≤10）：更深层异常替换父层，更深层无异常保持父层；返回最深异常簇
8. 异常卡的劣化值 = 对应簇比例

**示例**：数据 `[10, 10, 20, 10]`，阈值 1.3，方向 "max"
- kmeans 切出 {10×3}（均值 10）与 {20}（均值 20）
- 基线簇 = {10×3}（方向极值 = 最小均值簇），基线均值 10
- 卡 20：20/10 = 2.0 > 1.3 → 异常，劣化 = 2.0

**与旧版（间隙分裂 + 多数基线）的差别**：旧版按"谁多谁有理"选基线、用间隙切分；新版统一为 kmeans 聚类 + 方向极值基线 + 比例显著性，且对异常簇递归精化（更深层异常替换父层，避免浅层聚类吞掉深层结构）。kmeans++ 的 D² 采样使用**固定种子（seed=42）**，同一数据多次运行结果一致。

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
| `HOST_INFO` | `hostUid, hostName` | 卡所属物理节点标识（慢 CPU 分组依据） |
| `NPU_INFO` | `id` | NPU 编号（输出 npu_info_{N}.json） |

运行时创建索引：`idx_string_ids_value`, `idx_device_op_time`, `idx_task_time_type`

### 2.9 并行域名称

`tp`, `dp_cp`, `dp`, `cp`, `exp`（Expert Parallel，非 "ep"）, `tp_exp`, `pp`, `cp_ring`, `cp_ulysses`, `default_group`

主检测组优先级：`tp → exp → ep → tp_exp → cp → cp2 → cp_ulysses → cp_ring → dp → dp_cp → dp_modulo_exp_cp`

### 2.10 边界情况（Profiler）

| 场景 | 处理 |
|------|------|
| 无 .db 文件 | 递归查找失败 → 退出 |
| ZP_Kernel 数据不全 | 慢计算降级为 ZP_Duration + 方向 "min" |
| 通信算子缺失 | 除 ZP_Host 外所有指标填充 -99999；ZP_Host 回退用 KERNEL_AICORE Host 耗时 |
| 通信耗时 > step 总耗时 | ZP_Device 钳位到 0 |
| 组内有效卡 < 2 | 跳过该组/该桶检测（minRanksInGroup = 2） |
| PP = 1（无流水线并行） | ppStageNum=1，所有代表卡放同一桶聚类 |
| 跨节点拓扑 | getDetectionGroups 通过 nodeGlobalRank 集合过滤 |
| group_info 写入竞态 | sync.Once 保证每个文件名只写一次 |
| HOST_INFO 表缺失 | queryHostUid 返回空串，对应卡跳过 hostUid 预处理 |
| 物理节点数 < 2 | 慢CPU 检测可执行但无区分度，stdout 摘要整行不显示 |
| DataLoader 不存在 | DataLoader = 0 |
| Kernel 查询无数据 | ZP_Kernel = 0 |

---

## 三、守护进程模式（`--daemon`）

### 3.1 定位与数据流

一次性模式按需手动运行；`--daemon` 常驻运行，每周期自动执行完整检测链路，结果/控制走 HTTP。检测管线与一次性模式**共用同一实现**（`main.detectFromParsedData`，以 `DetectFunc` 注入 daemon，避免 import cycle），差异只在输入来源与编排：

```
                 slowNodeDetection --daemon
                          │
            ┌─────────────┴─────────────┐
            │    周期循环（interval）    │
            └─────────────┬─────────────┘
                          ▼
  ┌────────────────────────────────────────────────────────────┐
  │ runCycle（每周期 = --profiler-dir 根下全部 rank 子目录）     │
  │ 1. dyno 触发采集 → 校验 commandStatus=effective + 命中 PID   │
  │ 2. 等待 collect-wait（每个 rank 一个 master_*_ascend_pt）    │
  │ 3. python analyse 转 .db（torch_npu，对整个根目录）          │
  │ 4. 发现 ascend_pytorch_profiler_*.db（空 → 周期失败）        │
  │ 5. dataparse.StartProcess（非 DataParsing，不 os.Exit）      │
  │ 6. KPI 检测（读 --kpi-dir 最新数据，同 --kpi-jsonl-dir 语义）│
  │ 7. Profiler 检测（detectFromParsedData，同一次性模式）       │
  │ 8. 合并 {"kpi","profiler"} JSON 落盘 + daemon_meta.json      │
  │ 9. 结果落盘 daemon_results/<start>/ → 周期结束删整个 profiler-dir │
  └────────────────────────────────────────────────────────────┘
                          │
                          ▼
          HTTP 查询（/status /straggler/*）+ 控制（/daemon/*）
```

- 首个周期在启动 **`interval` 之后**运行（不等启动即跑）；`POST /daemon/trigger` 手动补跑；已有周期在跑时 tick 跳过（single-flight）。
- `config.FilePath` 每周期设为 `--profiler-dir` 根目录；KPI 每周期重读 `--kpi-dir` 取最新数据，无跨周期状态。
- 未提供 `--kpi-dir`：步骤 6 跳过，合并 JSON 不含 `kpi` 键，周期仍成功（仅 Profiler）。
- 退出：SIGINT/SIGTERM → 停 HTTP → 等 in-flight 周期（≤10min）→ 杀掉自己拉起的 dynolog。

### 3.2 HTTP 接口契约

路由用 Go 1.22+ ServeMux method patterns，**无 `/api/v1` 前缀**。

| 方法 & 路径 | 作用 | 成功 | 失败 |
|-------------|------|------|------|
| `GET /healthz` | 存活探针 | 200 `ok` | — |
| `GET /status` | 状态总览 | 200 `statusResponse` | — |
| `GET /straggler/results/latest` | 最近一轮合并 JSON | 200 文件 | 404 无结果 |
| `GET /straggler/results/history?limit=N` | 本次会话全部周期摘要（倒序；`?limit=N` 可选，限制条数） | 200 `{cycles: []}` | — |
| `GET /straggler/results/{id}` | 指定周期合并 JSON | 200 文件 | 400 id 非法 / 404 无该周期 |
| `GET /straggler/report/latest` | 最近一轮文本报告 | 200 text/plain | 404 无报告 |
| `GET /straggler/report/{id}` | 指定周期文本报告 | 200 text/plain | 400 id 非法 / 404 无该周期 / 404 该周期无报告 |
| `POST /daemon/start` | 恢复运行 | 200 `{"state":"running"}` | — |
| `POST /daemon/pause` | 暂停（在跑周期跑完） | 200 `{"state":"paused"}` | — |
| `POST /daemon/stop` | 优雅关闭守护进程（停 HTTP、等周期结束、杀 dynolog、删除全部落盘结果） | 200 `{"status":"stopping"}` | — |
| `POST /daemon/interval` | 改周期 | 200 `{"interval_sec":N}` | 400 越界 [60,86400] / body 非法 |
| `POST /daemon/trigger` | 立即补跑一轮 | 200 `{"status":"triggered"}` | 409 已有周期在跑 / 已暂停 |

**查询数据源 = 本次会话内存 store**：latest/history/{id} 从进程内 store 读（daemon 重启即空，看不到历史；latest/{id} 的 JSON 内容经 `JSONPath` 指向的 `daemon_results/<start>/straggler_output.json` 文件读取）；结果在 `--profiler-dir` 之外，`--profiler-dir` 周期结束时整个删除，互不影响。

**`GET /status` 响应**：

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
    "summary": { "profiler": { "cal": 1, "comm": 0, "cpu": 0, "npu_bubble": 0 }, "kpi": { "temp": 1 } }
  },
  "next_run_at": "2026-08-20T10:10:00+08:00"
}
```

`cycles_total` / `cycles_failed` 为本进程内累计（重启归零）；`next_run_at` 仅 running 且非零时出现。

### 3.3 落盘布局

```
<profiler-dir>/                        # 采集根目录（dyno 写每个 rank 一个子目录；周期结束时整个删除）
├── master_<pid>_<ts>_ascend_pt/       # 每个 rank 一个子目录（dyno 落盘）
├── ascend_pytorch_profiler_*.db       # python analyse 转出（findDBs 递归发现，位置由 torch_npu 定）
├── op_metric/                         # dataparse 中间产物（StartProcess 写根下）
└── analysis_result/detection_report.log

daemon_results/<start>/                # 每周期结果直接落盘于此（dump 目录之外，查询数据源）
├── straggler_output.json              # 本轮合并结果（查询数据源：latest/{id}）
├── daemon_meta.json                   # 周期元数据（归档记录，查询不读）
└── analysis_result/detection_report.log   # 文本报告（归档记录，report/latest 走内存）
```

运行目录另有一份最新的 `straggler_output.json`（与一次性模式同形状，覆盖写）。

### 3.4 采集链路与错误处理

- **dynolog**：启动时 `--enable-ipc-monitor --certs-dir NO_CERTS` 拉起；IPC 已被占用 → 记日志复用现有实例（dyno 走 IPC 通信）。
- **dyno 触发**：`nputrace --start-step -1 --iterations <N> --activities NPU,CPU --profiler-level Level0 --msprof-tx --export-type Db --log-file <profiler-dir>`（N = `--profiler-iterations`，默认 5）；从 stdout `"response = {...}"` 解析 JSON 校验。

| 错误场景 | 周期结果 |
|----------|----------|
| dyno 触发失败 / 非零退出 | error=触发失败，周期记失败 |
| `commandStatus=ineffective` 或 `processesMatched` 空 | error=未命中 vllm 进程（未设 MSMONITOR_USE_DAEMON=1），周期记失败 |
| python analyse 失败 | error=转换失败，周期记失败 |
| 无 .db（analyse 后根目录递归找不到 .db） | error=无产物（附根目录顶层清单），周期记失败 |
| dynolog 已占用 | 复用现有实例（不视为失败），首个周期验证连通 |
| KPI 目录为空 / 检测失败 | 该维度本轮跳过（profiler 照常，不阻断周期） |

失败的周期照常记录到内存 store（error 非空），history 可查。本次会话历史无条数上限，`/straggler/results/history` 默认返回全部周期。

### 3.5 dyno/dynolog 安装与构建

- dyno/dynolog **不进版本库、不 embed**：build.sh 从 msmonitor daily bucket 直接下载 `dynolog_0.3.2_1.aarch64.deb`（`https://ascend-package.obs.cn-north-4.myhuaweicloud.com/msmonitor/daily/2026040207/deb/aarch64/`），用系统包管理器安装（dpkg 原生；rpm 系需 `alien` 转换；权限不足经 sudo），二者直接可从 PATH 调用。`go build` 完全不依赖它们。
- daemon 启动时 `exec.LookPath("dyno")` / `exec.LookPath("dynolog")` 从 PATH 解析路径注入 `Config`；解析失败 → 报错退出，提示先跑 build.sh。
- `build.sh`：架构检查(aarch64) → 下载 msmonitor zip 装 dyno/dynolog(.deb) → Python 版本检查(3.9–3.12) → pip 装 mindstudio_monitor wheel → Go 工具链检查（缺失/过旧时从阿里云下载并持久化 PATH）→ `CGO_ENABLED=0 go build -o slowNodeDetection .`。详见 README「九、构建与部署」或 build.sh 注释。

---

## 包结构

| 包 | 职责 |
|------|--------|
| `main` | CLI 参数解析、双模式编排（KPI → Profiler 降级链）、合并 JSON 输出、daemon 启动时 PATH 解析 dyno/dynolog |
| `daemon` | 守护进程：周期调度（dynolog/dyno 采集）、runCycle 编排、HTTP 查询/控制、结果落盘 |
| `resource` | KPI 检测引擎：解析 → 聚合 → 空间检测 → 指标分组 → 报告 → JSON 导出 |
| `clustering` | 共享 kmeans 比例检测算法（KPI 空间检测与 Profiler 均质化聚类共用） |
| `config` | Profiler 全局配置（FilePath、CalThreshold、CommThreshold）、DegradationData 结果聚合 |
| `profiling/dataparse` | SQLite `.db` 解析 → CSV + JSON 中间文件（含 host_info/npu_info） |
| `profiling/detector` | 并行域拓扑解析、单步快照、四类检测逻辑、debug 诊断分 |
| `utils` | Profiler 结果写入（stdout 摘要 + 节点聚合结构） |
| `report` | Profiler 文本报告生成 |

---

## 关键设计决策

- **双模式分离**：KPI（资源指标时序）和 Profiler（单步快照）是完全不同的检测范式和管线，在 `main.go` 中分支，`resource/` 和 `profiling/` 各自独立。
- **KPI: 纯空间 peer 对比**：已移除时间维度与基线/检测窗口，异常完全由最后一个聚合点的空间 peer 对比判定（kmeans 簇比例 / 错误计数绝对阈值）。
- **KPI: 指标独立检测**：每个指标独立做空间检测（cluster 双方向投票 / absolute 绝对阈值），输出按指标分组；无计算/通信的卡级归类与"继发"标记。
- **KPI: 裁剪均值聚合**：原始数据 ~2s 采集，每 10 秒聚合窗口（`AggregationWindowSec=10`）内使用 25% 裁剪均值，抵抗采集噪声（温度/功耗传感器的瞬时抖动）。
- **KPI: HBM 双指标并存**：`hbm_bandwidth_util`（带宽）+ `hbm_util`（内存）都参与空间检测；语义上带宽更贴合性能瓶颈判断，内存使用率参考价值较低但仍检测。
- **KPI: ≤0 钳制参与**：真实 0（空闲/关闭）是有意义的读数，钳制到 `zeroFloor=1e-3` 参与聚类；钳制只在资源层做，共享聚类包保持过滤 ≤0（Profiler 侧 0/缺失不参与）。
- **Profiler: 合并 Step**：所有 step 合并为单聚合 step（minStart → maxEnd），CSV 仅一行。Profiler 时间分辨率低，逐 step 不可靠。
- **Profiler: 倒数第二行**：多行数据取 n-2 行，避免末行不完整。
- **-99999 哨兵**（Profiler）：统一无效数据标记，在 GetCurJobLastStepData、detectionZpBubbleData、report.filterValid 中跳过。
- **Profiler: 单一算法**：kmeans 比例检测（`clustering` 包）是唯一的异常检测器，所有场景通用，并与 KPI 空间检测共享同一实现。
- **Profiler: 不做时序分析**：仅处理单次快照，不进行趋势/移动平均/变点检测。
- **单一检测管线**：daemon 与一次性模式共用 `detectFromParsedData`（main 以 `DetectFunc` 注入 daemon，避免 import cycle）。
- **每周期 = `--profiler-dir` 根下全部 rank 子目录**：周期之间无增量状态；周期结束时删除整个 `--profiler-dir` 防堆积（成功/失败都删，dyno 采集时自动重建）；查询只看本次会话内存 store（重启即空）。
- **落盘 JSON 为查询数据源**：HTTP 查询读 `daemon_results/<start>/` 直接落盘的结果文件，进程内 store 只是最新周期的快速路径。
- **采集工具走系统安装而非 embed**：dyno/dynolog 由 build.sh 用系统包管理器安装（`dynolog_*.deb`），走 PATH 调用，仓库不携带第三方制品；代价是 daemon 机器须先装好，换来编译与交付简单（Go 产物与采集工具解耦，任何平台可出包）。
- **KPI 复用**：daemon 的 KPI 检测与一次性模式同一实现（`resource.RunDetectionFromData`），输入换成每周期重读 `--kpi-dir`，无额外状态。

---

## 构建

**标准构建（aarch64 主机，daemon 模式必需）**：

```bash
cd feature/straggler
bash build.sh   # 架构检查 → 装 dyno/dynolog(.deb) → Python 版本检查 → 装 wheel → Go 工具链 → go build
```

`go build` 不依赖 dyno/dynolog（无 embed），任何平台均可直接编译；daemon 模式运行时才要求目标 aarch64 主机已装好二者（跑一次 build.sh）。

**跨平台出包**（一次性模式）：

```bash
# Linux ARM64（目标平台）
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o slowNodeDetection .

# Linux AMD64
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o slownode_linux_amd64 .

# Windows AMD64
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -o slownode_win_amd64.exe .
```

全静态二进制，SQLite 驱动使用 `modernc.org/sqlite`（纯 Go 实现，无需 CGO）。
