# NPU 资源指标异常检测 — 设计文档

## 1. 概述

### 1.1 背景与动机

现有慢节点检测基于 Ascend PyTorch Profiler Level0 数据，做**单次快照**检测。Profiler 存在两个限制：

- **性能开销大**：Profiler API 侵入式采集，产生大量 `.db` 文件，不适合常态化

本方案基于 `kpi_collect.sh` / CATMonitor 采集的**NPU 资源 KPI 时序**（保留 15 天，聚合窗口 10 秒），以**无侵入、常态化**方式实现：

- **纯空间 peer 对比**：异常完全由最后一个聚合点的空间维度判定（时间维度、基线/检测窗口、根因定界、四象限均已移除）
- **10秒截尾均值抗噪**：排序→去两端 25%→中间 50% 均值，单采样点噪声不污染检测结果

### 1.2 与 Profiler 检测的定位

```
                    ┌─────────────────────────────────────────┐
                    │         慢节点检测体系                     │
                    │                                         │
                    │  ┌──────────────────────┐               │
                    │  │ KPI 资源指标检测      │  ← 第一道防线  │
                    │  │ (本方案)              │    轻量、常态化 │
                    │  │ 纯空间 peer 对比      │    15天数据    │
                    │  └─────────┬────────────┘               │
                    │            │                            │
                    │            │ 未发现异常 / 交叉验证        │
                    │            ▼                            │
                    │  ┌──────────────────────┐               │
                    │  │ Profiler 慢节点检测   │  ← 第二道防线  │
                    │  │ (已有)                │    精查、深度   │
                    │  │ 单次快照、4维检测      │    按需触发    │
                    │  └──────────────────────┘               │
                    └─────────────────────────────────────────┘
```

**检测顺序：先 KPI → KPI 查不到异常 → 再触发 Profiling**（KPI 发现异常且有 `path` 时也跑 Profiler 做交叉验证；仅 KPI 无 `path` 时 KPI 结果即为最终输出）

- KPI 检测覆盖面广（热降频、网络错误、功耗异常等硬件级问题），且无额外开销
- Profiling 检测覆盖 KPI 看不到的软件级问题（Kernel 慢、通信慢），按需触发避免常态开销

---

## 2. 数据格式

### 2.1 输入：每节点 CSV 目录（`--kpi-path`）与 CATMonitor JSONL（`--kpi-jsonl-dir`）

两种输入等价，最终都重建同一个 `TimeSeriesData`：

```
{dir}/
├── node-a.csv               # 每节点一个 CSV，单元格为平铺 {cardID: value}
├── node-b.csv
└── node_config.json         # {文件名: {node: 节点名, cards: [实际使用卡号]}}
```

```
{dir}/
├── node-a/straggler_kpi_2026-08-13.jsonl   # 每节点一个子目录（JSONL 模式）
├── node-b/straggler_kpi_2026-08-13.jsonl
└── node_config.json
```

每个 CSV/JSONL 的指标单元格为**平铺** `{cardID: value}`，card ID 在**节点内**从 0 编号；`node_config.json` 的 `cards` 之外的卡被过滤。JSONL 无 `node_config.json` 时按单目录读取（`vals` 平铺 → 节点 `"none"`；`vals` 外层为节点名的嵌套形态 → 按节点解析）。单文件 `ParseCSV`（支持平铺/嵌套 JSON 单元格，平铺 → 节点 `"none"`）仅内部/测试用，CLI 主路径走目录方式。

内部把 `(node, cardID)` 映射为全局整数卡 ID（`cardIndexer`），并记录 `NodeOf`（全局ID→节点名）与 `LocalID`（全局ID→节点内卡ID）；平铺输入全局 ID = 原始卡 ID。**空间检测的 peer 组是同一节点内的卡**，跨节点不互比。

| 列 | 含义 | 单位 | 类型 |
|---|------|------|------|
| `timestamp` | 采集时间戳 | Unix秒 | int64 |
| `NPU_CARD_POWER` | 每卡功耗 | W | JSON dict[card→float] |
| `NPU_CARD_TEMP` | 每卡温度 | ℃ | JSON dict[card→float] |
| `NPU_CARD_AICORE_FREQ` | 每卡 AI Core 频率 | MHz | JSON dict[card→float] |
| `NPU_CARD_AICORE_UTIL` | 每卡 AI Core 利用率 | % | JSON dict[card→float] |
| `NPU_CARD_HBM_BANDWIDTH_UTIL` | 每卡 HBM 带宽使用率 | % | JSON dict[card→float] |
| `NPU_CARD_HBM_UTIL` | 每卡 HBM 内存使用率 | % | JSON dict[card→float] |
| `NPU_TX_BANDWIDTH` | 每卡发送带宽 | ? | JSON dict[card→float] |
| `NPU_RX_PFC_PKT` | 每卡接收 PFC 暂停帧 | 包数 | JSON dict[card→float] |
| `NPU_ROCE_TX_ERR_PKT` | 每卡 RoCE 发送错误包 | 包数 | JSON dict[card→float] |
| `NPU_ROCE_OUT_OF_ORDER` | 每卡 RoCE 乱序包 | 包数 | JSON dict[card→float] |
| `NPU_ROCE_NEW_PKT_RTY` | 每卡 RoCE 重传包 | 包数 | JSON dict[card→float] |
| `NPU_NIC_RX_ALL_PKG` | 每卡 NIC 接收总包 | 包数 | JSON dict[card→float] |
| `CPU_average` | 各 CPU 平均利用率 | % | JSON dict[cpu→string] |

### 2.2 数据分区

时间维度与基线/检测窗口已移除：全部数据聚合后，空间检测只取**最后一个聚合点**（最接近当前时刻的读数）做 peer 对比。历史数据仅用于 10 秒聚合，不参与判定。

---

## 3. 数据预处理：10秒截尾均值聚合

### 3.1 为什么需要预处理

原始 KPI 采集频率可能很高（秒级甚至亚秒级），单个采样点受瞬时波动、采集噪声、短时尖峰影响大。直接用裸数据点做检测会导致：

- **误报**：一个瞬时尖峰被标记为异常
- **漏报**：持续偏高但因单点波动大被统计方法稀释

解决方案：**将每个聚合窗口（`AggregationWindowSec`，默认 10 秒）内的所有原始采样点聚合为一个稳健的统计量**，作为后续检测的"一个数据点"。

### 3.2 截尾均值算法（Midmean）

```
输入：10 秒窗口内某卡某指标的 N 个原始采样值
输出：该 10 秒窗口该卡该指标的聚合值

步骤：
  1. 排序：将 N 个值升序排列 → sorted[0..N-1]
  2. 截尾：去掉前 25% 和后 25%，取中间 50%
     trim = floor(N * 0.25)（trim == 0 时强制为 1）
     保留区间 = sorted[trim .. N-1-trim]
  3. 平均：对保留区间内的值取算术平均
     midmean = avg(sorted[trim .. N-1-trim])
     截尾后不足 2 个点 → 中位数兜底
  4. N < MinSamplesForTrim(4) → 降级为普通均值
```

**示例**：某卡在 10 秒内采集了 20 个温度值（N=20，采集频率 2Hz）

```
原始值: [45, 47, 46, 48, 62, 47, 46, 49, 45, 48, 47, 46, 51, 47, 46, 48, 45, 47, 46, 49]
排序后: [45, 45, 45, 46, 46, 46, 46, 46, 47, 47, 47, 47, 47, 48, 48, 48, 49, 49, 51, 62]
         │←─ 前5个(25%)去掉 ─→│←────── 中间10个(50%)保留 ──────→│←─ 后5个(25%)去掉 ─→│
trim = 5
保留区间 = [46, 46, 46, 46, 47, 47, 47, 47, 47, 48]
midmean = (46+46+46+46+47+47+47+47+47+48) / 10 = 46.7

对比：
  - 全量均值 = 48.1  （被 62 和 51 拉高）
  - 中位数   = 47.0  （只看中间一个点）
  - 截尾均值 = 46.7  ← 最接近真实稳定温度，不被尖峰污染
```

### 3.3 特殊指标的处理

| 指标类型 | 聚合方式 | 理由 |
|---------|---------|------|
| 连续型指标（TEMP, POWER, FREQ, UTIL, BANDWIDTH） | 截尾均值 | 需要消除尖峰，取稳定代表值 |
| 计数型指标（ERR_PKT, RETRY, OUT_OF_ORDER, PFC_PKT） | **增量（counter delta）** | 错误包是累积计数器，应取窗口内（10 秒）的增量而非均值 |
| NIC_RX_ALL_PKG | 截尾均值 | 接收包数波动大，截尾后更稳定 |
| CPU_average | 取桶内最后一个值 | 机器粒度，仅采集不参与检测 |

对于计数型指标，聚合时注意处理计数器回绕（counter wrap）：
```
该窗口增量 = counter[t_end] - counter[t_start]
if 增量 < 0: 增量 += 2^64 （处理回绕；仍 < 0 → 数据异常，取 0）
if 增量 == 0: 正常，无错误
if 增量 > 0: 聚合值 = 增量
```

### 3.4 聚合前后数据量变化

```
聚合前：N 行/10秒（N = 采集频率 × 10）
  e.g., 每秒采集 1 次 → 10 行/10秒 → 15天 = 1,296,000 行

聚合后：1 行/10秒
  e.g., 15天 = 129,600 行

聚合后的每行数据结构保持不变（timestamp 取该窗口起始时间），
仅 value 从"瞬时采样值"变为"截尾均值/增量"。
```

### 3.5 在管线中的位置

```
原始 CSV/JSONL（秒级）
  │
  ▼
[解析]  逐行解析 → rawRows（TimeSeriesData）
  │
  ▼
[聚合]  ← 本步骤（AggregationWindowSec，默认 10 秒）
  对每 10 秒窗口：
    按(窗口, 卡号)分组 → 排序 → 截尾25% → 中间50%均值
    （或计数型指标：取增量）
  输出：聚合行 (每 10 秒 1 行)
  │
  ▼
[空间检测]  → 只取最后一个聚合点，节点内 peer 对比（kmeans 簇比例 / 绝对阈值）
  │
  ▼
[输出]  → 按指标分组：异常指标 → 卡 + 空间 score
```

---

## 4. 空间检测模型

### 4.1 核心思想

时间维度与基线/检测窗口已移除：是否异常**完全由空间维度判定**——在最后一个聚合点与其他卡 peer 对比，问"这张卡比别人差吗？"。peer 组 = 同一节点内的在场卡（跨节点不互比；平铺输入为单节点 "none"，等同全体卡）。

### 4.2 判定

```
空间维度
  某指标某卡正常 → 正常
  某指标某卡异常 → 该卡异常（输出按指标分组：异常指标 → 卡 + score）
```

| 状态 | 空间 | 判定 | 含义 |
|------|------|------|------|
| 正常 | 正常 | ✓ 正常 | 该卡一切正常 |
| 异常 | 异常 | ✗ 告警 | 最后一个聚合点上该卡偏离同伴群体 |

（quadrant / composite_score / severity 已移除；输出只保留异常指标及其空间 score。）

### 4.3 空间评分

```
对指标 m，卡 c：

  空间分 S_space[m][c] = 簇比例（kmeans）：簇均值 / 基线簇均值
                         （两方向同一定义；absolute 方法：值 > 0 → sentinel 999）
  基线簇成员 score = 1.0（真实比值）
  其他未标记簇 score = 真实比值（max 侧如 1.2，min 侧如 0.9）
  被标记卡 score = 其真实比值（max 侧 > SpaceRatioThreshold，min 侧 < 1/SpaceRatioThreshold）
```

- 只取全部数据的**最后一个聚合点**判定（无窗口切分）
- 网络错误类指标不适用 kmeans（正常值恒为 0），改用绝对阈值（> 0 即异常）

---

## 5. 检测算法设计

### 5.1 空间维度检测（Peer Comparison）

**只取全部数据的最后一个聚合点**判定（`detectSpaceAnomalies`），peer 组 = 同一节点内的在场卡（跨节点不互比；平铺输入为单节点 "none"，等同全体卡）。**主方法为 kmeans 比例检测（MethodCluster）**，与 Profiler 均质化聚类共享 `clustering` 包：空间维度问"谁偏离同伴"，同伴的标准是**双方向各检一次**——max 方向（基线 = 最小均值簇）与 min 方向（基线 = 最大均值簇），标记数少的方向为异常（相等不上报）。

**方法 A：kmeans 比例检测（MethodCluster，默认）**

```
对最后时间点 t，指标 m，节点 N 内在场卡的读数 V（值 ≤ 0 的读数钳制到极小值 zeroFloor=1e-3——真实 0 是有意义的空闲/关闭读数，参与聚类而非丢弃；NaN 排除）：
1. 不足 2 张 → 全 0 退出
2. Z-score 标准化（std≈0 → 强制 1）
3. 肘部法选 k（K=2..min(n,10)，取 inertia 二阶差分最大）
4. kmeans++ 初始化（首个质心 = data[0]，后续 D² 加权采样，固定种子 seed=42）
5. Lloyd 迭代（≤300 轮，空簇处理，收敛 1e-9）
6. **双方向各检一次**：max 方向（基线 = 最小均值簇，标记高于它且 score > 阈值的簇）→ α1；min 方向（基线 = 最大均值簇，标记低于它且 score < 1/阈值的簇）→ α2
7. 比较 |α1| 与 |α2|：**少数者为异常**；**个数相等 → 不上报**（含 0==0 健康情形与 50/50 歧义情形）
8. 对选中方向的异常簇递归（深度 ≤10）：更深层异常替换父层，更深层无异常保持父层
9. 参与聚类的卡都有 score = **簇均值 / 基线均值（两方向统一）**：基线簇成员恰为 1.0，其他未标记簇保留真实比值（max 侧如 1.2，min 侧如 0.9），被标记卡为其比值（max 侧 > 2.0，min 侧 < 0.5）；缺失/NaN 的卡为 0（无读数，无法计算比值）
聚合：判定用选中方向递归 Detect 的标记（Flagged 数组）；score 为选中方向的真实簇比值
```

适用：POWER, TEMP, AICORE_FREQ, AICORE_UTIL, HBM_BANDWIDTH_UTIL, HBM_UTIL, TX_BANDWIDTH（在各节点内独立检测）

**设计要点**：
- **双方向投票**：无需预判异常方向（KPI 难区分小值异常还是大值异常）——两个方向各检一次，标记数少者为异常；单卡降频、升温、冷却都能检出，多卡同向异常一起标记；多数整片偏移只是正常模式，不会被误报；无 mean/std 稀释
- **比例阈值防误报**：score = 簇均值/基线均值，max 侧需 > 2.0、min 侧需 < 0.5（即 < 1/2.0）才算异常，自然散布（如 54..60°C，最大比 ≈1.1）不会被当作异常
- **极小值参与（zeroFloor）**：KPI 层在调用共享聚类前把 ≤ 0 读数钳制到 `zeroFloor=1e-3`，真实 0 是有意义的空闲/关闭读数，不再被过滤丢弃——「aicore_util 1 卡 100% 其余 0」「aicore_freq 1 卡 0MHz 其余 1800」这类单卡忙/卡死场景得以检出；钳制在资源层做，共享聚类包保持过滤 ≤0，Profiler 侧 0/缺失值不参与
- **递归精化**：对异常簇递归到最深异常层，避免浅层聚类吞掉深层结构；更深层无异常则保持父层
- **只判最后一点**：空间检测退化为单个聚合点判定，实时反映最新状态
- **固定种子**：kmeans++ 采样使用固定种子（seed=42），同一数据多次运行结果一致
- **无历史基线**：kmeans 无需历史噪声尺度，基线/检测窗口已移除；唯一旋钮是 `SpaceRatioThreshold`（`--space-ratio-threshold`）

**方法 B：绝对阈值（MethodAbsolute）**

```
某卡该指标值 > AbsThreshold（0）→ sentinel 999 → 异常
```

适用：网络错误类（RX_PFC_PKT, ROCE_TX_ERR_PKT, ROCE_OUT_OF_ORDER, ROCE_NEW_PKT_RTY）——正常值恒为 0，统计方法失效，> 0 即异常。

**其他指标**：
- **AICORE_FREQ**：频率为固定档位值（离散）。并入方法 A（kmeans 比例检测），只标记 >2× 的严重降频；多卡同档降频一起标记
- **CPU_average**：机器粒度，只解析不参与卡级检测

### 5.2 时间维度检测（已移除）

时间维度（MAD/经典 Z-Score 自对比、趋势检测）与基线/检测窗口已在重构中删除：KPI 异常完全由空间维度（第 5.1 节）判定。`time_detector.go` / `baseline.go` 及相关配置参数（`MinBaselineSamples` / `BaselineHours` / `DetectionHours` / `TimeZThreshold` / `TimeWeight` / `EnableTrend` 等）均已移除。

### 5.3 指标分类：计算类 vs 通信类

KPI 指标天然分属两个层面（分类用于文档/注册表语义，检测本身不按类别先后执行）：

| 类别 | 指标 | 含义 |
|------|------|------|
| **计算** | `AICORE_FREQ` | AI Core 频率 |
| | `AICORE_UTIL` | AI Core 利用率 |
| | `HBM_BANDWIDTH_UTIL` | HBM 带宽使用率 |
| | `HBM_UTIL` | HBM 内存使用率 |
| | `TEMP` | 温度 |
| | `POWER` | 功耗 |
| **通信** | `TX_BANDWIDTH` | 发送带宽 |
| | `RX_PFC_PKT` | PFC 暂停帧 |
| | `ROCE_TX_ERR_PKT` | RoCE 发送错误 |
| | `ROCE_OUT_OF_ORDER` | 乱序包 |
| | `ROCE_NEW_PKT_RTY` | 重传包 |

### 5.4 检测与输出（按指标独立）

先计算后通信的排序、"可能继发"标记、根因定界规则均已在重构中移除：**每个指标独立做空间检测**，输出按指标分组——某指标异常即列出该指标下异常的卡及空间 score，不做计算/通信的卡级归类与优先级判定。

---

## 6. 输出（根因定界与跨卡关联已移除）

根因定界（C1-C10 / N1-N4 规则）与跨卡关联已删除：输出只保留**异常指标及其空间 score（劣化程度）**。

---

## 7. 检测流程：KPI 优先 + Profiling 降级

### 7.1 整体流程

```
                    ┌─────────────┐
                    │  CLI 入口    │
                    └──────┬──────┘
                           │
                           ▼
               ┌────────────────────┐
               │ 有 KPI 或 Profiler  │
               │ 输入？(至少一个)      │
               └──────┬─────────────┘
                      │ 无 → 用法提示退出
                      ▼ 有
               ┌────────────────────┐
               │ KPI 检测（若有输入）  │
               │ 1. CSV/JSONL 解析    │
               │ 2. 10秒聚合          │
               │ 3. 空间检测（最后一点 │
               │    peer，指标独立）   │
               │ 4. stdout 报告       │
               └──────────┬─────────┘
                          │
                          ▼
               ┌────────────────────┐
               │ Profiler 检测       │
               │（若有 path）         │
               │ 数据解析 → 拓扑 →    │
               │ 4 类检测 → 节点聚合   │
               └──────────┬─────────┘
                          │
                          ▼
               ┌────────────────────┐
               │ 合并输出             │
               │ straggler_output.json│
               │ {"kpi","profiler"}  │
               └────────────────────┘
```

### 7.2 与 main.go 的集成

```go
// main.go 实际流程（简化）
func main() {
    // 1. CLI 解析：path / degradation / --kpi-path / --kpi-jsonl-dir /
    //    --space-ratio-threshold / --debug-output
    //    KPI 输入优先 --kpi-jsonl-dir；两个输入都没有 → 用法提示退出

    // ── 第一道防线：KPI 资源指标检测 ──
    if kpiInput != "" {
        kpiCfg := resource.DefaultDetectionConfig()
        kpiCfg.EnableDebug = debugOutput
        if spaceRatioThreshold > 0 { kpiCfg.SpaceRatioThreshold = spaceRatioThreshold }
        // --kpi-jsonl-dir → ReadKPIFiles → RunDetectionFromData
        // --kpi-path       → RunDetectionFromDir
        kpiResult, err := ...
        if err != nil {
            // 告警；有 path 则继续 Profiler，无则最终无输出文件
        } else {
            fmt.Print(resource.WriteReport(kpiResult))       // stdout 文本报告
            // 交叉验证决策消息（不阻断流程）：
            //   有异常 + 无 path → "Done."
            //   有异常 + 有 path → 继续 Profiler 交叉验证
            //   无异常 + 有 path → fallback 到 Profiler
        }
    }

    // ── 第二道防线：Profiler 慢节点检测 ──
    if inputPath != "" {
        config.FilePath = inputPath
        config.CalThreshold = 1 + degradation
        config.CommThreshold = 1 + degradation*5
        dataparse.DataParsing(inputPath)                        // SQLite → 中间文件
        parallels, validRanks := detector.GetCurDetectionInfo(inputPath)
        stepData := detector.GetCurJobLastStepData(validRanks)
        result := detector.DelimitDetection(stepData, parallels, validRanks)
        profilerOut, _ := utils.BuildNodeResult(result, parallels, debugInfo)
        report.WriteReport(stepData, parallels, validRanks, inputPath, result, inputPath, degradation)
    }

    // ── 合并输出 ──
    if kpiResult != nil || profilerOut != nil {
        writeCombinedJSON(kpiResult, profilerOut, "straggler_output.json") // 运行目录
    }
}
```

### 7.3 KPI 与 Profiling 的能力互补

| 故障类型 | KPI 能发现？ | Profiling 能发现？ |
|---------|------------|------------------|
| 热降频（TEMP↑ + FREQ↓） | ✓ 直接 | ✗ |
| 网络链路错误（ERR_PKT↑） | ✓ 直接 | ✗ |
| 网络拥塞（PFC_PKT↑） | ✓ 直接 | ✗ |
| 散热不足（POWER↑ + TEMP↑） | ✓ 直接 | ✗ |
| Straggler（UTIL↓ + POWER↓） | ✓ 间接发现 | ✓ 精确发现 |
| 单卡 Kernel 计算慢 | ✗（UTIL 可能仍高） | ✓ 精确发现 |
| 集体通信延迟 | ✗ 间接 | ✓ 精确发现 |
| CPU Host 处理慢 | ✗ | ✓ |
| Bubble 时间异常 | ✗ | ✓ |

**总结**：KPI 擅长硬件/物理层异常，Profiling 擅长软件/性能层异常。两者互补。

---

## 8. 模块设计

### 8.1 包结构

```
feature/straggler/
  ├── resource/               # KPI 资源异常检测
  │   ├── types.go            # 数据结构 + 指标注册表 + 配置
  │   ├── parser.go           # CSV 解析 / KPI 目录解析（node 感知全局卡号）
  │   ├── json_reader.go      # CATMonitor straggler_kpi JSONL 读取
  │   ├── aggregator.go       # 10秒截尾均值聚合
  │   ├── space_detector.go   # 空间维度检测（peer 对比，最后一点）
  │   └── report.go           # 管线编排 + stdout 文本报告
  ├── clustering/             # 共享 kmeans 比例检测（与 Profiler 共用）
  │   └── kmeans.go
  └── config/                 # Profiler 共享配置（KPI 配置在 resource 包内）
```

### 8.2 核心数据结构

```go
// ==================== types.go ====================

// CSVRow 一行原始 CSV/JSONL 数据。
type CSVRow struct {
    Timestamp        int64
    Power            map[int]float64 // cardID → watts
    Temp             map[int]float64 // cardID → celsius
    AICoreFreq       map[int]float64 // cardID → MHz
    AICoreUtil       map[int]float64 // cardID → %
    HBMBandwidthUtil map[int]float64 // cardID → %（带宽利用率）
    HBMUtil          map[int]float64 // cardID → %（内存利用率）
    TXBandwidth      map[int]float64 // cardID → ?
    RXPfcPkt         map[int]float64 // cardID → 包（累积计数器）
    RocETxErrPkt     map[int]float64 // cardID → 包（累积计数器）
    RocEOutOfOrder   map[int]float64 // cardID → 包（累积计数器）
    RocENewPktRty    map[int]float64 // cardID → 包（累积计数器）
    NICRxAllPkg      map[int]float64 // cardID → 包（只采集不检测）
    CPUAvg           map[string]string // cpuName → 利用率 %
}

// TimeSeriesData 解析后的完整时间序列。
type TimeSeriesData struct {
    Rows    []CSVRow // 聚合后的行（1 行/聚合窗口）
    CardIDs []int    // 全局卡 ID
    RawRows []CSVRow // 聚合前的原始行（计数器增量用）
    NodeOf  map[int]string // 全局卡 ID → 节点名
    LocalID map[int]int    // 全局卡 ID → 节点内卡 ID
}

// MetricName 指标枚举。
type MetricName string
const (
    MetricTemp           MetricName = "temp"
    MetricPower          MetricName = "power"
    MetricAICoreFreq     MetricName = "aicore_freq"
    MetricAICoreUtil     MetricName = "aicore_util"
    MetricHBMBandwidthUtil MetricName = "hbm_bandwidth_util"
    MetricHBMUtil        MetricName = "hbm_util"
    MetricTXBandwidth    MetricName = "tx_bandwidth"
    MetricRXPfcPkt       MetricName = "rx_pfc_pkt"
    MetricRocETxErrPkt   MetricName = "roce_tx_err_pkt"
    MetricRocEOutOfOrder MetricName = "roce_out_of_order"
    MetricRocENewPktRty  MetricName = "roce_new_pkt_rty"
)

// DetectionMethod 检测方法。
type DetectionMethod string
const ( MethodAbsolute DetectionMethod = "absolute"; MethodCluster DetectionMethod = "cluster" )

// MetricMeta 指标检测参数（cluster 方向自适应，无 Direction 字段）。
type MetricMeta struct { Name MetricName; Category AnomalyCategory; Method DetectionMethod; AbsThreshold float64 }

// ==================== 检测结果 ====================

// MetricAnomalyDetail 单个指标的空间异常详情。
type MetricAnomalyDetail struct {
    Metric   MetricName
    Score    float64 // 空间簇比例
    Abnormal bool    // 空间维是否异常
    Method   DetectionMethod
}

// AnomalousCard 某指标下的一张卡。
type AnomalousCard struct { Node string; CardID int; Score float64; Abnormal bool }

// MetricAnomaly 指标优先的异常分组。
type MetricAnomaly struct { Metric MetricName; Method DetectionMethod; Cards []AnomalousCard }

// DetectionResult KPI 检测完整结果（指标优先）。
type DetectionResult struct {
    Summary DetectionSummary   `json:"summary"`
    Metrics []MetricAnomaly    `json:"anomaly_metrics,omitempty"`
    Debug   bool               `json:"-"` // --debug-output 标记（不序列化）
}

// DetectionSummary 概览。
type DetectionSummary struct {
    TotalCards          int     `json:"total_cards"`
    TotalNodes          int     `json:"total_nodes"`
    Anomalies           int     `json:"anomalies"`
    Normal              int     `json:"normal"`
    Source              string  `json:"source"`
    DataPoints          int     `json:"data_points"`
    SpaceRatioThreshold float64 `json:"space_ratio_threshold"`
}

// ==================== 配置 ====================

type DetectionConfig struct {
    AggregationWindowSec int     // 聚合窗口（秒），默认 10
    TrimRatio            float64 // 截尾比例，默认 0.25
    MinSamplesForTrim    int     // 截尾最少样本数，默认 4
    SpaceRatioThreshold  float64 // kmeans 簇比例阈值，默认 2.0
    EnableDebug          bool    // --debug-output：全量输出（含正常卡）
}
```

### 8.3 接口设计

```go
// ==================== parser.go ====================
// ParseCSV 解析单文件 KPI CSV（平铺/嵌套单元格，内部/测试用）。
func ParseCSV(filePath string) (*TimeSeriesData, error)

// ParseKPIDir 解析 KPI 目录（每节点 CSV + node_config.json，node 感知全局卡号）。
func ParseKPIDir(dir string) (*TimeSeriesData, error)

// ==================== json_reader.go ====================
// ReadKPIFiles 读取目录内全部 straggler_kpi_{date}.jsonl（含多节点子目录布局）。
func ReadKPIFiles(dir string) (*TimeSeriesData, error)

// ==================== aggregator.go ====================
// AggregateByMinute 按聚合窗口（默认 10 秒）做截尾均值 / 计数器增量聚合。
func AggregateByMinute(rawRows []CSVRow, cardIDs []int, cfg DetectionConfig) ([]CSVRow, error)

// ==================== space_detector.go ====================
// detectSpaceAnomalies 对最后一个聚合点执行空间 peer 对比（节点内互比，
// 双方向 kmeans 比例 / 绝对阈值）。
func detectSpaceAnomalies(detectionRows []CSVRow, cardIDs []int, cfg DetectionConfig, nodeOf ...map[int]string) *SpaceDetectionResult

// ==================== report.go ====================
// RunDetectionFromDir / RunDetectionFromData / RunDetection 是 KPI 检测入口
//（解析 → 聚合 → 空间检测 → 按指标分组）。
func RunDetectionFromDir(dir string, cfg DetectionConfig) (*DetectionResult, error)
func RunDetectionFromData(ts *TimeSeriesData, source string, cfg DetectionConfig) (*DetectionResult, error)

// buildAnomalyMetrics 以纯空间结果按指标分组异常卡（指标优先输出）。
func buildAnomalyMetrics(spaceDetails map[int]map[MetricName]*MetricAnomalyDetail, cardIDs []int, nodeOf map[int]string, localID map[int]int, cfg DetectionConfig) ([]MetricAnomaly, int)

// HasAnomaly 结果中是否有异常卡。
func HasAnomaly(result *DetectionResult) bool

// WriteReport 生成 KPI 文本报告（仅 stdout，不落盘）。
func WriteReport(result *DetectionResult) string

// ==================== clustering/kmeans.go ====================
// Detect 递归 kmeans 比例检测（过滤 ≤0；不足 2 个 → nil）。
func Detect(values []float64, ratioThreshold float64, highIsAnomaly bool) []Result
// Diagnose 单层诊断：每点 cluster/ratio/flag。
func Diagnose(values []float64, ratioThreshold float64, highIsAnomaly bool) []DiagnoseEntry
```

---

## 9. CLI 设计

```
# 仅 KPI 检测
slowNodeDetection --kpi-path=/dir/of/kpi_csvs [options]
slowNodeDetection --kpi-jsonl-dir=/var/lib/catmonitor/straggler [options]

# KPI + Profiling 联合（KPI 优先，无异常则 fallback Profiling）
slowNodeDetection path=/data/dir --kpi-path=/dir/of/kpi_csvs [options]

# 仅 Profiling（已有，不变）
slowNodeDetection path=/data/dir [degradation=0.3]

KPI 检测专用选项:
  --kpi-path=<dir>                KPI 模式：每节点 CSV + node_config.json 的目录
  --kpi-jsonl-dir=<dir>           KPI 模式：CATMonitor straggler_kpi_{date}.jsonl 目录（优先于 --kpi-path）
  --space-ratio-threshold=<float> 空间簇比例阈值，默认 2.0（独立旋钮，不随 degradation 变化）
  --debug-output                  输出全量数据：KPI 全部指标×全部卡；Profiler 全部节点/通信组
```

> 注：`--baseline-hours` / `--detection-hours` / `--space-method` / `--space-z-threshold` / `--time-z-threshold` / `--time-weight` / `--no-trend` / `--no-fallback` / `--always-profiling` 等旧 flag 已移除（时间维度与基线/检测窗口已删除，KPI 异常完全由空间维度判定）。

---

## 10. 输出格式

### 10.1 JSON（`straggler_output.json` 的 `kpi` 段）

```json
{
  "summary": { "total_cards": 8, "total_nodes": 1, "anomalies": 1, "normal": 7,
               "source": "/data/kpi_jsonl_dir", "data_points": 129600, "space_ratio_threshold": 2.0 },
  "anomaly_metrics": [
    {
      "metric": "temp",
      "method": "cluster",
      "cards": [
        { "node": "86", "card_id": 3, "score": 3.2, "abnormal": true }
      ]
    },
    {
      "metric": "aicore_freq",
      "method": "cluster",
      "cards": [
        { "node": "86", "card_id": 3, "score": 5.0, "abnormal": true }
      ]
    }
  ]
}
```
（输出为指标优先：`anomaly_metrics[].cards[]` 列出该指标异常的卡及其空间 score（劣化程度）；`abnormal` 仅在 debug 模式出现。）

### 10.2 stdout 文本报告

KPI 文本报告**只打印到 stdout，不再写入文件**（原 `npu_resource_detection_report.log` 已移除）。包含：
- 检测摘要（正常 / 异常卡数统计）
- 异常指标详情（指标在前，其后为异常卡及空间 score，如 `aicore_freq  node86:card1(2.25)`）

---

## 11. 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| 1 | **10秒截尾均值预处理** | 单采样点噪声大。窗口内排序→去前后各25%→中间50%取均值，比全量均值稳健（抗尖峰），比中位数有代表性（保留分布信息） |
| 2 | **指标优先输出** | 输出按指标分组（异常指标 → 异常卡 + score），卡级不再有象限/复合评分/计算通信归类 |
| 3 | **纯空间 peer 对比** | 已移除时间维度与基线/检测窗口。异常完全由最后一个聚合点的空间对比判定（kmeans 簇比例 / 错误计数绝对阈值），简单且无需历史基线 |
| 4 | **KPI 优先 + Profiling 降级** | KPI 无侵入开销、覆盖硬件层异常，适合常态化；Profiling 开销大、覆盖软件层异常，按需触发 |
| 5 | **空间 kmeans 簇比例为主** | 只取最后一个聚合点（每节点少量卡），kmeans O(n·k·iter) 开销可忽略；方向极值簇作基线 + 比例阈值判异常，免调参 |
| 6 | **双方向投票免预判** | 无需声明各指标异常方向（KPI 难区分小值/大值异常）：两个方向各检一次，标记数少者为异常，相等不上报 |
| 7 | **score 即劣化程度** | 每个异常指标的卡直接带其 score（真实簇比值），无需卡级复合评分 |
| 8 | **网络错误用绝对阈值** | ERR_PKT/RETRY 正常值为 0，统计方法失效。>0 即异常 |
| 9 | **计数型指标取增量而非截尾** | ERR_PKT/RETRY/PFC_PKT 是累积计数器，应取窗口增量。截尾会抹掉真正的错误尖峰 |
| 10 | **≤0 钳制到 zeroFloor 参与** | 真实 0（空闲/关闭）是有意义的读数，不丢弃；「1 卡忙其余 0」「1 卡 0MHz 其余 1800」得以检出。钳制只在资源层，共享包与 Profiler 行为不变 |
| 11 | **正常 / 异常二元判定** | 某指标某卡空间异常 → 该卡异常；不再有四象限概念 |

---

## 12. 边界情况

| 场景 | 处理 |
|------|------|
| CSV/JSONL 无有效数据 | 报错退出，不 fallback Profiling |
| 聚合窗口内某卡某指标采样数 < 4 | 降级为普通均值；截尾后不足 2 个点 → 中位数兜底 |
| 窗口边界时间戳不齐 | 按 `timestamp / AggregationWindowSec * AggregationWindowSec` 向下取整分桶 |
| 计数型指标出现 counter wrap | `增量 < 0` 时 += 2^64 修正，若仍 < 0 标记数据异常取 0 |
| 某卡在某时间点数据缺失 | 该时间点该卡不参与该指标（缺失/NaN 卡 score=0） |
| ≤0 读数（含真实 0） | 钳制到 `zeroFloor=1e-3` 参与聚类（空闲/关闭读数不丢弃） |
| 网络错误类全为 0 | absolute 阈值不触发，指标无异常 |
| 节点内在场卡 < 2 | 该节点该指标全 0（无法 peer 对比），其他节点不受影响 |
| 全部卡同时异常 | 双方向标记数相等（或 0==0）→ 不上报（同伴一致 = 正常模式） |
| 双方向标记数相等（50/50） | 歧义情形 → 不上报 |
| 瞬态尖峰后又恢复 | 聚合窗口裁剪均值会稀释瞬态尖峰影响 |
| JSONL 某天文件不存在 | 天然跳过（只读存在的文件） |

---

## 13. 后续扩展方向

1. **在线流式检测**：不等待完整 CSV/JSONL，逐行消费 + 实时更新
2. **多快照联合**：跨多个时间点的空间检测结果联合（当前只取最后一个聚合点）
3. **与告警系统集成**：Prometheus AlertManager / 企业微信 / 邮件通知
4. **多 Job 联合分析**：同集群多个训练任务的 KPI 数据联合分析，发现集群级基础设施问题
