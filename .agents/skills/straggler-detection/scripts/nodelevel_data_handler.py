"""
Node Level 数据读取和解析模块

主要功能：
1. 读取 CSV 数据
2. 读取 group_info_*.json 获取并行域信息
3. 获取最新 step 的数据快照
"""

import csv
import json
import os
import logging
import re
import sqlite3
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("[SLOWNODE ALGO]")

# 常量定义
data_file_field_group_name = "group_name"
data_file_field_global_ranks = "global_ranks"


def read_csv_detection_data_all(file_path: str) -> Optional[Dict[str, List[float]]]:
    """
    读取 CSV 文件全部数据
    """
    try:
        with open(file_path, 'r') as f:
            reader = csv.reader(f)
            data = list(reader)
    except Exception as e:
        logger.error(f"读取 CSV 文件失败：{e}")
        return None

    if len(data) <= 1:
        logger.warning(f"[SLOWNODE ALGO] empty {file_path}")
        return None

    # 获取表头
    valid_header = data[0]

    # 存储结果
    ret = defaultdict(list)
    flag_tmp = store_valid_data(ret, data[1:], valid_header)

    if not flag_tmp:
        return None

    return dict(ret)


def store_valid_data(container: Dict[str, List[float]], data: List[List[str]], header: List[str]) -> bool:
    """
    存储有效数据到容器
    """
    if container is None:
        logger.error("invalid container!")
        return False

    for row in data:
        if len(row) != len(header):
            logger.warning("unexpected data array lacked something!")
            continue

        for index, data_val in enumerate(row):
            try:
                num = float(data_val)
            except ValueError as e:
                logger.warning(f"解析数据失败：{e}")
                continue

            if index >= len(header):
                logger.warning("unexpected index out of header range!")
                continue

            if header[index] not in container:
                container[header[index]] = []

            container[header[index]].append(num)

    return True


def get_cur_job_last_step_data(ranks: List[int]) -> Dict[str, Dict[int, float]]:
    """
    获取当前路径下所有 global_rank_npuId.csv 文件的"准最新"数据点

    规则：
    - 若某 (metric, rank) 的时间序列长度 > 1，取倒数第二个
    - 若长度 == 1，取第一个（也是最后一个）
    - 长度为 0 则忽略

    返回单个快照：map[string]map[int]float64
    """
    # 用于暂存每个 (metric, rank) 的完整时间序列
    all_data: Dict[str, Dict[int, List[float]]] = defaultdict(lambda: defaultdict(list))

    for npu_id in ranks:
        file_path = os.path.join(config.get_output_path(), "op_metric", f"global_rank_{npu_id}.csv")
        data = read_csv_detection_data_all(file_path)

        if data is None:
            logger.warning(f"[SLOWNODE ALGO] unexpected npu{npu_id} csv data!")
            continue

        # 跳过 StepIndex 列
        for key, val in data.items():
            if key == "StepIndex":
                continue

            all_data[key][npu_id].extend(val)

    # 构建最终结果：单个快照
    result: Dict[str, Dict[int, float]] = {}

    for metric, rank_map in all_data.items():
        for rank, values in rank_map.items():
            n = len(values)
            if n == 0:
                continue

            # 取倒数第二个（如果存在），否则取第一个
            if n > 1:
                value = values[n - 2]
            else:
                value = values[0]

            if metric not in result:
                result[metric] = {}
            result[metric][rank] = value

    return result


def get_cur_detection_info(job_path: str) -> Tuple[Dict[str, List[List[int]]], List[int]]:
    """
    获取当前检测信息（并行域和有效 ranks）
    """
    # 读取所有 rank 的并行拓扑信息
    cur_job_ranks_parallel, valid_ranks = get_cur_job_all_ranks_topo(job_path)

    if not cur_job_ranks_parallel or not valid_ranks:
        return {}, []

    # 提前返回空结果
    if not cur_job_ranks_parallel:
        return {}, []

    # 获取所有唯一的 group_name
    group_names = set()
    for rank_id, rank_groups in cur_job_ranks_parallel.items():
        if not isinstance(rank_groups, dict):
            continue

        for group_val in rank_groups.values():
            if not isinstance(group_val, dict):
                continue
            name = group_val.get("group_name")
            # 允许空字符串作为有效的 group_name（用于无命名并行域场景）
            if name is not None:
                group_names.add(name)

    # 构建 parallels：每个命名域（group_name 非空）对应其 global_ranks 分组。
    # 空域名（group_name == ""）不做分组推导：无法确认它是 TP/EP/CP，推导分组不可靠，
    # 统一交由下方"无命名域时的节点分组回退"处理。
    parallels: Dict[str, List[List[int]]] = {}

    for group_name in group_names:
        if not group_name:
            continue  # 空域名跳过，避免先入为主假设为 TP 域
        parallel_info = get_detection_job_parallel_info(cur_job_ranks_parallel, group_name)
        if parallel_info and len(parallel_info) > 0 and len(parallel_info[0]) > 1:
            parallels[group_name] = parallel_info

    # 是否有命名通信域：存在任何非空域名且其分组有效
    config.set_has_named_domain(any(name for name in parallels))

    # 无命名域时（情况 A：所有 group_name 都为空，或命名域分组均无效），
    # 按物理节点分组：同一 hostUid 的所有卡作为一组检测组。
    # 只有有效卡且存在节点映射时才回退，否则维持短路（无法分组）。
    if not config.get_has_named_domain():
        if valid_ranks and config.get_host_rank_map():
            logger.info(
                f"[SLOWNODE ALGO] 无命名通信域，回退按物理节点分组检测: "
                f"valid_ranks={valid_ranks}"
            )
            node_groups = _build_node_fallback_groups(valid_ranks)
            if node_groups:
                # 用空字符串域名 key，get_cal_detection_group 的 "" 分支可识别
                parallels[""] = node_groups
            else:
                return {}, []
        else:
            return {}, []

    # 对 valid_ranks 排序
    valid_ranks.sort()

    # 判定是否为“集群数据”（Case A 集群 / Case B 非集群）
    determine_cluster_data(job_path, valid_ranks)

    return parallels, valid_ranks


def determine_cluster_data(job_path: str, valid_ranks: List[int]):
    """
    判定是否为“集群数据”（算法规格第二节）。

    依次检查：
    1. 是否存在 group_info_*.json 文件；
    2. group_info_*.json 文件个数 == global_rank_*.csv 文件个数；
    3. 用 group_info_*.json 拼凑出来的完整通信域卡数 == group_info 文件个数。

    三个条件全部满足 → 集群数据；否则 → 非集群数据。
    结果写入 config.IsClusterData。

    参数:
        job_path: 数据目录（仅用于日志）
        valid_ranks: 已收集的有效 rank（与 group_info 文件一一对应）
    """
    op_metric_dir = os.path.join(config.get_output_path(), "op_metric")

    group_files = []
    csv_files = []
    if os.path.exists(op_metric_dir):
        for fn in os.listdir(op_metric_dir):
            if fn.startswith("group_info_") and fn.endswith(".json"):
                group_files.append(fn)
            elif fn.startswith("global_rank_") and fn.endswith(".csv"):
                csv_files.append(fn)

    # 条件 1：存在 group_info 文件
    if not group_files:
        config.set_is_cluster_data(False)
        logger.info(f"[SLOWNODE ALGO] {job_path} 无 group_info 文件，判定为非集群数据")
        return

    # 条件 2：group_info 个数 == global_rank CSV 个数
    if len(group_files) != len(csv_files):
        config.set_is_cluster_data(False)
        logger.info(
            f"[SLOWNODE ALGO] {job_path} group_info({len(group_files)}) != global_rank csv({len(csv_files)})，"
            f"判定为非集群数据"
        )
        return

    # 条件 3：由 group_info 拼出的完整通信域卡数 == group_info 文件个数
    # 通信域卡数 = 所有 group_info 中 global_ranks 的并集（去掉 -1 等无效占位）
    domain_ranks = set()
    for fn in group_files:
        try:
            with open(os.path.join(op_metric_dir, fn), 'r') as f:
                topo = json.load(f)
            for key, parallel in topo.items():
                if not isinstance(parallel, dict):
                    continue
                gr = parallel.get("global_ranks")
                if isinstance(gr, list):
                    for r in gr:
                        try:
                            r = int(r)
                        except (TypeError, ValueError):
                            continue
                        if r >= 0:
                            domain_ranks.add(r)
        except Exception as e:
            logger.warning(f"[SLOWNODE ALGO] 读取 {fn} 失败：{e}")
            continue

    if len(domain_ranks) != len(group_files):
        config.set_is_cluster_data(False)
        logger.info(
            f"[SLOWNODE ALGO] {job_path} 通信域拼出卡数({len(domain_ranks)}) != group_info 个数({len(group_files)})，"
            f"判定为非集群数据"
        )
        return

    config.set_is_cluster_data(True)
    logger.info(
        f"[SLOWNODE ALGO] {job_path} 判定为集群数据：group_info={len(group_files)}, csv={len(csv_files)}, "
        f"通信域卡数={len(domain_ranks)}"
    )


def _build_node_fallback_groups(valid_ranks: List[int]) -> List[List[int]]:
    """
    无通信域名时，按物理节点分组构造检测组。

    用 config.HostRankMap（rank -> hostUid，解析阶段从 HOST_INFO 表填充，
    hostUid 相同的 rank 归为同一物理节点）
    把相同 hostUid 的 rank 归为一组；组内至少 2 卡才有聚类区分度，
    单卡组会拆分到其它组保持有效（与 Homogenization 要求 ≥2 一致）。

    返回:
        节点分组列表，如 [[0,1],[2,3]]；无法构成任何有效组时返回 []
    """
    node_map = config.get_host_rank_map()
    if not node_map:
        return []

    # 按 hostName 分组
    host_to_ranks: Dict[str, List[int]] = defaultdict(list)
    for rank in valid_ranks:
        host = node_map.get(str(rank), str(rank))  # 无 host 信息时用 rank 自身作 host
        host_to_ranks[host].append(int(rank))

    # 每组内排序，保证 rank 顺序稳定
    raw_groups = [sorted(ranks) for ranks in host_to_ranks.values()]

    # 聚类检测要求组内 >=2 卡才有区分度，丢弃单卡节点组
    groups = [g for g in raw_groups if len(g) >= 2]

    if not groups:
        return []

    logger.info(f"[SLOWNODE ALGO] 节点回退分组: {groups}")
    return groups


def get_cur_job_all_ranks_topo(job_path: str) -> Tuple[Dict[int, Any], List[int]]:
    """
    获取所有卡的并行域信息
    """
    cur_job_ranks_topo = {}
    valid_ranks = []

    # 遍历 op_metric 目录下的所有 group_info_*.json 文件
    # op_metric 位于独立输出目录（get_output_path），与原始数据目录分离
    op_metric_dir = os.path.join(config.get_output_path(), "op_metric")

    if not os.path.exists(op_metric_dir):
        logger.error(f"目录不存在：{op_metric_dir}")
        return {}, []

    for file_name in os.listdir(op_metric_dir):
        if not file_name.startswith("group_info_") or not file_name.endswith(".json"):
            continue

        # 提取 rank ID
        try:
            rank_id = int(file_name[len("group_info_"):-len(".json")])
        except ValueError:
            continue

        valid_ranks.append(rank_id)

        # 读取 JSON 文件
        file_path = os.path.join(op_metric_dir, file_name)
        try:
            with open(file_path, 'r') as f:
                cur_rank_topo = json.load(f)
            cur_job_ranks_topo[rank_id] = cur_rank_topo
        except Exception as e:
            logger.error(f"读取文件 {file_path} 失败：{e}")
            continue

    if not valid_ranks:
        logger.warning(f"[SLOWNODE ALGO] {job_path} no valid rank!")
        return {}, []

    return cur_job_ranks_topo, valid_ranks


def get_target_parallel_group_info(
    rank_id: int,
    rank_info: Dict[str, Any],
    groups: List[List[int]],
    parallel_info: Dict[int, Dict[int, bool]],
    target: str,
    all_names: Optional[Dict[str, bool]] = None
) -> bool:
    """
    获取目标并行域信息
    """
    for key, parallel in rank_info.items():
        if not isinstance(parallel, dict):
            logger.warning(f"[SLOWNODE ALGO] Invalid rank {rank_id} parallel domain info!")
            return False

        group_name = parallel.get(data_file_field_group_name)
        # 允许空字符串作为有效的 group_name
        if group_name is None:
            logger.warning(f"[SLOWNODE ALGO] Rank {rank_id} parallel domain without goup_name!")
            return False

        name = group_name if isinstance(group_name, str) else str(group_name)

        # 用于记录所有并行域名称
        if target == "" and all_names is not None:
            all_names[name] = True
            continue

        # 获取目标并行域信息
        if not isinstance(group_name, str) or name != target:
            continue

        # 获取并行域中的卡
        parallel_npus = parallel.get(data_file_field_global_ranks)
        if not parallel_npus:
            logger.warning(f"[SLOWNODE ALGO] Rank {rank_id} parallel domain without global_ranks!")
            return False

        if not isinstance(parallel_npus, list):
            logger.warning(f"[SLOWNODE ALGO] Invalid rank {rank_id} parallel domain global_ranks: {parallel_npus}")
            return False

        add_domain_to_array(parallel_npus, groups, rank_id, parallel_info)

    return True


def add_domain_to_array(
    npu_group: List[Any],
    groups: List[List[int]],
    rank_id: int,
    parallel_info: Dict[int, Dict[int, bool]]
):
    """
    添加并行域到数组
    """
    # 转换 float64 到 int（JSON 解析后数字是 float）
    npu_parallel = transfer_float_array_to_int(npu_group)
    if npu_parallel is None:
        return

    # 如果在同一个并行域中则不需要再添加
    if not check_rank_parallel_exist(parallel_info, rank_id, npu_parallel):
        groups.append(npu_parallel)


def transfer_float_array_to_int(npu_group: List[Any]) -> Optional[List[int]]:
    """
    将 JSON 数字字符数组转换为 int 数组
    """
    if not npu_group:
        return None

    npus = []
    for num in npu_group:
        if isinstance(num, (int, float)):
            npus.append(int(num))
        else:
            logger.warning("[SLOWNODE ALGO] Transfer npu id failed!")
            return None

    return npus


def check_rank_parallel_exist(
    parallel_info: Dict[int, Dict[int, bool]],
    rank_id: int,
    npu_group: List[int]
) -> bool:
    """
    检查当前 rank 的并行域信息是否已经通过其他同一个并行域中的 rank 并行域信息添加
    """
    # 检查并行域中是否已经存在当前并行域组
    for parallel_domain_groups in parallel_info.values():
        if rank_id in parallel_domain_groups:
            return True

    # 添加到当前并行域信息中
    if rank_id not in parallel_info:
        parallel_info[rank_id] = {}

    for npu in npu_group:
        parallel_info[rank_id][npu] = True

    return False


def get_detection_job_parallel_info(
    cur_job_ranks_topo: Dict[int, Any],
    target: str
) -> List[List[int]]:
    """
    获取当前 JOB 的并行域信息
    """
    if not cur_job_ranks_topo:
        logger.warning("[SLOWNODE ALGO] Invalid job rank info!")
        return []

    # 辅助用于避免重复添加并行域信息
    parallel_info: Dict[int, Dict[int, bool]] = {}
    groups = []

    for rank_id, topo_info in cur_job_ranks_topo.items():
        if not isinstance(topo_info, dict):
            logger.warning(f"[SLOWNODE ALGO] Invalid rank {rank_id} info!")
            return []

        if not get_target_parallel_group_info(
            rank_id, topo_info, groups, parallel_info, target, None
        ):
            continue

    logger.info(f"[SLOWNODE ALGO] {target} parallel info: {groups}")
    return groups


# ---------------------------------------------------------------------------
# 数据级别识别（levelnone / level1 / cluster）
# ---------------------------------------------------------------------------
# 只取真实数据行：HCCS 有真实 transit_size/time。
_BAND_TYPE = "HCCS"
_AGG_OP_NAME = "Total Op Info"


def detect_profiling_level(input_path: str) -> str:
    """识别输入数据级别：levelnone / level0 / level1 / cluster。

    - 输入是**文件**：能被 sqlite 打开、且含 cluster 需要的表（`CommunicationGroupMapping`、
      `ClusterCommunicationBandwidth`，且均有数据）→ cluster；否则 → levelnone。
      （不限制文件名。）
    - 目录：找 `ascend_pytorch_profiler_*.db`：
      1. 任一 db 同目录的 `analysis.db` 有 `CommAnalyzerBandwidth` 数据 → level1；
      2. 否则看任一 db 的 `TASK.taskType`：含 KERNEL_AICORE / KERNEL_AIVEC / MEMCPY_ASYNC → level0；
      3. 都没有（仅 MsTx）→ levelnone。
    """
    if os.path.isfile(input_path):
        return "cluster" if is_cluster_db(input_path) else "levelnone"

    profiler_dbs = []
    for root, _dirs, files in os.walk(input_path):
        for fn in files:
            if fn.startswith("ascend_pytorch_profiler_") and fn.endswith(".db"):
                profiler_dbs.append(os.path.join(root, fn))
    if not profiler_dbs:
        return "levelnone"

    for db in profiler_dbs:
        if _analysis_db_has_bandwidth(os.path.join(os.path.dirname(db), "analysis.db")):
            logger.info(f"[SLOWNODE ALGO] 检测到 level1 数据（{os.path.dirname(db)}/analysis.db 有带宽数据）")
            return "level1"

    # 无 analysis.db 带宽：按 TASK.taskType 区分 level0 / levelnone（数据同构，查第一个 profiler db 即可）
    if _db_has_kernel_tasks(profiler_dbs[0]):
        return "level0"
    return "levelnone"


def _db_has_kernel_tasks(db_path: str) -> bool:
    """profiler db 的 TASK.taskType 是否含 KERNEL_AICORE / KERNEL_AIVEC / MEMCPY_ASYNC。

    这些类型缺失（仅 MsTx）说明没有计算/搬运类算子数据 → levelnone，跳过对应检测。
    """
    if not os.path.isfile(db_path):
        return False
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        row = cur.execute(
            "SELECT 1 FROM TASK t JOIN STRING_IDS s ON t.taskType = s.id "
            "WHERE s.value IN ('KERNEL_AICORE','KERNEL_AIVEC','MEMCPY_ASYNC') LIMIT 1"
        ).fetchone()
        con.close()
        return row is not None
    except Exception as e:
        logger.warning(f"[SLOWNODE ALGO] 读取 {db_path} 的 TASK 失败：{e}")
        return False


def _sqlite_has_tables(db_path: str, tables: Tuple[str, ...]) -> bool:
    """db 文件能被 sqlite 打开，且包含指定表、每张表都至少有一行数据。"""
    if not os.path.isfile(db_path):
        return False
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        for t in tables:
            row = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if not row or not cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
                con.close()
                return False
        con.close()
        return True
    except Exception as e:
        logger.warning(f"[SLOWNODE ALGO] 读取 {db_path} 失败：{e}")
        return False


def is_cluster_db(db_path: str) -> bool:
    """单文件 cluster 库：含 `CommunicationGroupMapping` 与 `ClusterCommunicationBandwidth` 且均有数据。"""
    return _sqlite_has_tables(db_path, ("CommunicationGroupMapping", "ClusterCommunicationBandwidth"))


def _analysis_db_has_bandwidth(analysis_db: str) -> bool:
    """analysis.db 是否存在 CommAnalyzerBandwidth 表且有数据。"""
    if not os.path.exists(analysis_db):
        return False
    try:
        con = sqlite3.connect(analysis_db)
        cur = con.cursor()
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='CommAnalyzerBandwidth'"
        ).fetchone()
        if not row:
            con.close()
            return False
        n = cur.execute("SELECT COUNT(*) FROM CommAnalyzerBandwidth").fetchone()[0]
        con.close()
        return n and n > 0
    except Exception as e:
        logger.warning(f"[SLOWNODE ALGO] 读取 {analysis_db} 失败：{e}")
        return False


def _op_type(hccl_op_name: str) -> str:
    """提取算子类型：hcom_allReduce__612_0_1 → 'allreduce'（非 hccl 记录返回空串）。"""
    s = (hccl_op_name or "").strip()
    if "_" in s:
        return s.split("_")[1].lower()
    return ""


def _rank_from_profiler_filename(db_file: str) -> Optional[int]:
    """从 ascend_pytorch_profiler_<rank>.db 文件名提取 global rank。"""
    base = os.path.basename(db_file)
    prefix = "ascend_pytorch_profiler_"
    if not (base.startswith(prefix) and base.endswith(".db")):
        return None
    try:
        return int(base[len(prefix):-len(".db")])
    except ValueError:
        return None


def _read_bandwidth_rows(analysis_db: str):
    """读 analysis.db 的 CommAnalyzerBandwidth（HCCS、最后一个 step）→
       [(op_type, count, size, time), ...]（每算子每 transport_type 一行）。"""
    out = []
    con = sqlite3.connect(analysis_db)
    cur = con.cursor()
    row = cur.execute("SELECT MAX(step) FROM CommAnalyzerBandwidth").fetchone()
    step = row[0] if row else None
    q = (
        "SELECT hccl_op_name, count, transit_size, transit_time "
        "FROM CommAnalyzerBandwidth WHERE transport_type=? AND hccl_op_name<>? "
        "AND step=?"
    )
    for op, cnt, size, time in cur.execute(q, (_BAND_TYPE, _AGG_OP_NAME, step)):
        s = float(size or 0.0)
        t = float(time or 0.0)
        if s > 0 and t > 0:
            out.append((_op_type(op), int(cnt or 0), s, t))
    con.close()
    return out


def load_level1_comm(input_path: str, parallels: Dict[str, List[List[int]]]) -> List[Tuple[str, str, int, int, float]]:
    """level1：遍历各 rank 的 analysis.db，读 CommAnalyzerBandwidth（HCCS），
       逐算子返回 (域, 算子类型, count, rank, bw=size/time)。

    域来自该 rank 所属的命名并行域（parallels）；无命名域时用 "default_group"。
    """
    # rank -> 所属域集合
    rank_domains: Dict[int, set] = {}
    for domain, groups in (parallels or {}).items():
        for g in groups:
            for r in g:
                rank_domains.setdefault(int(r), set()).add(domain)

    comm: List[Tuple[str, str, int, int, float]] = []
    for root, _dirs, files in os.walk(input_path):
        for fn in files:
            if not (fn.startswith("ascend_pytorch_profiler_") and fn.endswith(".db")):
                continue
            rank = _rank_from_profiler_filename(os.path.join(root, fn))
            if rank is None:
                continue
            analysis_db = os.path.join(root, "analysis.db")
            if not os.path.exists(analysis_db):
                continue
            domains = rank_domains.get(rank) or {"default_group"}
            for op_type, count, size, time in _read_bandwidth_rows(analysis_db):
                for domain in domains:
                    comm.append((domain, op_type, count, rank, size / time))
    logger.info(f"[SLOWNODE ALGO] level1 带宽数据：{len(comm)} 条 per-op 记录")
    return comm


def load_cluster_comm(db_path: str) -> Tuple[Dict[str, List[List[int]]], List[Tuple[str, str, int, int, float]]]:
    """cluster_analysis.db：读 CommunicationGroupMapping → parallels={域:[[ranks]]}；
       读 ClusterCommunicationBandwidth（HCCS、排除聚合行）→ 逐算子 (域, 算子类型, count, rank, bw)。"""
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    g2pg: Dict[str, str] = {}
    parallels: Dict[str, List[List[int]]] = {}
    cur.execute(
        "SELECT group_name, pg_name, rank_set FROM CommunicationGroupMapping "
        "WHERE type='collective' AND pg_name IS NOT NULL AND pg_name != ''"
    )
    for gname, pg, rank_set in cur.fetchall():
        if pg in ("pp", "embd", "intra_dist_opt_instance", "default_group"):
            continue
        g2pg[gname] = pg
        ranks = sorted(int(x) for x in re.findall(r"\d+", rank_set or ""))
        if len(ranks) >= 2:
            parallels.setdefault(pg, []).append(ranks)

    comm: List[Tuple[str, str, int, int, float]] = []
    q = (
        "SELECT group_name, rank_id, hccl_op_name, count, transit_size, transit_time "
        "FROM ClusterCommunicationBandwidth WHERE band_type=? AND hccl_op_name<>?"
    )
    for gname, rank, op, cnt, size, time in cur.execute(q, (_BAND_TYPE, _AGG_OP_NAME)):
        pg = g2pg.get(gname)
        if not pg:
            continue
        s = float(size or 0.0)
        t = float(time or 0.0)
        if s <= 0 or t <= 0:
            continue
        comm.append((pg, _op_type(op), int(cnt or 0), int(rank), s / t))
    con.close()
    logger.info(f"[SLOWNODE ALGO] cluster 带宽数据：{len(parallels)} 域，{len(comm)} 条 per-op 记录")
    return parallels, comm
