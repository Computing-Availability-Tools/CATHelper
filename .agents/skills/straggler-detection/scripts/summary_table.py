"""
检测结果汇总表工具模块。

提供 ASCII 框线表底层渲染与「检测结果 → 表格单元格」的格式化，
供控制台最终汇总表（build_summary_table）与文本报告摘要（markdown_viz）复用。
"""

import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


# 最终汇总表"类别"列显示名（大小写对齐 op_metric 的指标列名）
CATEGORY_DISPLAY = {
    "KERNEL_AICORE": "KERNEL_AICORE",
    "kernel_aivec": "KERNEL_AIVEC",
    "memcpy_async": "MEMCPY_ASYNC",
    "comm": "comm",
    "pp_comm": "pp_comm",
    "cpu": "cpu",
    "npu_bubble": "npu_bubble",
}


def _disp_len(text: str) -> int:
    """估算字符串在终端中的显示宽度（CJK/全角字符按 2 列计）。"""
    return sum(2 if 0x2E80 <= ord(ch) <= 0x9FFF or 0xFF00 <= ord(ch) <= 0xFFEF else 1
               for ch in text)


def _disp_ljust(text: str, width: int) -> str:
    """按显示宽度左填充空格，保证 CJK 与 ASCII 在终端中对齐。"""
    pad = width - _disp_len(text)
    return text + " " * max(pad, 0)


def _render_box_table(headers: List[str], rows: List[List[str]]) -> str:
    """渲染 ASCII 表格（+--+--+），按列自动对齐（考虑 CJK 显示宽度）。

    使用 ASCII 边框而非 Unicode 框线：框线字符（─│┌┐等）属于 East Asian
    Ambiguous 宽度，在 CJK 字体/终端下按 2 列渲染，会导致 Notepad、Linux cat
    等纯文本环境错位；ASCII 的 + - | 固定 1 列，任何环境都能对齐。
    """
    all_rows = [headers] + rows
    ncols = len(headers)
    widths = [0] * ncols
    for row in all_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _disp_len(cell))

    def _line(left, mid, right, fill="-"):
        # 单元格总宽 = 内容宽 + 左右各 1 空格边距（与数据行 " " + cell + " " 对齐）
        return left + mid.join(fill * (w + 2) for w in widths) + right

    top = _line("+", "+", "+")
    mid = _line("+", "+", "+")
    bot = _line("+", "+", "+")
    last_idx = len(all_rows) - 1
    lines = [top]
    for idx, row in enumerate(all_rows):
        cells = "|" + "|".join(" " + _disp_ljust(cell, widths[i]) + " "
                               for i, cell in enumerate(row)) + "|"
        lines.append(cells)
        # 表头后、以及非最后一行的数据行后画横线；最后一行直接接底边框，
        # 避免出现多余的分隔线（底部看起来像空行）
        if idx < last_idx:
            lines.append(mid)
    lines.append(bot)
    return "\n".join(lines)


def _domain_of_group(parallels: dict, ranks_key: str) -> str:
    """
    在 parallels 中查找某个 rank 组（如 "0,1"）所属的并行域。
    用于 comm 组类别展示，能区分该通信组是 tp/ep/dp 等哪个域。
    匹配不到时返回 ""（此时退化为仅展示 rank）。
    """
    if not parallels:
        return ""
    try:
        target = sorted(int(r) for r in ranks_key.split(","))
    except (TypeError, ValueError):
        return ""
    for domain_name, groups in parallels.items():
        if not domain_name:
            continue
        for group in groups:
            try:
                g = sorted(int(x) for x in group)
            except (TypeError, ValueError):
                continue
            if g == target:
                return domain_name
    return ""


def _parse_ranks_from_key(key: str):
    """把类别结果的 key 解析成 rank 列表（单卡 key 是 "0"，组 key 是 "0,1,2"）"""
    return [int(r) for r in key.split(",")]


# 物理设备列显示宽度上限（超出以 ... 截断）
DEVICE_DISPLAY_LIMIT = 30


def _rank_to_device(rank: int) -> str:
    """把 rank 转成物理设备标识：{hostName}:Device{npu_id}（取自 config.RankDeviceMap）。"""
    info = config.get_rank_device_map().get(str(rank))
    if info:
        hn = info.get("host_name") or "?"
        nid = info.get("npu_id") or "?"
        return f"{hn}:Device{nid}"
    return f"rank{rank}"


def _truncate_display(text: str, limit: int = DEVICE_DISPLAY_LIMIT) -> str:
    """按显示宽度截断文本，超出部分以 ... 结尾（ASCII，避免歧义宽）。"""
    suffix = "..."
    if _disp_len(text) <= limit:
        return text
    out = ""
    for ch in text:
        if _disp_len(out + ch) > limit - _disp_len(suffix):
            break
        out += ch
    return out + suffix


def _summary_cells(category: str, items: dict, parallels: dict = None):
    """
    生成某类别在汇总表中的 (异常卡, 劣化指数, 物理设备) 三列文本。

    - 单卡类别: 异常卡="rank 0, 3"；劣化指数="0:1.397，3:1.398"
    - cpu:     异常卡="worker3"（节点显示名 hostName）；劣化指数="worker3:3.719"
    - comm:    异常卡="tp[0, 1]"；劣化指数="tp[0, 1]:2.5"
    - pp_comm: 异常卡="[0, 2, 4, 6]"（整组）；劣化指数="[0, 2, 4, 6]:0.08"
    物理设备列列出涉及卡的 hostName:Device，过长时截断。
    """
    sorted_items = sorted(items.items(), key=lambda x: -x[1])

    if category == "pp_comm":
        # PP 通信组：key 是组内 rank 列表，按组展示
        cards_parts = []
        deg_parts = []
        dev_parts = []
        for key, val in sorted_items:
            ranks = _parse_ranks_from_key(key)
            label = "[" + ", ".join(str(r) for r in ranks) + "]"
            cards_parts.append(label)
            deg_parts.append(f"{label}:{val:g}")
            dev_parts.append(", ".join(_rank_to_device(r) for r in ranks))
        cards_str = "，".join(cards_parts)
        deg_str = "，".join(deg_parts)
    elif category == "comm":
        cards_parts = []
        deg_parts = []
        dev_parts = []
        for key, val in sorted_items:
            ranks = _parse_ranks_from_key(key)
            domain_name = _domain_of_group(parallels, key)
            inner = ", ".join(str(r) for r in ranks)
            label = f"{domain_name}[{inner}]" if domain_name else f"[{inner}]"
            cards_parts.append(label)
            deg_parts.append(f"{label}:{val:g}")
            dev_parts.append(", ".join(_rank_to_device(r) for r in ranks))
        cards_str = "，".join(cards_parts)
        deg_str = "，".join(deg_parts)
    elif category == "cpu":
        # cpu 检测按物理节点拉齐，key 是节点显示名（hostName），不是 rank
        cards_str = "，".join(key for key, _ in sorted_items)
        deg_str = "，".join(f"{key}:{val:g}" for key, val in sorted_items)
        node_ranks = config.get_node_ranks_map()
        dev_parts = [
            ", ".join(_rank_to_device(r) for r in node_ranks.get(key, [])) or "-"
            for key, _ in sorted_items
        ]
    else:
        # 单卡类别：按 rank 升序，逐卡列出
        abnormal_ranks = []
        for key in items:
            abnormal_ranks.extend(_parse_ranks_from_key(key))
        abnormal_ranks = sorted(set(abnormal_ranks))
        deg_parts = []
        dev_parts = []
        for r in abnormal_ranks:
            v = items.get(str(r))
            deg_parts.append(f"{r}:{v:g}" if v is not None else f"{r}:?")
            dev_parts.append(_rank_to_device(r))
        cards_str = "rank " + ", ".join(str(r) for r in abnormal_ranks)
        deg_str = "，".join(deg_parts)

    dev_str = _truncate_display("; ".join(dev_parts))

    return cards_str, deg_str, dev_str


def build_summary_table(result: dict, parallels: dict = None, step_data: dict = None) -> str:
    """
    生成逐类别汇总的 ASCII 框线表格字符串（渲染到调用方 agent 的最终输出，不进任何 log 文件）。

    参数:
        result: 检测结果 {category: {key: degradation}}
        parallels: 并行域信息（可选，用于通信组带域名展示）
        step_data: 保留兼容（当前未使用）

    返回:
        ASCII 表格字符串；无任何异常时返回表头 + “无异常”提示。
    """
    headers = ["类别", "异常卡", "劣化指数", "劣化阈值"]

    ordered_categories = [
        "KERNEL_AICORE", "kernel_aivec", "memcpy_async",
        "comm", "pp_comm", "cpu", "npu_bubble",
    ]
    known = set(ordered_categories)
    dynamic = [c for c in result.keys() if c not in known]
    all_categories = ordered_categories + sorted(dynamic)

    rows = []
    threshold = {
        "compute": config.get_compute_threshold(),
        "io": config.get_io_threshold(),
        "comm": config.get_comm_threshold(),
        "bubble": config.BUBBLE_THRESHOLD_NS,
    }

    for category in all_categories:
        items = result.get(category) or {}
        if not items:
            continue

        # 异常卡列 + 劣化指数列（劣化指数用 "key:值" 与异常卡一一对应）
        cards_str, deg_str, _devices = _summary_cells(category, items, parallels)

        # 劣化阈值列
        if category == "npu_bubble":
            th_str = f"<{threshold['bubble']}ns"
        elif category == "pp_comm":
            th_str = f">{config.PP_WAIT_RATIO_THRESHOLD * 100:g}%"
        elif category == "comm":
            th_str = f"{threshold['comm']:g}x"
        elif category in config.IO_CATEGORIES:
            th_str = f"{threshold['io']:g}x"
        else:
            th_str = f"{threshold['compute']:g}x"

        # 类别列（大小写对齐 op_metric 指标列名）
        category_str = CATEGORY_DISPLAY.get(category, category)

        rows.append([category_str, cards_str, deg_str, th_str])

    if not rows:
        return _render_box_table(headers, [["无异常", "-", "-", "-"]])

    return _render_box_table(headers, rows)
