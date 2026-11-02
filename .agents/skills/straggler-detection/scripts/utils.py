import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Any
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import summary_table

def parse_bandwidth_col(prefix: str, col: str):
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
    output_data = {}
    known_cats = ["KERNEL_AICORE", "comm", "pp_comm", "cpu", "npu_bubble", "memcpy_async", "kernel_aivec"]
    all_cats = known_cats + [c for c in final_result.keys() if c not in known_cats]
    for default_cat in all_cats:
        output_data[default_cat] = []
    for category, items in final_result.items():
        if not items:
            continue
        kvs = [(k, v) for k, v in items.items()]
        is_bubble = (category == "npu_bubble")
        is_group_category = category in ("comm", "pp_comm")
        if is_bubble:
            kvs.sort(key=lambda x: x[1])
        else:
            kvs.sort(key=lambda x: x[1], reverse=True)
        rank_set_to_domain = {}
        if is_group_category and parallels:
            for domain_name, domain_groups in parallels.items():
                for group in domain_groups:
                    sorted_group = tuple(sorted(group))
                    rank_set_to_domain[sorted_group] = domain_name
        print(f"\n【{category}】全部异常项（共 {len(kvs)} 项）:")
        json_items = []
        for i, (key, value) in enumerate(kvs):
            if category == "pp_comm":
                display_key = "[" + ", ".join(key.split(",")) + "]"
            elif is_group_category:
                key_domain, ranks_key = summary_table._split_domain_key(key)
                ranks = ranks_key.split(",")
                if not key_domain:
                    rank_tuple = tuple(int(r) for r in ranks)
                    key_domain = rank_set_to_domain.get(rank_tuple, "")
                if key_domain:
                    display_key = f"{key_domain}[{', '.join(ranks)}]"
                else:
                    display_key = "[" + ", ".join(ranks) + "]"
            else:
                display_key = key
            if is_bubble:
                print(f"  {i+1}. {display_key} → bubble = {value:.3f}")
            elif category == "pp_comm":
                print(f"  {i+1}. {display_key} → 等待占比 = {value * 100:.2f}%")
            else:
                print(f"  {i+1}. {display_key} → 劣化指数 = {value:.3f}")
            json_items.append({
                "display_key": display_key,
                "metric_value": value,
                "is_abnormal": True
            })
        output_data[category] = json_items
    output_path = os.path.join(config.get_output_path(), "straggler_detection_result.json")
    try:
        with open(output_path, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\n检测结果已保存至：{output_path}")
    except Exception as e:
        print(f"[ERROR] 无法创建结果文件 {output_path}: {e}")

def _is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False

def confirm_clean(input_path: str) -> bool:
    items_to_clean = [
        "op_metric",
        "straggler_analysis_output",
        "straggler_detection_result.json",
        "straggler_detection_result",
        "detection_report.log",
        "conclusion.md",
        "joint_failure_analysis.log"
    ]
    existing = []
    for item in items_to_clean:
        item_path = os.path.join(input_path, item)
        if os.path.exists(item_path):
            existing.append(item)
    if not existing:
        return True
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
    compute, io, comm = thresholds
    if not _is_interactive():
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
    items_to_clean = [
        "op_metric",
        "straggler_analysis_output",
        "straggler_detection_result.json",
        "straggler_detection_result",
        "detection_report.log",
        "conclusion.md",
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
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在：{file_path}")
    with open(file_path, "rb") as f:
        return f.read()

def check_parallel_domain_is_exist(parallel: List[List[int]], cur_npus: int) -> bool:
    if not parallel:
        return False
    per_domain_nums = len(parallel[0]) if parallel else 0
    has_multi_card_group = False
    for domain in parallel:
        if len(domain) > 1:
            has_multi_card_group = True
        if len(domain) != per_domain_nums:
            print(f"[SLOWNODE ALGO] 通信域间卡的数量不一致:{parallel}")
            return False
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
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"san_batch_detection_result_{date_str}.json")
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(batch_results, f, indent=2, ensure_ascii=False)
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
