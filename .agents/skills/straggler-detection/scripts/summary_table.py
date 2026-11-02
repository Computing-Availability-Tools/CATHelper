import os
import sys
from typing import Dict, List
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
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
    return sum(2 if 0x2E80 <= ord(ch) <= 0x9FFF or 0xFF00 <= ord(ch) <= 0xFFEF else 1
               for ch in text)

def _disp_ljust(text: str, width: int) -> str:
    pad = width - _disp_len(text)
    return text + " " * max(pad, 0)

def _render_box_table(headers: List[str], rows: List[List[str]]) -> str:
    all_rows = [headers] + rows
    ncols = len(headers)
    widths = [0] * ncols
    for row in all_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _disp_len(cell))
    def _line(left, mid, right, fill="-"):
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
        if idx < last_idx:
            lines.append(mid)
    lines.append(bot)
    return "\n".join(lines)

def _domain_of_group(parallels: dict, ranks_key: str) -> str:
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

def _split_domain_key(key: str):
    prefix, sep, rest = str(key).partition(":")
    if sep and rest and not prefix.isdigit():
        return prefix, rest
    return "", str(key)

def _parse_ranks_from_key(key: str):
    _, ranks_key = _split_domain_key(key)
    return [int(r) for r in ranks_key.split(",")]
DEVICE_DISPLAY_LIMIT = 30

def _rank_to_device(rank: int) -> str:
    info = config.get_rank_device_map().get(str(rank))
    if info:
        hn = info.get("host_name") or "?"
        nid = info.get("npu_id") or "?"
        return f"{hn}:Device{nid}"
    return f"rank{rank}"

def _truncate_display(text: str, limit: int = DEVICE_DISPLAY_LIMIT) -> str:
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
    sorted_items = sorted(items.items(), key=lambda x: -x[1])
    if category == "pp_comm":
        cards_parts = []
        deg_parts = []
        dev_parts = []
        for key, val in sorted_items:
            ranks = _parse_ranks_from_key(key)
            label = "[" + ", ".join(str(r) for r in ranks) + "]"
            cards_parts.append(label)
            deg_parts.append(f"{label}:{val * 100:g}%")
            dev_parts.append(", ".join(_rank_to_device(r) for r in ranks))
        cards_str = "，".join(cards_parts)
        deg_str = "，".join(deg_parts)
    elif category == "comm":
        cards_parts = []
        deg_parts = []
        dev_parts = []
        for key, val in sorted_items:
            key_domain, ranks_key = _split_domain_key(key)
            ranks = _parse_ranks_from_key(key)
            domain_name = key_domain or _domain_of_group(parallels, ranks_key)
            inner = ", ".join(str(r) for r in ranks)
            label = f"{domain_name}[{inner}]" if domain_name else f"[{inner}]"
            cards_parts.append(label)
            deg_parts.append(f"{label}:{val:g}")
            dev_parts.append(", ".join(_rank_to_device(r) for r in ranks))
        cards_str = "，".join(cards_parts)
        deg_str = "，".join(deg_parts)
    elif category == "cpu":
        cards_str = "，".join(key for key, _ in sorted_items)
        deg_str = "，".join(f"{key}:{val:g}" for key, val in sorted_items)
        node_ranks = config.get_node_ranks_map()
        dev_parts = [
            ", ".join(_rank_to_device(r) for r in node_ranks.get(key, [])) or "-"
            for key, _ in sorted_items
        ]
    else:
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
        cards_str, deg_str, _devices = _summary_cells(category, items, parallels)
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
        category_str = CATEGORY_DISPLAY.get(category, category)
        rows.append([category_str, cards_str, deg_str, th_str])
    if not rows:
        return _render_box_table(headers, [["无异常", "-", "-", "-"]])
    return _render_box_table(headers, rows)
