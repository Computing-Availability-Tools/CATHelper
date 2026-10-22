---
name: straggler-detection
description: Detect slow nodes (stragglers) in AI training/inference clusters from Ascend PyTorch Profiler .db files. Use when the user wants to run slow-node/亚健康 detection, parse ascend_pytorch_profiler_*.db files, identify slow computing cards (KERNEL_AICORE), slow communication domains (comm), slow CPU cards (cpu), NPU bubbles, or analyze compute/IO/communication metric breakdown across ranks. Performs KMeans + Z-score + elbow general anomaly detection over metric classes and produces a joint failure analysis report.
---

# straggler-detector

Slow Node Detection 算法，用于检测 AI 训练/推理集群中的慢节点（亚健康检测）。

本 skill 自带完整可运行代码包（`scripts/` 下的 `*.py`）。

## 触发条件

当用户需要：
- 执行慢节点 / 亚健康检测分析
- 解析 Ascend PyTorch Profiler 的 `.db` 文件
- 识别慢计算卡、慢通信域、慢 CPU 卡
- 看各卡在通信类 / 计算类 / IO 类指标下的耗时分布与异常

## 检测的 7 类指标

| 类别 | 指标列 | 检测方式 |
|---|---|---|
| `comm` | `{domain}_{opType}_{count}`（带宽） | 带宽聚类（min 方向，多 opType 交叉验证） |
| `pp_comm` | `PP_WaitGap`（点对点结束→下一次集合通信结束间隔） | 比较域（TP>CP>DP）内逐 rank，(组内最大−本值)/会话 > 5%；按 PP 通信组上报（取组内最大） |
| `KERNEL_AICORE` | `KERNEL_AICORE` | 检测组内 + 通用算法 |
| `kernel_aivec` | `KERNEL_AIVEC` | 检测组内 + 通用算法 |
| `memcpy_async` | `MEMCPY_ASYNC` | 检测组内 + 通用算法 |
| `npu_bubble` | `ZP_Bubble` | 单阈值 < 5000ns |
| `cpu` | `ZP_Host` | 节点对齐 + 通用算法 |

> 数据级别影响检测范围：`level0`/`level1` 做全部 7 类；`levelnone`（TASK 仅 `MsTx`，无计算/搬运算子）跳过 `KERNEL_AICORE`/`kernel_aivec`/`memcpy_async`；`cluster` 单文件仅 `comm`。

## 使用方法

### 正确运行目录

所有命令都应在**本 skill 的 `scripts/` 目录**（含 `main.py` 的目录）内执行：`cd "C:\Users\admin\Desktop\Projects\CATHelper\.agents\skills\straggler-detection\scripts"`。

### 命令行执行

```bash
python main.py path=/path/to/data compute=1.3 io=2.5 comm=1.3 clean=ask
# 可选：追加 no-align 关闭慢通信带宽的跨 rank 对齐
```

参数说明：
| 参数 | 说明 | 默认值 |
|------|------|--------|
| `path` | 数据目录，或 cluster db 单文件（文件名不限，含 `CommunicationGroupMapping`/`ClusterCommunicationBandwidth` 表即可）（必需） | - |
| `compute` | 计算类阈值（KERNEL_AICORE / kernel_aivec） | 1.3 |
| `io` | IO/CPU 类阈值（cpu / memcpy_async） | 2.5 |
| `comm` | 通信类阈值（comm / pp_comm） | 1.3 |
| `clean` | `yes`/`no`/`ask`：是否清理中间数据并重新解析 | `ask` |
| `no-align` | 关闭慢通信带宽的跨 rank 对齐（裸参，或 `no-align=true`） | 关（跨 rank 对齐） |

（`npu_bubble` 为固定硬阈值 `< 5000ns`，不询问。）

### 作为 Python 模块调用

```python
import main
result = main.run_detection("/path/to/data", compute=1.3, io=2.5, comm=1.3, clean="ask", no_align=False)
```

## 执行规则（重要）

### 必须在执行前询问用户

每次执行检测前，**必须依次询问以下参数**，等用户明确答复后再执行，**禁止跳过询问环节**：

1. **三组检测阈值**——**逐个询问**（一次只问一个阈值，禁止把多个阈值合成一个问题）：
   - 计算类阈值 `compute`（KERNEL_AICORE / kernel_aivec），固定候选：`1.3`（默认）
   - IO/CPU 类阈值 `io`（cpu / memcpy_async），固定候选：`2.5`（默认）
   - 通信类阈值 `comm`（comm / pp_comm），固定候选：`1.3`（默认）
   - `npu_bubble` 固定硬阈值 `< 5000ns`，不询问。
   - **候选项必须恒定**：同一阈值每次询问展示的候选项完全一致，不要临时增删变化。
   - **不要自行添加「其他」选项**：询问工具自带「输入其他内容」，自定义值走它即可。
   - 用户给出数值后，命令中加 `compute=<值> io=<值> comm=<值>`。
   - **按数据级别决定问哪几组**：`cluster`（单文件 db）只问 `comm`；`levelnone`（TASK 仅 MsTx）只问 `io` + `comm`（`cpu` 用 io、`comm`/`pp_comm` 用 comm，计算类已跳过）；`level0`/`level1` 问 `compute`+`io`+`comm`。
2. **是否删除已有数据**（op_metric 等中间文件）：
   - 用户选"删除" → 加 `clean=yes`
   - 用户选"保留" → 加 `clean=no`
   - **cluster db 单文件无需询问**：无解析步骤、无中间产物，输出文件（`straggler_detection_result.json` / `detection_report.log`）每次直接覆盖写，`clean` 对它无意义。
3. **是否关闭慢通信带宽的跨 rank 对齐**（`no-align`，默认**否**）——**仅 `levelnone` 数据需要问**；若输入是 cluster db 单文件或 `level1` 数据（用新两级带宽口径），**跳过此问**：
   - 用户选"关闭"（用全体 rank 算子时长的扁平池算带宽，适用于跨 rank 对齐失败的数据）→ 加 `no-align`
   - 用户选"不关闭"（默认，跨 rank 对齐后取组内最短时长）→ 不加

### 清理范围

若用户选删除，会清理：`op_metric/`、`straggler_analysis_output/`、`analysis_result/`、`straggler_detection_result.json`、`straggler_detection_result/`，然后从 `.db` 重新解析。

## 输入数据

`path` 可以是一个含多层子目录的父目录（`os.walk` 递归查找 `ascend_pytorch_profiler_*.db`），典型的：
```
<path>/
├── ascend_pytorch_profiler_0.db
├── ascend_pytorch_profiler_1.db
└── ...
```
或常见 dump 结构：
```
<path>/
├── master_xxx_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_0.db
├── master_yyy_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_1.db
└── ...
```

### verl colocate（训练 / rollout 混合采集，自动分离检测）

verl 混合部署时，一次采集会在同一节点产出**多组 worker*_ascend_pt 目录**（每组各自独立进程、一个 rank 一个 db）。此时训练（FSDP）与 rollout（vLLM/SGLang）的 rank 编号会重复（如都含 0~3），**绝不能混在一起检测**。

- 传入含同层 `worker*_ascend_pt` 的根目录，检测前会自动用 `classify_role.py` 分层判据（L1 backward 决定性 > L3 行为指纹 > L2 引擎词表）把 worker 分成训练 / rollout 两个"世界"，并做时间窗交叉验证。
- 每个世界**独立解析 + 独立检测**，结果分开输出：
  ```
  <path>/detection_output/training/    # 训练（FSDP）4 rank 完整检测
  <path>/detection_output/rollout/     # rollout（推理引擎）4 rank 完整检测
  ```
- 两世界各含自己的 `op_metric/`，避免 `global_rank_0.csv` 互相覆盖。
- 非 colocate 的普通数据目录不受影响，走原检测流程。

## 重要注意事项

- **禁止创建 `_db` / 软链接中转目录**：不要为了分类或去重创建任何中转目录；直接以原始数据目录作为输入。
- **输入与输出目录分离**：纯结果目录只放检测产物，绝不混入 db 原始数据。
- **空 / master db 处理**：部分 `master_*` 目录下有空的 master db（无 `STEP_TIME`/`PYTORCH_API`/`TASK` 表），应跳过，仅用含核心表的 rank db。
- **无通信域名时（情况 A，group_name 全空）的检测退化**：按 `HOST_INFO.hostUid` 物理节点分组（相同 hostUid 的 rank 为一组）作为检测组，检测计算/IO/通信单卡类指标；通信域组间指标（`comm`，`HasNamedDomain=False` 时）**直接跳过**（无域名无法解释对应 tp/ep）。Host 维持节点间拉齐，Bubble 维持固定阈值。
- **有命名域但未命中检测优先级（情况 B）**：检测组同样退化到物理节点分组，但通信域组间指标**仍然检测**（`HasNamedDomain=True`），检出慢通信组时可带域名。

## 输出

1. `op_metric/global_rank_*.csv` — 解析后的性能指标
2. `op_metric/group_info_*.json` — 并行域信息
3. `straggler_detection_result.json` — 检测结果（含全部类别）
4. `analysis_result/detection_report.log` — 可视化详情报告
5. **最终输出逐类别汇总表**（渲染到调用方 agent 的 stdout，不进任何 log 文件）——检测结束时在 stdout 打印一张 ASCII 框线表格，一行一个"有异常的类别"，列：`类别 | 异常卡 | 劣化指数 | 劣化阈值`（类别用 op_metric 指标列名，如 `KERNEL_AIVEC`/`MEMCPY_ASYNC`；劣化指数为 `项:值`，通信组如 `tp[0,1]:2.5`、PP 链路如 `0->4:2.5`）；无异常时打印含"无异常"提示的单行表。

## 算法说明

检测核心为 `kmeans_detector.py` 的 `general_anomaly_detection`：过滤 ≤0/-99999 → Z-score → 肘部法选 K → KMeans++ → 偏大方向异常簇（簇均值 > 基线×阈值）→ **异常簇递归细分**（对异常簇数据再次聚类，更深层异常替换父层、更深层无异常保持父层，减少误检；劣化指数统一用**第一次 KMeans（全数据）的基线簇均值**为分母，degradation = 异常值/第一次基线，同一刻度可比）。阈值分组：计算类（KERNEL_AICORE / kernel_aivec）= `compute`（默认 1.3），IO/CPU 类（cpu / memcpy_async）= `io`（默认 2.5），通信类（comm / pp_comm）= `comm`（默认 1.3）；`npu_bubble` 用固定阈值 `< 5000ns`。检测组由 `nodelevel.get_cal_detection_group` 按优先级（tp→exp→ep→…→dp）选定，集群数据用完整分组、非集群按节点过滤，无命名域时退化按 hostUid 物理节点分组。

**PP 慢通信（`pp_comm`）另一方案**：PP 传输（点对点）不在带宽白名单内，单独检测。**若已检出计算类故障（`KERNEL_AICORE`/`kernel_aivec`）则直接跳过**（v6pp 统计区间内必含计算算子，会被污染）。解析后回填每卡 `PP_WaitGap` = 「点对点传输结束 → 其下一次集合通信结束」的间隔之和（锚点：出现 `BatchSendRecv` 用 BatchSendRecv，否则用 Send/Recv；找其后第一个集合通信、跳过中间点对点；无下一次集合通信则不计）。检测时按**比较域**（命名域优先级 **TP > CP > DP**，都没有时用 **PP stage 转置**）把 rank 分组，域内**逐 rank** 与组内最大值比较：`(组内最大 − 本 rank) / 会话时长 > 5%` 判异常（**偏小异常**）。**上报单位为 PP 通信组**：组内至少一个 rank 异常即上报该组，取组内异常 rank 的最大占比（不上报具体 rank）。会话时长取各 rank 合并 profiling 窗口的最大值。

**PP 分组来源**：PP 组直接读 `parallel_group_info` 的 `pp` 项（`group_name=="pp"` 的 `global_ranks`），读不到则不检测。同一 PP 组内 rank 升序视为 stage 顺序；相邻两 stage 组成一条链路，两端点对点算子按传输序号（slot）对齐后取时间窗重叠作为该链路指标。
