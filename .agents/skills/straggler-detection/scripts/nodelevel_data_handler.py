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
data_file_field_group_name = "group_name"
data_file_field_global_ranks = "global_ranks"

def read_csv_detection_data_all(file_path: str) -> Optional[Dict[str, List[float]]]:
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
    valid_header = data[0]
    ret = defaultdict(list)
    flag_tmp = store_valid_data(ret, data[1:], valid_header)
    if not flag_tmp:
        return None
    return dict(ret)

def store_valid_data(container: Dict[str, List[float]], data: List[List[str]], header: List[str]) -> bool:
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
    all_data: Dict[str, Dict[int, List[float]]] = defaultdict(lambda: defaultdict(list))
    for npu_id in ranks:
        file_path = os.path.join(config.get_output_path(), "op_metric", f"global_rank_{npu_id}.csv")
        data = read_csv_detection_data_all(file_path)
        if data is None:
            logger.warning(f"[SLOWNODE ALGO] unexpected npu{npu_id} csv data!")
            continue
        for key, val in data.items():
            if key == "StepIndex":
                continue
            all_data[key][npu_id].extend(val)
    result: Dict[str, Dict[int, float]] = {}
    for metric, rank_map in all_data.items():
        for rank, values in rank_map.items():
            n = len(values)
            if n == 0:
                continue
            if n > 1:
                value = values[n - 2]
            else:
                value = values[0]
            if metric not in result:
                result[metric] = {}
            result[metric][rank] = value
    return result

def get_cur_detection_info(job_path: str) -> Tuple[Dict[str, List[List[int]]], List[int]]:
    cur_job_ranks_parallel, valid_ranks = get_cur_job_all_ranks_topo(job_path)
    if not cur_job_ranks_parallel or not valid_ranks:
        return {}, []
    if not cur_job_ranks_parallel:
        return {}, []
    group_names = set()
    for rank_id, rank_groups in cur_job_ranks_parallel.items():
        if not isinstance(rank_groups, dict):
            continue
        for group_val in rank_groups.values():
            if not isinstance(group_val, dict):
                continue
            name = group_val.get("group_name")
            if name is not None:
                group_names.add(name)
    parallels: Dict[str, List[List[int]]] = {}
    for group_name in group_names:
        if not group_name:
            continue
        parallel_info = get_detection_job_parallel_info(cur_job_ranks_parallel, group_name)
        if parallel_info and len(parallel_info) > 0 and len(parallel_info[0]) > 1:
            parallels[group_name] = parallel_info
    config.set_has_named_domain(any(name for name in parallels))
    if not config.get_has_named_domain():
        if valid_ranks and config.get_host_rank_map():
            logger.info(
                f"[SLOWNODE ALGO] 无命名通信域，回退按物理节点分组检测: "
                f"valid_ranks={valid_ranks}"
            )
            node_groups = _build_node_fallback_groups(valid_ranks)
            if node_groups:
                parallels[""] = node_groups
            else:
                return {}, []
        else:
            return {}, []
    valid_ranks.sort()
    determine_cluster_data(job_path, valid_ranks)
    return parallels, valid_ranks

def determine_cluster_data(job_path: str, valid_ranks: List[int]):
    op_metric_dir = os.path.join(config.get_output_path(), "op_metric")
    group_files = []
    csv_files = []
    if os.path.exists(op_metric_dir):
        for fn in os.listdir(op_metric_dir):
            if fn.startswith("group_info_") and fn.endswith(".json"):
                group_files.append(fn)
            elif fn.startswith("global_rank_") and fn.endswith(".csv"):
                csv_files.append(fn)
    if not group_files:
        config.set_is_cluster_data(False)
        logger.info(f"[SLOWNODE ALGO] {job_path} 无 group_info 文件，判定为非集群数据")
        return
    if len(group_files) != len(csv_files):
        config.set_is_cluster_data(False)
        logger.info(
            f"[SLOWNODE ALGO] {job_path} group_info({len(group_files)}) != global_rank csv({len(csv_files)})，"
            f"判定为非集群数据"
        )
        return
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
    node_map = config.get_host_rank_map()
    if not node_map:
        return []
    host_to_ranks: Dict[str, List[int]] = defaultdict(list)
    for rank in valid_ranks:
        host = node_map.get(str(rank), str(rank))
        host_to_ranks[host].append(int(rank))
    raw_groups = [sorted(ranks) for ranks in host_to_ranks.values()]
    groups = [g for g in raw_groups if len(g) >= 2]
    if not groups:
        return []
    logger.info(f"[SLOWNODE ALGO] 节点回退分组: {groups}")
    return groups

def get_cur_job_all_ranks_topo(job_path: str) -> Tuple[Dict[int, Any], List[int]]:
    cur_job_ranks_topo = {}
    valid_ranks = []
    op_metric_dir = os.path.join(config.get_output_path(), "op_metric")
    if not os.path.exists(op_metric_dir):
        logger.error(f"目录不存在：{op_metric_dir}")
        return {}, []
    for file_name in os.listdir(op_metric_dir):
        if not file_name.startswith("group_info_") or not file_name.endswith(".json"):
            continue
        try:
            rank_id = int(file_name[len("group_info_"):-len(".json")])
        except ValueError:
            continue
        valid_ranks.append(rank_id)
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
    for key, parallel in rank_info.items():
        if not isinstance(parallel, dict):
            logger.warning(f"[SLOWNODE ALGO] Invalid rank {rank_id} parallel domain info!")
            return False
        group_name = parallel.get(data_file_field_group_name)
        if group_name is None:
            logger.warning(f"[SLOWNODE ALGO] Rank {rank_id} parallel domain without goup_name!")
            return False
        name = group_name if isinstance(group_name, str) else str(group_name)
        if target == "" and all_names is not None:
            all_names[name] = True
            continue
        if not isinstance(group_name, str) or name != target:
            continue
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
    npu_parallel = transfer_float_array_to_int(npu_group)
    if npu_parallel is None:
        return
    if not check_rank_parallel_exist(parallel_info, rank_id, npu_parallel):
        groups.append(npu_parallel)

def transfer_float_array_to_int(npu_group: List[Any]) -> Optional[List[int]]:
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
    for parallel_domain_groups in parallel_info.values():
        if rank_id in parallel_domain_groups:
            return True
    if rank_id not in parallel_info:
        parallel_info[rank_id] = {}
    for npu in npu_group:
        parallel_info[rank_id][npu] = True
    return False

def get_detection_job_parallel_info(
    cur_job_ranks_topo: Dict[int, Any],
    target: str
) -> List[List[int]]:
    if not cur_job_ranks_topo:
        logger.warning("[SLOWNODE ALGO] Invalid job rank info!")
        return []
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
_BAND_TYPE = "HCCS"
_AGG_OP_NAME = "Total Op Info"

def detect_profiling_level(input_path: str) -> str:
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
    if _db_has_kernel_tasks(profiler_dbs[0]):
        return "level0"
    return "levelnone"

def _db_has_kernel_tasks(db_path: str) -> bool:
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
    return _sqlite_has_tables(db_path, ("CommunicationGroupMapping", "ClusterCommunicationBandwidth"))

def _analysis_db_has_bandwidth(analysis_db: str) -> bool:
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
    s = (hccl_op_name or "").strip()
    if "_" in s:
        return s.split("_")[1].lower()
    return ""

def _rank_from_profiler_filename(db_file: str) -> Optional[int]:
    base = os.path.basename(db_file)
    prefix = "ascend_pytorch_profiler_"
    if not (base.startswith(prefix) and base.endswith(".db")):
        return None
    try:
        return int(base[len(prefix):-len(".db")])
    except ValueError:
        return None

def _read_bandwidth_rows(analysis_db: str):
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

def cluster_group_map(cur):
    """CommunicationGroupMapping -> (group_name->pg_name, pg_name->[ranks])。

    检测层与结论层共用这一份实现，保证域名（pg_name）、过滤规则、rank 解析一致。
    """
    g2pg = {}
    parallels = {}
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
    return g2pg, parallels

def load_cluster_parallels(db_path: str):
    """只读 CommunicationGroupMapping，返回 {并行域: [组 rank 列表]}。"""
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        _g2pg, parallels = cluster_group_map(con.cursor())
        return parallels
    finally:
        con.close()

def load_cluster_comm(db_path: str):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    g2pg, parallels = cluster_group_map(cur)
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
