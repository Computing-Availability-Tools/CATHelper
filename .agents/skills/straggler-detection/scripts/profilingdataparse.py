"""
Profiling 数据解析模块
将 SQLite profiling 数据库解析为 CSV/JSON 格式
"""

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
    """Step 时间范围结构体"""
    id: int
    start_ns: int
    end_ns: int


@dataclass
class CommunicationOp:
    """通信算子结构体"""
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
    """Host 端算子结构体"""
    start_ns: int
    end_ns: int


@dataclass
class OpStat:
    """算子统计信息"""
    duration: int
    count: int


@dataclass
class PerformanceMetrics:
    """性能指标结构体"""
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
    host_duration: int = 0      # HostDuration 列：通信算子侧 Host 执行耗时均值（仅 CSV 展示，不参与检测）
    data_loader: int = 0
    durations: Dict[str, int] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)


def data_parsing(folder_path: str):
    """
    数据解析主入口
    递归查找目录下的 ascend_pytorch_profiler_*.db 文件，不使用别人处理后的中间数据（如 analysis.db、cluster_analysis.db 等）
    """
    # 递归查找所有符合条件的原始数据库文件
    db_files = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            # 只使用 ascend_pytorch_profiler_*.db 原始数据，不使用其他中间数据
            if file.startswith("ascend_pytorch_profiler_") and file.endswith(".db"):
                db_path = os.path.join(root, file)
                # 检查文件大小，跳过空文件
                if os.path.getsize(db_path) > 0:
                    db_files.append(db_path)

    if not db_files:
        logger.error(f"未找到数据库文件：{folder_path}")
        return

    # 清空节点映射，开始新一轮解析填充
    config.reset_host_rank_map()
    config.reset_rank_device_map()

    start_process(db_files, folder_path)


def data_parsing_paths(db_files: list, folder_path: str):
    """
    按显式给定的 db 列表解析（verl colocate 场景：某角色世界只解析属于它的 db）。

    db_files 为待解析的 ascend_pytorch_profiler_*.db 绝对路径列表（调用方已按角色分组）。
    每个角色世界解析前都会清空节点映射，再填充该世界自己的 rank→hostName。
    """
    db_files = [f for f in db_files if os.path.getsize(f) > 0] if db_files else []
    if not db_files:
        logger.error(f"未指定可用的数据库文件：{folder_path}")
        return

    # 每个世界独立解析前清空节点映射，避免跨世界 host 分组串扰
    config.reset_host_rank_map()
    config.reset_rank_device_map()

    start_process(db_files, folder_path)


def start_process(db_files: List[str], output_folder: str):
    """
    并发处理数据库文件
    强制重新解析原始 db 数据，不使用已存在的中间数据

    db 从 input（folder_path）递归发现，op_metric 结果写入独立输出目录
    （config.get_output_path()，多 job 场景下与原始数据目录分离）。
    """
    # 解析结果写入独立输出目录；未设置时回退到输出入目录（单 job 场景）
    write_root = config.get_output_path() or output_folder

    # 删除已存在的 op_metric 目录，强制重新解析
    output_metric_dir = os.path.join(write_root, "op_metric")
    if os.path.exists(output_metric_dir):
        try:
            import shutil
            shutil.rmtree(output_metric_dir)
            logger.info(f"已删除旧的 op_metric 目录，重新解析：{output_metric_dir}")
        except Exception as e:
            logger.warning(f"删除 op_metric 目录失败：{e}")

    # Python 中简单串行处理（如需并发可用 threading）
    for db_file in db_files:
        try:
            process_database(db_file, write_root)
        except Exception as e:
            logger.error(f"处理数据库文件 {db_file} 时出错：{e}")


def _op_name_is_optimizer_update(op_name: str) -> bool:
    """
    通过结构模式判断一个算子名是否属于“优化器更新”。

    不依赖具体的优化器名称（不同模型可能用 AdamW/SGD/LAMB/Adafactor 等），
    而是通过 PyTorch 优化器统一的方法名后缀 .step / .zero_grad 来判断，
    因此换用不同优化器也能通用识别。

    参数:
        op_name: 算子名（来自 STRING_IDS.value / PYTORCH_API.name）

    返回:
        True - 该算子属于优化器更新；False - 不是
    """
    if not op_name:
        return False
    name = op_name.strip()
    # 结构模式：方法名以 .step 或 .zero_grad 结尾
    # 例：Optimizer.step#AdamW.step、AdamW.zero_grad、SGD.step、LAMB.step
    return name.endswith(".step") or name.endswith(".zero_grad")


def detect_job_type(conn: sqlite3.Connection) -> str:
    """
    判断当前 job 的类型（training / rollout）。

    通过扫描 PYTORCH_API 中的算子名（JOIN STRING_IDS），
    若存在优化器更新算子（.step / .zero_grad）则判定为 training，否则为 rollout。

    返回:
        "training" - 存在优化器更新算子
        "rollout"  - 不存在优化器更新算子
    """
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
    """
    处理单个数据库文件并将结果保存到 CSV
    """
    try:
        conn = sqlite3.connect(db_file_path)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        logger.error(f"无法打开数据库文件 {db_file_path}: {e}")
        return False

    try:
        # 启用 WAL 模式提升性能
        conn.execute("PRAGMA journal_mode=WAL;")

        # 创建索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_string_ids_value ON STRING_IDS(value);")
        # 注意：DEVICE_OP 表在某些数据库版本中不存在，跳过该索引创建
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_device_op_time ON DEVICE_OP(startNs, endNs);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_time_type ON TASK(startNs, endNs, taskType);")

        # 判断并记录当前 job 类型（training/rollout）
        config.set_job_type(detect_job_type(conn))

        # 从文件名提取 global rank
        global_rank = extract_global_rank_from_filename(db_file_path)
        if global_rank is None:
            logger.error(f"无法从文件名提取 rank: {db_file_path}")
            return False

        # 创建输出目录
        output_metric_dir = os.path.join(output_dir, "op_metric")
        os.makedirs(output_metric_dir, exist_ok=True)

        output_file = os.path.join(output_metric_dir, f"global_rank_{global_rank}.csv")
        group_info_file = os.path.join(output_metric_dir, f"group_info_{global_rank}.json")

        # 获取并行域信息
        parallel_group_info = get_parallel_group_info(conn, group_info_file)
        if not parallel_group_info:
            logger.error("获取 parallel_group_info 失败")
            return False

        xp_to_group_name, group_name_to_global_ranks, group_name_to_id = create_group_name_dicts(parallel_group_info)

        logger.info("ParallelGroup Info:")
        for k, v in group_name_to_global_ranks.items():
            logger.info(f"  {k}: {v}")
        logger.info(f"Group Name to ID: {group_name_to_id}")

        # 获取该卡的节点信息（hostName），存入内存 config.HostRankMap
        get_host_info(conn, global_rank)

        # 获取所有 step 时间
        all_steps = get_all_step_times(conn)
        if not all_steps:
            logger.warning("未找到任何 step 数据")
            return True

        # 创建聚合 step：开始时间为所有 step 的最小值，结束时间为最大值
        min_start = min(s.start_ns for s in all_steps)
        max_end = max(s.end_ns for s in all_steps)
        aggregated_step = StepTime(id=0, start_ns=min_start, end_ns=max_end)

        # 使用聚合 step 进行检测
        pms = []
        time_diff = time_diff_for_step(conn, xp_to_group_name, aggregated_step)
        if time_diff is not None:
            time_diff.step_index = 0
            time_diff.step_duration = max_end - min_start

            # 保留数据（包括带有 -99999 标记的数据）
            # 只要 KERNEL_AICORE 有值或者是 -99999 标记的数据，都写入 CSV
            pms.append(time_diff)

        # 写入 CSV
        write_results_to_csv(output_file, pms)
        logger.info(f"成功写入 CSV 文件：{output_file}")
        return True

    finally:
        conn.close()


def extract_global_rank_from_filename(db_file_path: str) -> Optional[str]:
    """从文件名提取 global rank"""
    base_name = os.path.basename(db_file_path)
    prefix = "ascend_pytorch_profiler_"
    suffix = ".db"

    if not base_name.startswith(prefix) or not base_name.endswith(suffix):
        return None

    return base_name[len(prefix):-len(suffix)]


def get_parallel_group_info(conn: sqlite3.Connection, filename: str) -> Dict[str, Any]:
    """
    获取并行域信息
    """
    try:
        cursor = conn.execute("SELECT value FROM META_DATA WHERE name = 'parallel_group_info'")
        row = cursor.fetchone()
        if not row:
            return {}

        value = row[0]
        result = json.loads(value)

        # 写入文件（只写一次）
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
    """
    创建 group_name 字典

    返回:
        xp_to_group_name: {"dp": "group_name_115", ...}
        group_name_to_global_ranks: {"dp": [0, 2, 4, ...], ...}
        group_name_to_id: {"dp": 115, ...}  # 直接从键名中提取的数字 ID
    """
    xp_to_group_name = {}
    group_name_to_global_ranks = {}
    group_name_to_id = {}

    for key, v in data.items():
        if isinstance(v, dict) and "group_name" in v:
            group_name = v["group_name"]
            xp_to_group_name[group_name] = key
            group_name_to_global_ranks[group_name] = v.get("global_ranks", [])

            # 从键名中提取数字 ID，如 "group_name_115" -> 115
            if key.startswith("group_name_"):
                try:
                    group_id = int(key[len("group_name_"):])
                    group_name_to_id[group_name] = group_id
                except ValueError:
                    pass

    return xp_to_group_name, group_name_to_global_ranks, group_name_to_id


def get_host_info(conn: sqlite3.Connection, global_rank: str):
    """
    从 HOST_INFO 表获取该卡所属节点信息（hostUid / hostName），
    从 NPU_INFO 表获取该卡的物理设备 id，均存入内存：
      - config.HostRankMap（hostUid，供 CPU 检测按物理节点分组）
      - config.RankDeviceMap（hostName + npu_id，供报告"物理设备"列展示）
    不再生成 node_hostname_map.json 文件。
    """
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
    """
    获取所有 step 时间范围
    """
    # 优先从 STEP_TIME 表获取
    if table_exists(conn, "STEP_TIME"):
        return get_step_times_from_step_time(conn)

    # 尝试从 TASK 表获取
    step_times = get_step_times_from_task(conn)
    if step_times:
        return step_times

    # 返回默认值
    return [StepTime(id=-1, start_ns=float('-inf'), end_ns=float('inf'))]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """检查表是否存在"""
    cursor = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
        (table_name,)
    )
    return cursor.fetchone()[0] == 1


def get_step_times_from_step_time(conn: sqlite3.Connection) -> List[StepTime]:
    """从 STEP_TIME 表获取数据"""
    cursor = conn.execute(
        "SELECT id, startNs, endNs FROM STEP_TIME ORDER BY id DESC"
    )

    steps = []
    for row in cursor:
        steps.append(StepTime(id=row[0], start_ns=row[1], end_ns=row[2]))

    # 反转顺序
    steps.reverse()
    return steps


def get_step_times_from_task(conn: sqlite3.Connection) -> List[StepTime]:
    """从 TASK 表推导 step 时间"""
    re_pattern = re.compile(r'step\s+(\d+)')

    cursor = conn.execute("SELECT id, value FROM STRING_IDS")

    step_times = []
    for row in cursor:
        string_id, value = row[0], row[1]
        matches = re_pattern.search(value)
        if not matches:
            continue

        step_id = int(matches.group(1))

        # 查询 TASK 表
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

    # 按 ID 排序
    step_times.sort(key=lambda x: x.id)
    return step_times


def time_diff_for_step(
    conn: sqlite3.Connection,
    xp_to_group_name: Dict[str, str],
    step_time: StepTime
) -> Optional[PerformanceMetrics]:
    """
    计算 step 的时间差
    当无法获取数据时，使用 -99999 标记缺失字段
    """
    # 定义无效数据标记
    INVALID_MARKER = -99999

    if not xp_to_group_name:
        return PerformanceMetrics(durations={}, counts={})

    # 查询 DataLoader ID
    data_loader_id = query_data_loader_id(conn)

    # 初始化 metrics
    metrics = PerformanceMetrics(
        durations={xp: 0 for xp in xp_to_group_name},
        counts={xp: 0 for xp in xp_to_group_name}
    )

    # 获取 group_name → id 映射
    group_names = list(xp_to_group_name.values())
    if not group_names:
        return metrics

    placeholders = ",".join("?" * len(group_names))
    cursor = conn.execute(
        f"SELECT value, id FROM STRING_IDS WHERE value IN ({placeholders})",
        group_names
    )

    group_name_to_id = {row[0]: row[1] for row in cursor}

    # 构建 id → xp 反向映射
    id_to_xp = {}
    group_name_ids = []
    for xp, group_name in xp_to_group_name.items():
        if group_name in group_name_to_id:
            group_id = group_name_to_id[group_name]
            id_to_xp[group_id] = xp
            group_name_ids.append(group_id)

    if not group_name_ids:
        # 无通信域信息，但仍尝试从 KERNEL_AICORE 获取 Host 耗时
        kernel_host_durations = get_kernel_host_durations(conn, step_time)
        if kernel_host_durations:
            metrics.zp_host = calculate_mean(kernel_host_durations)
        else:
            metrics.zp_host = INVALID_MARKER

        kernel_duration = get_avg_kernel_task_duration(conn, step_time)
        if kernel_duration != 0:
            metrics.zp_kernel = kernel_duration
        # 标记通信相关字段为无效
        metrics.zp_device = INVALID_MARKER
        metrics.zp_duration = INVALID_MARKER
        metrics.zp_bubble = INVALID_MARKER
        return metrics

    # 获取 device ops
    device_ops = get_device_op_list(conn, group_name_ids, step_time)
    if not device_ops:
        # 无通信算子，但仍尝试从 KERNEL_AICORE 获取 Host 耗时
        kernel_host_durations = get_kernel_host_durations(conn, step_time)
        if kernel_host_durations:
            metrics.zp_host = calculate_mean(kernel_host_durations)
        else:
            metrics.zp_host = INVALID_MARKER

        kernel_duration = get_avg_kernel_task_duration(conn, step_time)
        if kernel_duration != 0:
            metrics.zp_kernel = kernel_duration
        # 标记通信相关字段为无效
        metrics.zp_device = INVALID_MARKER
        metrics.zp_duration = INVALID_MARKER
        metrics.zp_bubble = INVALID_MARKER
        return metrics

    # 收集 connection IDs
    connection_id_set = set(op.connection_id for op in device_ops)

    # 获取 Host 端时间
    cann_map = get_host_op_from_table(conn, "CANN_API", list(connection_id_set))
    mstx_map = get_host_op_from_table(conn, "MSTX_EVENTS", list(connection_id_set))

    # 填充 Host 时间
    host_durations = []
    bubble_durations = []
    comm_intervals = []
    ops_by_xp: Dict[str, List[CommunicationOp]] = {}

    for op in device_ops:
        conn_id = op.connection_id

        # 填充 Host 时间
        if conn_id in cann_map:
            host_op = cann_map[conn_id]
            op.h_start_ns = host_op.start_ns
            op.h_end_ns = host_op.end_ns
        elif conn_id in mstx_map:
            host_op = mstx_map[conn_id]
            op.h_start_ns = host_op.start_ns
            op.h_end_ns = host_op.end_ns

        # 收集 Host 耗时
        if op.h_start_ns > 0 and op.h_end_ns >= op.h_start_ns:
            host_durations.append(op.h_end_ns - op.h_start_ns)
            bubble = op.start_ns - op.h_end_ns
            if bubble > 0:
                bubble_durations.append(bubble)

        comm_intervals.append((op.start_ns, op.end_ns))

        # 按 xp 分组
        if op.domain_id in id_to_xp:
            xp = id_to_xp[op.domain_id]
            if xp not in ops_by_xp:
                ops_by_xp[xp] = []
            ops_by_xp[xp].append(op)

    # 计算 ZP_Host 和 ZP_Bubble
    # 可靠性设计：同时纳入 KERNEL_AICORE 的 host 耗时，确保无通信算子时也能获取 CPU 数据
    kernel_host_durations = get_kernel_host_durations(conn, step_time)
    all_host_durations = host_durations + kernel_host_durations
    metrics.zp_host = calculate_mean(all_host_durations)
    metrics.zp_bubble = calculate_mean(bubble_durations)
    # HostDuration 列 = 通信算子侧 host 执行耗时均值（不含 kernel，区别于 zp_host；仅 CSV 展示，不参与检测）
    metrics.host_duration = calculate_mean(host_durations)

    # 计算通信总时长
    total_comm_duration = merge_intervals_simple(comm_intervals)
    step_duration = step_time.end_ns - step_time.start_ns
    non_comm_time = step_duration - total_comm_duration

    if non_comm_time < 0:
        logger.warning(f"通信总耗时超过 step 总耗时 (step={step_duration}, comm={total_comm_duration})")
        non_comm_time = 0

    metrics.zp_device = non_comm_time
    metrics.zp_duration = total_comm_duration

    # 计算各 xp 组的通信时长
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

    # DataLoader 和 Kernel
    metrics.data_loader = query_data_loader_duration(conn, data_loader_id, step_time)
    kernel_duration = get_avg_kernel_task_duration(conn, step_time)
    if kernel_duration != 0:
        metrics.zp_kernel = kernel_duration

    # 参照 KERNEL_AICORE 生成方式，额外统计 MEMCPY_ASYNC / KERNEL_AIVEC 的平均耗时
    memcpy_async_duration = get_avg_task_duration_by_name(conn, step_time, "MEMCPY_ASYNC")
    if memcpy_async_duration != 0:
        metrics.memcpy_async = memcpy_async_duration

    kernel_aivec_duration = get_avg_task_duration_by_name(conn, step_time, "KERNEL_AIVEC")
    if kernel_aivec_duration != 0:
        metrics.kernel_aivec = kernel_aivec_duration

    return metrics


def get_device_op_list(
    conn: sqlite3.Connection,
    group_name_ids: List[int],
    step_time: StepTime
) -> List[CommunicationOp]:
    """获取通信算子列表"""
    if not table_exists(conn, "COMMUNICATION_OP"):
        return []

    placeholders = ",".join("?" * len(group_name_ids))
    # 注意 SELECT 列顺序：0=opName, 1=startNs, 2=endNs, 3=connectionId,
    # 4=count, 5=_rowid_, 6=groupName。下面按此顺序取下标。
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
            start_ns=row[1],           # startNs
            end_ns=row[2],             # endNs
            connection_id=row[3],      # connectionId
            count=int(row[4]) if isinstance(row[4], str) else row[4],  # count
            op_stream_index=row[5],    # _rowid_
            domain_id=row[6],          # groupName
        ))

    return device_ops


def get_host_op_from_table(
    conn: sqlite3.Connection,
    table_name: str,
    connection_ids: List[int]
) -> Dict[int, HostOp]:
    """从指定表获取 Host 端时间"""
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
    """合并区间并返回总覆盖时长"""
    if not intervals:
        return 0

    # 按 Start 排序
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
    """计算均值（过滤负数）"""
    valid_values = [v for v in values if v >= 0]
    if not valid_values:
        return 0
    return int(sum(valid_values) / len(valid_values) + 0.5)


def calculate_mid_mean_pair(stats: List[OpStat]) -> Tuple[int, int]:
    """计算中间均值"""
    n = len(stats)
    if n == 0:
        return 0, 0

    sum_dur = sum(s.duration for s in stats)
    sum_cnt = sum(s.count for s in stats)

    mean_duration = int(sum_dur / n + 0.5)
    mean_count = int(sum_cnt / n + 0.5)

    return mean_duration, mean_count


def query_data_loader_id(conn: sqlite3.Connection) -> int:
    """查询 DataLoader ID"""
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
    """查询 DataLoader 耗时"""
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
    """获取 Kernel 平均耗时"""
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
    """获取指定 taskType 名称算子的平均耗时，参照 KERNEL_AICORE（get_avg_kernel_task_duration）的生成方式"""
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


def get_kernel_host_durations(
    conn: sqlite3.Connection,
    step_time: StepTime
) -> List[int]:
    """获取 KERNEL_AICORE 类型算子的 Host 端耗时列表"""
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

    # 通过 connectionId 查 CANN_API / MSTX_EVENTS 获取 host 耗时
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
    """将结果写入 CSV 文件"""
    if not pms:
        logger.warning("无数据可写入")
        return

    # 收集所有 XP keys
    xp_keys = set()
    for pm in pms:
        xp_keys.update(pm.durations.keys())
        xp_keys.update(pm.counts.keys())

    sorted_xp_keys = sorted(xp_keys)

    # 构造表头
    headers = [
        "StepIndex", "StepDuration", "ZP_Device", "ZP_Duration",
        "ZP_Host", "ZP_Bubble", "ZP_Count", "KERNEL_AICORE",
        "MEMCPY_ASYNC", "KERNEL_AIVEC", "HostDuration",
        "DataLoader"
    ]
    for xp in sorted_xp_keys:
        headers.extend([f"{xp}_Duration", f"{xp}_Count"])

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
            writer.writerow(record)

    logger.info(f"成功写入 {len(pms)} 条记录到 {output_file}")


# ======================================================================
# 慢通信带宽回填
#
# 解析完成后，重新扫描 .db、重建并行拓扑、跨卡对齐集合通信算子，为每个
# (opType, count) 组合计算带宽（count / 组内最快 10% 最短耗时均值），并把这些
# 带宽写回 op_metric/global_rank_{N}.csv 的动态列 "<domain>_<opType>_<count>"。
# 该回填只对集合通信域有效；pp / embd 以及 Send/Recv 被跳过。
# ======================================================================

PP_DOMAIN_NAME = "pp"
EMBD_DOMAIN_NAME = "embd"
WALLCLOCK_TOLERANCE_NS = 5_000_000  # 5 ms

# 纯集合通信算子白名单：只有这些算子族参与慢通信带宽检测
# （allReduce / reduceScatter / Send / Recv 等被排除）。
PURE_COMM_TYPES = {
    "allgather", "allgatherv", "allgatherbase",
    "alltoall", "alltoallv", "alltoallsingle",
    "scatter", "gather",
}


@dataclass
class BwOp:
    """一个通信算子的带宽统计所需字段。"""
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
    """把算子名归一为纯集合通信 token，不在白名单时返回空串。"""
    s = _strip_vendor_prefix(name)
    s = _leading_letters(s).lower()
    return s if s in PURE_COMM_TYPES else ""


class _BwIndex:
    """按 (opType, count) 分桶并维护按 start 排序，供 wall-clock 匹配。"""

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
    """跨卡对齐集合通信算子，返回 per-(opType,count) 带宽（count / 最快 10% 最短耗时均值）。"""
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
        res[k] = k[1] / mean_dur  # bandwidth = count / mean_dur
    return res


def _compute_bandwidth_from_ops_flat(
    members: Dict[int, List[BwOp]], ranks: List[int]
) -> Dict[Tuple[str, int], float]:
    """
    --no-align：不做跨 rank 对齐，把组内全体 rank
    符合条件的算子时长按 (opType,count) 汇成一个扁平池，直接取池内前 10% 均值作分母，
    带宽 = count / 该均值。
    """
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
    """把该 rank 的所有 step 时间窗合并为一个覆盖整段 profiling 的窗口。"""
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
    """查询通信算子，返回带 opName(STRING_IDS id) 与 count 的原始行。"""
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
    """加载某 rank 在某域类型下的集合通信算子（白名单过滤 + count 下限）。"""
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
    """把动态带宽列追加写回某 rank 的 CSV；已存在的列跳过。"""
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
    """递归发现 ascend_pytorch_profiler_*.db（跳过空文件）。"""
    out = []
    for root, _dirs, files in os.walk(input_path):
        for file in files:
            if file.startswith("ascend_pytorch_profiler_") and file.endswith(".db"):
                db_path = os.path.join(root, file)
                if os.path.getsize(db_path) > 0:
                    out.append(db_path)
    return out


def backfill_slow_domain_bandwidth(input_path: str, db_files: Optional[List[str]] = None):
    """
    慢通信带宽回填主入口。
    在 DataParsing 之后、检测读取 CSV 之前调用。失败不致命：调用方记录日志并继续。
    """
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

    # 发现域分组，按 "type|sorted-ranks" 去重。
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

    # 只保留有 db 信息的 rank。
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


# ======================================================================
# PP 等待回填（PP 慢通信检测的辅助数据）
#
# PP 组从 parallel_group_info 的 pp 项读取（读不到就整段跳过）；对每张卡统计「点对点传输
# 结束 → 其下一次集合通信结束」的间隔之和，写回该卡 CSV 动态列 "PP_WaitGap"（锚点：出现
# BatchSendRecv 用 BatchSendRecv，否则用 Send/Recv）。检测端按比较域逐 rank、按 PP 组上报。
# ======================================================================

def _is_pp_recv(name: str) -> bool:
    """判断算子名是否为 PP 点对点接收（Recv/Receive）。"""
    s = _leading_letters(_strip_vendor_prefix(name)).lower()
    return s.startswith("recv") or s.startswith("receive")


def _load_all_comm_ops(
    conn: sqlite3.Connection, step_time: StepTime
) -> List[Dict[str, Any]]:
    """加载某 rank 在时间窗内的全部通信算子（带名字），按 startNs 升序。"""
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
    """判断算子名是否为 PP 点对点发送（Send）。"""
    s = _leading_letters(_strip_vendor_prefix(name)).lower()
    return s.startswith("send")


def _p2p_dir(name: str) -> str:
    """把算子名归一化为点对点方向：BatchSendRecv->'both'，Send->'send'，
    Recv/Receive->'recv'，非点对点（集合通信等）返回空串 ''。"""
    if _is_batch_send_recv(name):
        return "both"
    if _is_pp_send(name):
        return "send"
    if _is_pp_recv(name):
        return "recv"
    return ""


def _collect_pp_groups(rank_to_info: Dict[int, Dict[str, Any]]) -> List[List[int]]:
    """
    从各 rank 的 parallel_group_info 里收集 group_name == "pp" 的分组。

    读不到 pp 项时返回 []（调用方据此跳过 PP 检测——只有 profiler 明确声明了 PP 域，
    才认为这些点对点传输属于 PP；否则可能属于 CP/Ring Attention，不检测）。
    """
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
    """是否为 BatchSendRecv（兼容 batch_send_recv 等下划线命名）。"""
    return "batchsendrecv" in _strip_vendor_prefix(name).lower().replace("_", "")


def _compute_pp_wait_value(ops: List[Dict[str, Any]], batch_mode: bool) -> Optional[int]:
    """
    每卡「锚点结束 → 下一次集合通信结束」的间隔之和（无下一次集合通信则不计）。
    锚点：batch 模式（PP 链路上出现 BatchSendRecv）下为 BatchSendRecv；否则为 Send / Recv。
    ops 已按 startNs 升序；无有效值返回 None。
    """
    total = 0
    found = False
    for i, o in enumerate(ops):
        d = _p2p_dir(o["name"])
        anchor = (d == "both") if batch_mode else (d in ("send", "recv"))
        if not anchor:
            continue
        # 找其后第一个集合通信（跳过中间连续的点对点传输）
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
    """
    回填每卡 PP 等待列（PP_WaitGap）：点对点传输结束 → 下一次集合通信结束 的间隔之和。

    锚点：batch 模式（PP 链路上出现 BatchSendRecv）下为 BatchSendRecv；否则为 Send / Recv。
    同时按各 rank 合并窗口的最大值填充 config.PP_SESSION_NS（会话时长）。
    """
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
