import os
import logging
from typing import Dict, List, Optional
import markdown_viz
import summary_table
import utils
logger = logging.getLogger("[VISUALIZER]")

def _filter_valid(data: Dict[int, float]) -> Dict[int, float]:
    return {k: v for k, v in data.items() if v != -99999 and v > 0}

def _format_ns(value: float) -> str:
    if value >= 1e9:
        return f"{value/1e9:.2f}s"
    elif value >= 1e6:
        return f"{value/1e6:.2f}ms"
    elif value >= 1e3:
        return f"{value/1e3:.2f}us"
    else:
        return f"{value:.0f}ns"

def plot_metric_bar(data: Dict[int, float], metric_name: str):
    filtered = _filter_valid(data)
    if not filtered:
        logger.warning(f"{metric_name} 无有效数据")
        return
    sorted_items = sorted(filtered.items(), key=lambda x: x[1], reverse=True)
    print(f"\n【{metric_name} 排序】")
    print(f"{'Rank':>8}  {'耗时':>12}")
    print("-" * 24)
    for rank_str, val in sorted_items:
        print(f"{rank_str:>8}  {_format_ns(val):>12}")

def plot_comm_bandwidth(
    domain_name: str,
    domain_groups: List[List[int]],
    step_data: Dict[str, Dict[int, float]],
    abnormal_groups: Optional[List[List[int]]] = None,
):
    cols = utils.domain_bandwidth_cols(step_data, domain_name)
    if not cols:
        return
    abnormal_set = set()
    if abnormal_groups:
        for ag in abnormal_groups:
            abnormal_set.add(",".join(str(r) for r in sorted(ag)))
    for op in sorted({o for o, _c, _col in cols}):
        rows = []
        for g in domain_groups:
            reps = utils.group_representative_bandwidth(step_data, domain_name, g)
            bw = next((b for o, _c, b in reps if o == op), None)
            if bw is None:
                continue
            rows.append((",".join(str(r) for r in sorted(g)), bw))
        if not rows:
            continue
        rows.sort(key=lambda x: x[1])
        print(f"\n【{domain_name}/{op} 带宽排序（越小越慢）】")
        print(f"{'Group':>20}  {'带宽':>14}")
        print("-" * 40)
        for label, bw in rows:
            mark = " ***" if label in abnormal_set else ""
            print(f"{label:>20}  {bw:>14.4g}{mark}")

def run_visualization(
    step_data: Dict[str, Dict[int, float]],
    parallels: Dict[str, List[List[int]]],
    valid_ranks: List[int],
    output_dir: str,
    detection_result: Optional[Dict[str, Dict[str, float]]] = None,
    data_path: str = "",
):
    result_dir = output_dir
    os.makedirs(result_dir, exist_ok=True)
    for metric_name in ["KERNEL_AICORE", "ZP_Host", "KERNEL_AIVEC", "MEMCPY_ASYNC"]:
        if metric_name in step_data:
            plot_metric_bar(step_data[metric_name], metric_name)
    if parallels:
        comm_abnormal = detection_result.get("comm", {}) if detection_result else {}
        abnormal_groups = []
        for group_key in comm_abnormal:
            try:
                _, ranks_key = summary_table._split_domain_key(group_key)
                abnormal_groups.append([int(r) for r in ranks_key.split(",")])
            except (ValueError, AttributeError, TypeError):
                pass
        for domain_name, domain_groups in parallels.items():
            if not domain_name or not domain_groups or domain_name in ("pp", "embd"):
                continue
            plot_comm_bandwidth(domain_name, domain_groups, step_data, abnormal_groups)
    pp_section = markdown_viz._pp_comm_section(parallels or {}, step_data)
    if pp_section:
        print(pp_section)
    report_path = markdown_viz.write_report(
        step_data, parallels, valid_ranks, result_dir,
        detection_result=detection_result,
        input_path=data_path,
    )
    logger.info(f"可视化完成，报告已保存至: {report_path}")
    print(f"可视化完成，报告已保存至: {report_path}")
    return report_path
