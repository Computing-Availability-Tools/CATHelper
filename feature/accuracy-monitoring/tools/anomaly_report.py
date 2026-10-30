#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推理异常数据分析报告生成器。

从 anomaly_middleware 落盘的 pkl 异常现场数据生成《推理异常数据分析报告》
（Markdown + HTML 双格式）：支持单日 / 最近 N 天 / 任意日期区间，多实例聚合，
离线重跑检测器还原异常定位细节（生僻字逐位置、乱码/重复触发窗口），每条明细
可回查 pkl 异常编号。统计严格限定在请求时间窗内。

用法示例：
    python tools/anomaly_report.py --days 7
    python tools/anomaly_report.py --date 2026-10-20
    python tools/anomaly_report.py --from 2026-09-21 --to 2026-10-20
    python tools/anomaly_report.py --days 30 --no-detector --instance vllm-prod-1

退出码：0=成功（含零异常报告） 1=运行错误 2=参数错误 3=全部实例失败
"""

import argparse
import html as _html
import math
import os
import pickle
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    import numpy as np
except ImportError:
    np = None

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ILL_NAMES = {1: "生僻字", 2: "乱码", 3: "重复", 4: "NaN"}
ILL_ORDER = [1, 2, 3, 4]
ILL_FILTER = {"rare_character": 1, "garbled": 2, "repetition": 3, "nan_value": 4}
DEFAULT_MARKERS = ["</think>"]
STATUS_TIMEOUT = 2.0
DISPLAY_MAX_TOKENS = 200
RARE_MAX_ROWS = 20
CONTEXT_RADIUS = 8
RERUN_OP_CAP = 2_000_000


# ---------------------------------------------------------------- utilities

def eprint(*a):
    print(*a, file=sys.stderr)


def fail(code, msg):
    eprint(msg)
    sys.exit(code)


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def ts_to_date(ts):
    return datetime.fromtimestamp(int(ts)).date()


def esc(s):
    return _html.escape(str(s if s is not None else ""))


def short(s, n=12):
    s = str(s if s is not None else "")
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_ymd(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def daterange(d1, d2):
    for n in range((d2 - d1).days + 1):
        yield d1 + timedelta(days=n)


# ---------------------------------------------------------------- args

def build_parser():
    p = argparse.ArgumentParser(
        prog="anomaly_report.py", description="生成《推理异常数据分析报告》")
    g = p.add_argument_group("时间窗（四选一，互斥；缺省=今天）")
    g.add_argument("--date", help="单日：YYYY-MM-DD | today | yesterday")
    g.add_argument("--days", type=int, help="最近 N 天（含今天），N>=1；统计严格限定该窗口")
    g.add_argument("--from", dest="from_date", help="区间起：YYYY-MM-DD")
    g.add_argument("--to", dest="to_date", help="区间止：YYYY-MM-DD")
    g.add_argument("--all", action="store_true",
                   help="数据中出现的全部日期（仅当用户明确要求全部数据时使用）")
    p.add_argument("--config", default=str(REPO_ROOT / "configs" / "instances.yaml"))
    p.add_argument("--report-dir", help="报告输出目录（缺省取配置 report_dir，再缺省 ./reports）")
    p.add_argument("--instance", nargs="+", help="过滤实例名（缺省全部）")
    p.add_argument("--ill-type", nargs="+", choices=sorted(ILL_FILTER), dest="ill_types",
                   help="过滤异常类型（缺省全部）")
    p.add_argument("--no-detector", action="store_true", help="跳过离线重跑定位，仅统计分布")
    p.add_argument("--max-detail", type=int, default=5, help="每实例每类型明细上限（默认 5）")
    p.add_argument("--format", choices=["html", "markdown", "both"], default="both",
                   help="输出格式（默认 both）")
    return p


def resolve_window(args):
    """返回 (window, mode)；--all 返回 ((None, None), 'all') 由数据回填。"""
    used = [bool(args.date), args.days is not None,
            args.from_date is not None or args.to_date is not None, args.all]
    if sum(used) > 1:
        fail(2, "参数互斥：--date / --days / --from+--to / --all 只能四选一")
    if (args.from_date is None) != (args.to_date is None):
        fail(2, "--from 与 --to 必须成对使用")
    if args.days is not None and args.days < 1:
        fail(2, "--days 必须 >= 1")
    today = date.today()
    if args.date:
        if args.date == "today":
            d = today
        elif args.date == "yesterday":
            d = today - timedelta(days=1)
        else:
            d = parse_ymd(args.date)
            if d is None:
                fail(2, "--date 格式非法：%s（应为 YYYY-MM-DD）" % args.date)
        return (d, d), "single"
    if args.days is not None:
        return (today - timedelta(days=args.days - 1), today), "days"
    if args.from_date:
        d1, d2 = parse_ymd(args.from_date), parse_ymd(args.to_date)
        if d1 is None or d2 is None:
            fail(2, "--from/--to 格式非法（应为 YYYY-MM-DD）")
        if d1 > d2:
            fail(2, "--from 不能晚于 --to")
        return (d1, d2), "range"
    if args.all:
        return (None, None), "all"
    return (today, today), "single"


# ---------------------------------------------------------------- config

def load_config(path):
    if not Path(path).exists():
        fail(1, "配置文件不存在：%s\n请按模板说明创建 configs/instances.yaml 后重试" % path)
    if yaml is None:
        fail(1, "缺少依赖 PyYAML：pip install PyYAML")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    insts = cfg.get("instances")
    if not insts:
        fail(1, "配置文件中 instances 为空，请补充实例信息后重试（name/server/pkl_file/tokenizer_path）")
    return cfg


# ---------------------------------------------------------------- pkl loading

def _as_float_list(row):
    """topk_logprobs 行归一化：dict{token_id:logp} 或 list -> (logps, token_ids)。"""
    if isinstance(row, dict):
        items = []
        for k, v in row.items():
            try:
                items.append((int(k), float(v)))
            except (TypeError, ValueError):
                continue
        items.sort(key=lambda x: -x[1])
        return [v for _, v in items], [k for k, _ in items]
    if isinstance(row, (list, tuple)):
        try:
            return [float(v) for v in row], None
        except (TypeError, ValueError):
            return None, None
    return None, None


def _norm_record(rec):
    """防御性归一化 pkl 记录；失败返回 None。"""
    if not isinstance(rec, dict):
        return None
    out = {"time": rec.get("time"), "ill_type": rec.get("ill_type"),
           "prompt": rec.get("prompt"), "text": rec.get("text"),
           "model_name": rec.get("model_name"),
           "topk_logprobs": rec.get("topk_logprobs"),
           "tokens_ids": rec.get("tokens_ids"),
           "text_tokenids": rec.get("text_tokenids") or rec.get("text_tokenid")}
    try:
        out["time"] = int(out["time"])
        out["ill_type"] = int(out["ill_type"])
    except (TypeError, ValueError):
        return None
    if out["ill_type"] not in ILL_ORDER:
        return None
    return out


def load_instance_records(inst, cfg):
    """返回 (records:list[norm], data_dates:set, error:str|None, meta:dict)"""
    pkl_dir = cfg.get("pkl_dir")
    pf = inst.get("pkl_file")
    path = None
    if pf:
        p = Path(str(pf))
        path = p if p.is_absolute() else (Path(str(pkl_dir)) / p if pkl_dir else None)
    if path is None:
        return [], set(), "未配置 pkl_file 且缺少 pkl_dir", {}
    if not Path(path).exists():
        return [], set(), "pkl 文件缺失：%s" % path, {"path": str(path)}
    try:
        with open(path, "rb") as f:
            raw = pickle.load(f)
    except Exception as e:
        return [], set(), "pkl 解析失败：%s" % e, {"path": str(path)}
    if not isinstance(raw, dict):
        return [], set(), "pkl 内容非 dict 结构", {"path": str(path)}
    records, dates, bad = [], set(), 0
    for key in sorted(raw.keys()):
        rec = _norm_record(raw[key])
        if rec is None:
            bad += 1
            continue
        rec["pkl_key"] = key
        try:
            rec["date"] = ts_to_date(rec["time"])
            dates.add(rec["date"])
        except Exception:
            bad += 1
            continue
        records.append(rec)
    meta = {"path": str(path), "loaded": len(raw), "bad": bad}
    return records, dates, None, meta


# ---------------------------------------------------------------- detector re-run

class Rerunner:
    """离线重跑 ILLDetector：有词表 -> 无词表依序尝试，任一与线上检出一致即采用。"""

    def __init__(self, detector_config, tokenizer_path, enabled=True):
        self.ok = False
        self.reason = ""
        self.config_path = detector_config
        self.det_v = None      # 有词表检测器（惰性）
        self.det_n = None      # 无词表检测器
        self.tok = None
        self.tok_cache = {}
        if not enabled:
            self.reason = "--no-detector 跳过重跑"
            return
        try:
            from anomaly_middleware.detector import ILLDetector  # noqa
        except Exception as e:
            self.reason = "检测器不可用：%s" % e
            return
        if np is None:
            self.reason = "缺少 numpy，无法重跑"
            return
        try:
            self.det_n = ILLDetector(detector_config)
            self.ok = True
        except Exception as e:
            self.reason = "检测器构造失败：%s" % e
            return
        if tokenizer_path:
            try:
                from transformers import AutoTokenizer
                self.tok = AutoTokenizer.from_pretrained(
                    str(tokenizer_path), local_files_only=True, trust_remote_code=True)
            except Exception as e:
                self.reason = "tokenizer 加载失败（重跑仅走无词表模式）：%s" % e

    def _vocab_detector(self):
        if self.det_v is not None:
            return self.det_v
        if self.tok is None:
            return None
        try:
            from anomaly_middleware.token_categorizer import generate_tk2cat
            tk2cat, vocab_size = generate_tk2cat(self.tok)
            if not tk2cat:
                return None
            from anomaly_middleware.detector import ILLDetector
            det = ILLDetector(self.config_path)
            det.set_vocabulary(tk2cat, vocab_size)
            self.det_v = det
            return det
        except Exception:
            return None

    def decode(self, tid):
        if self.tok is None:
            return None
        if tid in self.tok_cache:
            return self.tok_cache[tid]
        try:
            t = self.tok.decode([int(tid)])
            t = t if t else None
        except Exception:
            t = None
        self.tok_cache[tid] = t
        return t

    def _run_once(self, det, lp, ti):
        if det is None:
            return None
        try:
            # 对齐线上语义：_garbled_count 每请求复位
            try:
                det._garbled_count = 0
            except Exception:
                pass
            r = det.detector(lp, ti, topk_n=lp.shape[1])
            return (bool(r.is_ill), int(r.ill_type))
        except Exception:
            return None

    def rerun(self, rec):
        """返回 dict(ok, ill_type, mode, consistent, reason)。"""
        out = {"ok": False, "ill_type": None, "mode": None,
               "consistent": None, "reason": self.reason}
        if not self.ok:
            return out
        lp, ti = self._build_arrays(rec)
        if lp is None:
            out["reason"] = "topk 数据缺失或格式异常，无法重跑"
            return out
        online = rec["ill_type"]
        tries = []
        dv = self._vocab_detector()
        if dv is not None:
            tries.append(("有词表", self._run_once(dv, lp, ti)))
        tries.append(("无词表", self._run_once(self.det_n, lp, ti)))
        for mode, res in tries:
            if res is None:
                continue
            out.update(ok=True, ill_type=res[1], mode=mode,
                       consistent=(res[1] == online))
            if res[1] == online:
                return out
        if out["ok"]:
            out["reason"] = "两种模式重跑均与线上不一致（线上=%s）" % ILL_NAMES.get(online, online)
        return out

    def _build_arrays(self, rec):
        rows_p, rows_t = rec.get("topk_logprobs"), rec.get("tokens_ids")
        if not isinstance(rows_p, (list, tuple)) or not rows_p:
            return None, None
        lp_rows, ti_rows = [], []
        for i, prow in enumerate(rows_p):
            ps, ts = _as_float_list(prow)
            if ps is None:
                return None, None
            trow = rows_t[i] if isinstance(rows_t, (list, tuple)) and i < len(rows_t) else None
            if ts is None:
                if isinstance(trow, (list, tuple)):
                    ts = [int(v) for v in trow]
                else:
                    ts = [-1] * len(ps)
            elif trow is not None and isinstance(trow, (list, tuple)):
                try:
                    ts = [int(v) for v in trow]
                except (TypeError, ValueError):
                    pass
            n = min(len(ps), len(ts))
            if n <= 0:
                return None, None
            lp_rows.append([float(v) for v in ps[:n]])
            ti_rows.append([int(v) for v in ts[:n]])
        k = min(len(r) for r in lp_rows)
        if k <= 0:
            return None, None
        lp = np.array([r[:k] for r in lp_rows], dtype=np.float32)
        ti = np.array([r[:k] for r in ti_rows], dtype=np.int32)
        order = np.argsort(-lp, axis=1, kind="stable")
        lp = np.take_along_axis(lp, order, axis=1)
        ti = np.take_along_axis(ti, order, axis=1)
        return lp, ti


# ---------------------------------------------------------------- localization

def get_seq_ids(rec):
    """展示序列：优先 text_tokenids，回退 tokens_ids 的 top-1 列。"""
    tt = rec.get("text_tokenids")
    if isinstance(tt, (list, tuple)) and tt and all(
            isinstance(v, int) for v in tt[:50]):
        return [int(v) for v in tt], True
    rows = rec.get("tokens_ids")
    if isinstance(rows, (list, tuple)) and rows:
        first = rows[0]
        if isinstance(first, int):
            return [int(v) for v in rows], False
        if isinstance(first, (list, tuple)):
            col = []
            for r in rows:
                if isinstance(r, (list, tuple)) and r:
                    try:
                        col.append(int(r[0]))
                    except (TypeError, ValueError):
                        col.append(-1)
                else:
                    col.append(-1)
            return col, False
    return [], False


def decode_seq(rr, seq):
    if rr is None or rr.tok is None or not seq:
        return []
    texts = []
    for tid in seq:
        t = rr.decode(tid)
        texts.append(t if t else "")
    return texts


def thought_end(texts, markers):
    """首个思维链结束标记位置；序列首 token 即含标记（无思维链）或未找到 → None。"""
    for i, t in enumerate(texts):
        for m in markers:
            if m and m in (t or ""):
                if i == 0:
                    return None
                return i
    return None


def find_repetition_window(seq, max_period=32, op_cap=RERUN_OP_CAP):
    """启发式：最大周期重复窗口（窗口覆盖 token 数最多）。返回 (start, end) 或 None。"""
    T = len(seq)
    best = None  # (span, start, end)
    ops = 0
    for p in range(1, max_period + 1):
        if T < 2 * p:
            break
        i = 0
        while i + 2 * p <= T:
            if seq[i:i + p] != seq[i + p:i + 2 * p]:
                i += 1
                ops += 1
                continue
            blocks = 2
            j = i + 2 * p
            while j + p <= T and seq[i + (blocks - 1) * p: i + blocks * p] == seq[j:j + p]:
                blocks += 1
                j += p
                ops += 1
            span = p * blocks
            if best is None or span > best[0]:
                best = (span, i, i + span)
            i += max(1, p)
            if ops > op_cap:
                return (best[1], best[2]) if best else None
    return (best[1], best[2]) if best else None


def _is_unusual(text):
    if not text:
        return False
    odd = sum(1 for ch in text if not (
        "\u4e00" <= ch <= "\u9fff" or ch.isascii() and ch.isalnum()
        or ch in " ，。、；：！？,.!?;:'\"()[]{}<>-_=+/\\|@#$%^&*~`…《》"))
    return odd / len(text) > 0.5


def find_garbled_window(texts):
    """启发式：最长连续异常 token 段。返回 (start, end) 或 None。"""
    best = None
    i, T = 0, len(texts)
    while i < T:
        if _is_unusual(texts[i]):
            j = i
            while j < T and _is_unusual(texts[j]):
                j += 1
            if best is None or (j - i) > (best[1] - best[0]):
                best = (i, j)
            i = j
        else:
            i += 1
    return best if (best and best[1] - best[0] >= 3) else None


def display_span(T, w):
    """触发窗口 -> 展示区间（前铺 72 token，总长 <=200）。"""
    if w is None:
        start, end = max(0, T - DISPLAY_MAX_TOKENS), T
        label = "启发式未定位，展示末段"
    else:
        ws, we = w
        if we - ws > DISPLAY_MAX_TOKENS:
            ws = we - DISPLAY_MAX_TOKENS
        start, end = max(0, ws - 72), min(T, we)
        label = "定位"
    if end - start > DISPLAY_MAX_TOKENS:
        end = start + DISPLAY_MAX_TOKENS
    return start, end, label


# ---------------------------------------------------------------- trend chart (SVG)

def svg_trend(days, counts, title, fmt="%m-%d", hover="%Y-%m-%d"):
    """检出数趋势图（纯内联 SVG，无外部依赖）。days 元素需支持 strftime(fmt/hover)。"""
    n = len(counts)
    if n == 0:
        return ""
    W, H = 660, 190
    L, R, T, B = 42, 16, 14, 30
    pw, ph = W - L - R, H - T - B
    ymax = max(max(counts), 1)
    step = max(1, math.ceil(ymax / 4))
    top = step * math.ceil(ymax / step)

    def X(i):
        return L + (pw * i / max(n - 1, 1))

    def Y(v):
        return T + ph * (1 - (v / top))

    parts = ['<svg width="%d" height="%d" viewBox="0 0 %d %d" xmlns='
             '"http://www.w3.org/2000/svg" role="img" aria-label="%s">'
             % (W, H, W, H, esc(title))]
    tv = 0
    while tv <= top:
        y = Y(tv)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e6ebf1"/>'
                     % (L, y, W - R, y))
        parts.append('<text x="%d" y="%.1f" font-size="10" fill="#8a94a0" '
                     'text-anchor="end">%d</text>' % (L - 6, y + 3, tv))
        tv += step
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#c2ccd6"/>'
                 % (L, Y(0), W - R, Y(0)))
    if n == 1:
        idxs = [0]
    else:
        stepx = max(1, (n - 1) // 6)
        idxs = list(range(0, n, stepx))
        if idxs[-1] != n - 1:
            idxs.append(n - 1)
    for i in idxs:
        parts.append('<text x="%.1f" y="%d" font-size="10" fill="#8a94a0" '
                     'text-anchor="middle">%s</text>' % (X(i), H - 10, days[i].strftime(fmt)))
    pts = " ".join("%.1f,%.1f" % (X(i), Y(v)) for i, v in enumerate(counts))
    parts.append('<polyline points="%s" fill="none" stroke="#1a5fb4" stroke-width="2"/>' % pts)
    for i, v in enumerate(counts):
        parts.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="#1a5fb4">'
                     '<title>%s：%d 条</title></circle>'
                     % (X(i), Y(v), days[i].strftime(hover), v))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- rendering

CSS = """body{font-family:"Microsoft YaHei",Segoe UI,Arial,sans-serif;margin:24px auto;
max-width:1100px;color:#222;line-height:1.6;font-size:14px;padding:0 18px}
h1{font-size:20px;border-bottom:2px solid #2f4d6e;padding-bottom:8px;color:#1c2f42}
h2{font-size:17px;margin-top:34px;border-left:4px solid #2f4d6e;padding-left:9px;color:#1c2f42}
h3{font-size:14.5px;margin-top:22px;color:#2a3a4a}
h4{font-size:14px;color:#3a4a5a;margin-top:16px}
p{margin:6px 0}
.meta,.note{font-size:12px;color:#66707a}
.note{margin:4px 0}
.err{color:#a02020}
table{border-collapse:collapse;margin:10px 0;font-size:12.5px}
th,td{border:1px solid #c9d2dc;padding:4px 9px;text-align:left}
th{background:#edf2f7;color:#2a3a4a}
tr:nth-child(even) td{background:#f7f9fb}
code,pre{background:#f4f6f8;border-radius:4px;font-family:Consolas,monospace;font-size:12.5px}
pre{padding:10px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;margin:6px 0}
a{color:#1a5fb4;text-decoration:none}
a:hover{text-decoration:underline}
details.detail{border:1px solid #d7dee6;border-radius:6px;padding:6px 12px;margin:8px 0;
background:#fbfcfd}
details.detail>summary{cursor:pointer;font-size:13px;color:#1c2f42;font-weight:600;padding:3px 0}
details.detail[open]{background:#fff}
.chart{margin:10px 0 4px}"""


def _cell(c):
    """单元格归一化：("@link", target, text) 元组 -> 锚点链接；其余原样。"""
    if isinstance(c, tuple) and len(c) == 3 and c[0] == "@link":
        return c
    return str(c)


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        cells = []
        for c in r:
            c = _cell(c)
            if isinstance(c, tuple):
                cells.append(str(c[2]))
            else:
                cells.append(c.replace("|", "\\|").replace("\n", " "))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def html_table(header, rows):
    th = "".join("<th>%s</th>" % esc(h) for h in header)
    trs = []
    for r in rows:
        tds = []
        for c in r:
            c = _cell(c)
            if isinstance(c, tuple):
                tds.append('<td><a href="%s">%s</a></td>' % (esc(c[1]), esc(c[2])))
            else:
                tds.append("<td>%s</td>" % esc(c))
        trs.append("<tr>%s</tr>" % "".join(tds))
    return "<table><tr>%s</tr>%s</table>" % (th, "".join(trs))


class Report:
    """双格式累积渲染器：md 行列表 + html 片段列表。"""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.md = []
        self.html = []

    def h1(self, t):
        self.md.append("# " + t)
        self.html.append("<h1>%s</h1>" % esc(t))

    def h2(self, t, anchor=None):
        self.md.append("\n## " + t)
        a = ' id="%s"' % anchor if anchor else ""
        self.html.append("<h2%s>%s</h2>" % (a, esc(t)))

    def h3(self, t):
        self.md.append("\n### " + t)
        self.html.append("<h3>%s</h3>" % esc(t))

    def h4(self, t):
        self.md.append("\n#### " + t)
        self.html.append("<h4>%s</h4>" % esc(t))

    def para(self, t, cls=None):
        self.md.append(t)
        c = ' class="%s"' % cls if cls else ""
        self.html.append("<p%s>%s</p>" % (c, esc(t)))

    def md_only(self, t):
        self.md.append(t)

    def html_only(self, h):
        self.html.append(h)

    def table(self, header, rows):
        self.md.append(md_table(header, rows))
        self.html.append(html_table(header, rows))

    def code(self, t):
        self.md.append("\n```\n%s\n```\n" % t)
        self.html.append("<pre>%s</pre>" % esc(t))

    def details_block(self, summary_md, summary_html, body):
        """Markdown 平铺；HTML 折叠（details 可下拉查看）。"""
        self.md.append(summary_md)
        self.md.extend(body.md)
        self.html.append('<details class="detail"><summary>%s</summary>%s</details>'
                         % (summary_html, "".join(body.html)))

    def render_html(self, title):
        return ("<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
                "<title>%s</title><style>%s</style></head><body>%s</body></html>"
                % (esc(title), CSS, "\n".join(self.html)))


def build_report(ctx):
    r = Report(ctx)
    win1, win2 = ctx["window"]
    r.h1("推理精度异常数据分析报告")
    r.para("数据日期：%s ~ %s    报告生成时间：%s    实例数：%d" % (
        win1.strftime("%Y-%m-%d"), win2.strftime("%Y-%m-%d"),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(ctx["instances"])), cls="meta")

    # ---- 1. 总览 ----
    r.h2("1. 总览")
    rows = []
    for inst in ctx["instances"]:
        rows.append([inst["name"], inst["model_text"], inst["server_text"],
                     inst["hardware_text"], inst["status_text"], inst["form_text"],
                     inst["in_window"],
                     ("@link", "#inst-%s" % _slug(inst["name"]), "详细分析")])
    r.table(["服务实例", "模型", "部署服务器", "硬件", "状态", "部署方式", "检出异常数", "详细分析"],
            rows)
    if ctx["instance_errors"]:
        r.h3("实例状态与错误")
        r.table(["实例", "问题", "补救动作"],
                [[e["name"], e["reason"], e["advice"]] for e in ctx["instance_errors"]])

    # ---- 2. 逐实例详细解析（章节埋点 ar:instance:<name>:begin/end 供合并器定位拼接）----
    for inst in ctx["instances"]:
        _nm = esc(inst["name"])
        r.html_only("<!-- ar:instance:%s:begin -->" % _nm)
        r.h2("%s 详细解析" % inst["name"], anchor="inst-%s" % _slug(inst["name"]))
        if inst["error"]:
            r.para("该实例数据不可用：%s" % inst["error"], cls="err")
        elif inst["in_window"] == 0:
            r.para("窗口内无异常检出。")
        else:
            r.h3("异常类型分布")
            drows = []
            for t in ILL_ORDER:
                keys = inst["type_keys"].get(t, [])
                drows.append([ILL_NAMES[t], len(keys),
                              "、".join(str(k) for k in keys) if keys else "-"])
            r.table(["异常类型", "数量", "pkl文件数据位置序号"], drows)
            f, l = inst["first_day_types"], inst["last_day_types"]
            new = sorted(l - f, key=lambda t: -inst["type_counts"].get(t, 0))
            gone = sorted(f - l)
            if new or gone:
                parts_ = []
                if new:
                    parts_.append("新出现 " + "、".join(ILL_NAMES[t] for t in new))
                if gone:
                    parts_.append("消失 " + "、".join(ILL_NAMES[t] for t in gone))
                r.para("类型变化（窗口首日 vs 末日）：%s" % "；".join(parts_), cls="note")

            # 检出趋势图（自适应粒度：窗口 ≤2 天按小时分桶铺满全窗口，更长按天；仅 HTML）
            if ctx["trend_gran"] == "hour":
                t0 = datetime.combine(ctx["window"][0], datetime.min.time())
                buckets = [t0 + timedelta(hours=i) for i in range(len(ctx["days"]) * 24)]
                counts = [inst["hourly"].get(b, 0) for b in buckets]
                lbl, fmt, hover = "按小时", "%m-%d %H时", "%Y-%m-%d %H:00"
            else:
                buckets = ctx["days"]
                counts = [inst["daily"].get(d, 0) for d in buckets]
                lbl, fmt, hover = "按天", "%m-%d", "%Y-%m-%d"
            if any(counts):
                r.html_only("<h3>检出趋势（%s）</h3>" % lbl)
                r.html_only('<div class="chart">%s</div>' % svg_trend(
                    buckets, counts, "%s 检出数（%s）" % (inst["name"], lbl),
                    fmt=fmt, hover=hover))

            r.h3("异常实例具体分析")
            for t in (1, 2, 3):
                keys = inst["type_keys"].get(t, [])
                if not keys:
                    continue
                shown = inst["details"].get(t, [])
                r.h4(ILL_NAMES[t])
                if not shown:
                    r.para("该类型无可展示明细（重跑不可用）。")
                    continue
                for idx, d in enumerate(shown, 1):
                    _render_detail(r, t, d, idx, ctx)
                rest = len(keys) - len(shown)
                if rest > 0:
                    r.para("篇幅原因，只展示前 %d 条异常记录，其余 %d 条请按 pkl 序号回查源文件"
                           "内容。" % (len(shown), rest), cls="note")
        r.html_only("<!-- ar:instance:%s:end -->" % _nm)
    return r


def _render_detail(r, t, d, idx, ctx):
    """单条异常明细：HTML 折叠块（可下拉查看），Markdown 平铺。"""
    summary_md = "（%d）pkl文件数据位置序号 %d    检出时间：%s" % (
        idx, d["key"], fmt_ts(d["rec"]["time"]))
    summary_html = ("（%d）pkl 序号 %d · %s · %s"
                    % (idx, d["key"], ILL_NAMES[t], esc(fmt_ts(d["rec"]["time"]))))
    body = Report(ctx)

    if t == 1:
        rows = [[p["pos"], p["tid"], p["tok"], p["ctx"], p["cands"]]
                for p in d.get("positions", [])]
        if rows:
            body.table(["位置", "token_id", "token", "该token所在位置上下文",
                        "该token所在位置排名前20的候选token"], rows)
        else:
            body.para("无逐位置明细（%s）。" % d.get("pos_reason", "定位不可用"), cls="note")
    else:
        w = d.get("window")
        if w:
            body.para("触发窗口：[%d,%d) 展示区间:[%d,%d)"
                      % (w[0], w[1], d["span"][0], d["span"][1]))
        else:
            body.para("展示区间:[%d,%d)%s" % (d["span"][0], d["span"][1],
                      "（%s）" % d["span_label"] if d.get("span_label") else ""))
        body.code(d.get("text", "") or "(空)")
    te = d.get("thought_end")
    if te is None:
        body.para("当前序列未找到思维链结束标记，无法区分思维链与输出", cls="note")
    else:
        body.para("当前序列思维链结束位置：%d" % te, cls="note")
    if d.get("reason_note"):
        body.para(d["reason_note"], cls="note")
    r.details_block(summary_md, summary_html, body)


def _slug(s):
    return "".join(ch if ch.isalnum() else "-" for ch in str(s))


# ---------------------------------------------------------------- main

def check_online(inst):
    server, port = inst.get("server"), inst.get("port")
    if not server or not port:
        return "-"
    mp = inst.get("metrics_path") or "/anomaly/metrics"
    url = "http://%s:%s%s" % (server, port, mp if str(mp).startswith("/") else "/" + str(mp))
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=STATUS_TIMEOUT) as resp:
            return "在线" if resp.status == 200 else "离线"
    except Exception:
        return "离线"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args()
    (win1, win2), mode = resolve_window(args)
    cfg = load_config(args.config)

    instances_cfg = cfg.get("instances") or []
    if args.instance:
        wanted = set(args.instance)
        instances_cfg = [i for i in instances_cfg if i.get("name") in wanted]
        if not instances_cfg:
            fail(2, "--instance 过滤后无匹配实例：%s" % ", ".join(sorted(wanted)))
    type_filter = {ILL_FILTER[t] for t in args.ill_types} if args.ill_types else None

    report_dir = args.report_dir or cfg.get("report_dir") or "./reports"
    detector_config = cfg.get("detector_config") or str(REPO_ROOT / "configs" / "detector.yaml")

    # ---- 加载各实例 ----
    insts, all_dates = [], set()
    instance_errors = []
    for icfg in instances_cfg:
        name = str(icfg.get("name") or "?")
        records, dates, err, meta = load_instance_records(icfg, cfg)
        all_dates |= dates
        inst = {"cfg": icfg, "name": name, "records": records, "error": err,
                "meta": meta, "all_dates": dates}
        if err:
            instance_errors.append({"name": name, "reason": err,
                                    "advice": "检查 pkl 是否同步 / pkl_file 配置是否正确"})
        insts.append(inst)

    # --all 窗口回填（仅当用户显式要求全部数据）
    if mode == "all":
        if all_dates:
            win1, win2 = min(all_dates), max(all_dates)
        else:
            win1 = win2 = date.today()
    days = list(daterange(win1, win2))
    rerun = Rerunner(detector_config, None, enabled=not args.no_detector)

    # ---- 逐实例统计 ----
    totals = Counter()
    data_lo, data_hi = None, None
    ok_count = 0
    for inst in insts:
        icfg = inst["cfg"]
        name = inst["name"]
        recs = [r_ for r_ in inst["records"] if win1 <= r_["date"] <= win2]
        if type_filter:
            recs = [r_ for r_ in recs if r_["ill_type"] in type_filter]
        inst["loaded_in_window"] = len(recs)
        if inst["records"]:
            ds = [r_["date"] for r_ in inst["records"]]
            lo, hi = min(ds), max(ds)
            data_lo = lo if data_lo is None else min(data_lo, lo)
            data_hi = hi if data_hi is None else max(data_hi, hi)
        type_keys = defaultdict(list)
        type_counts = Counter()
        daily = Counter()
        hourly = Counter()
        for r_ in recs:
            type_keys[r_["ill_type"]].append(r_["pkl_key"])
            type_counts[r_["ill_type"]] += 1
            daily[r_["date"]] += 1
            try:
                hourly[datetime.fromtimestamp(int(r_["time"])).replace(
                    minute=0, second=0, microsecond=0)] += 1
            except Exception:
                pass
            totals[r_["ill_type"]] += 1
        inst.update({"type_keys": type_keys, "type_counts": type_counts,
                     "daily": daily, "hourly": hourly, "in_window": len(recs),
                     "first_day_types": {r_["ill_type"] for r_ in recs
                                         if r_["date"] == win1},
                     "last_day_types": {r_["ill_type"] for r_ in recs
                                        if r_["date"] == win2}})
        # 总览字段
        inst["hardware_text"] = icfg.get("hardware") or "-"
        form = icfg.get("form")
        inst["form_text"] = {"single": "单机", "pd_proxy": "PD(proxy)"}.get(form, form or "-")
        inst["server_text"] = str(icfg.get("server") or "-")
        inst["model_text"] = str(icfg.get("model") or
                                 (Counter(r_["model_name"] for r_ in recs
                                          if r_["model_name"]).most_common(1)[0][0]
                                  if recs and any(r_["model_name"] for r_ in recs) else "-"))
        inst["status_text"] = check_online(icfg)
        if not inst["error"]:
            ok_count += 1
        # 重跑定位（每实例 tokenizer 惰性建 Rerunner）
        details = defaultdict(list)
        if not args.no_detector and inst["in_window"] > 0:
            rr = rerun
            if rr.tok is None and icfg.get("tokenizer_path") and rr.ok:
                rr2 = Rerunner(detector_config, icfg.get("tokenizer_path"), enabled=True)
                if rr2.ok:
                    rr = rr2
            for t in (1, 2, 3):
                keys = type_keys.get(t, [])[: max(0, args.max_detail)]
                for key in keys:
                    rec = next(r_ for r_ in recs if r_["pkl_key"] == key)
                    res = rr.rerun(rec) if rr.ok else {
                        "ok": False, "reason": rr.reason}
                    detail = {"key": key, "rec": rec}
                    seq, seq_is_real = get_seq_ids(rec)
                    texts = decode_seq(rr if rr.ok and rr.tok else None, seq)
                    if not texts and rec.get("text"):
                        texts = list(rec["text"])
                        detail["reason_note"] = "无 tokenizer，展示文本为输出原文近似切片"
                    detail["thought_end"] = thought_end(texts, icfg.get(
                        "reasoning_end_markers") or DEFAULT_MARKERS)
                    if t == 1:
                        detail["positions"] = _rare_positions(rec, seq, texts, rr, detail)
                        if not detail["positions"]:
                            detail["pos_reason"] = "topk 数据缺失或序列为空"
                    else:
                        w = (find_repetition_window(seq) if t == 3
                             else find_garbled_window(texts))
                        s, e, label = display_span(len(seq), w)
                        detail["window"] = w
                        detail["span"] = (s, e)
                        detail["span_label"] = None if w else label
                        detail["text"] = "".join(texts[s:e]) if texts else "(空)"
                    details[t].append(detail)
        inst["details"] = details

    if ok_count == 0:
        eprint("全部实例失败：")
        for e in instance_errors:
            eprint("  - %s: %s" % (e["name"], e["reason"]))
        sys.exit(3)

    # 实际数据窗口 = pkl 数据覆盖 ∩ 统计窗口（报告计数仅覆盖该区间）
    data_window = None
    if data_lo:
        ilo, ihi = max(data_lo, win1), min(data_hi, win2)
        if ilo <= ihi:
            data_window = (ilo, ihi)

    # ---- 组装与写出 ----
    ctx = {"window": (win1, win2), "days": days, "instances": insts,
           "totals": totals, "instance_errors": instance_errors,
           "trend_gran": "hour" if len(days) <= 2 else "day"}
    rep = build_report(ctx)

    base = ("推理异常数据分析报告_%s" % win1.strftime("%Y-%m-%d")
            if win1 == win2
            else "推理异常数据分析报告_%s_to_%s" % (win1.strftime("%Y-%m-%d"),
                                                 win2.strftime("%Y-%m-%d")))
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if args.format in ("markdown", "both"):
        p = out_dir / (base + ".md")
        p.write_text("\n".join(rep.md) + "\n", encoding="utf-8")
        written.append(p)
    if args.format in ("html", "both"):
        p = out_dir / (base + ".html")
        p.write_text(rep.render_html("推理精度异常数据分析报告"), encoding="utf-8")
        written.append(p)
    # ---- stats.json 侧车：总览行结构化孪生（合并器按列名对齐取值，不刮 HTML）----
    stats = {
        "stats_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"from": win1.strftime("%Y-%m-%d"), "to": win2.strftime("%Y-%m-%d"),
                   "days": len(days)},
        "data_window": ({"from": data_window[0].strftime("%Y-%m-%d"),
                         "to": data_window[1].strftime("%Y-%m-%d")} if data_window else None),
        "overview_columns": ["服务实例", "模型", "部署服务器", "硬件", "状态",
                             "部署方式", "检出异常数", "详细分析"],
        "trend": {"granularity": ctx["trend_gran"],
                  "points": len(days) * 24 if ctx["trend_gran"] == "hour" else len(days)},
        "rows": [{"instance": i["name"], "model": i["model_text"],
                  "server": i["server_text"], "hardware": i["hardware_text"],
                  "status": i["status_text"], "form": i["form_text"],
                  "anomaly_count": i["in_window"],
                  "anchor": "inst-%s" % _slug(i["name"]),
                  "error": i["error"]} for i in insts],
        "errors": [{"name": e["name"], "reason": e["reason"], "advice": e["advice"]}
                   for e in instance_errors],
        "type_counts": {i["name"]: {ILL_NAMES[t]: i["type_counts"].get(t, 0)
                                    for t in ILL_ORDER} for i in insts},
    }
    import json as _json
    p = out_dir / (base + ".stats.json")
    p.write_text(_json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(p)

    # ---- stdout 摘要 ----
    total = sum(i["in_window"] for i in insts)
    print("窗口: %s ~ %s（共 %d 天，统计严格限定该窗口）  异常总数: %d" % (
        win1.strftime("%Y-%m-%d"), win2.strftime("%Y-%m-%d"), days_n := (win2 - win1).days + 1,
        total))
    print("实例: %d（成功 %d / 失败 %d）" % (len(insts), ok_count, len(instance_errors)))
    for inst in insts:
        tc = inst["type_counts"]
        print("  %s: 检出=%d (生僻字=%d 乱码=%d 重复=%d NaN=%d) %s%s" % (
            inst["name"], inst["in_window"], tc.get(1, 0), tc.get(2, 0),
            tc.get(3, 0), tc.get(4, 0),
            "[问题:%s]" % inst["error"] if inst["error"] else "",
            " [状态:%s]" % inst["status_text"]))
    if data_window:
        print("实际数据窗口: %s ~ %s（= 统计窗口 ∩ pkl 数据覆盖）" % (
            data_window[0].strftime("%Y-%m-%d"), data_window[1].strftime("%Y-%m-%d")))
    for e in instance_errors:
        print("  [失败] %s: %s（%s）" % (e["name"], e["reason"], e["advice"]))
    print("侧车(合并用): %s" % (out_dir / (base + ".stats.json")).resolve())
    main_file = next(p for p in written if p.suffix == ".html") if any(
        p.suffix == ".html" for p in written) else written[0]
    print("报告已生成: %s" % main_file.resolve())
    sys.exit(0)


def _rare_positions(rec, seq, texts, rr, detail):
    """生僻字逐位置：按 top1 logprob 升序（最不可能的 token 优先），≤20 行。"""
    rows_p, rows_t = rec.get("topk_logprobs"), rec.get("tokens_ids")
    if not isinstance(rows_p, (list, tuple)) or not seq:
        return []
    pos_score = []
    for i, prow in enumerate(rows_p[:len(seq)]):
        if isinstance(prow, dict):
            vals = [v for v in prow.values() if isinstance(v, (int, float))]
            top1 = min(vals) if vals else None
        elif isinstance(prow, (list, tuple)) and prow:
            try:
                top1 = min(float(v) for v in prow)
            except (TypeError, ValueError):
                top1 = None
        else:
            top1 = None
        if top1 is not None:
            pos_score.append((top1, i))
    pos_score.sort(key=lambda x: x[0])
    out = []
    for top1, i in pos_score[:RARE_MAX_ROWS]:
        tid = seq[i] if i < len(seq) else -1
        tok = texts[i] if i < len(texts) else ""
        ctx_l = texts[max(0, i - CONTEXT_RADIUS): i]
        ctx_r = texts[i + 1: i + 1 + CONTEXT_RADIUS]
        ctx = ("".join(ctx_l) + "【" + (tok or "?") + "】" + "".join(ctx_r))[:80]
        cands = _candidates(rr, rows_t, i)
        out.append({"pos": i, "tid": tid, "tok": short(tok or "?", 16),
                    "ctx": ctx, "cands": cands})
    return out


def _candidates(rr, rows_t, i):
    """该位置排名前 20 的候选 token（解码展示）。"""
    if not isinstance(rows_t, (list, tuple)) or i >= len(rows_t):
        return "-"
    row = rows_t[i]
    ids = []
    if isinstance(row, dict):
        ids = [k for k, _ in sorted(row.items(), key=lambda x: -x[1])[:20]]
    elif isinstance(row, (list, tuple)):
        ids = list(row[:20])
    if not ids:
        return "-"
    toks = []
    for tid in ids:
        try:
            tid_i = int(tid)
        except (TypeError, ValueError):
            continue
        t = rr.decode(tid_i) if rr is not None else None
        toks.append(short(t if t else str(tid_i), 10))
    return " / ".join(toks) if toks else "-"


if __name__ == "__main__":
    main()
