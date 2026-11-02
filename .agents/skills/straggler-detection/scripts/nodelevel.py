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
import nodelevel_data_handler
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("[SLOWNODE ALGO]")
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
GENERAL_METRIC_CATEGORIES = [
    (zpKernelColumn, "KERNEL_AICORE"),
    (kernelAivecColumn, "kernel_aivec"),
    (memcpyAsyncColumn, "memcpy_async"),
]

def delimit_detection(
    step_data: Dict[str, Dict[int, float]],
    parallels: Dict[str, List[List[int]]],
    valid_ranks: List[int],
    comm: Optional[Dict[Any, Dict[int, float]]] = None,
    comm_threshold: Optional[float] = None,
) -> Dict[str, Dict[str, float]]:
    local_result = config.DegradationData()
    if not step_data:
        logger.info("[INFO] 无单卡快照数据（cluster 单文件）：仅检测通信域")
        if comm:
            logger.info("\n慢通信域检测（新两级带宽口径，cluster）:")
            detect_slow_comm_two_level(parallels, comm, local_result, comm_threshold)
        return dict(local_result)
    cal_detection_group_name, cal_detection_group = get_cal_detection_group(parallels, valid_ranks)
    if not cal_detection_group or (cal_detection_group_name is None and cal_detection_group != []):
        logger.warning("获取检测组失败")
        return {}
    detection_zp_bubble_data(step_data.get(zpBubbleColumn, {}), local_result)
    if config.get_profiling_level() == "levelnone":
        logger.info("[SKIP] levelnone（TASK 仅 MsTx）：跳过 KERNEL_AICORE / KERNEL_AIVEC / MEMCPY_ASYNC 检测")
    else:
        logger.info("\n慢计算卡检测 (KERNEL_AICORE):")
        get_slow_calculate_ranks(cal_detection_group, step_data, cal_detection_group_name, local_result)
        logger.info("\n通用检测（选定的检测组）:")
        for column, category in GENERAL_METRIC_CATEGORIES:
            if column == zpKernelColumn:
                continue
            get_slow_metric_ranks(cal_detection_group, step_data, column, category, local_result)
            logger.info(f"  - {column} -> {category}")
    if comm:
        logger.info("\n慢通信域检测（新两级带宽口径，level1/cluster）:")
        detect_slow_comm_two_level(parallels, comm, local_result, comm_threshold)
    elif config.get_has_named_domain():
        logger.info("\n慢通信域检测（带宽聚类）:")
        detect_slow_domain_by_bandwidth(parallels, step_data, local_result)
        if local_result.get("KERNEL_AICORE") or local_result.get("kernel_aivec"):
            logger.info("[SKIP] 存在计算类故障（KERNEL_AICORE / kernel_aivec），跳过 PP 慢通信检测")
        else:
            logger.info("\nPP 流水线慢通信检测:")
            detect_pp_slow_domain(parallels, step_data, local_result)
    else:
        logger.info("[SKIP] 无通信域名，跳过慢通信域检测（comm）")
    logger.info("\nCPU 资源卡检测（集群整体拉齐）:")
    get_slow_host_ranks_by_homogenize(valid_ranks, step_data.get(zpHostDataColumn, {}), local_result)
    return dict(local_result)

def get_detection_groups(tp_ranks: List[List[int]], node_global_rank: List[int]) -> Optional[List[List[int]]]:
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
    if not aligned_data or (len(aligned_data.get(zpDeviceColumn, {})) == 0 and
                            len(aligned_data.get(zpKernelColumn, {})) == 0):
        logger.warning("[SLOWNODE ALGO] empty aligned ZP_device map data")
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
    return kmeans_detector.general_anomaly_detection(
        ranks, values, config.get_compute_threshold()
    )

def det_metric_for_one_group(
    aligned_data: Dict[str, Dict[int, float]],
    npu_group: List[int],
    column: str,
    multiplier: float,
) -> Tuple[List[int], List[float]]:
    values = []
    ranks = []
    for npu_id in npu_group:
        val = aligned_data.get(column, {}).get(npu_id, 0)
        if val != 0 and val != -99999:
            values.append(val)
            ranks.append(npu_id)
    if len(ranks) < minRanksInGroup:
        return [], []
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
    if not npu_data:
        return
    for npu_id, value in npu_data.items():
        if value == -99999:
            continue
        if value <= 0:
            continue
        if value < config.BUBBLE_THRESHOLD_NS:
            local_result.add_single("npu_bubble", npu_id, value)

def _trimmed_mean(group_data: List[float]) -> float:
    if len(group_data) > 2:
        sorted_data = sorted(group_data)
        trimmed_data = sorted_data[1:-1]
        return sum(trimmed_data) / len(trimmed_data)
    return sum(group_data) / len(group_data)

def process_cpu_data_by_node(
    have_data_ranks: List[int],
    ranks_data: List[float]
) -> List[str]:
    node_map = config.get_host_rank_map()
    labels = [None] * len(have_data_ranks)
    if not node_map:
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
    have_data_ranks = []
    ranks_data = []
    for npu in npus:
        if npu in detection_data:
            val = detection_data[npu]
            if val != -99999:
                have_data_ranks.append(npu)
                ranks_data.append(val)
    labels = process_cpu_data_by_node(have_data_ranks, ranks_data)
    abnormal_ranks, rank_deg_severitys = kmeans_detector.general_anomaly_detection(
        have_data_ranks, ranks_data, config.get_io_threshold()
    )
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
    if not parallels or not cur_npus:
        return "", []
    is_cluster = config.get_is_cluster_data()
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
                detection_groups = list(parallel_info)
            else:
                detection_groups = get_detection_groups(parallel_info, cur_npus)
            return domain, detection_groups
    if "" in parallels and parallels[""]:
        parallel_info = parallels[""]
        if parallel_info:
            logger.info(f"[SLOWNODE ALGO] use '' (unnamed/node fallback) parallel detection: {parallel_info}")
            if is_cluster:
                detection_groups = list(parallel_info)
            else:
                detection_groups = get_detection_groups(parallel_info, cur_npus)
            return "", detection_groups
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
            reps = []
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
    config.set_comm_two_level_detail(None)
    if threshold is None:
        threshold = config.get_comm_threshold()
    if threshold <= 0:
        threshold = 1.3
    if not comm:
        return
    base_vals: Dict[Any, List[float]] = {}
    for domain, op_type, count, rank, bw in comm:
        base_vals.setdefault((domain, op_type, count), []).append(bw)
    base = {k: statistics.median(v) for k, v in base_vals.items() if v}
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
    detail: Dict[str, Any] = {"level1": [], "level2": {}}
    for domain, groups in parallels.items():
        if domain == ppParallelDomainName or domain == "embd":
            continue
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
                    local_result.add_single("comm", r, d, domain)
            detail["level1"].append(
                {"domain": domain, "group": list(group), "ranks": ranks_out})
        if len(groups) < 2:
            continue
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
    return utils.parse_bandwidth_col(prefix, col)

def _collect_op_types(group_bws: List[List[Dict[str, Any]]]) -> set:
    s = set()
    for bws in group_bws:
        for e in bws:
            s.add(e["op_type"])
    return s

def _pp_compare_groups(parallels: Dict[str, List[List[int]]]) -> List[List[int]]:
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
    vals = step_data.get(config.PP_WAIT_GAP_COLUMN, {})
    if not vals:
        return
    session = config.get_pp_session_ns()
    if session <= 0:
        return
    compare_groups = _pp_compare_groups(parallels)
    if not compare_groups:
        return
    rank_to_ppgroup = {}
    for g in parallels.get(ppParallelDomainName) or []:
        gg = sorted(g)
        for r in gg:
            rank_to_ppgroup[r] = gg
    group_deg = {}
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
                continue
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
    _detect_pp_rank_ratio(parallels, step_data, local_result)
