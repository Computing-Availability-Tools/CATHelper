"""
工具函数模块
"""

import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Any

# 添加父目录到路径以便导入 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


def parse_bandwidth_col(prefix: str, col: str):
    """
    解析带宽列名 "<opType>_<count>"（给定域前缀，如 "tp_"），返回 (opType, count)。
    非带宽列（含 Duration/Count 诊断列或非数字尾）返回 (None, 0)。
    """
    if not col.startswith(prefix):
        return None, 0
    rest = col[len(prefix):]
    if rest.startswith("_"):
        rest = rest[1:]
    idx = rest.rfind("_")
    if idx <= 0 or idx == len(rest) - 1:
        return None, 0
    op_type = rest[:idx]
    try:
        count = int(rest[idx + 1:])
    except ValueError:
        return None, 0
    if op_type in ("Duration", "Count"):
        return None, 0
    return op_type, count


def domain_bandwidth_cols(step_data: Dict[str, Dict[int, float]], domain: str) -> List[Any]:
    """返回该域的所有带宽列 [(opType, count, col), ...]，按 (opType, count) 排序。"""
    prefix = domain + "_"
    out = []
    for col in step_data:
        op_type, count = parse_bandwidth_col(prefix, col)
        if op_type is None:
            continue
        out.append((op_type, count, col))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def group_representative_bandwidth(
    step_data: Dict[str, Dict[int, float]], domain: str, group: List[int]
) -> List[Any]:
    """
    对某组，每个 opType 取 count 最大的有效带宽作为代表（与检测逻辑一致）。
    返回 [(opType, count, bw), ...]（按 opType 排序）。
    """
    by_op: Dict[str, Any] = {}
    for op_type, count, col in domain_bandwidth_cols(step_data, domain):
        by_rank = step_data.get(col) or {}
        v = None
        for r in group:
            rv = by_rank.get(r)
            if rv is not None and rv != -99999 and rv > 0:
                v = rv
                break
        if v is None:
            continue
        cur = by_op.get(op_type)
        if cur is None or count > cur[0]:
            by_op[op_type] = (count, v)
    return [(op_type, cnt, bw) for op_type, (cnt, bw) in sorted(by_op.items())]


def write_result(final_result: Dict[str, Dict[str, float]], parallels: Dict[str, List[List[int]]] = None):
    """
    将检测结果写入 JSON 文件并打印到控制台

    参数:
        final_result: 检测结果
        parallels: 并行域信息（可选），用于 comm 类型的 display_key 显示域名称
    """
    output_data = {}

    # 始终包含所有已知类别（即使为空），并集上实际检测出的键（覆盖动态类别）
    known_cats = ["KERNEL_AICORE", "comm", "pp_comm", "cpu", "npu_bubble", "memcpy_async", "kernel_aivec"]
    all_cats = known_cats + [c for c in final_result.keys() if c not in known_cats]
    for default_cat in all_cats:
        output_data[default_cat] = []

    for category, items in final_result.items():
        if not items:
            continue

        # 转换为列表并排序
        kvs = [(k, v) for k, v in items.items()]

        is_bubble = (category == "npu_bubble")
        # 组键类别（每个 key 是一组 rank，如 "0,1,2"），显示时带域名称
        is_group_category = category in ("comm", "pp_comm")

        # bubble 升序（小值异常），其余降序（大值异常）
        if is_bubble:
            kvs.sort(key=lambda x: x[1])
        else:
            kvs.sort(key=lambda x: x[1], reverse=True)

        # 预构建 rank集合→域名称 的映射（组键类别需要）
        rank_set_to_domain = {}
        if is_group_category and parallels:
            for domain_name, domain_groups in parallels.items():
                for group in domain_groups:
                    sorted_group = tuple(sorted(group))
                    rank_set_to_domain[sorted_group] = domain_name

        # 打印并构建 JSON 数据
        print(f"\n【{category}】全部异常项（共 {len(kvs)} 项）:")
        json_items = []

        for i, (key, value) in enumerate(kvs):
            # 处理显示 key
            if category == "pp_comm":
                # PP 通信组：key 是组内 rank 列表，显示为 [r0, r1, ...]
                display_key = "[" + ", ".join(key.split(",")) + "]"
            elif is_group_category:
                ranks = key.split(",")
                # 查找这个 group 属于哪个域
                rank_tuple = tuple(int(r) for r in ranks)
                domain_name = rank_set_to_domain.get(rank_tuple, "")
                if domain_name:
                    display_key = f"{domain_name}[{', '.join(ranks)}]"
                else:
                    display_key = "[" + ", ".join(ranks) + "]"
            else:
                display_key = key

            if is_bubble:
                print(f"  {i+1}. {display_key} → bubble = {value:.3f}")
            else:
                print(f"  {i+1}. {display_key} → 劣化指数 = {value:.3f}")

            json_items.append({
                "display_key": display_key,
                "metric_value": value,
                "is_abnormal": True
            })

        output_data[category] = json_items

    # 写入 JSON 文件（输出到独立结果目录）
    output_path = os.path.join(config.get_output_path(), "straggler_detection_result.json")
    try:
        with open(output_path, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\n检测结果已保存至：{output_path}")
    except Exception as e:
        print(f"[ERROR] 无法创建结果文件 {output_path}: {e}")


def _is_interactive() -> bool:
    """当前是否为可交互终端（非 TTY 时各询问函数自动用默认值，避免 input() 抛 EOFError）。"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def confirm_clean(input_path: str) -> bool:
    """
    检查是否存在已有数据，并询问用户是否删除清理。

    遍历 items_to_clean 中的目录/文件，如果存在则提示用户确认是否删除。
    如果没有需要清理的内容，直接返回 True（继续执行）。

    参数:
        input_path: 数据目录路径

    返回:
        True  - 用户同意删除（或无需清理），继续执行清理+重新解析
        False - 用户不同意删除，跳过清理和重新解析
    """
    items_to_clean = [
        "op_metric",
        "straggler_analysis_output",
        "analysis_result",
        "straggler_detection_result.json",
        "straggler_detection_result",
        "joint_failure_analysis.log"
    ]

    existing = []
    for item in items_to_clean:
        item_path = os.path.join(input_path, item)
        if os.path.exists(item_path):
            existing.append(item)

    if not existing:
        return True  # 无需清理，继续执行

    if not _is_interactive():
        print(f"\n[非交互] 发现已有数据（{', '.join(existing)}），默认保留、不删除")
        return False

    print(f"\n发现以下已有数据：{', '.join(existing)}")
    while True:
        ans = input("是否删除已有数据并重新解析？(y/N): ").strip().lower()
        if ans in ('y', 'yes'):
            return True
        elif ans in ('n', 'no', ''):
            return False
        print("请输入 y 或 n")


def _ask_threshold(name: str, default: float) -> float:
    """交互式提问单个阈值（回车用默认，>0）。"""
    while True:
        ans = input(f"请输入{name}（默认 {default}，回车使用默认）: ").strip()
        if ans == "":
            return default
        try:
            v = float(ans)
            if v > 0:
                return v
            print("请输入一个大于 0 的数")
        except ValueError:
            print("请输入一个数字")


def confirm_thresholds(thresholds: tuple, keys: tuple = ("compute", "io", "comm")) -> tuple:
    """
    交互式提问检测阈值（回车用默认）。默认问三组，可用 `keys` 只问其中若干组
    （cluster 只检测 comm 时传 keys=("comm",)）。

    参数:
        thresholds: (计算类, IO/CPU 类, 通信类) 默认值元组
        keys: 需要提问的组，取值 "compute" / "io" / "comm"

    返回:
        (计算类, IO/CPU 类, 通信类) 用户确认后的阈值元组（未提问的用传入默认值）
    """
    compute, io, comm = thresholds
    if not _is_interactive():
        # 非交互（无 TTY）：不询问，直接用传入值
        return (compute, io, comm)
    print("检测阈值设置（回车使用默认值）：")
    if "compute" in keys:
        compute = _ask_threshold("计算类阈值（KERNEL_AICORE / kernel_aivec）", compute)
    if "io" in keys:
        io = _ask_threshold("IO/CPU 类阈值（cpu / memcpy_async）", io)
    if "comm" in keys:
        comm = _ask_threshold("通信类阈值（comm / pp_comm）", comm)
    return (compute, io, comm)


def confirm_no_align(default: bool = False) -> bool:
    """询问慢通信带宽是否关闭跨 rank 对齐（no-align，回车用默认）。

    关闭（yes）→ 用全体 rank 的算子时长扁平池取前 10% 均值作带宽分母；
    不关闭（no，默认）→ 跨 rank 时间窗对齐后取组内最短时长。
    """
    if not _is_interactive():
        return default
    hint = "Y/n" if default else "y/N"
    while True:
        ans = input(
            f"慢通信带宽是否关闭跨 rank 对齐（--no-align：用全体 rank 扁平池；{hint}）："
        ).strip().lower()
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y 或 n")


def clean_detection_outputs(input_path: str):
    """
    删除检测前需要清理的文件和目录
    包括：op_metric, straggler_analysis_output, analysis_result, straggler_detection_result

    参数:
        input_path: 数据目录路径

    返回:
        清理的项目数量
    """
    items_to_clean = [
        "op_metric",
        "straggler_analysis_output",
        "analysis_result",
        "straggler_detection_result.json",
        "straggler_detection_result",
        "joint_failure_analysis.log"
    ]

    cleaned_count = 0
    for item in items_to_clean:
        item_path = os.path.join(input_path, item)
        if os.path.exists(item_path):
            if os.path.isdir(item_path):
                import shutil
                shutil.rmtree(item_path)
                cleaned_count += 1
                print(f"[CLEAN] 已删除目录：{item_path}")
            else:
                os.remove(item_path)
                cleaned_count += 1
                print(f"[CLEAN] 已删除文件：{item_path}")

    return cleaned_count


def read_file(file_path: str) -> bytes:
    """
    读取文件内容
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在：{file_path}")

    with open(file_path, "rb") as f:
        return f.read()


def check_parallel_domain_is_exist(parallel: List[List[int]], cur_npus: int) -> bool:
    """
    检查并行域是否有效

    注意：不再要求域中总卡数等于本节点卡数（cur_npus），
    因为 group_info 是全集群拓扑，可能包含其他节点的卡。
    下游检测函数会通过 -99999 过滤来处理数据缺失的情况。
    """
    if not parallel:
        return False

    per_domain_nums = len(parallel[0]) if parallel else 0
    has_multi_card_group = False

    for domain in parallel:
        if len(domain) > 1:
            has_multi_card_group = True

        # 各个域卡数参差不齐
        if len(domain) != per_domain_nums:
            print(f"[SLOWNODE ALGO] 通信域间卡的数量不一致:{parallel}")
            return False

    # 并行域中只有卡本身：不存在卡间并行域
    if not has_multi_card_group:
        return False

    return True


def write_batch_result(
    batch_results: Dict[str, Any],
    output_dir: str,
    total_cases: int,
    success_count: int,
    failed_count: int,
    skipped_count: int
) -> str:
    """
    将批量检测结果写入 JSON 文件并打印汇总信息

    参数:
        batch_results: 批量检测结果字典，包含 metadata、summary、cases
        output_dir: 输出文件目录
        total_cases: 总案例数
        success_count: 成功检测数
        failed_count: 检测失败数
        skipped_count: 已跳过数

    返回:
        输出文件路径

    JSON 输出格式:
        {
          "metadata": {
            "base_path": "...",
            "thresholds": {"compute": 1.3, "io": 2.5, "comm": 1.3},
            "execution_time": "2026-06-10T10:16:15.233121",
            "total_cases": 77
          },
          "summary": {
            "success": 76,
            "failed": 0,
            "skipped": 1
          },
          "cases": {
            "case_name_1": {
              "path": "...",
              "status": "success",
              "result": {"KERNEL_AICORE": {"12": 14.12}},
              "summary": "KERNEL_AICORE: 1 个异常 (['12'])",
              "error": null,
              "reason": null
            },
            ...
          }
        }
    """
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"san_batch_detection_result_{date_str}.json")

    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(batch_results, f, indent=2, ensure_ascii=False)

        # 打印汇总信息
        print("\n" + "="*80)
        print("批量检测结果汇总")
        print("="*80)
        print(f"总案例数：{total_cases}")
        print(f"成功检测：{success_count} 个")
        print(f"检测失败：{failed_count} 个")
        print(f"已跳过：{skipped_count} 个")
        print(f"\n详细结果已保存至：{output_file}")
        print("="*80)

        return output_file

    except Exception as e:
        print(f"[ERROR] 无法创建结果文件 {output_file}: {e}")
        return ""