# 检测原理（实现规格）

> 本文是**逐条对着代码核对过**的检测原理说明，供 agent 需要解释结论时按需加载。
> 对应实现：`scripts/kmeans_detector.py`（通用算法）、`scripts/nodelevel.py`（各类指标检测）、
> `scripts/nodelevel_data_handler.py`（取数与检测组）、`scripts/profilingdataparse.py`（解析与带宽/PP 回填）。
> 默认数值均取自 `scripts/config.py`。

---

## 1. 七类指标总览

| 类别 | 指标列 | 检测方式 |
|---|---|---|
| `KERNEL_AICORE` | KERNEL_AICORE 算子平均耗时 | 检测组内通用算法（**偏大**异常） |
| `kernel_aivec` | KERNEL_AIVEC 算子平均耗时 | 检测组内通用算法（**偏大**异常） |
| `memcpy_async` | MEMCPY_ASYNC 算子平均耗时 | 检测组内通用算法（**偏大**异常） |
| `cpu` | `ZP_Host`（通信 + Kernel 的 Host 耗时均值） | 按物理节点拉齐后通用算法（异常项 = 节点 `hostName`） |
| `npu_bubble` | `ZP_Bubble`（通信算子启动间隔） | 固定硬阈值 `< 5000ns`（不询问） |
| `comm` | 集合通信带宽 | 带宽聚类（**min 方向**），两套口径见 §4 |
| `pp_comm` | `PP_WaitGap`（点对点结束 → 下一次集合通信结束的间隔） | 比较域内逐 rank 比较，按 PP 通信组上报 |

阈值分组：

| 分组 | 常量 | 默认 |
|---|---|---|
| 计算类（`KERNEL_AICORE` / `kernel_aivec`） | `COMPUTE_THRESHOLD` | 1.3 |
| IO/CPU 类（`cpu` / `memcpy_async`） | `IO_THRESHOLD` | 2.5 |
| 通信类（`comm` / `pp_comm`） | `COMM_THRESHOLD` / `PP_WAIT_RATIO_THRESHOLD` | 1.3 / 5% |
| `npu_bubble` | `BUBBLE_THRESHOLD_NS` | 5000ns（固定，不询问） |

---

## 2. 通用异常算法（`kmeans_detector.general_anomaly_detection`）

所有「检测组内」的指标共用这一套（与 `scripts/kmeans_detector.py` 模块 docstring 一致）：

1. **过滤**：剔除 `≤0` 与 `-99999`（`INVALID_MARKER`）；过滤后不足 2 个 → 判无异常退出。
2. **Z-score 标准化**；标准差 ≈ 0 时强制置 1，避免除零。
3. **肘部法选 K**：`K = 2..min(n, MAX_K=10)`，算 inertia（簇内平方和），取**二阶差分最大**者。
4. **KMeans++ 初始化**：首质心 = `data[0]`，后续按 D² 加权随机采样（固定种子 RNG，保证可复现）。
5. **Lloyd 迭代** ≤ `MAX_ITERATIONS=300` 轮：最近质心分配 → 质心 = 簇均值；空簇质心放到离其分配质心最远的样本；收敛条件 = 质心位移 < eps **且**无分配变化。
6. **识别异常簇**：按方向选基线——`max` 方向基线 = **最小均值簇**，`min` 方向基线 = **最大均值簇**；
   `max` 方向 `簇均值 > 基线 × 倍率` → 该簇异常；`min` 方向 `簇均值 < 基线` 且 `ratio < 1/倍率` → 该簇异常。
7. 无异常簇 → 无异常退出。
8. **递归细分**：以**异常簇的数据**为输入回到步骤 2（`depth+1 ≤ max_depth=10`）；
   更深层有异常 → 用更深层结果**替换**父层；更深层无异常 → **保持父层**（向外排除边缘成员，减少误检）。
9. **劣化指数**统一用**第一次 KMeans（全数据）的基线簇均值**作参考：
   `max` 方向 `degradation = 异常值 / 基线`，`min` 方向 `degradation = 基线 / 异常值`，均 > 1、越大越异常，同刻度可比。

纯 Python 实现（仅 `math` / `random`），零依赖、可复现。

---

## 3. 检测组的选择（`nodelevel.get_cal_detection_group`）

按优先级取**第一个命中的命名域**作为检测组：

```
tp → exp → ep → tp_exp → cp → cp2 → cp_ulysses → cp_ring
   → dp → dp_cp → dp_modulo_exp_cp → default_group
```

- **集群数据（Case A）**：用完整集群分组，不做节点过滤；
- **非集群数据（Case B）**：按节点过滤（`get_detection_groups`）；
- **完全没有命名域**：退化为按 `HOST_INFO.hostUid` 的**物理节点分组**，此时通信域组间指标 `comm` **直接跳过**；
- **有命名域但都不在优先级内（情况 B）**：检测组同样回退到物理节点分组，但 `comm` **仍然检测**（可带域名）。

---

## 4. 各类指标细节

**单卡类（`KERNEL_AICORE` / `kernel_aivec` / `memcpy_async`）**
在检测组内对每张卡的值做通用算法，**偏大**异常。

`memcpy_async` 的检测量是**混合平均**（外部拷贝与设备内拷贝一起取均值），因此除 `MEMCPY_ASYNC`
均值外，CSV 还落 3 个计数列，供结论层判断该异常是否由"拷贝组成不同"解释：

| 列 | 含义 |
| --- | --- |
| `MEMCPY_ASYNC_Count` | 该 step 内 `MEMCPY_ASYNC` 任务总数 |
| `MEMCPY_LINKED_Count` | `TASK.connectionId` 能关联到 `CANN_API` 的任务数 |
| `MEMCPY_EXTERNAL_Count` | 关联到的 `CANN_API.name == 'aclrtMemcpyAsync'` 的任务数 |

取数口径与检测一致：`n>1` 取倒数第二个 step，`n==1` 取第 0 个。`CANN_API` 表缺失时后两列写 `-1`
表示**不可用**（不是 0）。内部拷贝 = 总数 − 外部，不单独枚举 API。

**`cpu`（`ZP_Host`）**
先按物理节点（`hostUid`）分组、组内**去首尾求均值拉齐**，再对整组做通用算法。同节点卡值被拉平，故异常落在**整个节点**上，异常项用节点显示名（`hostName`）而不是 rank。

**`npu_bubble`（`ZP_Bubble`）**
通信算子启动间隔 `< 5000ns` 记为异常（**小值**异常），固定阈值、不询问。

**`comm`（慢通信域）——两套口径**

- **旧口径（`levelnone` / `level0`）**，数据来自带宽回填写入 CSV 的动态列 `<domain>_<opType>_<count>`：
  1. **带宽回填**：默认按 wall-clock 时间窗把每次 occurrence 跨 rank 对齐、取组内最短时长，再对全部 occurrence 的最短时长取**前 10% 均值**（`ceil(n × 0.10)`）作分母，`带宽 = count / 该均值`；开 `no-align` 则不做对齐，改用**全体 rank 算子时长的扁平池**取前 10% 均值。回填时只统计 `count ≥ SLOW_COMM_MIN_COUNT(=1000)` 的集合通信算子。
  2. **检测**（需该域 **≥2 个组**）：每个 `opType` 上，每组取 **count 最大**的条目作代表；保留 `count ≥ 组间最大 × 0.5` **且** `> SLOW_COMM_COUNT_FLOOR(=10240)` 的组；对代表带宽用通用算法（**min 方向**，带宽越小越慢）聚类。
  3. **只有在该域所有 `opType` 上都异常的组**才上报，劣化指数取各 `opType` 的最大值。

- **新两级口径（`level1` / `cluster`）**，取自 `analysis.db` 的 `CommAnalyzerBandwidth`（或 `cluster_analysis.db` 的 `ClusterCommunicationBandwidth`）：
  每 `(域, 算子类型, count)` 取**全体 rank 带宽的中位数**作基准；每 rank 指标 = 各 `(算子类型, count)` 上 `带宽 / 中位数` 的均值（≈1 正常、越小越慢）。
  - **Level-1（组内）**：对组内 rank 指标做 kmeans（min 方向）→ 慢 rank，上报为**单卡键**（键含域名，如 `ep:5`，展示为 `ep[5]`）；**单组域也做**。
  - **Level-2（组间）**：每组取组内均值，域内所有组做 kmeans（min 方向）→ 慢组，上报为**组键**；需 **≥2 个组**。
  - 两级结果都写进 `comm`。此口径**不使用** `no-align`。

**`pp_comm`（PP 慢通信）**
点对点传输不在带宽白名单内，单独检测。

- **前置跳过**：若本次已检出**计算类故障**（`KERNEL_AICORE` / `kernel_aivec`），直接跳过 PP 检测——v6pp 统计区间内必含计算算子，会被污染。
- **回填** `PP_WaitGap` = 每卡「点对点传输**结束** → 其**下一次集合通信结束**」的间隔之和（锚点：出现 `BatchSendRecv` 用 BatchSendRecv，否则用 `Send` / `Recv`；取其后第一个集合通信、跳过中间点对点；无下一次集合通信则不计）。
- **比较域**：命名域优先级 **TP > CP > DP**，都没有时用 **PP stage 转置**；域内**逐 rank** 与组内最大值比较，`(组内最大 − 本值) / 会话时长 > PP_WAIT_RATIO_THRESHOLD(=5%)` 判异常（**偏小**异常）。
- **上报单位为 PP 通信组**：组内至少一个 rank 异常即上报该组，劣化指数取组内异常 rank 的最大占比（**不上报具体 rank**）。会话时长取各 rank 合并 profiling 窗口的最大值。
- **PP 分组来源**：直接读 `parallel_group_info` 的 `pp` 项（`group_name=="pp"` 的 `global_ranks`），读不到则不检测。同一 PP 组内 rank 升序视为 stage 顺序；相邻两 stage 组成一条链路，两端点对点算子按传输序号（slot）对齐后取时间窗重叠作为该链路指标。

---

## 5. 实现文件一览

| 文件 | 职责 |
|---|---|
| `scripts/config.py` | 全局配置、阈值、劣化数据容器、节点/域名标志 |
| `scripts/main.py` | 入口（skill 调用 `run_detection` / CLI） |
| `scripts/conclusion.py` | 结论层：把检测结果表逐类别翻成自然语言（`conclusion.md`），不做任何额外判断 |
| `scripts/profilingdataparse.py` | `.db`(SQLite) → `op_metric` CSV/JSON；带宽 / PP 等待回填 |
| `scripts/nodelevel.py` | 各维度慢节点检测核心逻辑 |
| `scripts/nodelevel_data_handler.py` | 读 CSV/拓扑，构建快照与检测组 |
| `scripts/kmeans_detector.py` | 通用异常算法（KMeans + Z-score + 肘部法 + 异常簇递归细分） |
| `scripts/summary_table.py` | 汇总表渲染（ASCII，供 stdout 输出） |
| `scripts/markdown_viz.py` / `scripts/visualizer.py` | 文本详情报告（`detection_report.log`） |
| `scripts/utils.py` | 结果写入、清理、交互询问 |
| `scripts/classify_role.py` | verl colocate：训练 / rollout 分层判据 |
