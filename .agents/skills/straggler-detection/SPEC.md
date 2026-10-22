# straggler-detector 方案设计说明书（SPEC）

> 本文档逐模块阐述 `straggler-detector` skill 的完整方案：架构、数据流、核心算法、各指标的定义与生成方式、检测逻辑与结果输出。
> 检测核心为 `kmeans_detector.py` 的通用检测算法（KMeans + Z-score + 肘部法 + 异常簇递归细分）。

---

## 1. 目标与定位

检测 AI 训练集群中的慢节点（straggler / 亚健康）。基于**单次快照**（single snapshot）的多卡性能数据，识别：

- 慢计算卡（`KERNEL_AICORE`）
- 慢矢量/搬运（`kernel_aivec` / `memcpy_async`）
- 慢通信域（`comm`）
- 慢 CPU 卡（`cpu`）
- NPU 空泡（`npu_bubble`）

**输入**：Ascend PyTorch Profiler 生成的 `.db` 文件（每 NPU 一个）。
**输出**：
- `op_metric/global_rank_{N}.csv`（单快照性能指标）
- `op_metric/group_info_{N}.json`（并行域拓扑）
- `straggler_detection_result.json`（检测结果）
- `analysis_result/detection_report.log`（可视化详情报告）
- **最终输出逐类别汇总表**（ASCII 框线表格，渲染到调用方 agent 的 stdout，**不进任何 log 文件**）

---

## 2. 目录结构与模块职责

```
straggler-detection/
├── scripts/                    # 可运行代码包（本 skill 的调用入口，含 main.py）
│   ├── config.py               # 全局配置、阈值、劣化数据容器、节点映射、域名标志
│   ├── utils.py                # 结果写入、清理、交互式询问、通用工具
│   ├── kmeans_detector.py      # 通用检测算法（KMeans + Z-score + 肘部法 + 异常簇递归细分）
│   ├── profilingdataparse.py   # .db(SQLite) → op_metric CSV/JSON
│   ├── nodelevel.py            # 各维度慢节点检测核心逻辑
│   ├── nodelevel_data_handler.py   # 读取 op_metric CSV + group_info JSON，构建快照/并行域/节点组
│   ├── classify_role.py        # verl colocate：训练 / rollout 分层的角色判据
│   ├── markdown_viz.py         # 生成文本格式检测报告
│   ├── visualizer.py           # 可视化编排（控制台 + 详情报告）
│   ├── summary_table.py        # 检测结果汇总表工具（ASCII 表格渲染 + 单元格格式化）
│   └── main.py                 # 主入口 / skill 调用入口
├── SKILL.md                    # skill 使用说明（面向 agent）
└── README.md / SPEC.md         # 使用说明 / 本文档
```

---

## 3. 整体数据流

```
ascend_pytorch_profiler_{N}.db（每 NPU 一个）
        │
        ▼
[profilingdataparse.data_parsing]
  └── start_process → process_database（逐库）
        ├── META_DATA.parallel_group_info → group_info_{N}.json
        ├── HOST_INFO.hostUid → config.HostRankMap（内存，不落盘）
        ├── 聚合 STEP_TIME → 单条 aggregated step
        ├── time_diff_for_step() 计算各指标
        └── global_rank_{N}.csv（仅 1 条数据行）
        │
        ▼
[nodelevel_data_handler.get_cur_detection_info]
  ├── 遍历 group_info_*.json → parallels {域名: [[rank组]...]}
  ├── 设置 config.IsClusterData / config.HasNamedDomain
  └── 无命名域时 parallels[""] = _build_node_fallback_groups（按 hostUid 节点分组）
        │
        ▼
[nodelevel_data_handler.get_cur_job_last_step_data]
  └── 读 global_rank_{N}.csv → 单快照 {metric: {rank: value}}
      （多行取倒数第二行；单行取自身；跳过 StepIndex）
        │
        ▼
[nodelevel.delimit_detection]
  ├── detection_zp_bubble_data()             → npu_bubble
  ├── get_slow_calculate_ranks()             → KERNEL_AICORE（levelnone 跳过）
  ├── get_slow_metric_ranks() ×2             → kernel_aivec / memcpy_async（levelnone 跳过）
  ├── detect_slow_domain_by_bandwidth()      → comm（level0/levelnone，HasNamedDomain 时，旧口径）
  ├── detect_slow_comm_two_level()           → comm（level1/cluster，新两级带宽口径；跳过 PP）
  ├── detect_pp_slow_domain()                → pp_comm（点对点→集合通信间隔，逐 rank 比较）
  ├── get_slow_host_ranks_by_homogenize()    → cpu
  └── （step_data 为空，即 cluster 单文件时：只跑 detect_slow_comm_two_level，其余指标不检测）
        │
        ▼
[utils.write_result]                        → straggler_detection_result.json
[visualizer.run_visualization]              → analysis_result/detection_report.log
```

---

## 4. 配置模块（config.py）

| 全局 | 作用 |
|------|------|
| `FilePath` / `OutputPath` | 输入数据目录 / 输出目录（多 job 场景独立；为空回退到 FilePath） |
| `COMPUTE_THRESHOLD` / `IO_THRESHOLD` / `COMM_THRESHOLD` | 三组检测阈值，默认 1.3 / 2.5 / 1.3（运行时 `confirm_thresholds` 询问用户） |
| `BUBBLE_THRESHOLD_NS` | npu_bubble 固定硬阈值，5000ns（不询问） |
| `MAX_K` / `MAX_ITERATIONS` / `RECURSION_DEPTH` / `CONVERGENCE_EPS` | 算法参数：10 / 300 / 10 / 1e-9 |
| `ZP_BUBBLE_ABNORMAL_BOUNDARY` | desc 保留，实际 bubble 用 `BUBBLE_THRESHOLD_NS` |
| `IsClusterData` | 集群数据标志（Case A 集群 / Case B 非集群） |
| `HasNamedDomain` | 是否有命名通信域标志（决定通信域组间指标是否检测） |
| `HostRankMap` | `{rank: hostName/hostUid}`，解析阶段内存填充，**不生成文件** |
| `JobType` | Job 类型（training/rollout，由优化器更新算子判断） |

`set_thresholds(compute, io, comm)` 写入三组阈值到全局；`get_threshold_for_category(category)` 按类别返回所属组阈值（计算类 / IO-CPU 类 / 通信类）。`DegradationData`（继承 dict）：
- 结构 `{category: {key: value}}`，key 为单卡 `"0"` 或组 `"0,1,2"`。
- `add_single(category, rank, degradation)`：单卡。
- `add_group(category, ranks, degradation)`：组，按排序后 rank 集去重，保留最大劣化值。

---

## 5. 通用检测算法（kmeans_detector.py）—— 唯一异常检测算法

`general_anomaly_detection(ranks, values, anomaly_multiplier, ...)`，始终 **max 方向**（值偏大为异常）。返回 `(异常 rank 列表, 各异常 degradation 列表)`。

### 5.1 核心流程

1. 过滤 ≤0 及 `-99999`；过滤后不足 2 个 → 无异常退出。
2. Z-score 标准化；标准差 ≈ 0 → 强制置 1 避免除零。
3. **肘部法选最优 K**：K=2..min(n,MAX_K)，算 inertia（簇内平方和），取二阶差分最大者；退化为 2。
4. **KMeans++ 初始化质心**：首质心 = data[0]，后续 D² 加权随机采样（`random.Random(seed=42)` 保复现）。
5. **Lloyd 迭代** ≤MAX_ITERATIONS 轮：最近质心分配 → 质心 = 簇均值；空簇质心放到离其分配质心最远的样本；收敛 = 质心位移 < eps 且无分配变化。
6. **识别异常簇**：按原始值均值降序，基线 = 最小均值簇；簇均值 > 基线×倍率 → 该簇异常；从大到小遍历，遇第一个不满足即停止。
7. 无异常簇 → 无异常退出。
8. **异常簇递归细分**：以**异常簇的数据**为输入回到步骤 2（depth+1 ≤ max_depth）；更深层有异常 → 用更深层结果**替换**父层；更深层无异常 → 保持父层（向外排除边缘成员、减少误检）。
9. 劣化指数统一用**第一次 KMeans（全数据）的基线簇均值**作为分母，`degradation = 异常值 / 第一次基线`，使所有异常在同一刻度上可比。

> 与旧版差异：旧 homogeneous（spacedetector）是递归二分——本版对**异常簇数据**继续聚类细分，而非对剩余正常数据剥离。

### 5.2 阈值分组（skill 调用时询问用户）

- 计算类（`KERNEL_AICORE`, `kernel_aivec`）→ `COMPUTE_THRESHOLD`（默认 1.3）
- IO/CPU 类（`cpu`, `memcpy_async`）→ `IO_THRESHOLD`（默认 2.5）
- 通信类（`comm`, `pp_comm`）→ `COMM_THRESHOLD`（默认 1.3）
- `npu_bubble` → 固定硬阈值 `< 5000ns`

---

## 6. Profiling 数据解析（profilingdataparse.py）

### 6.1 表结构访问

| 表 | 用途 |
|----|------|
| `META_DATA` | `name='parallel_group_info'` 存 JSON 拓扑 |
| `STEP_TIME` | step 起止时间 |
| `TASK` | 算子执行，`taskType` → `STRING_IDS.value` |
| `STRING_IDS` | id ↔ 名称映射 |
| `COMMUNICATION_OP` | 通信算子（含 groupName、connectionId） |
| `CANN_API` / `PYTORCH_API` / `MSTX_EVENTS` | Host 端算子 |
| `HOST_INFO` | `hostUid` / `hostName`，节点归属（仅一行） |

### 6.2 聚合 step（关键设计）

`process_database` 将所有 step 合并为**单条聚合 step**：`start = min(startNs)`，`end = max(endNs)`。因此每张卡 CSV **只有 1 条数据行**。

### 6.3 指标生成（time_diff_for_step）

无数据/无通信域时用 `-99999` 标记或 `0`：

| 指标（CSV 列） | 生成方式 |
|----------------|----------|
| `StepDuration` | DB 数据总时间间隔 |
| `ZP_Device` | `StepDuration` − 通信总耗时（非通信时长） |
| `ZP_Duration` | 通信算子区间合并总时长 |
| `ZP_Host` | 通信算子 Host 耗时 + `KERNEL_AICORE` Host 耗时的均值（无通信算子时仍取 Kernel Host） |
| `ZP_Bubble` | `op.startNs − op.h_endNs`（>0）的均值 |
| `KERNEL_AICORE` | `AVG(endNs−startNs)`，`taskType='KERNEL_AICORE'` |
| `MEMCPY_ASYNC` / `KERNEL_AIVEC` | `AVG(endNs−startNs)`，`taskType=对应名`（参照 KERNEL_AICORE） |
| `HostDuration` | Host 端执行耗时均值 |
| `DataLoader` | `MSTX_EVENTS` 中 dataloader 事件的 `endNs−startNs` |
| `{xp}_Duration` / `{xp}_Count` | 每个并行域算子 `endNs−startNs` 的均值 / `count` 均值 |

> 算子类型（KERNEL_AICORE / KERNEL_AIVEC / MEMCPY_ASYNC）只能从 `TASK.taskType → STRING_IDS` 推断；CANN_API 等 Host 端算子是 API 函数名，无法归类为 kernel 类型。

### 6.4 CSV 落盘（write_results_to_csv）

表头：`StepIndex, StepDuration, ZP_Device, ZP_Duration, ZP_Host, ZP_Bubble, ZP_Count, KERNEL_AICORE, MEMCPY_ASYNC, KERNEL_AIVEC, HostDuration, DataLoader` + 各域 `{xp}_Duration, {xp}_Count`。

### 6.5 节点信息（get_host_info）

从 `HOST_INFO` 表取 **`hostUid`**（仅一行），调用 `config.set_host_rank_map(rank, host_uid)` 存入**内存**，供检测阶段按物理节点分组。不生成文件。`data_parsing` 开始时 `config.reset_host_rank_map()` 清空。

---

## 7. 数据读取与快照构建（nodelevel_data_handler.py）

- `get_cur_detection_info(job_path)`：
  - 遍历 `group_info_*.json`，收集 `valid_ranks` 与每卡拓扑。
  - 聚合所有 `group_name`，构建 `parallels {域名: [[rank组]...]}`，仅保留存在**多卡组**的域。
  - 设置 `config.set_is_cluster_data(...)` 与 `config.set_has_named_domain(any(name ...))`。
  - **无命名域**（`not get_has_named_domain()`）时：`parallels[""] = _build_node_fallback_groups(valid_ranks)`，不再用 `get_detection_job_parallel_info` 推导的空域名分组。
- `_build_node_fallback_groups(ranks)`：按 `config.HostRankMap`（hostUid）将同节点 rank 分为一组；组内 ≥2 卡才保留，单卡组丢弃/拆分；无法分组→`{}`。
- `get_cur_job_last_step_data(ranks)`：
  - 读每张卡 CSV，跳过 `StepIndex` 列。
  - 每个 `(metric, rank)` 时间序列：长度 >1 取**倒数第二个**（n-2），=1 取第一个。
  - 返回 `{metric: {rank: value}}` 单快照。

---

## 8. 检测逻辑（nodelevel.py）

常量列名：`ZP_Device`、`KERNEL_AICORE`（原 `ZP_Kernel`）、`ZP_Duration`、`ZP_Host`、`ZP_Bubble`、`DataLoader`、`MEMCPY_ASYNC`、`KERNEL_AIVEC`、`StepDuration`、`HostDuration`；`minRanksInGroup=2`。

### 8.1 检测组选择（get_cal_detection_group）

并行域优先级：`tp → exp → ep → tp_exp → cp → cp2 → cp_ulysses → cp_ring → dp → dp_cp → dp_modulo_exp_cp`。
- 命中优先级域：集群数据（Case A）→ 完整集群分组；非集群（Case B）→ `get_detection_groups` 过滤为本地节点卡。
- 空域名 `""` 分支：用 `parallels[""]`（节点回退分组）。
- **未命中任何优先级域（情况 B）**：不再 `return "", []` 短路，回退 `_build_node_fallback_groups` 节点分组，保证单卡指标仍能检测。

### 8.2 慢计算卡 KERNEL_AICORE（get_slow_calculate_ranks / det_cal_for_one_group）

对每个检测组用 `KERNEL_AICORE`（方向 max），进入通用算法，结果写入 `KERNEL_AICORE`。

### 8.3 KERNEL_AIVEC / MEMCPY_ASYNC（get_slow_metric_ranks / det_metric_for_one_group）

`GENERAL_METRIC_CATEGORIES`：`(KERNEL_AICORE, KERNEL_AICORE)`、`(KERNEL_AIVEC, kernel_aivec)`、`(MEMCPY_ASYNC, memcpy_async)`。KERNEL_AICORE 已单独检测跳过，其余两列用检测组 + 通用算法，方向 max。

### 8.4 NPU 空泡 npu_bubble（detection_zp_bubble_data）

排除 -99999 与 ≤0；`value < BUBBLE_THRESHOLD_NS`（默认 5000ns）记异常，写入 `npu_bubble`（小值异常）。

### 8.5 慢通信域带宽检测（detect_slow_domain_by_bandwidth）

数据来源：解析后带宽回填（`profilingdataparse.backfill_slow_domain_bandwidth`）写进 CSV 的动态列 `<domain>_<opType>_<count>`。**带宽分母**默认（跨卡对齐）：按 wall-clock 时间窗把每次集合通信 occurrence 跨 rank 对齐，每个 occurrence 取**组内最短时长**，再对全部 occurrence 的最短时长升序取**前 10% 均值**，`带宽 = count / 该均值`；传 `no-align` 时改为**不做跨卡对齐**：把组内**全体 rank** 符合条件的算子时长按 `(opType,count)` 汇成扁平池，直接取池内前 10% 均值作分母。

检测规则：
- 遍历每个集合通信域（跳过 `pp` / `embd`，组数 <2 跳过）。
- 对每个 opType：每组取 count 最大的条目作代表，保留 `count >= max×0.5` 且 `> SLOW_COMM_COUNT_FLOOR(10240)` 的组；用通用检测（**min 方向**，带宽越小越慢）聚类代表带宽，阈值 = `COMM_THRESHOLD`（默认 1.3）。
- 只报告在每个 opType 上都异常的组，劣化指数取各 opType 最大值，写入 `comm`（组键，`display_key` 带域名）。

**守卫**：`config.get_has_named_domain()` 为假（情况 A，无命名通信域）→ 无带宽列，直接跳过。

### 8.6 慢 CPU 卡 cpu（get_slow_host_ranks_by_homogenize）

- 收集 `ZP_Host` 有效值（排除 -99999）。
- `process_cpu_data_by_node`：按**物理节点**（`config.HostRankMap`，hostUid）分组，组内去首尾后求均值覆盖组内卡值，并返回每个 rank 的节点显示名；无节点映射时回退按 4 卡分组（节点名用 `rank0-3` 区间）。
- 通用算法 max；因同一节点组内卡值相同，异常按**节点归并成一项**，写入 `cpu`，key 用节点显示名（`config.get_node_name`，优先 `hostName`，退化 hostUid / `rank{n}`）。

### 8.7 PP 慢通信 pp_comm（detect_pp_slow_domain）

PP 传输（点对点）不在带宽白名单内，单独检测。PP 组取自 `parallel_group_info` 的 pp 项；读不到时退回按比较域（见下）。

数据来源：解析后回填（`profilingdataparse.backfill_pp_wait`）写进 CSV 动态列 `PP_WaitGap` = 每卡「点对点传输**结束** → 其**下一次集合通信结束**」的间隔之和（无下一次集合通信则不计）。锚点：batch 模式（PP 链路上出现 `BatchSendRecv`）下为 `BatchSendRecv`；否则为 `Send` / `Recv`（找其后第一个集合通信，跳过中间连续的点对点）。会话时长 `config.PP_SESSION_NS` = 各 rank 合并 profiling 窗口 `end−start` 的最大值（解析时填充）。

检测规则（`detect_pp_slow_domain` → `_detect_pp_rank_ratio`）：
- **前置跳过**：若本次已检出计算类故障（`KERNEL_AICORE` / `kernel_aivec` 异常），直接跳过 PP 检测——v6pp 统计的「点对点结束 → 下一次集合通信结束」区间内**必含计算算子**，会被计算异常污染。
- **比较域**（`_pp_compare_groups`）：命名域优先级 **TP > CP > DP**（有 `tp` 用 `tp` 分组，否则 `cp`，否则 `dp`）；都没有时用 **PP stage 转置**（各 PP 组同一 stage 位置的成员归一组）。
- 域内**逐 rank** 与组内最大值比较：`score = (组内最大时间 − 本 rank 时间) / 会话时长`；`> 5%` 判异常（**偏小异常**）。
- **上报单位为 PP 通信组**：把异常 rank 归到其所属 PP 组（`parallels["pp"]`），组内至少一个 rank 异常即上报该组，劣化指数取组内异常 rank 的最大 score（不上报具体 rank）。
- 异常项是 PP 组（`add_group`），汇总表「异常卡」列显示 `[0, 2, 4, 6]`，劣化阈值显示 `>5%`。

---

## 9. 结果输出

### 9.1 JSON（utils.write_result）

`straggler_detection_result.json`：
```json
{
  "KERNEL_AICORE": [{"display_key":"0","metric_value":1.5,"is_abnormal":true}],
  "comm": [{"display_key":"tp[0, 1]","metric_value":1.8,"is_abnormal":true}],
  "cpu": [...], "npu_bubble": [...],
  "kernel_aivec": [...], "memcpy_async": [...]
}
```
始终包含全部 6 类（空则 `[]`）；非 bubble 降序，bubble 升序；comm 的 `display_key` 带域名。

### 9.2 清理（utils）

`clean_detection_outputs` 清理：`op_metric`、`straggler_analysis_output`、`analysis_result`、`straggler_detection_result.json`、`straggler_detection_result`、`joint_failure_analysis.log`。
`confirm_clean` 交互式询问（skill 调用时**须先问用户**）。

---

## 10. 检测结果汇总（summary_table.py）

`summary_table.py` 只保留 ASCII 框线表的底层渲染（`_render_box_table`）与「检测结果 → 表格单元格」的格式化（`_summary_cells`），供控制台最终汇总表与文本报告摘要复用。早期版本的「故障联合分析」（硬件流水线因果推断、传播链、根因卡判定、`joint_failure_analysis.log`）已移除。

### 10.1 类别与指标映射

| 类别 | 单卡指标列 | 检测方式 / 异常方向 |
|------|-----------|----------|
| `KERNEL_AICORE` | `KERNEL_AICORE` | 单卡 / 大值 |
| `kernel_aivec` | `KERNEL_AIVEC` | 单卡 / 大值 |
| `memcpy_async` | `MEMCPY_ASYNC` | 单卡 / 大值 |
| `cpu` | `ZP_Host` | 节点级（key=hostName）/ 大值 |
| `npu_bubble` | `ZP_Bubble` | 单卡 / 小值（固定 <5000ns） |
| `comm` | `{domain}_{opType}_{count}`（带宽） | 通信域组级别 |

### 10.2 最终输出逐类别汇总表（build_summary_table）

`build_summary_table(result, parallels=None, step_data=None) -> str`：生成 **ASCII 框线表格**（`_render_box_table`，按列宽 + CJK 显示宽度自动对齐），一行一个"有异常的类别"，**由 `main.py` 经 `_safe_print` 打印到调用方 agent 的 stdout，不进任何 log 文件**。表头：`类别 | 异常卡 | 劣化指数 | 劣化阈值`。

- **类别**：`CATEGORY_DISPLAY`（大小写对齐 op_metric 指标列名），如 `KERNEL_AICORE`、`KERNEL_AIVEC`、`MEMCPY_ASYNC`、`comm`、`pp_comm`、`cpu`、`npu_bubble`（无括号描述）。
- **异常卡**：由 result 各 key 解析——单卡 `rank 0, 3`；comm 组 `tp[0, 1]`；pp_comm 链路 `0->4`。
- **劣化指数**：逐项 `项:值`（自然精度，`g` 格式）——单卡 `0:1.397，3:1.398`；comm `tp[0, 1]:2.5`；pp_comm `0->4:2.5`。
- **劣化阈值**：`npu_bubble` → `<{BUBBLE_THRESHOLD_NS}ns`；通信类（comm / pp_comm）→ `COMM_THRESHOLD`；IO/CPU 类（cpu / memcpy_async）→ `IO_THRESHOLD`；计算类（KERNEL_AICORE / kernel_aivec）→ `COMPUTE_THRESHOLD`。
- 无任何异常时返回含"无异常"提示的单行表。

---

## 11. 可视化（visualizer.py + markdown_viz.py）

- `visualizer.run_visualization`：控制台实时反馈 + 调用 `markdown_viz.write_report` 生成 `analysis_result/detection_report.log`。
- `markdown_viz`：文本报告，含指标排序柱状图、异常卡高亮、统计信息、各域集合通信带宽排序、各域带宽概览。其中 `cpu`（ZP_Host）段按**物理节点**聚合展示（一行一个节点、标签列显示 hostName、节点值取去首尾均值、异常节点标 `***`）；无节点映射时退回逐卡。
- **`pp_comm 占比排序` 段**（控制台 + 报告）：按比较域分段、逐 rank 列出 `PP等待 / ΔPP等待 / Δ占比`（`ΔPP等待 = 域内最大 PP等待 − 本卡`；`Δ占比 = ΔPP等待 / 会话时长`），柱状图绘制 **Δ占比**，`> 5%` 标 `***`（Δ占比越大越异常）。
- **`慢通信域（两级带宽口径）` 段**（`level1` / `cluster`）：优先用检测时记录的**逐 rank / 逐组指标明细**（`config.get_comm_two_level_detail()`，检测时由 `detect_slow_comm_two_level` 写入内存）做**全量绘制** —— Level-1 按**组**列出（每组内逐 rank、组内按指标升序）、Level-2 按**域**列出（域内逐组、升序），画出**全部** rank / 组并画柱状图（指标 = 各 `(算子类型,count)` 上 `带宽/中位基准` 的均值，≈1 正常、越小越慢），异常项标 `***`；拿不到明细时退回只列异常项。`levelnone` 仍按 `step_data` 带宽列渲染各域排序 + 概览。`cluster` 分支也会生成该报告（只含本段）。

---

## 12. 主入口（main.py）

- CLI：`python main.py path=<dir> [compute=1.3] [io=2.5] [comm=1.3] [no-align] [clean=ask|yes|no]`。
- `run_detection(input_path, compute=1.3, io=2.5, comm=1.3, skip_parsing=False, clean='ask', no_align=None)`：skill 调用入口，返回 `{category: {key: degradation}}`。
- 流程：确认阈值（计算/IO/通信）→ 清理/解析 → 获取并行域与有效 ranks → 取最新 step 快照 → `delimit_detection` → `write_result` → `run_visualization` → `build_summary_table`（`_safe_print` 打印到 stdout，失败仅告警不中断）。

---

## 13. 关键设计要点与约定

1. **单快照**：不跨 step 做时间序列分析；CSV 只落 1 条聚合数据。
2. **倒数第二点**：多行 CSV 取 n-2 行，规避最后一行不完整。
3. **无效标记 `-99999`**：贯穿解析、读取、各检测函数，用于跳过缺失数据。
4. **统一异常算法**：`kmeans_detector.general_anomaly_detection`（KMeans + Z-score + 肘部法 + 异常簇递归细分），唯一参数为阈值（由类别组决定，skill 调用时询问用户）。
5. **异常簇递归细分**：对异常簇数据再次聚类，**更深层异常替换父层、更深层无异常保持父层**（减少误检）；**劣化指数**统一用第一次 KMeans（全数据）的基线簇均值作分母，分子是异常值本身，同一刻度可比。
6. **阈值分组**：计算类（KERNEL_AICORE / kernel_aivec）= `COMPUTE_THRESHOLD`（默认 1.3）；IO/CPU 类（cpu / memcpy_async）= `IO_THRESHOLD`（默认 2.5）；通信类（comm / pp_comm）= `COMM_THRESHOLD`（默认 1.3）；`npu_bubble` 固定 `< 5000ns`。三组阈值在 skill 调用时询问用户。
7. **7 类指标**：`KERNEL_AICORE`, `kernel_aivec`, `memcpy_async`, `npu_bubble`, `cpu`, `comm`, `pp_comm`。
8. **无命名域退化（情况 A）**：检测组按 hostUid 物理节点分组；通信域组间指标直接跳过；单卡指标在节点组内检测。
9. **未命中优先级（情况 B）**：检测组同样退化到物理节点分组，但通信域组间指标仍检测（HasNamedDomain=True），检出慢通信组时可带域名。
10. **CPU/节点分组**：使用内存 `config.HostRankMap`（源自 `HOST_INFO.hostUid`），不落盘文件；无映射时回退按 4 卡。异常项 key 用节点显示名（`config.get_node_name`，优先 `hostName`），汇报时经 `config.get_node_ranks_map()` 反查该节点的 rank 集合。
