"""
可视化模块 - 控制台实时反馈 + 生成文本报告

功能:
1. 单卡指标排序（控制台）
2. 各并行域集合通信带宽排序（控制台）
3. 调用 markdown_viz 生成 analysis_result/detection_report.log
"""

import os
import logging
from typing import Dict, List, Optional

import markdown_viz
import utils

logger = logging.getLogger("[VISUALIZER]")


def _filter_valid(data: Dict[int, float]) -> Dict[int, float]:
    """过滤掉 -99999 和 <=0 的无效数据"""
    return {k: v for k, v in data.items() if v != -99999 and v > 0}


def _format_ns(value: float) -> str:
    """将纳秒格式化为可读单位"""
    if value >= 1e9:
        return f"{value/1e9:.2f}s"
    elif value >= 1e6:
        return f"{value/1e6:.2f}ms"
    elif value >= 1e3:
        return f"{value/1e3:.2f}us"
    else:
        return f"{value:.0f}ns"


def plot_metric_bar(data: Dict[int, float], metric_name: str):
    """控制台实时反馈：单个指标按值降序打印各卡。"""
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
    """
    控制台实时反馈：各并行域集合通信带宽（越小越慢）。
    每个 opType 每组取 count 最大的条目为代表，按带宽升序打印（最慢在前）。
    """
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
        rows.sort(key=lambda x: x[1])  # 升序：带宽最小（最慢）在前
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
    """
    运行所有可视化，生成 Markdown 报告

    参数:
        step_data: step 快照数据
        parallels: 并行域信息
        valid_ranks: 有效 rank 列表
        output_dir: 输出目录
        detection_result: 检测结果（用于异常高亮）
        data_path: 原始数据目录（用于报告中显示）
    """
    result_dir = os.path.join(output_dir, "analysis_result")
    os.makedirs(result_dir, exist_ok=True)

    # 控制台：单卡指标实时反馈
    for metric_name in ["KERNEL_AICORE", "ZP_Host", "KERNEL_AIVEC", "MEMCPY_ASYNC"]:
        if metric_name in step_data:
            plot_metric_bar(step_data[metric_name], metric_name)

    # 控制台：各并行域集合通信带宽（跳过 pp / embd）
    if parallels:
        comm_abnormal = detection_result.get("comm", {}) if detection_result else {}
        abnormal_groups = []
        for group_key in comm_abnormal:
            try:
                abnormal_groups.append([int(r) for r in group_key.split(",")])
            except (ValueError, AttributeError):
                pass

        for domain_name, domain_groups in parallels.items():
            if not domain_name or not domain_groups or domain_name in ("pp", "embd"):
                continue
            plot_comm_bandwidth(domain_name, domain_groups, step_data, abnormal_groups)

    # 控制台：pp_comm 占比排序（Δ占比 = (域内最大 PP等待 − 本卡)/会话）
    pp_section = markdown_viz._pp_comm_section(parallels or {}, step_data)
    if pp_section:
        print(pp_section)

    # 生成完整 Markdown 报告
    report_path = markdown_viz.write_report(
        step_data, parallels, valid_ranks, result_dir,
        detection_result=detection_result,
        input_path=data_path,
    )

    logger.info(f"可视化完成，报告已保存至: {report_path}")
    print(f"可视化完成，报告已保存至: {report_path}")
    return report_path
