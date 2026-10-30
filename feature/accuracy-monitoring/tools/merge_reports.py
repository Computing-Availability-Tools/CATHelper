#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多节点报告合并器：把各服务实例节点生成的报告合并为一份总报告。

输入：各节点回传的 <报告>.html（内含 ar:instance:<名>:begin/end 章节埋点）
      + 同基名 <报告>.stats.json（总览行结构化孪生，与 HTML 同次运行生成）
输出：一份合并 HTML：
      1. 总览 = 一张表：表头一行 + 每实例一行（按列名从 stats.json 对齐取值，
         不重算求和、不刮 HTML）；"详细分析"列锚点指向拼接后的实例章节
      2+. 各实例章节原样拼接（分布表/SVG 趋势图/明细折叠块随章节整体搬运）
      尾：实例状态与错误汇总（各节点错误 + --error 传入的整节点失败）

用法：
    python tools/merge_reports.py --output <合并.html> \
        [--title 标题] [--error 实例名=原因 ...] <报告1.html> <报告2.html> ...

退出码：0=成功 2=用法/输入错误 3=无有效输入
"""
import argparse
import html as _html
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from anomaly_report import CSS, esc, html_table  # noqa: E402  复用生成器样式/转义/表格

STATS_VERSION = 1
COLUMNS = ["服务实例", "模型", "部署服务器", "硬件", "状态", "部署方式",
           "检出异常数", "详细分析"]


def eprint(*a):
    print(*a, file=sys.stderr)


def extract_section(html_text, name):
    """按埋点截取实例章节；返回 None=埋点缺失（工具版本过旧）。"""
    key = _html.escape(str(name), quote=False)
    begin = "<!-- ar:instance:%s:begin -->" % key
    end = "<!-- ar:instance:%s:end -->" % key
    i = html_text.find(begin)
    if i < 0:
        return None
    j = html_text.find(end, i)
    if j < 0:
        return None
    return html_text[i: j + len(end)]


def load_input(html_path):
    html_path = Path(html_path)
    # 报告名形如 推理异常数据分析报告_X_to_Y.html → 侧车同名 .stats.json
    stats_path = html_path.parent / (html_path.stem + ".stats.json")
    if not html_path.exists():
        raise FileNotFoundError("报告不存在: %s" % html_path)
    if not stats_path.exists():
        raise FileNotFoundError("侧车缺失: %s（需与 html 同基名、同目录）" % stats_path)
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if int(stats.get("stats_version", 0)) != STATS_VERSION:
        raise ValueError("%s: stats_version=%s 不兼容（需 %d）——节点侧工具版本过旧或过新"
                         % (html_path.name, stats.get("stats_version"), STATS_VERSION))
    html_text = html_path.read_text(encoding="utf-8")
    return html_text, stats


def build_merged(inputs, extra_errors, title):
    wins = set()
    rows, errors, sections = [], [], []
    seen_names = {}
    for html_path, html_text, stats in inputs:
        wins.add((stats["window"]["from"], stats["window"]["to"]))
        for row in stats.get("rows", []):
            name = row.get("instance", "?")
            if name in seen_names:
                eprint("警告：实例 %s 重复出现（%s 与 %s），两行都保留"
                       % (name, seen_names[name], html_path.name))
            seen_names[name] = html_path.name
            rows.append([name, row.get("model", "-"), row.get("server", "-"),
                         row.get("hardware", "-"), row.get("status", "-"),
                         row.get("form", "-"), str(row.get("anomaly_count", 0)),
                         ("@link", "#%s" % row.get("anchor", ""), "详细分析")])
            sec = extract_section(html_text, name)
            if sec is None:
                raise ValueError(
                    "%s 未找到实例 %s 的章节埋点（ar:instance:%s）——节点侧工具缺少埋点改造"
                    % (html_path.name, name, name))
            sections.append(sec)
        for e in stats.get("errors", []):
            errors.append([e.get("name", "?"), e.get("reason", "-"),
                           e.get("advice", "-")])
    for e in extra_errors:
        errors.append([e[0], e[1], "检查节点连通性 / 工具部署 / 查看节点侧错误后重跑"])

    parts = ['<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
             '<title>%s</title><style>%s</style></head><body>' % (esc(title), CSS)]
    parts.append('<h1>%s</h1>' % esc(title))
    win_text = " ~ ".join(wins.pop()) if len(wins) == 1 else \
        "；".join("%s ~ %s" % w for w in sorted(wins)) + "（注意：各节点窗口不一致）"
    parts.append('<p class="meta">数据日期：%s    合并生成时间：%s    实例数：%d</p>'
                 % (esc(win_text), esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    len(rows)))
    parts.append('<h2>1. 总览</h2>')
    parts.append(html_table(COLUMNS, rows))
    if errors:
        parts.append('<h3>实例状态与错误</h3>')
        parts.append(html_table(["实例", "问题", "补救动作"], errors))
    parts.append('<h2>2. 逐实例详细解析</h2>')
    parts.extend(sections)
    parts.append("</body></html>")
    return "".join(parts)


def main():
    p = argparse.ArgumentParser(prog="merge_reports.py",
                                description="合并多节点异常分析报告为一份总报告")
    p.add_argument("reports", nargs="+", help="各节点回传的 <报告>.html（同目录需有同基名 .stats.json）")
    p.add_argument("--output", required=True, help="合并报告输出路径（.html）")
    p.add_argument("--title", default="推理精度异常数据分析报告（合并）")
    p.add_argument("--error", action="append", default=[], dest="extra_errors",
                   metavar="实例名=原因", help="整节点失败声明（无报告可回传时），可多次")
    args = p.parse_args()

    extra_errors = []
    for e in args.extra_errors:
        name, _, reason = e.partition("=")
        extra_errors.append([name.strip(), reason.strip() or "未提供原因", "重跑该节点报告后重新合并"])
    if not args.reports and not extra_errors:
        eprint("错误：无报告输入且无失败声明")
        sys.exit(3)

    inputs = []
    for rp in args.reports:
        try:
            inputs.append((Path(rp), *load_input(rp)))
        except Exception as e:
            eprint("错误：%s" % e)
            sys.exit(2)
    if not inputs:
        eprint("错误：无有效报告输入")
        sys.exit(3)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_merged(inputs, extra_errors, args.title), encoding="utf-8")
    print("实例数: %d（章节拼接 %d）  失败声明: %d" % (
        sum(len(s["rows"]) for _, _, s in inputs), len(inputs), len(extra_errors)))
    print("合并报告已生成: %s" % out.resolve())
    sys.exit(0)


if __name__ == "__main__":
    main()
