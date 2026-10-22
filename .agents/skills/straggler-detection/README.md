# straggler-detector（慢节点检测 skill）

这是一个 **skill**：用于从 Ascend PyTorch Profiler 生成的 `.db` 文件，检测 AI 训练 / 推理集群里的**慢节点（straggler / 亚健康）**，给出「哪张卡 / 哪个通信域 / 哪个 PP 通信组异常、劣化多少」的结论。

skill 自带完整可运行代码包（`scripts/` 下的 `*.py` 都是它的实现与辅助脚本）。**本文档面向使用者**，介绍怎么调用、有哪些参数、输入输出、以及检测原理；面向 agent 的执行流程约束（例如「执行前先向用户确认参数」）见 `SKILL.md`。

---

## 一、如何使用

### 1. 怎么触发

**通过 agent（skill，推荐）——用自然语言说一句就行，例如：**

> 用 straggler-detector 分析一下 `/path/to/data` 这批 profiling 数据，我们训练性能出现了问题，表现为部分卡明显偏慢、step 耗时变高，看看是哪里拖慢了性能、是哪些卡 / 通信域的问题。

agent 会识别到本 skill 并执行：先按 `SKILL.md` 的流程**向你确认参数**（三组阈值 / 是否清理中间数据 / 是否 `no-align`），再跑检测，最后给出结论（那张汇总表，指出哪个 rank / 通信域 / PP 组异常、劣化多少）。**你不需要自己写代码或敲命令。**

**直接跑代码（手动排查 / 开发调试）：** agent 底层也是调这个入口。

```bash
cd <本 skill 目录>/scripts     # 含 main.py 的目录
python main.py path=/path/to/data compute=1.3 io=2.5 comm=1.3 clean=ask
```

```python
import main
result = main.run_detection(
    "/path/to/data",
    compute=1.3, io=2.5, comm=1.3,   # 三组阈值
    clean="ask",                     # 是否清理中间数据并重新解析
    no_align=False,                  # 是否关闭慢通信带宽的跨 rank 对齐
)
# result = {"KERNEL_AICORE": {"3": 287.59}, "comm": {"0,1": 1.8}, "cpu": {"worker3": 3.7}, ...}
```

> 所有命令都要在**本 skill 的 `scripts/` 目录内**执行。

### 2. 参数说明

| 参数 | 说明 | 默认 |
|------|------|------|
| `path` | 数据目录，或 cluster db 单文件（含所需表即可，**文件名不限**）（必需） | - |
| `compute` | 计算类阈值（`KERNEL_AICORE` / `kernel_aivec`） | `1.3` |
| `io` | IO/CPU 类阈值（`cpu` / `memcpy_async`） | `2.5` |
| `comm` | 通信类阈值（`comm` / `pp_comm`） | `1.3` |
| `clean` | `yes` / `no` / `ask`：是否清理中间数据并重新解析 | `ask` |
| `no_align` | 是否关闭慢通信带宽的跨 rank 对齐（详见原理部分） | `False` |

- `npu_bubble` 为固定硬阈值 `< 5000ns`，不作为参数。
- `no_align` 仅对 `levelnone` 的旧带宽口径有效；`level1` / `cluster` 用新口径，不受影响（见「检测原理 → comm」）。因此通过 agent 触发时，**只有 `levelnone` 会问 `no-align`**，`level1` / `cluster` 不问。
- 若通过 agent 以 skill 方式触发，agent 会在执行前先向你确认这几项（阈值 / 是否清理 / 是否 `no-align`）。按级别决定问哪些阈值：`cluster` 单文件**只问 `comm`**（且不问 `no-align`/`clean`）；`levelnone` **只问 `io` + `comm`**（`cpu` 用 io、`comm`/`pp_comm` 用 comm；`compute` 已跳过）；`level0`/`level1` 问 `compute`+`io`+`comm`。直接跑命令行时按上表传参即可。

### 3. 输入数据

`path` 可以是任意层级的父目录，脚本会递归查找 `ascend_pytorch_profiler_*.db`：

```
<path>/
├── ascend_pytorch_profiler_0.db
├── ascend_pytorch_profiler_1.db
├── analysis.db            # 可选：含 CommAnalyzerBandwidth 带宽 → level1
└── ...
```

也支持常见 dump 结构 `master_xxx_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_N.db`（空的 master db 会被跳过）；或直接传入单文件 cluster db（**文件名不限**，只要能被 sqlite 打开、含 `CommunicationGroupMapping` 与 `ClusterCommunicationBandwidth` 表且均有数据即可）。每张卡一个 profiler db。

**数据级别**（自动识别，决定 `comm` 的口径与检测范围）：

| 级别 | 触发条件 | 检测范围 |
|------|----------|----------|
| `level0` | 目录、`ascend_pytorch_profiler_*.db` 的 `TASK.taskType` 含 `KERNEL_AICORE`/`KERNEL_AIVEC`/`MEMCPY_ASYNC` | 全部 7 类 |
| `levelnone` | 目录、`TASK.taskType` 仅 `MsTx`（无计算/搬运算子） | **跳过** `KERNEL_AICORE`/`kernel_aivec`/`memcpy_async`；其余（`cpu`/`npu_bubble`/`comm`/`pp_comm`）照跑 |
| `level1` | profiler db 同目录有 `analysis.db` 且含 `CommAnalyzerBandwidth` 带宽 | 7 类，但 `comm` 用**新两级带宽口径**，且跳过 `pp_comm` |
| `cluster` | `path` 是单文件 db，含 `CommunicationGroupMapping` 与 `ClusterCommunicationBandwidth` 表 | 仅 `comm`（新两级带宽口径） |

### 4. 输出

**① stdout 逐类别汇总表（给 agent 的最终结论）**

检测结束时在 stdout 打印一张 ASCII 表，一行一个「有异常的类别」，列 `类别 | 异常卡 | 劣化指数 | 劣化阈值`。它渲染到**调用方 agent 的最终输出**，不写进任何 log 文件；无异常时打印含「无异常」的单行表。

```
+---------------+--------------+-------------------+----------+
| 类别          | 异常卡       | 劣化指数          | 劣化阈值 |
+---------------+--------------+-------------------+----------+
| KERNEL_AICORE | rank 0, 3    | 0:1.397，3:1.398  | 1.3x     |
+---------------+--------------+-------------------+----------+
| comm          | tp[0, 1]     | tp[0, 1]:2.5      | 1.3x     |
+---------------+--------------+-------------------+----------+
| pp_comm       | [0, 2, 4, 6] | [0, 2, 4, 6]:0.08 | >5%      |
+---------------+--------------+-------------------+----------+
| cpu           | worker3      | worker3:3.719     | 2.5x     |
+---------------+--------------+-------------------+----------+
```

- 「异常卡」列：单卡类 `rank 0, 3`、通信域 `tp[0, 1]`、PP 通信组 `[0, 2, 4, 6]`、CPU 节点 `worker3`。
- 「劣化指数」列：逐项 `项:值`。
- 「劣化阈值」列：计算/IO/通信类显示 `1.3x` / `2.5x`，`npu_bubble` 显示 `<5000ns`，`pp_comm` 显示 `>5%`。

**② 结果文件**

- `straggler_detection_result.json` — 结构化检测结果（按类别列出异常项与劣化指数）。
- `analysis_result/detection_report.log` — 各指标排序 / 通信带宽 / 统计信息的文本详情报告。

**③ 中间产物**：`op_metric/global_rank_N.csv` + `group_info_N.json`（解析产物；`clean=no` 可直接复用）。

---

## 二、检测原理

### 1. 总览

从每个 rank 的 `.db` 解析出单快照指标，对 **7 类指标**分别做异常检测，最终汇总成上面的那张表。（`levelnone` / `level1` 做完整 7 类；`cluster` 级别只有 `comm`。）

| 类别 | 指标 | 检测方式 |
|------|------|----------|
| `KERNEL_AICORE` | KERNEL_AICORE 算子平均时间 | 单卡，检测组内通用算法（偏大异常） |
| `kernel_aivec` | KERNEL_AIVEC 算子平均时间 | 单卡，检测组内通用算法（偏大异常） |
| `memcpy_async` | MEMCPY_ASYNC 算子平均时间 | 单卡，检测组内通用算法（偏大异常） |
| `cpu` | ZP_Host（通信 + Kernel 的 Host 耗时均值） | 按物理节点拉齐后通用算法（异常项 = 节点 hostName） |
| `npu_bubble` | ZP_Bubble（通信算子启动间隔） | 固定硬阈值 `< 5000ns` |
| `comm` | `{domain}_{opType}_{count}` 算子带宽 | 带宽聚类（min 方向，多 opType 交叉验证） |
| `pp_comm` | `PP_WaitGap`（点对点结束→下一次集合通信结束间隔） | 比较域（TP>CP>DP）内逐 rank 比较，按 PP 通信组上报 |

### 2. 通用异常算法（`kmeans_detector.general_anomaly_detection`）

所有「检测组内」的指标共用一套算法：过滤 `≤0 / -99999` → Z-score → 肘部法选 K → KMeans++ → 取偏大（或偏小）方向的异常簇（簇均值超过 / 低于基线×阈值）→ **异常簇递归细分**（对异常簇再次聚类，更深层异常替换父层、无异常保持父层，减少误检）。

**劣化指数**统一以**第一次 KMeans（全数据）的基线簇均值为分母**，同一刻度可比：偏大异常 `degradation = 值/基线`，偏小异常 `degradation = 基线/值`（均 > 1，越大越异常）。

阈值按类别组取：计算类 `compute`、IO/CPU 类 `io`、通信类 `comm`（默认 1.3 / 2.5 / 1.3），`npu_bubble` 固定 `< 5000ns`。

### 3. 检测组的选择

先按优先级（`tp → exp → ep → … → dp`）选一个命名通信域作为**检测组**。集群数据用完整分组、非集群按节点过滤；若没有任何命名通信域，则退化为按 `HOST_INFO.hostUid` 的**物理节点分组**（此时通信域组间指标 `comm` 直接跳过）。

### 4. 各类指标细节

**单卡类（KERNEL_AICORE / kernel_aivec / memcpy_async）**：在检测组内对每张卡的值做通用算法，偏大异常。

**cpu（ZP_Host）**：先按物理节点（`hostUid`）分组、组内去首尾求均值**拉齐**，再对整组做通用算法。因为同节点卡值被拉平，异常落在**整个节点**上，异常项用节点显示名（`hostName`）而非 rank。

**npu_bubble（ZP_Bubble）**：通信算子启动间隔小于 `5000ns` 记为异常（小值异常）。

**comm（慢通信域）**：分两套口径。

- **旧口径（`levelnone`）**：先把每次集合通信的**带宽**回填进 CSV —— 默认按 wall-clock 时间窗把每次 occurrence 跨 rank 对齐、取组内最短时长，再对全部 occurrence 的最短时长取**前 10% 均值**作分母，`带宽 = count / 该均值`；开启 `no-align` 则不做对齐、改用**全体 rank 算子时长的扁平池**取前 10% 均值。检测时每域每个 opType 每组建模：每组取 **count 最大**的条目作代表，保留 `count ≥ 最大×0.5 且 > 10240` 的组，用通用算法（**min 方向**，带宽越小越慢）聚类，**只有在该域所有 opType 上都异常的组**才上报，劣化指数取各 opType 最大值。
- **新两级口径（`level1` / `cluster`）**：数据来自 `analysis.db` 的 `CommAnalyzerBandwidth`（或 `cluster_analysis.db` 的 `ClusterCommunicationBandwidth`）。每 `(域, 算子类型, count)` 取**全体 rank 带宽的中位数**作基准；每 rank 指标 = 各 `(算子类型, count)` 上 `带宽 / 中位数` 的均值（≈1 正常、越小越慢）。分两级检测：**Level-1** 组内 kmeans（min 方向）→ 慢 rank（单卡键）；**Level-2** 各组指标均值 → 域内组间 kmeans → 慢组（组键）。两级结果都写进 `comm`。此口径不使用 `no-align`。

**pp_comm（PP 慢通信）**：PP 传输（点对点）单独检测。
- **前置跳过**：若本次已检出**计算类故障（`KERNEL_AICORE` / `kernel_aivec`）**，直接跳过 PP 检测 —— 因为 PP 统计的那段时间里必然包含计算算子，会被计算异常污染。
- 回填 `PP_WaitGap` = 每卡「点对点传输**结束** → 其**下一次集合通信结束**」的间隔之和（锚点：出现 `BatchSendRecv` 用 BatchSendRecv，否则用 `Send`/`Recv`；取其后第一个集合通信、跳过中间点对点；无下一次集合通信则不计）。会话时长取各 rank 合并 profiling 窗口的最大值。
- 检测时按**比较域**（命名域优先级 **TP > CP > DP**，都没有时用 **PP stage 转置**）把 rank 分组，域内**逐 rank** 与组内最大值比较：`(组内最大 − 本值) / 会话时长 > 5%` 判异常（**偏小异常**）。
- **上报单位为 PP 通信组**：组内至少一个 rank 异常即上报该组，劣化指数取组内异常 rank 的最大占比（不上报具体 rank）。

---

## 三、辅助脚本（实现说明）

以下脚本均在 `scripts/` 下，仅供了解实现，不影响使用：

```
config.py                 # 全局配置、阈值、劣化数据容器、节点/域名标志
main.py                   # 入口（skill 调用 run_detection / CLI）
profilingdataparse.py     # .db(SQLite) → op_metric CSV/JSON；带宽 / PP 等待回填
nodelevel.py              # 各维度慢节点检测核心逻辑
nodelevel_data_handler.py # 读 CSV/拓扑，构建快照与检测组
kmeans_detector.py        # 通用异常算法（KMeans + Z-score + 肘部法 + 异常簇递归细分）
summary_table.py          # 汇总表渲染（ASCII，供 stdout 输出）
markdown_viz.py / visualizer.py  # 文本详情报告（analysis_result/detection_report.log）
utils.py                  # 结果写入、清理、交互询问
```

## 四、依赖

Python 3.8+，仅标准库（`sqlite3`、`csv`、`json`、`os`、`logging`、`math`、`random`）。
