"""
Node Level 检测模块
慢节点检测核心逻辑
"""

import json
import os
import logging
import statistics
from typing import Dict, List, Tuple, Optional, Any
from collections import defaultdict

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import kmeans_detector
import utils
import nodelevel_data_handler  # 供 get_cal_detection_group 无命名域/未命中优先级时节点分组回退

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("[SLOWNODE ALGO]")

# 常量定义
minRanksInGroup = 2
zpDeviceColumn = "ZP_Device"
zpKernelColumn = "KERNEL_AICORE"
zpDurationColumn = "ZP_Duration"
zpHostDataColumn = "ZP_Host"
zpBubbleColumn = "ZP_Bubble"
dataLoaderDataColumn = "DataLoader"
ppParallelDomainName = "pp"
memcpyAsyncColumn = "MEMCPY_ASYNC"
kernelAivecColumn = "KERNEL_AIVEC"

# 通用检测（指标4-10）使用"选定的检测组"运行的指标列 → 类别映射
# 单卡级别检测，进入 kmeans_detector.general_anomaly_detection
GENERAL_METRIC_CATEGORIES = [
    (zpKernelColumn, "KERNEL_AICORE"),  # 指标7：KERNEL_AICORE 计算
    (kernelAivecColumn, "kernel_aivec"),  # 指标6：KERNEL_AIVEC
    (memcpyAsyncColumn, "memcpy_async"),  # 指标9：MEMCPY_ASYNC
]


def delimit_detection(
    step_data: Dict[str, Dict[int, float]],
    parallels: Dict[str, List[List[int]]],
    valid_ranks: List[int],
    comm: Optional[Dict[Any, Dict[int, float]]] = None,
    comm_threshold: Optional[float] = None,
) -> Dict[str, Dict[str, float]]:
    """
    单次定界检测（最新检测方式）

    参数:
        step_data: 单个 step 的快照数据，如 {"ZP_Device": {0: 1.66e9, 1: 1.67e9, ...}, ...}
        parallels: 并行域信息，如 {"tp": [[0,1], [2,3], ...], "pp": [[0,8], ...]}
        valid_ranks: 有效 rank 列表
        comm: level1/cluster 带宽数据 {(域,算子类型,count): {rank: bw}}；
              非空时用新两级带宽口径**替换**原 comm 检测（含跳过 PP 慢通信）
        comm_threshold: 新两级口径的 kmeans 阈值；None 时用 config.get_comm_threshold()

    step_data 为空（cluster 单文件 cluster_analysis.db）时，只做 comm（新两级口径），
    不做通信以外的任何指标检测。

    返回:
        检测结果：{"KERNEL_AICORE": {"0": 1.5}, "comm": {"0,1": 1.8}, "cpu": {"5": 2.1}}
    """
    local_result = config.DegradationData()

    # cluster（输入是单文件 cluster_analysis.db）：没有单卡快照，只有通信带宽数据，
    # 不做通信以外的任何指标检测，直接用新两级带宽口径检测 comm。
    if not step_data:
        logger.info("[INFO] 无单卡快照数据（cluster 单文件）：仅检测通信域")
        if comm:
            logger.info("\n慢通信域检测（新两级带宽口径，cluster）:")
            detect_slow_comm_two_level(parallels, comm, local_result, comm_threshold)
        return dict(local_result)

    # 以下检测需要单卡快照：先按优先级获取检测组和对应的并行域名
    cal_detection_group_name, cal_detection_group = get_cal_detection_group(parallels, valid_ranks)

    # 检查检测组是否有效（注意：并行域名称可能为空字符串 ""）
    if not cal_detection_group or (cal_detection_group_name is None and cal_detection_group != []):
        logger.warning("获取检测组失败")
        return {}

    # ===== 指标 11：BubbleTime，单阈值检测（<5000ns，沿用） =====
    detection_zp_bubble_data(step_data.get(zpBubbleColumn, {}), local_result)

    # ===== 指标 7 / 6 / 9：KERNEL_AICORE / KERNEL_AIVEC / MEMCPY_ASYNC =====
    # levelnone（TASK 仅 MsTx）没有这些计算/搬运算子 → 直接跳过。
    if config.get_profiling_level() == "levelnone":
        logger.info("[SKIP] levelnone（TASK 仅 MsTx）：跳过 KERNEL_AICORE / KERNEL_AIVEC / MEMCPY_ASYNC 检测")
    else:
        # ===== 指标 7：慢计算卡 KERNEL_AICORE（原 cal） =====
        logger.info("\n慢计算卡检测 (KERNEL_AICORE):")
        get_slow_calculate_ranks(cal_detection_group, step_data, cal_detection_group_name, local_result)

        # ===== 指标 4-10：使用选定的检测组，进入通用检测算法 =====
        # 指标 6/9：KERNEL_AIVEC / MEMCPY_ASYNC
        logger.info("\n通用检测（选定的检测组）:")
        for column, category in GENERAL_METRIC_CATEGORIES:
            if column == zpKernelColumn:
                continue  # KERNEL_AICORE 已单独检测
            get_slow_metric_ranks(cal_detection_group, step_data, column, category, local_result)
            logger.info(f"  - {column} -> {category}")

    # ===== 慢通信域检测（带宽聚类，comm） =====
    # level1 / cluster：有 analysis.db 的带宽数据 → 用新两级带宽口径（域内 (算子类型,count)
    # 中位带宽归一，min 方向），两级结果都写进 comm，并跳过 PP 慢通信检测。
    # levelnone / level0：无命名通信域（情况 A）时无带宽列可检测，直接跳过；
    # 否则按域组带宽做 min 方向聚类（情况 B / 正常数据）。
    if comm:
        logger.info("\n慢通信域检测（新两级带宽口径，level1/cluster）:")
        detect_slow_comm_two_level(parallels, comm, local_result, comm_threshold)
    elif config.get_has_named_domain():
        logger.info("\n慢通信域检测（带宽聚类）:")
        detect_slow_domain_by_bandwidth(parallels, step_data, local_result)

        # 计算类故障（KERNEL_AICORE / KERNEL_AIVEC）会污染 PP 等待区间
        # （v6pp 统计的「点对点结束 → 下一次集合通信结束」区间内必含计算算子），
        # 故一旦出现计算类异常，直接跳过 PP 慢通信检测。
        if local_result.get("KERNEL_AICORE") or local_result.get("kernel_aivec"):
            logger.info("[SKIP] 存在计算类故障（KERNEL_AICORE / kernel_aivec），跳过 PP 慢通信检测")
        else:
            logger.info("\nPP 流水线慢通信检测:")
            detect_pp_slow_domain(parallels, step_data, local_result)
    else:
        logger.info("[SKIP] 无通信域名，跳过慢通信域检测（comm）")

    # ===== CPU 资源卡检测（ZP_Host，集群整体拉齐） =====
    logger.info("\nCPU 资源卡检测（集群整体拉齐）:")
    get_slow_host_ranks_by_homogenize(valid_ranks, step_data.get(zpHostDataColumn, {}), local_result)

    return dict(local_result)


def get_detection_groups(tp_ranks: List[List[int]], node_global_rank: List[int]) -> Optional[List[List[int]]]:
    """
    通过并行域和当前节点侧任务级卡信息，获取检测组

    将 TP 域中的 rank 过滤，只保留本地节点上的 rank
    """
    rank_map = {rank: True for rank in node_global_rank}

    if not tp_ranks:
        logger.warning("[SLOWNODE ALGO] unexpected empty detection groups!")
        return None

    detection_groups = []
    for sub_rank_list in tp_ranks:
        valid_ranks = [rank for rank in sub_rank_list if rank in rank_map]
        if valid_ranks:
            detection_groups.append(valid_ranks)

    return detection_groups


def get_slow_calculate_ranks(
    detection_groups: List[List[int]],
    aligned_data: Dict[str, Dict[int, float]],
    detection_parallel: str,
    local_result: config.DegradationData
) -> bool:
    """
    获取通信域中的慢计算卡
    """
    if not aligned_data or (len(aligned_data.get(zpDeviceColumn, {})) == 0 and
                            len(aligned_data.get(zpKernelColumn, {})) == 0):
        logger.warning("[SLOWNODE ALGO] empty aligned ZP_device map data")

    # 注意：detection_parallel 可能为空字符串 ""（无命名并行域场景）
    if detection_parallel is None:
        logger.warning("[SLOWNODE ALGO] unexpected detection parallel name!")

    for npu_group in detection_groups:
        abnormal_ranks, rank_deg_severitys = det_cal_for_one_group(aligned_data, npu_group)

        for i in range(min(len(abnormal_ranks), len(rank_deg_severitys))):
            rank = abnormal_ranks[i]
            degradation = rank_deg_severitys[i]
            local_result.add_single("KERNEL_AICORE", rank, degradation)

    return True


def det_cal_for_one_group(
    aligned_data: Dict[str, Dict[int, float]],
    npu_group: List[int]
) -> Tuple[List[int], List[float]]:
    """
    对单个检测组进行慢计算检测

    统一使用 KERNEL_AICORE 计算耗时，方向为 "max"（偏大异常），
    进入通用检测算法（kmeans_detector.general_anomaly_detection）。
    排除 0 和 -99999 标记的无效数据。
    """
    # 收集有效数据（排除 0 和 -99999）
    col = zpKernelColumn
    values = []
    ranks = []
    for npu_id in npu_group:
        val = aligned_data.get(col, {}).get(npu_id, 0)
        if val != 0 and val != -99999:
            values.append(val)
            ranks.append(npu_id)

    if len(ranks) < minRanksInGroup:
        return [], []

    # 调用通用检测算法（新核心），cal 属计算类 → 用计算类阈值
    return kmeans_detector.general_anomaly_detection(
        ranks, values, config.get_compute_threshold()
    )


def det_metric_for_one_group(
    aligned_data: Dict[str, Dict[int, float]],
    npu_group: List[int],
    column: str,
    multiplier: float,
) -> Tuple[List[int], List[float]]:
    """
    对单个检测组进行指定指标的慢卡检测
    参照 det_cal_for_one_group 的检测逻辑（KERNEL_AICORE 生成方式）
    排除 0 和 -99999 标记的无效数据，进入通用检测算法（max 方向）
    """
    values = []
    ranks = []
    for npu_id in npu_group:
        val = aligned_data.get(column, {}).get(npu_id, 0)
        if val != 0 and val != -99999:
            values.append(val)
            ranks.append(npu_id)

    if len(ranks) < minRanksInGroup:
        return [], []

    # 通用检测算法（新核心），倍率由 category 决定（memcpy_async 用通信类倍率）
    return kmeans_detector.general_anomaly_detection(
        ranks, values, multiplier
    )


def get_slow_metric_ranks(
    detection_groups: List[List[int]],
    aligned_data: Dict[str, Dict[int, float]],
    column: str,
    category: str,
    local_result: config.DegradationData
) -> bool:
    """
    对指定指标列（MEMCPY_ASYNC/KERNEL_AIVEC 等）检测慢卡
    参照 get_slow_calculate_ranks 的逻辑：在 cal 检测组内逐个组做齐次化聚类
    """
    multiplier = config.get_threshold_for_category(category)
    for npu_group in detection_groups:
        abnormal_ranks, rank_deg_severitys = det_metric_for_one_group(
            aligned_data, npu_group, column, multiplier)

        for i in range(min(len(abnormal_ranks), len(rank_deg_severitys))):
            rank = abnormal_ranks[i]
            degradation = rank_deg_severitys[i]
            local_result.add_single(category, rank, degradation)

    return True


def detection_zp_bubble_data(npu_data: Dict[int, float], local_result: config.DegradationData):
    """
    检测 ZP bubble

    bubble < BUBBLE_THRESHOLD_NS（5000ns）视为异常
    排除 -99999 和 ≤0 的无效数据
    """
    if not npu_data:
        return

    for npu_id, value in npu_data.items():
        # 排除 -99999 标记的无效数据
        if value == -99999:
            continue
        # 排除 ≤0 的数据（数据缺失）
        if value <= 0:
            continue
        if value < config.BUBBLE_THRESHOLD_NS:
            local_result.add_single("npu_bubble", npu_id, value)


def _trimmed_mean(group_data: List[float]) -> float:
    """组内去最大值、最小值后求均值；不足 3 个时直接求均值。"""
    if len(group_data) > 2:
        sorted_data = sorted(group_data)
        trimmed_data = sorted_data[1:-1]  # 去掉最小和最大
        return sum(trimmed_data) / len(trimmed_data)
    return sum(group_data) / len(group_data)


def process_cpu_data_by_node(
    have_data_ranks: List[int],
    ranks_data: List[float]
) -> List[str]:
    """
    按物理节点分组计算组内均值并覆盖原值，返回每个 rank 对应的**节点显示名**列表
    （与 have_data_ranks 一一对应）。

    节点分组用内存 config.HostRankMap（解析阶段从 HOST_INFO 表填充，hostUid）；
    节点显示名用 config.get_node_name（优先 hostName）。无节点映射时回退按 4 卡分组，
    节点名用 rank 区间（如 "rank0-3"）。
    """
    node_map = config.get_host_rank_map()
    labels = [None] * len(have_data_ranks)

    if not node_map:
        # 回退：每 4 卡一组
        n = len(have_data_ranks)
        i = 0
        while i < n:
            end = min(i + 4, n)
            mean = _trimmed_mean(ranks_data[i:end])
            grp = have_data_ranks[i:end]
            label = f"rank{grp[0]}-{grp[-1]}" if len(grp) > 1 else f"rank{grp[0]}"
            for k in range(i, end):
                ranks_data[k] = mean
                labels[k] = label
            i = end
        return labels

    # 按节点分组：收集每个节点下的卡索引
    node_groups = {}
    for i, rank in enumerate(have_data_ranks):
        uid = node_map.get(str(rank)) or f"rank{rank}"
        node_groups.setdefault(uid, []).append(i)

    for idxs in node_groups.values():
        mean = _trimmed_mean([ranks_data[i] for i in idxs])
        label = config.get_node_name(have_data_ranks[idxs[0]])
        for i in idxs:
            ranks_data[i] = mean
            labels[i] = label
    return labels


def get_slow_host_ranks_by_homogenize(
    npus: List[int],
    detection_data: Dict[int, float],
    local_result: config.DegradationData
) -> List[int]:
    """
    获取慢 CPU ranks
    排除 -99999 标记的无效数据
    """
    have_data_ranks = []
    ranks_data = []

    for npu in npus:
        if npu in detection_data:
            val = detection_data[npu]
            # 排除 -99999 标记的无效数据
            if val != -99999:
                have_data_ranks.append(npu)
                ranks_data.append(val)

    # 按物理节点分组计算组内均值（取代固定按 4 分组）—— 集群整体拉齐
    labels = process_cpu_data_by_node(have_data_ranks, ranks_data)

    # cpu 属 IO/CPU 类阈值
    abnormal_ranks, rank_deg_severitys = kmeans_detector.general_anomaly_detection(
        have_data_ranks, ranks_data, config.get_io_threshold()
    )

    # 同一节点内所有卡值相同（组内拉齐），按节点归并成一项，key 用节点显示名（hostName）
    label_of_rank = {r: labels[i] for i, r in enumerate(have_data_ranks)}
    node_deg = {}
    for i in range(min(len(abnormal_ranks), len(rank_deg_severitys))):
        rank = abnormal_ranks[i]
        degradation = rank_deg_severitys[i]
        label = label_of_rank.get(rank, f"rank{rank}")
        if label not in node_deg or degradation > node_deg[label]:
            node_deg[label] = degradation

    for label, degradation in node_deg.items():
        local_result.add_single("cpu", label, degradation)

    return abnormal_ranks


def get_cal_detection_group(
    parallels: Dict[str, List[List[int]]],
    cur_npus: List[int]
) -> Tuple[str, List[List[int]]]:
    """
    选择用于检测的并行域，以及对应的检测组

    优先级：tp → exp → ep → tp_exp → cp → cp2 → cp_ulysses → cp_ring → dp → dp_cp → dp_modulo_exp_cp

    集群数据（Case A）：返回优先级域的完整集群分组，不做节点过滤；
    非集群数据（Case B）：返回按本地节点过滤后的分组，或 "" 节点回退分组。
    """
    if not parallels or not cur_npus:
        return "", []

    is_cluster = config.get_is_cluster_data()

    # 并行域检测优先级（按优先级从高到低）。
    # 末尾的 default_group 是兜底：level1 数据只有 default_group（整组），
    # 不在前面的命名域优先级里，补上后可直接当整组检测，无需退到按 hostUid 的
    # 节点回退（节点回退依赖解析阶段填充的 host_rank_map，clean=no 时为空）。
    detection_priority = [
        "tp", "exp", "ep", "tp_exp", "cp", "cp2", "cp_ulysses", "cp_ring",
        "dp", "dp_cp", "dp_modulo_exp_cp", "default_group"
    ]

    for domain in detection_priority:
        if domain in parallels and parallels[domain]:
            parallel_info = parallels[domain]
            if not parallel_info:
                continue

            logger.info(f"[SLOWNODE ALGO] use {domain} parallel detection: {parallel_info}")
            if is_cluster:
                # Case A：完整集群分组，不做节点过滤
                detection_groups = list(parallel_info)
            else:
                detection_groups = get_detection_groups(parallel_info, cur_npus)
            return domain, detection_groups

    # 如果并行域名称为空字符串（无命名并行域场景，情况 A），使用空字符串作为 key 的节点分组
    if "" in parallels and parallels[""]:
        parallel_info = parallels[""]
        if parallel_info:
            logger.info(f"[SLOWNODE ALGO] use '' (unnamed/node fallback) parallel detection: {parallel_info}")
            if is_cluster:
                detection_groups = list(parallel_info)
            else:
                detection_groups = get_detection_groups(parallel_info, cur_npus)
            return "", detection_groups

    # 未命中任何优先级域（情况 B：有命名域，但都不在检测优先级内）：
    # 检测组退化为按 hostUid 的物理节点分组，单卡指标在节点组内检测。
    # 这里不做短路 return "", []，避免丢失单卡指标检测。
    node_groups = nodelevel_data_handler._build_node_fallback_groups(cur_npus)
    if node_groups:
        logger.info(f"[SLOWNODE ALGO] 未命中检测优先级，回退按物理节点分组检测: {node_groups}")
        return "", node_groups

    logger.warning("[SLOWNODE ALGO] no valid parallel domain found for detection")
    return "", []


def detect_slow_domain_by_bandwidth(
    parallels: Dict[str, List[List[int]]],
    step_data: Dict[str, Dict[int, float]],
    local_result: config.DegradationData,
):
    """
    按带宽聚类检测慢通信组。

    数据来源：带宽回填写进 CSV 的动态列 "<domain>_<opType>_<count>"。
    对每个集合通信域、每个 opType：
      1. 每组取 count 最大的条目作为代表（count 越大带宽越准）；
      2. 保留 count >= max×0.5 且 > SLOW_COMM_COUNT_FLOOR 的组（滤 count 噪声）；
      3. 用通用检测（min 方向，带宽越小越慢）聚类代表带宽，阈值 = COMM_THRESHOLD。
    只报告在每个 opType 上都异常的组，劣化指数取各 opType 最大值。
    """
    ratio = config.get_comm_threshold()
    if ratio <= 0:
        ratio = 1.3

    for domain, groups in parallels.items():
        if domain == ppParallelDomainName or domain == "embd":
            continue
        if len(groups) < 2:
            continue

        group_bws = [_bw_set_for_group(domain, group, step_data) for group in groups]
        op_types = _collect_op_types(group_bws)
        if not op_types:
            continue

        anomalous: Dict[int, Dict[str, float]] = {}

        for op_type in op_types:
            # 每组取 count 最大的条目作为代表
            reps = []  # (groupIdx, count, bw)
            for gi, bws in enumerate(group_bws):
                max_c = -1
                max_bw = 0.0
                for e in bws:
                    if e["op_type"] != op_type or e["count"] <= max_c:
                        continue
                    max_c = e["count"]
                    max_bw = e["bw"]
                if max_c >= 0:
                    reps.append((gi, max_c, max_bw))

            if len(reps) < 2:
                continue

            max_count = max(r[1] for r in reps)
            half = max_count * 0.5
            kept = [r for r in reps if r[1] >= half and r[1] > config.SLOW_COMM_COUNT_FLOOR]
            if len(kept) < 2:
                continue

            kept_gidxs = [r[0] for r in kept]
            bw_values = [r[2] for r in kept]
            abnormal_gidxs, degradations = kmeans_detector.general_anomaly_detection(
                kept_gidxs, bw_values, ratio, high_is_anomaly=False
            )
            for gi, deg in zip(abnormal_gidxs, degradations):
                anomalous.setdefault(gi, {})[op_type] = deg

        # 只报告在每个 opType 上都异常的组，劣化指数取各 opType 最大值
        for gi, op_degs in anomalous.items():
            if len(op_degs) != len(op_types):
                continue
            max_deg = max(op_degs.values())
            local_result.add_group("comm", groups[gi], max_deg)


def detect_slow_comm_two_level(
    parallels: Dict[str, List[List[int]]],
    comm: List[Tuple[str, str, int, int, float]],
    local_result: config.DegradationData,
    threshold: Optional[float] = None,
):
    """新两级带宽口径（level1 / cluster）。

    comm: [(域, 算子类型, count, rank, bw), ...]，**逐算子**记录，bw = size/time。

    指标：每 rank = mean over 自己的所有算子 of ( bw / median_bw(域,算子类型,count) )，
        ≈1 正常、越小越慢（min 方向）。
    Level-1（组内）：每组内对组内 rank 指标做 kmeans（min 方向）→ 慢 rank（单卡键）。
    Level-2（组间）：每组取组内指标均值 → 域内所有组做 kmeans（min 方向）→ 慢组（组键）。
    两级结果都写进 comm 类别。
    """
    config.set_comm_two_level_detail(None)   # 先清空，避免多 job 进程内残留
    if threshold is None:
        threshold = config.get_comm_threshold()
    if threshold <= 0:
        threshold = 1.3
    if not comm:
        return

    # 基准：每 (域,算子类型,count) 全体算子的 bw 中位数
    base_vals: Dict[Any, List[float]] = {}
    for domain, op_type, count, rank, bw in comm:
        base_vals.setdefault((domain, op_type, count), []).append(bw)
    base = {k: statistics.median(v) for k, v in base_vals.items() if v}

    # 每 (域, rank)：mean over 自己的所有算子 of (bw / 基准)
    acc: Dict[Any, List[float]] = {}
    for domain, op_type, count, rank, bw in comm:
        b = base.get((domain, op_type, count))
        if not b or b <= 0:
            continue
        s, n = acc.get((domain, rank), [0.0, 0])
        acc[(domain, rank)] = [s + bw / b, n + 1]
    metric = {k: s / n for k, (s, n) in acc.items() if n > 0}
    if not metric:
        return

    # 逐 rank / 逐组指标明细（供报告全量绘制）：
    #   level1: [{"domain", "group", "ranks": [{"rank","metric","deg"}]}]   ← 按组
    #   level2: {域: [{"group", "metric", "deg"}]}                          ← 按域
    detail: Dict[str, Any] = {"level1": [], "level2": {}}

    for domain, groups in parallels.items():
        if domain == ppParallelDomainName or domain == "embd":
            continue
        if len(groups) < 2:
            continue

        # ---- Level-1：组内慢 rank（逐组） ----
        for group in groups:
            ranks = [int(r) for r in group if (domain, int(r)) in metric]
            if len(ranks) < minRanksInGroup:
                continue
            vals = [metric[(domain, r)] for r in ranks]
            abnormal, degs = kmeans_detector.general_anomaly_detection(
                ranks, vals, threshold, high_is_anomaly=False
            )
            deg_of = {r: d for r, d in zip(abnormal, degs)}
            ranks_out = []
            for r in ranks:
                d = deg_of.get(r)
                ranks_out.append({"rank": r, "metric": metric[(domain, r)], "deg": d})
                if d is not None:
                    local_result.add_single("comm", r, d)
            detail["level1"].append(
                {"domain": domain, "group": list(group), "ranks": ranks_out})

        # ---- Level-2：组间慢组 ----
        group_means = []
        for group in groups:
            vals = [metric[(domain, int(r))] for r in group if (domain, int(r)) in metric]
            if vals:
                group_means.append((group, sum(vals) / len(vals)))
        if len(group_means) < 2:
            continue
        ids = list(range(len(group_means)))
        means = [m for _, m in group_means]
        abnormal, degs = kmeans_detector.general_anomaly_detection(
            ids, means, threshold, high_is_anomaly=False
        )
        deg_of_gi = {gi: d for gi, d in zip(abnormal, degs)}
        for gi, (group, m) in enumerate(group_means):
            d = deg_of_gi.get(gi)
            detail["level2"].setdefault(domain, []).append(
                {"group": list(group), "metric": m, "deg": d})
            if d is not None:
                local_result.add_group("comm", group, d)

    config.set_comm_two_level_detail(detail)


def _bw_set_for_group(
    domain: str, group: List[int], step_data: Dict[str, Dict[int, float]]
) -> List[Dict[str, Any]]:
    """收集某通信组在各带宽列上的值（组内所有 rank 共享同一回填带宽，取第一个有值的）。"""
    prefix = domain + "_"
    out = []
    for col, by_rank in step_data.items():
        op_type, count = _parse_bandwidth_col(prefix, col)
        if op_type is None:
            continue
        v = None
        for r in group:
            if r in by_rank:
                v = by_rank[r]
                break
        if v is None:
            continue
        out.append({"op_type": op_type, "count": count, "bw": v})
    return out


def _parse_bandwidth_col(prefix: str, col: str):
    """解析带宽列名 "<opType>_<count>"（给定域前缀）。非数字尾/诊断列返回 None。"""
    return utils.parse_bandwidth_col(prefix, col)


def _collect_op_types(group_bws: List[List[Dict[str, Any]]]) -> set:
    s = set()
    for bws in group_bws:
        for e in bws:
            s.add(e["op_type"])
    return s


def _pp_compare_groups(parallels: Dict[str, List[List[int]]]) -> List[List[int]]:
    """
    v6p/v6pp 的比较域分组：命名域优先级 **TP > CP > DP**；都没有时用 PP 的 **stage 转置**
    （把各 PP 组同一 stage 位置的成员归一组，如 x1P=2,x2P=2,PP=2 → [0,1,2,3] / [4,5,6,7]）。
    """
    for domain in ("tp", "cp", "dp"):
        gs = parallels.get(domain)
        if gs:
            return [sorted(g) for g in gs if len(g) >= 2]
    pp = parallels.get(ppParallelDomainName)
    if not pp:
        return []
    n = max(len(g) for g in pp)
    out = []
    for k in range(n):
        members = [sorted(g)[k] for g in pp if len(g) > k]
        if len(members) >= 2:
            out.append(sorted(members))
    return out


def _detect_pp_rank_ratio(
    parallels: Dict[str, List[List[int]]],
    step_data: Dict[str, Dict[int, float]],
    local_result: config.DegradationData,
):
    """
    PP 慢通信检测：按比较域（TP > CP > DP，退化用 PP stage）分组，域内**逐 rank** 与组内
    最大值比较：`score = (组内最大时间 − 本 rank 时间) / 会话时长`，`> 10%` 判异常（偏小异常）。

    上报单位为 **PP 通信组**：把异常 rank 归到其所属 PP 组（`parallels["pp"]`），组内至少一个
    rank 异常即上报该组，劣化指数取组内异常 rank 的最大 score（不上报具体 rank）。
    """
    vals = step_data.get(config.PP_WAIT_GAP_COLUMN, {})
    if not vals:
        return

    session = config.get_pp_session_ns()
    if session <= 0:
        return

    compare_groups = _pp_compare_groups(parallels)
    if not compare_groups:
        return

    # rank -> 所属 PP 通信组（组键 = 该组 rank 升序列表）
    rank_to_ppgroup = {}
    for g in parallels.get(ppParallelDomainName) or []:
        gg = sorted(g)
        for r in gg:
            rank_to_ppgroup[r] = gg

    group_deg = {}  # tuple(pp 组) -> 组内异常 rank 的最大 score
    for g in compare_groups:
        pairs = [(r, vals[r]) for r in g if r in vals and vals[r] != -99999]
        if len(pairs) < 2:
            continue
        max_t = max(v for _, v in pairs)
        for r, v in pairs:
            score = (max_t - v) / session
            if score <= config.PP_WAIT_RATIO_THRESHOLD:
                continue
            ppg = rank_to_ppgroup.get(r)
            if ppg is None:
                continue  # 该 rank 不属于任何 PP 组，无法归组，跳过
            key = tuple(ppg)
            if score > group_deg.get(key, 0.0):
                group_deg[key] = score

    for key, deg in group_deg.items():
        local_result.add_group("pp_comm", list(key), deg)


def detect_pp_slow_domain(
    parallels: Dict[str, List[List[int]]],
    step_data: Dict[str, Dict[int, float]],
    local_result: config.DegradationData,
):
    """
    PP 流水线慢通信检测（v6pp）。

    数据来源：解析后回填的动态列 "PP_WaitGap"（每卡点对点传输结束 → 下一次集合通信结束 的间隔之和）。
    比较域按 TP > CP > DP，都没有时用 PP stage 转置；域内逐 rank 与组内最大值比较，
    (组内最大 − 本 rank) / 会话时长 > 10% 判异常（偏小异常）；上报单位为 PP 通信组（组内任一
    rank 异常即上报，取组内最大占比）。
    """
    _detect_pp_rank_ratio(parallels, step_data, local_result)
