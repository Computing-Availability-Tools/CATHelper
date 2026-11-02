import sqlite3
import csv
import json
import os
import re
import math
import bisect
import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
import config
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("[DATA PROCESS]")

@dataclass

class StepTime:
    id: int
    start_ns: int
    end_ns: int

@dataclass

class CommunicationOp:
    start_ns: int
    end_ns: int
    h_start_ns: int = 0
    h_end_ns: int = 0
    count: int = 0
    connection_id: int = 0
    domain_id: int = 0
    op_stream_index: int = 0

@dataclass

class HostOp:
    start_ns: int
    end_ns: int

@dataclass

class OpStat:
    duration: int
    count: int

@dataclass

class PerformanceMetrics:
    step_index: int = 0
    step_duration: int = 0
    zp_device: int = 0
    zp_duration: int = 0
    zp_host: int = 0
    zp_bubble: int = 0
    zp_count: int = 0
    zp_kernel: int = 0
    memcpy_async: int = 0
    kernel_aivec: int = 0
    host_duration: int = 0
    data_loader: int = 0
    memcpy_count: int = 0
    memcpy_linked: int = -1
    memcpy_external: int = -1
    durations: Dict[str, int] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)

def data_parsing(folder_path: str):
    db_files = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.startswith("ascend_pytorch_profiler_") and file.endswith(".db"):
                db_path = os.path.join(root, file)
                if os.path.getsize(db_path) > 0:
                    db_files.append(db_path)
    if not db_files:
        logger.error(f"未找到数据库文件：{folder_path}")
        return
    config.reset_host_rank_map()
    config.reset_rank_device_map()
    start_process(db_files, folder_path)

def data_parsing_paths(db_files: list, folder_path: str):
    db_files = [f for f in db_files if os.path.getsize(f) > 0] if db_files else []
    if not db_files:
        logger.error(f"未指定可用的数据库文件：{folder_path}")
        return
    config.reset_host_rank_map()
    config.reset_rank_device_map()
    start_process(db_files, folder_path)

def load_host_info_only(folder_path: str, db_files: List[str] = None) -> None:
    """只读取节点/设备映射，不解析任何指标。

    `clean=no` 复用已有 op_metric 时会跳过解析，导致 config 里的
    host_rank_map / rank_device_map 为空；cpu 检测会因此从"按节点聚合"
    退化到"每 N 张卡一组"，同数据换一套口径。这里补读一次。
    """
    files = [f for f in (db_files or []) if os.path.getsize(f) > 0]
    if not files:
        files = []
        for root, _dirs, names in os.walk(folder_path):
            for name in names:
                if name.startswith("ascend_pytorch_profiler_") and name.endswith(".db"):
                    path = os.path.join(root, name)
                    if os.path.getsize(path) > 0:
                        files.append(path)
    if not files:
        logger.warning("未找到数据库文件，跳过节点信息读取")
        return
    config.reset_host_rank_map()
    config.reset_rank_device_map()
    first = True
    for db_path in files:
        global_rank = extract_global_rank_from_filename(db_path)
        if global_rank is None:
            continue
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            get_host_info(conn, global_rank)
            if first:
                # clean=no 复用 op_metric 时也要拿到 Job 类型（training / rollout）
                config.set_job_type(detect_job_type(conn))
                first = False
        except Exception as e:
            logger.warning(f"读取节点信息失败（{db_path}）：{e}")
        finally:
            if conn is not None:
                conn.close()

def start_process(db_files: List[str], output_folder: str):
    write_root = config.get_output_path() or output_folder
    output_metric_dir = os.path.join(write_root, "op_metric")
    if os.path.exists(output_metric_dir):
        try:
            import shutil
            shutil.rmtree(output_metric_dir)
            logger.info(f"已删除旧的 op_metric 目录，重新解析：{output_metric_dir}")
        except Exception as e:
            logger.warning(f"删除 op_metric 目录失败：{e}")
    for db_file in db_files:
        try:
            process_database(db_file, write_root)
        except Exception as e:
            logger.error(f"处理数据库文件 {db_file} 时出错：{e}")

def _op_name_is_optimizer_update(op_name: str) -> bool:
    if not op_name:
        return False
    name = op_name.strip()
    return name.endswith(".step") or name.endswith(".zero_grad")

def detect_job_type(conn: sqlite3.Connection) -> str:
    has_optimizer = False
    try:
        if table_exists(conn, "PYTORCH_API") and table_exists(conn, "STRING_IDS"):
            cursor = conn.execute(
                "SELECT DISTINCT s.value FROM PYTORCH_API p "
                "JOIN STRING_IDS s ON p.name = s.id "
                "WHERE s.value IS NOT NULL"
            )
            for (op_name,) in cursor:
                if _op_name_is_optimizer_update(op_name):
                    has_optimizer = True
                    break
    except Exception as e:
        logger.warning(f"检测优化器更新算子失败：{e}")
    job_type = "training" if has_optimizer else "rollout"
    logger.info(f"Job 类型判定：{job_type}（存在优化器更新算子={has_optimizer}）")
    return job_type

def process_database(db_file_path: str, output_dir: str) -> bool:
    try:
        conn = sqlite3.connect(db_file_path)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        logger.error(f"无法打开数据库文件 {db_file_path}: {e}")
        return False
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_string_ids_value ON STRING_IDS(value);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_time_type ON TASK(startNs, endNs, taskType);")
        config.set_job_type(detect_job_type(conn))
        global_rank = extract_global_rank_from_filename(db_file_path)
        if global_rank is None:
            logger.error(f"无法从文件名提取 rank: {db_file_path}")
            return False
        output_metric_dir = os.path.join(output_dir, "op_metric")
        os.makedirs(output_metric_dir, exist_ok=True)
        output_file = os.path.join(output_metric_dir, f"global_rank_{global_rank}.csv")
        group_info_file = os.path.join(output_metric_dir, f"group_info_{global_rank}.json")
        parallel_group_info = get_parallel_group_info(conn, group_info_file)
        if not parallel_group_info:
            logger.error("获取 parallel_group_info 失败")
            return False
        xp_to_group_name, group_name_to_global_ranks, group_name_to_id = create_group_name_dicts(parallel_group_info)
        logger.info("ParallelGroup Info:")
        for k, v in group_name_to_global_ranks.items():
            logger.info(f"  {k}: {v}")
        logger.info(f"Group Name to ID: {group_name_to_id}")
        get_host_info(conn, global_rank)
        all_steps = get_all_step_times(conn)
        if not all_steps:
            logger.warning("未找到任何 step 数据")
            return True
        min_start = min(s.start_ns for s in all_steps)
        max_end = max(s.end_ns for s in all_steps)
        aggregated_step = StepTime(id=0, start_ns=min_start, end_ns=max_end)
        pms = []
        time_diff = time_diff_for_step(conn, xp_to_group_name, aggregated_step)
        if time_diff is not None:
            time_diff.step_index = 0
            time_diff.step_duration = max_end - min_start
            pms.append(time_diff)
        write_results_to_csv(output_file, pms)
        logger.info(f"成功写入 CSV 文件：{output_file}")
        return True
    finally:
        conn.close()

def extract_global_rank_from_filename(db_file_path: str) -> Optional[str]:
    base_name = os.path.basename(db_file_path)
    prefix = "ascend_pytorch_profiler_"
    suffix = ".db"
    if not base_name.startswith(prefix) or not base_name.endswith(suffix):
        return None
    return base_name[len(prefix):-len(suffix)]

def get_parallel_group_info(conn: sqlite3.Connection, filename: str) -> Dict[str, Any]:
    try:
        cursor = conn.execute("SELECT value FROM META_DATA WHERE name = 'parallel_group_info'")
        row = cursor.fetchone()
        if not row:
            return {}
        value = row[0]
        result = json.loads(value)
        if filename:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with open(filename, 'w') as f:
                json.dump(result, f, indent=2)
            logger.info(f"已成功写入 parallel_group_info: {filename}")
        return result
    except Exception as e:
        logger.error(f"获取 parallel_group_info 失败：{e}")
        return {}

def create_group_name_dicts(data: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, Any], Dict[str, int]]:
    xp_to_group_name = {}
    group_name_to_global_ranks = {}
    group_name_to_id = {}
    for key, v in data.items():
        if isinstance(v, dict) and "group_name" in v:
            group_name = v["group_name"]
            xp_to_group_name[group_name] = key
            group_name_to_global_ranks[group_name] = v.get("global_ranks", [])
            if key.startswith("group_name_"):
                try:
                    group_id = int(key[len("group_name_"):])
                    group_name_to_id[group_name] = group_id
                except ValueError:
                    pass
    return xp_to_group_name, group_name_to_global_ranks, group_name_to_id

def get_host_info(conn: sqlite3.Connection, global_rank: str):
    host_uid = None
    host_name = None
    try:
        cursor = conn.execute("SELECT hostUid, hostName FROM HOST_INFO LIMIT 1")
        row = cursor.fetchone()
        if row:
            host_uid = str(row[0]) if row[0] is not None else None
            host_name = str(row[1]) if row[1] is not None else None
    except Exception as e:
        logger.warning(f"读取 HOST_INFO 失败：{e}")
    npu_id = None
    try:
        cursor = conn.execute("SELECT id FROM NPU_INFO LIMIT 1")
        row = cursor.fetchone()
        if row and row[0] is not None:
            npu_id = str(row[0])
    except Exception as e:
        logger.warning(f"读取 NPU_INFO 失败：{e}")
    config.set_host_rank_map(global_rank, host_uid)
    config.set_rank_device_map(global_rank, host_name, npu_id)
    logger.info(
        f"节点信息：rank{global_rank} hostUid={host_uid} hostName={host_name} npuId={npu_id}"
    )

def get_all_step_times(conn: sqlite3.Connection) -> List[StepTime]:
    if table_exists(conn, "STEP_TIME"):
        return get_step_times_from_step_time(conn)
    step_times = get_step_times_from_task(conn)
    if step_times:
        return step_times
    return [StepTime(id=-1, start_ns=float('-inf'), end_ns=float('inf'))]

def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
        (table_name,)
    )
    return cursor.fetchone()[0] == 1

def get_step_times_from_step_time(conn: sqlite3.Connection) -> List[StepTime]:
    cursor = conn.execute(
        "SELECT id, startNs, endNs FROM STEP_TIME ORDER BY id DESC"
    )
    steps = []
    for row in cursor:
        steps.append(StepTime(id=row[0], start_ns=row[1], end_ns=row[2]))
    steps.reverse()
    return steps

def get_step_times_from_task(conn: sqlite3.Connection) -> List[StepTime]:
    re_pattern = re.compile(r'step\s+(\d+)')
    cursor = conn.execute("SELECT id, value FROM STRING_IDS")
    step_times = []
    for row in cursor:
        string_id, value = row[0], row[1]
        matches = re_pattern.search(value)
        if not matches:
            continue
        step_id = int(matches.group(1))
        cursor2 = conn.execute(
            "SELECT connectionId FROM MSTX_EVENTS WHERE message = ?",
            (str(string_id),)
        )
        conn_id_row = cursor2.fetchone()
        if not conn_id_row:
            continue
        conn_id = conn_id_row[0]
        cursor3 = conn.execute(
            "SELECT startNs, endNs FROM TASK WHERE connectionId = ?",
            (conn_id,)
        )
        task_row = cursor3.fetchone()
        if not task_row:
            continue
        step_times.append(StepTime(id=step_id, start_ns=task_row[0], end_ns=task_row[1]))
    step_times.sort(key=lambda x: x.id)
    return step_times

def time_diff_for_step(
    conn: sqlite3.Connection,
    xp_to_group_name: Dict[str, str],
    step_time: StepTime
) -> Optional[PerformanceMetrics]:
    INVALID_MARKER = -99999
    if not xp_to_group_name:
        return PerformanceMetrics(durations={}, counts={})
    data_loader_id = query_data_loader_id(conn)
    metrics = PerformanceMetrics(
        durations={xp: 0 for xp in xp_to_group_name},
        counts={xp: 0 for xp in xp_to_group_name}
    )
    group_names = list(xp_to_group_name.values())
    if not group_names:
        return metrics
    placeholders = ",".join("?" * len(group_names))
    cursor = conn.execute(
        f"SELECT value, id FROM STRING_IDS WHERE value IN ({placeholders})",
        group_names
    )
    group_name_to_id = {row[0]: row[1] for row in cursor}
    id_to_xp = {}
    group_name_ids = []
    for xp, group_name in xp_to_group_name.items():
        if group_name in group_name_to_id:
            group_id = group_name_to_id[group_name]
            id_to_xp[group_id] = xp
            group_name_ids.append(group_id)
    if not group_name_ids:
        kernel_host_durations = get_kernel_host_durations(conn, step_time)
        if kernel_host_durations:
            metrics.zp_host = calculate_mean(kernel_host_durations)
        else:
            metrics.zp_host = INVALID_MARKER
        kernel_duration = get_avg_kernel_task_duration(conn, step_time)
        if kernel_duration != 0:
            metrics.zp_kernel = kernel_duration
        metrics.zp_device = INVALID_MARKER
        metrics.zp_duration = INVALID_MARKER
        metrics.zp_bubble = INVALID_MARKER
        return metrics
    device_ops = get_device_op_list(conn, group_name_ids, step_time)
    if not device_ops:
        kernel_host_durations = get_kernel_host_durations(conn, step_time)
        if kernel_host_durations:
            metrics.zp_host = calculate_mean(kernel_host_durations)
        else:
            metrics.zp_host = INVALID_MARKER
        kernel_duration = get_avg_kernel_task_duration(conn, step_time)
        if kernel_duration != 0:
            metrics.zp_kernel = kernel_duration
        metrics.zp_device = INVALID_MARKER
        metrics.zp_duration = INVALID_MARKER
        metrics.zp_bubble = INVALID_MARKER
        return metrics
    connection_id_set = set(op.connection_id for op in device_ops)
    cann_map = get_host_op_from_table(conn, "CANN_API", list(connection_id_set))
    mstx_map = get_host_op_from_table(conn, "MSTX_EVENTS", list(connection_id_set))
    host_durations = []
    bubble_durations = []
    comm_intervals = []
    ops_by_xp: Dict[str, List[CommunicationOp]] = {}
    for op in device_ops:
        conn_id = op.connection_id
        if conn_id in cann_map:
            host_op = cann_map[conn_id]
            op.h_start_ns = host_op.start_ns
            op.h_end_ns = host_op.end_ns
        elif conn_id in mstx_map:
            host_op = mstx_map[conn_id]
            op.h_start_ns = host_op.start_ns
            op.h_end_ns = host_op.end_ns
        if op.h_start_ns > 0 and op.h_end_ns >= op.h_start_ns:
            host_durations.append(op.h_end_ns - op.h_start_ns)
            bubble = op.start_ns - op.h_end_ns
            if bubble > 0:
                bubble_durations.append(bubble)
        comm_intervals.append((op.start_ns, op.end_ns))
        if op.domain_id in id_to_xp:
            xp = id_to_xp[op.domain_id]
            if xp not in ops_by_xp:
                ops_by_xp[xp] = []
            ops_by_xp[xp].append(op)
    kernel_host_durations = get_kernel_host_durations(conn, step_time)
    all_host_durations = host_durations + kernel_host_durations
    metrics.zp_host = calculate_mean(all_host_durations)
    metrics.zp_bubble = calculate_mean(bubble_durations)
    metrics.host_duration = calculate_mean(host_durations)
    total_comm_duration = merge_intervals_simple(comm_intervals)
    step_duration = step_time.end_ns - step_time.start_ns
    non_comm_time = step_duration - total_comm_duration
    if non_comm_time < 0:
        logger.warning(f"通信总耗时超过 step 总耗时 (step={step_duration}, comm={total_comm_duration})")
        non_comm_time = 0
    metrics.zp_device = non_comm_time
    metrics.zp_duration = total_comm_duration
    valid_xp_groups = {"tp", "ep", "exp", "pp", "cp", "tp_exp", "dp_modulo_exp_cp", "embd", "mc2", "dp"}
    for xp, ops in ops_by_xp.items():
        xp_key = xp.lower()
        if xp_key not in valid_xp_groups or not ops:
            metrics.durations[xp] = 0
            metrics.counts[xp] = 0
            continue
        stats = [OpStat(duration=op.end_ns - op.start_ns, count=int(op.count) if isinstance(op.count, str) else op.count)
                 for op in ops if op.end_ns - op.start_ns >= 0 and (int(op.count) if isinstance(op.count, str) else op.count) >= 0]
        if stats:
            mean_dur, mean_cnt = calculate_mid_mean_pair(stats)
            metrics.durations[xp] = mean_dur
            metrics.counts[xp] = mean_cnt
    metrics.data_loader = query_data_loader_duration(conn, data_loader_id, step_time)
    kernel_duration = get_avg_kernel_task_duration(conn, step_time)
    if kernel_duration != 0:
        metrics.zp_kernel = kernel_duration
    memcpy_async_duration = get_avg_task_duration_by_name(conn, step_time, "MEMCPY_ASYNC")
    if memcpy_async_duration != 0:
        metrics.memcpy_async = memcpy_async_duration
    (metrics.memcpy_count,
     metrics.memcpy_linked,
     metrics.memcpy_external) = get_memcpy_counters(conn, step_time)
    kernel_aivec_duration = get_avg_task_duration_by_name(conn, step_time, "KERNEL_AIVEC")
    if kernel_aivec_duration != 0:
        metrics.kernel_aivec = kernel_aivec_duration
    return metrics

def get_device_op_list(
    conn: sqlite3.Connection,
    group_name_ids: List[int],
    step_time: StepTime
) -> List[CommunicationOp]:
    if not table_exists(conn, "COMMUNICATION_OP"):
        return []
    placeholders = ",".join("?" * len(group_name_ids))
    cursor = conn.execute(
        f"""
        SELECT opName, startNs, endNs, connectionId, count, _rowid_, groupName
        FROM COMMUNICATION_OP
        WHERE groupName IN ({placeholders})
          AND startNs >= ?
          AND endNs <= ?
        ORDER BY startNs ASC
        """,
        group_name_ids + [step_time.start_ns, step_time.end_ns]
    )
    rows = cursor.fetchall()
    device_ops = []
    for row in rows:
        device_ops.append(CommunicationOp(
            start_ns=row[1],
            end_ns=row[2],
            connection_id=row[3],
            count=int(row[4]) if isinstance(row[4], str) else row[4],
            op_stream_index=row[5],
            domain_id=row[6],
        ))
    return device_ops

def get_host_op_from_table(
    conn: sqlite3.Connection,
    table_name: str,
    connection_ids: List[int]
) -> Dict[int, HostOp]:
    results = {}
    if not connection_ids:
        return results
    if not table_exists(conn, table_name):
        logger.warning(f"表 {table_name} 不存在")
        return results
    placeholders = ",".join("?" * len(connection_ids))
    cursor = conn.execute(
        f"SELECT startNs, endNs, connectionId FROM {table_name} WHERE connectionId IN ({placeholders})",
        connection_ids
    )
    for row in cursor:
        results[row[2]] = HostOp(start_ns=row[0], end_ns=row[1])
    return results

def merge_intervals_simple(intervals: List[Tuple[int, int]]) -> int:
    if not intervals:
        return 0
    intervals.sort(key=lambda x: x[0])
    total = 0
    current_end = intervals[0][0]
    for start, end in intervals:
        if start > current_end:
            total += end - start
            current_end = end
        elif end > current_end:
            total += end - current_end
            current_end = end
    return total

def calculate_mean(values: List[int]) -> int:
    valid_values = [v for v in values if v >= 0]
    if not valid_values:
        return 0
    return int(sum(valid_values) / len(valid_values) + 0.5)

def calculate_mid_mean_pair(stats: List[OpStat]) -> Tuple[int, int]:
    n = len(stats)
    if n == 0:
        return 0, 0
    sum_dur = sum(s.duration for s in stats)
    sum_cnt = sum(s.count for s in stats)
    mean_duration = int(sum_dur / n + 0.5)
    mean_count = int(sum_cnt / n + 0.5)
    return mean_duration, mean_count

def query_data_loader_id(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "SELECT id FROM STRING_IDS WHERE value = ?",
        ("dataloader",)
    )
    row = cursor.fetchone()
    return row[0] if row else -1

def query_data_loader_duration(
    conn: sqlite3.Connection,
    data_loader_id: int,
    step_time: StepTime
) -> int:
    if data_loader_id == -1:
        return 0
    cursor = conn.execute(
        """
        SELECT startNs, endNs FROM MSTX_EVENTS
        WHERE message = ? AND startNs >= ? AND endNs <= ?
        LIMIT 1
        """,
        (str(data_loader_id), step_time.start_ns, step_time.end_ns)
    )
    row = cursor.fetchone()
    if not row:
        return 0
    start_ns, end_ns = row
    if end_ns < start_ns:
        return 0
    return end_ns - start_ns

def get_avg_kernel_task_duration(conn: sqlite3.Connection, step_time: StepTime) -> int:
    cursor = conn.execute(
        """
        SELECT AVG(t.endNs - t.startNs)
        FROM TASK t
        INNER JOIN STRING_IDS s ON t.taskType = s.id
        WHERE s.value IN ('KERNEL_AICORE')
          AND t.startNs >= ?
          AND t.endNs <= ?
        """,
        (step_time.start_ns, step_time.end_ns)
    )
    row = cursor.fetchone()
    if row and row[0] is not None:
        return int(round(row[0]))
    return 0

def get_avg_task_duration_by_name(conn: sqlite3.Connection, step_time: StepTime, name: str) -> int:
    cursor = conn.execute(
        """
        SELECT AVG(t.endNs - t.startNs)
        FROM TASK t
        INNER JOIN STRING_IDS s ON t.taskType = s.id
        WHERE s.value = ?
          AND t.startNs >= ?
          AND t.endNs <= ?
        """,
        (name, step_time.start_ns, step_time.end_ns)
    )
    row = cursor.fetchone()
    if row and row[0] is not None:
        return int(round(row[0]))
    return 0

MEMCPY_EXTERNAL_API = "aclrtMemcpyAsync"

def get_memcpy_counters(
    conn: sqlite3.Connection, step_time: StepTime
) -> Tuple[int, int, int]:
    """返回 (MEMCPY_ASYNC 任务总数, 能关联到 CANN_API 的任务数, 外部 aclrtMemcpyAsync 数)。

    CANN_API 表缺失时后两项返回 -1，表示"不可用"，调用方不得当成 0 处理。
    """
    cursor = conn.execute(
        """
        SELECT t.connectionId
        FROM TASK t
        INNER JOIN STRING_IDS s ON t.taskType = s.id
        WHERE s.value = 'MEMCPY_ASYNC'
          AND t.startNs >= ?
          AND t.endNs <= ?
        """,
        (step_time.start_ns, step_time.end_ns)
    )
    connection_ids = [row[0] for row in cursor if row[0] is not None]
    total = len(connection_ids)
    if total == 0 or not table_exists(conn, "CANN_API"):
        return total, -1, -1
    placeholders = ",".join("?" * len(connection_ids))
    rows = conn.execute(
        f"SELECT connectionId, name FROM CANN_API WHERE connectionId IN ({placeholders})",
        connection_ids
    ).fetchall()
    name_ids = sorted({row[1] for row in rows if row[1] is not None})
    id_to_name = _string_map_by_ids(conn, name_ids)
    cid_to_name = {row[0]: id_to_name.get(row[1], str(row[1])) for row in rows}
    linked = sum(1 for cid in connection_ids if cid in cid_to_name)
    external = sum(1 for cid in connection_ids
                   if cid_to_name.get(cid) == MEMCPY_EXTERNAL_API)
    return total, linked, external

def get_kernel_host_durations(
    conn: sqlite3.Connection,
    step_time: StepTime
) -> List[int]:
    cursor = conn.execute(
        """
        SELECT t.connectionId
        FROM TASK t
        INNER JOIN STRING_IDS s ON t.taskType = s.id
        WHERE s.value IN ('KERNEL_AICORE')
          AND t.startNs >= ?
          AND t.endNs <= ?
        """,
        (step_time.start_ns, step_time.end_ns)
    )
    connection_ids = [row[0] for row in cursor if row[0] is not None]
    if not connection_ids:
        return []
    cann_map = get_host_op_from_table(conn, "CANN_API", connection_ids)
    mstx_map = get_host_op_from_table(conn, "MSTX_EVENTS", connection_ids)
    host_durations = []
    for conn_id in connection_ids:
        if conn_id in cann_map:
            host_op = cann_map[conn_id]
        elif conn_id in mstx_map:
            host_op = mstx_map[conn_id]
        else:
            continue
        if host_op.start_ns > 0 and host_op.end_ns >= host_op.start_ns:
            host_durations.append(host_op.end_ns - host_op.start_ns)
    return host_durations

def write_results_to_csv(output_file: str, pms: List[PerformanceMetrics]):
    if not pms:
        logger.warning("无数据可写入")
        return
    xp_keys = set()
    for pm in pms:
        xp_keys.update(pm.durations.keys())
        xp_keys.update(pm.counts.keys())
    sorted_xp_keys = sorted(xp_keys)
    headers = [
        "StepIndex", "StepDuration", "ZP_Device", "ZP_Duration",
        "ZP_Host", "ZP_Bubble", "ZP_Count", "KERNEL_AICORE",
        "MEMCPY_ASYNC", "KERNEL_AIVEC", "HostDuration",
        "DataLoader"
    ]
    for xp in sorted_xp_keys:
        headers.extend([f"{xp}_Duration", f"{xp}_Count"])
    headers.extend(["MEMCPY_ASYNC_Count", "MEMCPY_LINKED_Count", "MEMCPY_EXTERNAL_Count"])
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for pm in pms:
            record = [
                str(pm.step_index),
                str(pm.step_duration),
                str(pm.zp_device),
                str(pm.zp_duration),
                str(pm.zp_host),
                str(pm.zp_bubble),
                str(pm.zp_count),
                str(pm.zp_kernel),
                str(pm.memcpy_async),
                str(pm.kernel_aivec),
                str(pm.host_duration),
                str(pm.data_loader),
            ]
            for xp in sorted_xp_keys:
                record.extend([
                    str(pm.durations.get(xp, 0)),
                    str(pm.counts.get(xp, 0))
                ])
            record.extend([
                str(pm.memcpy_count),
                str(pm.memcpy_linked),
                str(pm.memcpy_external),
            ])
            writer.writerow(record)
    logger.info(f"成功写入 {len(pms)} 条记录到 {output_file}")
PP_DOMAIN_NAME = "pp"
EMBD_DOMAIN_NAME = "embd"
WALLCLOCK_TOLERANCE_NS = 5_000_000
PURE_COMM_TYPES = {
    "allgather", "allgatherv", "allgatherbase",
    "alltoall", "alltoallv", "alltoallsingle",
    "scatter", "gather",
}

@dataclass

class BwOp:
    op_type: str
    count: int
    start: int
    end: int

def _strip_vendor_prefix(name: str) -> str:
    lower = name.lower()
    for p in ("hcom", "hccl", "acl"):
        if lower.startswith(p):
            return name[len(p):].lstrip("_")
    return name

def _leading_letters(s: str) -> str:
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    return s[:i]

def _pure_comm_kind(name: str) -> str:
    s = _strip_vendor_prefix(name)
    s = _leading_letters(s).lower()
    return s if s in PURE_COMM_TYPES else ""

class _BwIndex:
    def __init__(self, ops: List[BwOp]):
        self.ops = ops
        self.buckets: Dict[Tuple[str, int], List[int]] = {}
        self.starts: Dict[Tuple[str, int], List[int]] = {}
        for i, op in enumerate(ops):
            k = (op.op_type, op.count)
            self.buckets.setdefault(k, []).append(i)
        for k, idxs in self.buckets.items():
            idxs.sort(key=lambda i: ops[i].start)
            self.starts[k] = [ops[i].start for i in idxs]
    def wallclock(self, op: BwOp, tol: int) -> int:
        k = (op.op_type, op.count)
        bucket = self.buckets.get(k)
        if not bucket:
            return -1
        starts = self.starts[k]
        n = len(bucket)
        i = bisect.bisect_left(starts, op.start)
        nearest, nd = -1, float('inf')
        for j in (i - 1, i):
            if 0 <= j < n:
                d = abs(starts[j] - op.start)
                if d < nd:
                    nd, nearest = d, bucket[j]
        lo = bisect.bisect_left(starts, op.start - tol)
        hi = bisect.bisect_left(starts, op.end)
        best, bov = -1, 0
        for j in range(lo, min(hi, n)):
            idx = bucket[j]
            o = self.ops[idx]
            ov = min(op.end, o.end) - max(op.start, o.start)
            if ov > bov:
                bov, best = ov, idx
        if best >= 0 and bov > 0:
            return best
        if nearest >= 0 and nd <= tol:
            return nearest
        return -1

def _compute_bandwidth_from_ops(
    members: Dict[int, List[BwOp]], ranks: List[int]
) -> Dict[Tuple[str, int], float]:
    if not ranks:
        return {}
    base_ops = members.get(ranks[0], [])
    if not base_ops:
        return {}
    idxs = {r: _BwIndex(members[r]) for r in ranks}
    combos: Dict[Tuple[str, int], List[int]] = {}
    for base in base_ops:
        dur = [base.end - base.start]
        ok = True
        for r in ranks[1:]:
            j = idxs[r].wallclock(base, WALLCLOCK_TOLERANCE_NS)
            if j < 0:
                ok = False
                break
            o = members[r][j]
            dur.append(o.end - o.start)
        if not ok:
            continue
        k = (base.op_type, base.count)
        combos.setdefault(k, []).append(min(dur))
    res: Dict[Tuple[str, int], float] = {}
    for k, durs in combos.items():
        valid = [d for d in durs if d > 0]
        if not valid:
            continue
        valid.sort()
        n = int(math.ceil(len(valid) * 0.10))
        if n < 1:
            n = 1
        mean_dur = sum(valid[:n]) / n
        res[k] = k[1] / mean_dur
    return res

def _compute_bandwidth_from_ops_flat(
    members: Dict[int, List[BwOp]], ranks: List[int]
) -> Dict[Tuple[str, int], float]:
    combos: Dict[Tuple[str, int], List[int]] = {}
    for r in ranks:
        for op in members.get(r, []):
            d = op.end - op.start
            if d > 0:
                combos.setdefault((op.op_type, op.count), []).append(d)
    res: Dict[Tuple[str, int], float] = {}
    for k, durs in combos.items():
        valid = [d for d in durs if d > 0]
        if not valid:
            continue
        valid.sort()
        n = int(math.ceil(len(valid) * 0.10))
        if n < 1:
            n = 1
        mean_dur = sum(valid[:n]) / n
        res[k] = k[1] / mean_dur
    return res

def _merged_step(conn: sqlite3.Connection) -> StepTime:
    steps = get_all_step_times(conn)
    valid = [s for s in steps if s.start_ns != float('-inf') and s.end_ns != float('inf')]
    if not valid:
        return StepTime(id=-1, start_ns=-2 ** 62, end_ns=2 ** 62)
    min_s = min(s.start_ns for s in valid)
    max_e = max(s.end_ns for s in valid)
    return StepTime(id=-1, start_ns=min_s, end_ns=max_e)

def _batch_query_string_ids(conn: sqlite3.Connection, keys: List[str]) -> Dict[str, int]:
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    cursor = conn.execute(
        f"SELECT value, id FROM STRING_IDS WHERE value IN ({placeholders})", keys
    )
    return {row[0]: row[1] for row in cursor}

def _string_map_by_ids(conn: sqlite3.Connection, ids: List[int]) -> Dict[int, str]:
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"SELECT id, value FROM STRING_IDS WHERE id IN ({placeholders})", ids
    )
    return {row[0]: row[1] for row in cursor}

def _query_domain_ops(
    conn: sqlite3.Connection, group_name_ids: List[int], step_time: StepTime
) -> List[Dict[str, int]]:
    if not table_exists(conn, "COMMUNICATION_OP") or not group_name_ids:
        return []
    placeholders = ",".join("?" * len(group_name_ids))
    cursor = conn.execute(
        f"""
        SELECT opName, startNs, endNs, count FROM COMMUNICATION_OP
        WHERE groupName IN ({placeholders}) AND startNs >= ? AND endNs <= ?
        ORDER BY startNs ASC
        """,
        group_name_ids + [step_time.start_ns, step_time.end_ns],
    )
    out = []
    for row in cursor:
        out.append({
            "op_name": row[0],
            "start": row[1],
            "end": row[2],
            "count": int(row[3]) if isinstance(row[3], str) else row[3],
        })
    return out

def _load_domain_ops(
    conn: sqlite3.Connection,
    pgi: Dict[str, Any],
    typ: str,
    step_time: StepTime,
    min_count: int,
) -> List[BwOp]:
    keys = [k for k, v in pgi.items() if isinstance(v, dict) and v.get("group_name") == typ]
    if not keys:
        return []
    id_map = _batch_query_string_ids(conn, keys)
    if not id_map:
        return []
    group_name_ids = [id_map[k] for k in keys if k in id_map]
    if not group_name_ids:
        return []
    raw_ops = _query_domain_ops(conn, group_name_ids, step_time)
    if not raw_ops:
        return []
    name_ids = list({op["op_name"] for op in raw_ops})
    name_map = _string_map_by_ids(conn, name_ids)
    out = []
    for op in raw_ops:
        nm = name_map.get(op["op_name"], "")
        k = _pure_comm_kind(nm)
        if not k:
            continue
        if op["count"] < min_count:
            continue
        out.append(BwOp(
            op_type=k,
            count=op["count"], start=op["start"], end=op["end"],
        ))
    return out

def _backfill_bandwidth_csv(path: str, cols: Dict[str, str]):
    if not os.path.exists(path):
        return
    with open(path, 'r', newline='') as f:
        records = list(csv.reader(f))
    if not records:
        return
    header = records[0]
    existing = set(header)
    new_cols = sorted([name for name in cols if name not in existing])
    if not new_cols:
        return
    header = header + new_cols
    records[0] = header
    for i in range(1, len(records)):
        for name in new_cols:
            records[i].append(cols[name])
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(records)

def discover_db_files(input_path: str) -> List[str]:
    out = []
    for root, _dirs, files in os.walk(input_path):
        for file in files:
            if file.startswith("ascend_pytorch_profiler_") and file.endswith(".db"):
                db_path = os.path.join(root, file)
                if os.path.getsize(db_path) > 0:
                    out.append(db_path)
    return out

def backfill_slow_domain_bandwidth(input_path: str, db_files: Optional[List[str]] = None):
    min_count = config.SLOW_COMM_MIN_COUNT
    if min_count <= 0:
        min_count = 1000
    if db_files is None:
        db_files = discover_db_files(input_path)
    if not db_files:
        return
    rank_to_info: Dict[int, Dict[str, Any]] = {}
    for db_path in db_files:
        rank_str = extract_global_rank_from_filename(db_path)
        if rank_str is None:
            continue
        try:
            r = int(rank_str)
        except ValueError:
            continue
        try:
            conn = sqlite3.connect(db_path)
        except Exception:
            continue
        try:
            pgi = get_parallel_group_info(conn, None)
        except Exception:
            pgi = {}
        conn.close()
        if not pgi:
            continue
        rank_to_info[r] = {"rank": rank_str, "path": db_path, "pgi": pgi}
    if not rank_to_info:
        return
    group_map: Dict[str, Dict[str, Any]] = {}
    for ri in rank_to_info.values():
        for val in ri["pgi"].values():
            if not isinstance(val, dict):
                continue
            typ = val.get("group_name", "")
            if not typ:
                continue
            grp = [int(x) for x in val.get("global_ranks", [])]
            if not grp:
                continue
            grp.sort()
            key = f"{typ}|{','.join(map(str, grp))}"
            if key not in group_map:
                group_map[key] = {"typ": typ, "ranks": grp}
    for g in group_map.values():
        g["ranks"] = [r for r in g["ranks"] if r in rank_to_info]
    for g in group_map.values():
        typ = g["typ"]
        ranks = g["ranks"]
        if typ == PP_DOMAIN_NAME or typ == EMBD_DOMAIN_NAME:
            continue
        if len(ranks) < 2:
            continue
        members: Dict[int, List[BwOp]] = {}
        valid = True
        for r in ranks:
            ri = rank_to_info[r]
            try:
                conn = sqlite3.connect(ri["path"])
            except Exception:
                valid = False
                break
            step = _merged_step(conn)
            ops = _load_domain_ops(conn, ri["pgi"], typ, step, min_count)
            conn.close()
            members[r] = ops
        if not valid:
            continue
        if config.get_comm_no_align():
            res = _compute_bandwidth_from_ops_flat(members, ranks)
        else:
            res = _compute_bandwidth_from_ops(members, ranks)
        if not res:
            continue
        cols: Dict[str, str] = {}
        for (op_type, count), bw in res.items():
            cols[f"{typ}_{op_type}_{count}"] = repr(bw)
        write_root = config.get_output_path() or input_path
        for r in ranks:
            path = os.path.join(write_root, "op_metric", f"global_rank_{r}.csv")
            _backfill_bandwidth_csv(path, cols)
    logger.info("[SLOW-DOMAIN] 慢通信带宽回填完成")

def _is_pp_recv(name: str) -> bool:
    s = _leading_letters(_strip_vendor_prefix(name)).lower()
    return s.startswith("recv") or s.startswith("receive")

def _load_all_comm_ops(
    conn: sqlite3.Connection, step_time: StepTime
) -> List[Dict[str, Any]]:
    if not table_exists(conn, "COMMUNICATION_OP"):
        return []
    cursor = conn.execute(
        """
        SELECT opName, startNs, endNs FROM COMMUNICATION_OP
        WHERE startNs >= ? AND endNs <= ?
        ORDER BY startNs ASC
        """,
        (step_time.start_ns, step_time.end_ns),
    )
    rows = cursor.fetchall()
    if not rows:
        return []
    name_ids = list({row[0] for row in rows})
    name_map = _string_map_by_ids(conn, name_ids)
    out = []
    for row in rows:
        out.append({
            "name": name_map.get(row[0], ""),
            "start": row[1],
            "end": row[2],
        })
    return out

def _is_pp_send(name: str) -> bool:
    s = _leading_letters(_strip_vendor_prefix(name)).lower()
    return s.startswith("send")

def _p2p_dir(name: str) -> str:
    if _is_batch_send_recv(name):
        return "both"
    if _is_pp_send(name):
        return "send"
    if _is_pp_recv(name):
        return "recv"
    return ""

def _collect_pp_groups(rank_to_info: Dict[int, Dict[str, Any]]) -> List[List[int]]:
    seen = set()
    groups = []
    for ri in rank_to_info.values():
        for val in ri["pgi"].values():
            if not isinstance(val, dict) or val.get("group_name") != "pp":
                continue
            try:
                grp = sorted(int(x) for x in val.get("global_ranks", []))
            except (TypeError, ValueError):
                continue
            if len(grp) < 2:
                continue
            key = tuple(grp)
            if key not in seen:
                seen.add(key)
                groups.append(grp)
    groups.sort(key=lambda g: (min(g), g))
    return groups

def _is_batch_send_recv(name: str) -> bool:
    return "batchsendrecv" in _strip_vendor_prefix(name).lower().replace("_", "")

def _compute_pp_wait_value(ops: List[Dict[str, Any]], batch_mode: bool) -> Optional[int]:
    total = 0
    found = False
    for i, o in enumerate(ops):
        d = _p2p_dir(o["name"])
        anchor = (d == "both") if batch_mode else (d in ("send", "recv"))
        if not anchor:
            continue
        nxt = None
        for j in range(i + 1, len(ops)):
            if _p2p_dir(ops[j]["name"]) == "":
                nxt = ops[j]
                break
        if nxt is None:
            continue
        gap = nxt["end"] - o["end"]
        if gap > 0:
            total += gap
            found = True
    return total if found else None

def backfill_pp_wait(input_path: str, db_files: Optional[List[str]] = None):
    if db_files is None:
        db_files = discover_db_files(input_path)
    if not db_files:
        return
    rank_to_info: Dict[int, Dict[str, Any]] = {}
    for db_path in db_files:
        rank_str = extract_global_rank_from_filename(db_path)
        if rank_str is None:
            continue
        try:
            rank = int(rank_str)
        except ValueError:
            continue
        try:
            conn = sqlite3.connect(db_path)
        except Exception:
            continue
        try:
            pgi = get_parallel_group_info(conn, None)
        except Exception:
            pgi = {}
        conn.close()
        rank_to_info[rank] = {"path": db_path, "pgi": pgi or {}}
    if not rank_to_info:
        return
    pp_groups = _collect_pp_groups(rank_to_info)
    if not pp_groups:
        logger.info("[SLOW-DOMAIN] 无 PP 分组 metadata，跳过 PP 等待回填")
        return
    write_root = config.get_output_path() or input_path
    all_ranks = sorted({r for g in pp_groups for r in g})
    ops_cache: Dict[int, List[Dict[str, Any]]] = {}
    session_ns = 0
    def rank_ops(rank: int) -> List[Dict[str, Any]]:
        nonlocal session_ns
        if rank not in ops_cache:
            ops_cache[rank] = []
            info = rank_to_info.get(rank)
            if info is not None:
                try:
                    conn = sqlite3.connect(info["path"])
                    step = _merged_step(conn)
                    ops_cache[rank] = _load_all_comm_ops(conn, step)
                    conn.close()
                    dur = step.end_ns - step.start_ns
                    if 0 < dur < (1 << 62) and dur > session_ns:
                        session_ns = dur
                except Exception:
                    ops_cache[rank] = []
        return ops_cache[rank]
    loaded = {r: rank_ops(r) for r in all_ranks}
    batch_mode = any(_p2p_dir(o["name"]) == "both"
                     for ops in loaded.values() for o in ops)
    config.set_pp_session_ns(session_ns)
    column = config.PP_WAIT_GAP_COLUMN
    for rank in all_ranks:
        val = _compute_pp_wait_value(loaded[rank], batch_mode)
        if val is None:
            continue
        path = os.path.join(write_root, "op_metric", f"global_rank_{rank}.csv")
        _backfill_bandwidth_csv(path, {column: repr(float(val))})
    logger.info(
        f"[SLOW-DOMAIN] PP 等待回填完成（batch_mode={batch_mode}, session={session_ns}ns）"
    )
