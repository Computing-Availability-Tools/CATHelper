from __future__ import annotations
import csv
import logging
import json
import os
import re
from typing import Dict, List, Optional, Tuple

_CATEGORY_CN = {
    "KERNEL_AICORE": "KERNEL_AICORE类算子",
    "kernel_aivec": "KERNEL_AIVEC类算子",
    "memcpy_async": "MEMCPY_ASYNC类算子",
    "comm": "comm（通信组带宽）",
    "cpu": "cpu（主机侧 ZP_Host）",
    "npu_bubble": "npu_bubble（NPU 气泡 ZP_Bubble）",
    "pp_comm": "pp_comm（PP 流水线等待）",
}
_ORDER = ("KERNEL_AICORE", "kernel_aivec", "memcpy_async", "comm",
          "cpu", "npu_bubble", "pp_comm")
_COLUMN = {
    "KERNEL_AICORE": "KERNEL_AICORE",
    "kernel_aivec": "KERNEL_AIVEC",
    "memcpy_async": "MEMCPY_ASYNC",
    "cpu": "ZP_Host",
    "npu_bubble": "ZP_Bubble",
}
_DETAIL_CATS = ("KERNEL_AICORE", "kernel_aivec", "memcpy_async")
_LEVEL_NOTE = {
    "levelnone": "本次数据只含 MsTx，没有计算 / 搬运算子，计算类与搬运类未参与检测。",
    "cluster": "cluster 单文件只评估通信，其他类别未参与。",
}
_MEMCPY_NOTE_HEAD = """关于 memcpy_async 的说明：该指标统计的是「拷贝算子的平均耗时」，包括：设备拷贝（H2D/D2H）、
设备内拷贝、设备间拷贝（D2D）。部分情况下会让少数卡的均值结构性偏高，进而导致误报：

1. **PP>1 且 TP>1（Megatron 框架特有）**：只有首/末 stage 的 tp0 需要把 data/label 从主机搬到
   卡上、再广播给同 TP 组，其余卡为 0。例：PP4TP4 时就是 rank0 和 rank12。
2. **PP=1 且 TP>1（Megatron 框架特有，如 TP4DP4）**：各 DP 组的 tp0 承担同样的 H2D，
   例：rank0/4/8/12。
3. **任务数不均**：任务少的卡缺少短任务，平均值容易贴线偏高。"""

def _memcpy_note(ranks) -> List[str]:
    rs = sorted({int(r) for r in ranks})
    if not rs:
        return _MEMCPY_NOTE_HEAD.splitlines()
    if len(rs) == 1:
        who = f"rank{rs[0]}"
    elif len(rs) == 2:
        who = f"rank{rs[0]} 和 rank{rs[1]}"
    else:
        who = "、".join(f"rank{r}" for r in rs)
    tail = (f"单从「拷贝算子的平均耗时」来看，{who} 确实存在内存拷贝耗时长的问题，"
            "如需确定是否真实存在 IO 劣化，需进一步排查。")
    return _MEMCPY_NOTE_HEAD.splitlines() + ["", tail]


def _first_rank(item: Dict) -> str:
    ranks = _ranks_from_key(item.get("display_key"))
    return str(ranks[0]) if ranks else str(item.get("display_key"))

def _pp_percent() -> str:
    try:
        import config
        return f"{config.PP_WAIT_RATIO_THRESHOLD * 100:g}"
    except Exception:
        return "5"

def _fmt_ranks(ranks) -> str:
    rs = sorted({int(r) for r in (ranks or [])})
    if not rs:
        return ""
    if len(rs) <= 6:
        return "rank " + "、".join(str(r) for r in rs)
    return f"rank {rs[0]}–{rs[-1]} 等 {len(rs)} 张"


def _ranks_from_key(key) -> List[int]:
    s = str(key or "")
    if "[" in s and s.endswith("]"):
        s = s[s.index("[") + 1:-1]
    return sorted(int(x) for x in re.findall(r"\d+", s))


def _ranks_text(key) -> str:
    s = str(key or "").strip()
    m = re.match(r"^([A-Za-z0-9_]+)\s*\[(.*)\]$", s)
    if m:
        ranks = [x.strip() for x in m.group(2).split(",") if x.strip()]
        return f"{m.group(1)} 组的 rank {'、'.join(ranks)}"
    m = re.match(r"^\[(.*)\]$", s)
    if m:
        ranks = [x.strip() for x in m.group(1).split(",") if x.strip()]
        ranks = sorted(ranks, key=lambda x: int(x) if x.isdigit() else 0)
        return "rank " + "、".join(ranks)
    m = re.match(r"^rank(\d+)-(\d+)$", s)
    if m:
        return f"rank {m.group(1)}–{m.group(2)}"
    m = re.match(r"^rank(\d+)$", s)
    if m:
        return f"rank {m.group(1)}"
    if s.isdigit():
        return f"rank {s}"
    return s


def _pick_step(values: List[float]) -> Optional[float]:
    """与检测一致：n>1 取倒数第二个 step，n==1 取第 0 个。"""
    if not values:
        return None
    return values[-2] if len(values) > 1 else values[0]


def _read_metric(metric_dir: str, rank: int, column: str) -> Optional[float]:
    path = os.path.join(metric_dir, f"global_rank_{rank}.csv")
    if not os.path.isfile(path):
        return None
    vals: List[float] = []
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                raw = row.get(column)
                if raw in (None, ""):
                    continue
                try:
                    vals.append(float(raw))
                except (TypeError, ValueError):
                    pass
    except OSError:
        return None
    v = _pick_step(vals)
    return None if v is None else v / 1_000_000.0   # ns → ms


def _detection_groups(data_dir: str):
    """rank -> 所属检测组。直接读 op_metric/group_info_*.json，按检测层同一套优先级取域。"""
    priority = ("tp", "exp", "ep", "tp_exp", "cp", "cp2", "cp_ulysses",
                "cp_ring", "dp", "dp_cp", "dp_modulo_exp_cp", "default_group")
    metric_dir = os.path.join(data_dir, "op_metric")
    domains: Dict[str, Dict[tuple, List[int]]] = {}
    ranks_all = set()
    try:
        names = sorted(os.listdir(metric_dir))
    except OSError:
        return {}, []
    for name in names:
        if not (name.startswith("group_info_") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(metric_dir, name), encoding="utf-8") as f:
                info = json.load(f) or {}
        except (OSError, ValueError):
            continue
        for entry in info.values():
            if not isinstance(entry, dict):
                continue
            pg = str(entry.get("group_name") or "")
            try:
                ranks = sorted(int(r) for r in (entry.get("global_ranks") or []))
            except (TypeError, ValueError):
                continue
            if not ranks:
                continue
            ranks_all.update(ranks)
            domains.setdefault(pg, {})[tuple(ranks)] = ranks
    for pg in priority:
        groups = list(domains.get(pg, {}).values())
        if groups:
            mapping = {}
            for g in groups:
                for r in g:
                    mapping[r] = list(g)
            return mapping, groups
    if ranks_all:
        allr = sorted(ranks_all)
        return {r: allr for r in allr}, [allr]
    return {}, []

def _baseline(column: str, metric_dir: str, group: List[int],
              flagged: List[int]) -> Tuple[Optional[float], List[int]]:
    """组内非异常卡的平均值（ms）。"""
    peers = [r for r in group if r not in flagged]
    if not peers:
        peers = list(group)
    vals = [v for v in (_read_metric(metric_dir, r, column) for r in peers) if v is not None]
    if not vals:
        return None, []
    return sum(vals) / len(vals), sorted(peers)


def _detail_line(cat: str, items: List[Dict], metric_dir: str,
                 mapping: Dict[int, List[int]]) -> str:
    column = _COLUMN.get(cat)
    label = _CATEGORY_CN.get(cat, cat)
    if not column or not os.path.isdir(metric_dir):
        detail = "、".join(
            f"{_ranks_text(i.get('display_key'))} 约 {float(i.get('metric_value') or 0):.2f} 倍"
            for i in items)
        return f"{label}：{detail}"

    parts = []
    buckets: Dict[Tuple[int, ...], List[Dict]] = {}
    for i in items:
        ranks = _ranks_from_key(i.get("display_key"))
        group = mapping.get(ranks[0]) if ranks else None
        key = tuple(sorted(group)) if group else ()
        buckets.setdefault(key, []).append(i)

    for key, group_items in sorted(buckets.items()):
        group = list(key) if key else sorted(mapping) or []
        flagged = sorted({r for i in group_items for r in _ranks_from_key(i.get("display_key"))})
        ratios = "、".join(
            f"rank{_first_rank(i)} 相对检测组内基线约 "
            f"{float(i.get('metric_value') or 0):.2f} 倍" for i in group_items)
        values = []
        for r in flagged:
            v = _read_metric(metric_dir, r, column)
            if v is not None:
                values.append(f"rank{r} 为 {v:.3f} ms")
        base_ranks = [r for r in group if r not in flagged] or list(group)
        base = None
        for item in group_items:
            try:
                v = _read_metric(metric_dir, int(_first_rank(item)), column)
            except (TypeError, ValueError):
                v = None
            ratio = float(item.get("metric_value") or 0)
            if v is not None and ratio > 0:
                base = v / ratio      # 与检测层 kmeans 基线一致
                break
        if base is None:
            base, base_ranks = _baseline(column, metric_dir, group, flagged)
        who = ("rank " + "、".join(str(r) for r in base_ranks)) if base_ranks else "—"
        sentence = ratios
        if values:
            sentence += "，" + "，".join(values)
        if base is not None:
            sentence += f"，检测组内基线（{who}）为 {base:.3f} ms"
        parts.append(sentence)
    return f"{label}：" + "。".join(parts)


def _node_ranks_map() -> Dict[str, List[int]]:
    """节点名 -> 该节点的 rank 列表（来自 config 的 host 映射）。"""
    try:
        import config
        host_map = config.get_host_rank_map() or {}
    except Exception:
        return {}
    out: Dict[str, List[int]] = {}
    for rank_str in host_map:
        try:
            r = int(rank_str)
        except (TypeError, ValueError):
            continue
        try:
            label = config.get_node_name(r)
        except Exception:
            label = ""
        if label:
            out.setdefault(str(label), []).append(r)
    return {k: sorted(v) for k, v in out.items()}

def _node_value(metric_dir: str, nodes: Dict[str, List[int]], name: str):
    ranks = nodes.get(name) or []
    vals = [v for v in (_read_metric(metric_dir, r, "ZP_Host") for r in ranks) if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)

def _node_line(items: List[Dict], metric_dir: str) -> str:
    """cpu 类：节点维度描述（基线 = 其它节点）。"""
    label = _CATEGORY_CN.get("cpu", "cpu")
    nodes = _node_ranks_map()
    names = [str(i.get("display_key")) for i in items]
    ratios = "、".join(
        f"{n} 相对其它节点基线约 {float(i.get('metric_value') or 0):.2f} 倍"
        for n, i in zip(names, items))
    sentence = ratios
    values = []
    for n in names:
        v = _node_value(metric_dir, nodes, n)
        if v is not None:
            values.append(f"{n} 为 {v:.3f} ms")
    if values:
        sentence += "，" + "，".join(values)
    base_nodes = [n for n in nodes if n not in names]
    base = None
    for n, item in zip(names, items):
        v = _node_value(metric_dir, nodes, n)
        ratio = float(item.get("metric_value") or 0)
        if v is not None and ratio > 0:
            base = v / ratio          # 与检测层 kmeans 基线一致
            break
    if base is None:
        base_vals = [v for v in (_node_value(metric_dir, nodes, n) for n in base_nodes)
                     if v is not None]
        if base_vals:
            base = sum(base_vals) / len(base_vals)
    if base is not None:
        who = "、".join(base_nodes) if base_nodes else "—"
        sentence += f"，其它节点基线（{who}）为 {base:.3f} ms"
    else:
        sentence += "，其它节点基线无可用数据"
    return f"{label}：{sentence}"

def _cluster_domain_groups(db_path: str):
    """cluster_analysis.db 的并行域 -> 组列表。直接复用检测层的实现。"""
    try:
        from nodelevel_data_handler import load_cluster_parallels
        parallels = load_cluster_parallels(db_path)
        if parallels:
            return {k: [list(g) for g in v] for k, v in parallels.items()}
        logging.getLogger(__name__).warning(
            "[CONCLUSION] CommunicationGroupMapping 未解析出组信息（检测层同款查询）")
    except Exception as e:
        logging.getLogger(__name__).warning(f"[CONCLUSION] 读取 cluster 组信息失败：{e}")
    return {}

def _domain_groups_map(data_dir: str) -> Dict[str, List[List[int]]]:
    """通信域 -> 该域内的所有 Group。支持 op_metric 目录与 cluster 单文件 db。"""
    if data_dir and os.path.isfile(data_dir):
        return _cluster_domain_groups(data_dir)
    metric_dir = os.path.join(data_dir, "op_metric") if data_dir else ""
    out: Dict[str, List[List[int]]] = {}
    if not metric_dir:
        return out
    try:
        names = sorted(os.listdir(metric_dir))
    except OSError:
        return out
    for name in names:
        if not (name.startswith("group_info_") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(metric_dir, name), encoding="utf-8") as f:
                info = json.load(f) or {}
        except (OSError, ValueError):
            continue
        for entry in info.values():
            if not isinstance(entry, dict):
                continue
            pg = str(entry.get("group_name") or "")
            try:
                ranks = sorted(int(r) for r in (entry.get("global_ranks") or []))
            except (TypeError, ValueError):
                continue
            if not pg or not ranks:
                continue
            bucket = out.setdefault(pg, [])
            if ranks not in bucket:
                bucket.append(ranks)
    return {k: sorted(v) for k, v in out.items()}

def _brk(domain: str, ranks) -> str:
    """组标签统一用全角方括号（半角 [x] 会被 Markdown 当引用链接吞掉）。"""
    inner = ",".join(str(r) for r in ranks)
    return f"{domain}［{inner}］" if domain else f"［{inner}］"

def _groups_text(domain: str, groups: List[List[int]], limit: int = 6) -> str:
    shown = groups[:limit]
    text = "、".join(_brk(domain, g) for g in shown)
    if len(groups) > limit:
        text += f" 等 {len(groups)} 个组"
    return text

def _parse_comm_key(key) -> tuple:
    s = str(key or "")
    m = re.match(r"^([A-Za-z0-9_]+)\s*\[(.*)\]$", s)
    if not m:
        return "", []
    try:
        ranks = sorted(int(x) for x in m.group(2).split(",") if x.strip())
    except (TypeError, ValueError):
        ranks = []
    return m.group(1), ranks

_CATEGORY_GROUPS = (
    ("计算类异常", ("KERNEL_AICORE", "kernel_aivec")),
    ("IO类异常", ("memcpy_async",)),
    ("通信类异常", ("comm", "pp_comm")),
    ("Host异常", ("cpu",)),
    ("NPU空泡异常", ("npu_bubble",)),
)

def _body(text: str) -> str:
    """去掉「标签：」前缀，只留描述。"""
    return text.split("：", 1)[1] if "：" in text else text

_COMM_NOTE = "（取该组内劣化最严重的［并行域, 算子, Count］组合）"
_COMM_NOTE_LEVEL = "（各［算子, Count］组合的带宽与中位数之比取平均）"

def _comm_rows(items: List[Dict], data_dir: str = "", level: str = ""):
    """通信类描述。基线一律排除本类别已判为异常的卡 / 组。

    level1 / cluster 为两级口径，分两段输出：组内逐卡检测、组间逐组检测。
    """
    domains = _domain_groups_map(data_dir) if data_dir else {}
    two_level = level in ("level1", "cluster")

    parsed = [(i,) + _parse_comm_key(i.get("display_key")) for i in items]
    flagged_ranks: Dict[str, set] = {}
    flagged_groups: Dict[str, List[List[int]]] = {}
    for _i, domain, target in parsed:
        if not domain or not target:
            continue
        flagged_ranks.setdefault(domain, set()).update(target)
        if len(target) > 1:
            flagged_groups.setdefault(domain, []).append(target)

    def _clean_groups(domain: str, groups: List[List[int]], exclude) -> List[List[int]]:
        bad = flagged_ranks.get(domain) or set()
        out = []
        for g in groups:
            if exclude and g == exclude:
                continue
            if set(g) & bad:
                continue
            out.append(g)
        return out

    plain: List[str] = []
    within: List[str] = []
    between: List[str] = []
    for item, domain, target in parsed:
        ratio = float(item.get("metric_value") or 0)
        groups = domains.get(domain) or []

        if not two_level or len(target) > 1:
            others = _clean_groups(domain, groups, target)
            if others:
                who = _groups_text(domain, others)
            elif groups:
                who = "域内其它组均含异常卡，无可用基线"
            else:
                who = "域内组信息不可用"
            label = (_brk(domain, target)
                     if domain and target else _ranks_text(item.get("display_key")))
            head = f"{domain} 域的 " if not two_level and domain else ""
            text = f"{head}{label} 相对域内基线（{who}）约 {ratio:.2f} 倍"
            (between if two_level else plain).append(text)
            continue

        rank = target[0]
        group = next((g for g in groups if rank in g), [])
        bad = flagged_ranks.get(domain) or set()
        peers = [r for r in group if r != rank and r not in bad]
        if peers:
            who = _brk(domain, peers)
        elif group:
            who = "同组其它卡均为异常卡，无可用基线"
        else:
            who = "组信息不可用"
        within.append(f"{_brk(domain, [rank])} 相对同组基线（{who}）约 {ratio:.2f} 倍")

    out = []
    if plain:
        # 旧口径（levelnone / level0）：劣化指数取各 opType 的最大值
        out.append(("集合通信带宽", "；".join(plain) + _COMM_NOTE))
    if within:
        # 两级口径：指标 = 各（算子, Count）组合的带宽/中位数 的平均值
        out.append(("集合通信带宽（组内逐卡检测）",
                    "；".join(within) + _COMM_NOTE_LEVEL))
    if between:
        out.append(("集合通信带宽（组间逐组检测）",
                    "；".join(between) + _COMM_NOTE_LEVEL))
    return out

def _pp_line(items: List[Dict]) -> str:
    parts = []
    for i in items:
        pct = float(i.get("metric_value") or 0) * 100.0
        ranks = sorted({int(r) for r in _ranks_from_key(i.get("display_key"))})
        label = _brk("pp", ranks) if ranks else _ranks_text(i.get("display_key"))
        parts.append(f"{label} 的点对点等待时间占会话时长约 {pct:.2f}%")
    return f"{_CATEGORY_CN.get('pp_comm')}：" + "、".join(parts) + f"，超过阈值 {_pp_percent()}%"

def _bubble_line(items: List[Dict]) -> str:
    keys = "、".join(_ranks_text(i.get("display_key")) for i in items)
    return f"{_CATEGORY_CN.get('npu_bubble')}：{keys} 的算子启动间隔低于固定阈值 5000 ns"

def _threshold_text(level: str, thresholds) -> str:
    """只列出该数据级别实际用到的阈值。"""
    compute, io, comm = thresholds[0], thresholds[1], thresholds[2]
    parts = []
    if level == "cluster":
        return f"集合通信（comm）{comm} 倍"
    if level in ("level0", "level1"):
        parts.append(f"计算（针对KERNEL_AICORE和KERNEL_AIVEC）{compute} 倍")
    parts.append(f"IO/CPU（针对MEMCPY_ASYNC和Host）{io} 倍")
    parts.append(f"集合通信（comm）{comm} 倍")
    if level in ("level0", "levelnone"):
        parts.append(f"PP 点对点通信等待占比（pp_comm）{_pp_percent()}%")
    return "、".join(parts)

def render(detection: Dict, level: str, thresholds, metric_dir: str = "",
           data_dir: str = "") -> str:
    """把检测结果表翻成自然语言。只描述检测层给的结果，不做任何额外判断。"""
    lines: List[str] = ["# 检测结论", ""]
    found = []
    for cat in _ORDER:
        items = [i for i in (detection.get(cat) or [])
                 if i.get("is_abnormal") is not False]
        if items:
            items.sort(key=lambda i: float(i.get("metric_value") or 0), reverse=True)
            found.append((cat, items))

    level_text = f"`{level}`" if level else "未知"
    if not found:
        lines.append(f"数据级别 {level_text}。**本次未检出异常。**")
        return "\n".join(lines) + "\n"

    lines.append(f"数据级别 {level_text}。共检出 **{len(found)} 类异常**：")
    lines.append("")
    mapping, _groups = _detection_groups(data_dir) if data_dir else ({}, [])
    by_cat = dict(found)
    for title, cats in _CATEGORY_GROUPS:
        rows: List[Tuple[str, str]] = []
        for cat in cats:
            items = by_cat.get(cat)
            if not items:
                continue
            if cat == "cpu":
                rows.append(("cpu", _body(_node_line(items, metric_dir))))
            elif cat in _DETAIL_CATS:
                rows.append((_CATEGORY_CN.get(cat, cat),
                             _body(_detail_line(cat, items, metric_dir, mapping))))
            elif cat == "comm":
                rows.extend(_comm_rows(items, data_dir, level))
            elif cat == "pp_comm":
                rows.append(("PP点对点通信等待占比", _body(_pp_line(items))))
            elif cat == "npu_bubble":
                rows.append(("npu_bubble", _body(_bubble_line(items))))
            else:
                detail = "、".join(
                    f"{_ranks_text(i.get('display_key'))} 约 {float(i.get('metric_value') or 0):.2f} 倍"
                    for i in items)
                rows.append((_CATEGORY_CN.get(cat, cat), detail))
        if rows:
            lines.append(f"{title}：")
            for sub, content in rows:
                lines.append(f"  - {sub}：{content}")
            lines.append("")
    lines.append(
        "> 倍数 = 该卡相对检测组内基线的劣化倍数，越大越异常。本次阈值："
        + _threshold_text(level, thresholds) + "。")

    compute_hit = any(cat in ("KERNEL_AICORE", "kernel_aivec") for cat, _ in found)
    if (level in ("level0", "levelnone") and compute_hit
            and not (detection.get("pp_comm") or [])):
        lines.append("")
        lines.append("PP 点对点通信等待（pp_comm）未评估：本次已检出计算类故障"
                     "（KERNEL_AICORE / kernel_aivec），PP 检测被跳过"
                     "（其统计区间内必含计算算子，会被污染），不能据此判断 PP 是否正常。")

    memcpy_ranks = sorted({r for cat, items in found if cat == "memcpy_async"
                           for i in items for r in _ranks_from_key(i.get("display_key"))})
    if memcpy_ranks:
        lines.append("")
        lines.extend(_memcpy_note(memcpy_ranks))

    note = _LEVEL_NOTE.get(level or "")
    if note:
        lines.append("")
        lines.append(note)
    lines.append("")
    return "\n".join(lines)


def render_from_dir(out_dir: str, level: str, thresholds, source_path: str = "") -> str:
    """读检测结果 JSON，返回结论文本（只在控制台输出，不落盘）。"""
    det = {}
    src = os.path.join(out_dir, "straggler_detection_result.json")
    try:
        with open(src, "r", encoding="utf-8") as f:
            det = json.load(f) or {}
    except (OSError, ValueError):
        det = {}
    return render(det, level, thresholds,
                  metric_dir=os.path.join(out_dir, "op_metric"),
                  data_dir=source_path or out_dir)
