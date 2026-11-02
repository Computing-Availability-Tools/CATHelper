#!/usr/bin/env python3
import glob
import json
import os
import re
import sqlite3
import sys
BACKWARD_PATTERNS = [
    "Backward", "backward",
    "AccumulateGrad", "RmsNormGrad",
    "clip_grad", "optimizer", "Adam", "adam",
]
ENGINE_VOCAB = {
    "vllm": [
        "vllm::", "unified_attention",
    ],
    "sglang": [
        "sglang::", "sgl_kernel", "RadixAttention", "radix",
        "flashinfer",
    ],
}
INFERENCE_OP_PATTERNS = [
    "FusedInferAttention", "fused_infer_attention",
    "BatchDecode", "BatchPrefill", "single_prefill", "single_decode",
    "ReshapeAndCache", "reshape_and_cache", "set_kv_buffer", "StoreKV",
    "topk", "top_p", "top_k", "multinomial", "gumbel",
    "RandomSample", "Penalty",
]
FSDP_COMM_PATTERNS = ["ReduceScatter"]

def q(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []

def gather_signals(db_path):
    con = sqlite3.connect(db_path)
    sig = {"db": db_path}
    api = {v[0] for v in q(con, """select distinct s.value from PYTORCH_API p
                                   join STRING_IDS s on p.name=s.id""")}
    api_all = " | ".join(api)
    sig["has_backward"] = any(p in api_all for p in BACKWARD_PATTERNS)
    sig["engine"] = None
    for eng, pats in ENGINE_VOCAB.items():
        if any(p in api_all for p in pats):
            sig["engine"] = eng
            break
    dev = {v[0] for v in q(con, """select distinct s.value from COMPUTE_TASK_INFO c
                                   join STRING_IDS s on c.name=s.id""")}
    dev_all = " | ".join(dev)
    sig["infer_ops"] = sorted({p for p in INFERENCE_OP_PATTERNS
                               if p.lower() in dev_all.lower() or p.lower() in api_all.lower()})
    gemv = q(con, """select sh.value from COMPUTE_TASK_INFO c
                     join STRING_IDS s on c.name=s.id
                     join STRING_IDS sh on c.inputShapes=sh.id
                     where s.value like '%MatMul%' or s.value like '%Matmul%' limit 50""")
    sig["decode_gemv"] = any(re.search(r'(^|[\[,])\s*1\s*[,\]]', r[0] or "")
                             for r in gemv)
    mstx = " ".join(str(r[0]) for r in q(
        con, """select coalesce(s.value, '') from MSTX_EVENTS m
                left join STRING_IDS s on m.message=s.id"""))
    sig["fsdp_comm"] = any(p in mstx for p in FSDP_COMM_PATTERNS)
    rows = q(con, "select min(startNs), max(endNs) from TASK")
    sig["win_ns"] = (rows[0][0], rows[0][1]) if rows and rows[0][0] is not None \
        and rows[0][1] is not None else None
    sig["dur_s"] = (rows[0][1] - rows[0][0]) / 1e9 if sig["win_ns"] else None
    con.close()
    return sig

def classify(sig):
    if sig["has_backward"]:
        return "TRAINING"
    score = (2 if sig["engine"] else 0) + (2 if sig["infer_ops"] else 0) \
            + (1 if sig["decode_gemv"] else 0) + (2 if sig["fsdp_comm"] is False else 0)
    if sig["fsdp_comm"]:
        return "TRAINING (forward-only? 检查采样窗口)"
    return "ROLLOUT" if score >= 2 else "ROLLOUT (低置信, 需人工确认)"
ROLE_TRAINING = "training"
ROLE_ROLLOUT = "rollout"

def _normalize_role(role: str):
    if role.startswith("TRAINING"):
        return ROLE_TRAINING
    if role.startswith("ROLLOUT"):
        return ROLE_ROLLOUT
    return None

def _rank_from_db_filename(db_path: str):
    fn = os.path.basename(db_path)
    if not fn.startswith("ascend_pytorch_profiler_"):
        return None
    stem = fn[len("ascend_pytorch_profiler_"):]
    if stem.endswith(".db"):
        stem = stem[:-3]
    try:
        return int(stem)
    except ValueError:
        return None

def discover_worker_dbs(root: str):
    items = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return items
    for name in names:
        if not name.endswith("_ascend_pt"):
            continue
        wdir = os.path.join(root, name)
        if not os.path.isdir(wdir):
            continue
        search_dirs = []
        out_dir = os.path.join(wdir, "ASCEND_PROFILER_OUTPUT")
        if os.path.isdir(out_dir):
            search_dirs.append(out_dir)
        search_dirs.append(wdir)
        seen = set()
        for sd in search_dirs:
            if sd in seen:
                continue
            seen.add(sd)
            try:
                files = sorted(os.listdir(sd))
            except OSError:
                continue
            for fn in files:
                if fn.startswith("ascend_pytorch_profiler_") and fn.endswith(".db"):
                    db = os.path.join(sd, fn)
                    if os.path.getsize(db) > 0:
                        items.append({
                            "worker_dir": wdir,
                            "db": db,
                            "rank": _rank_from_db_filename(db),
                        })
    return items

def _window_overlap(a, b):
    if not a or not b:
        return None
    return a[0] < b[1] and b[0] < a[1]

def build_role_worlds(root: str):
    workers = discover_worker_dbs(root)
    if not workers:
        return []
    worlds = {}
    for w in workers:
        try:
            sig = gather_signals(w["db"])
        except Exception:
            continue
        role = _normalize_role(classify(sig))
        if not role:
            continue
        w["signals"] = sig
        worlds.setdefault(role, []).append(w)
    result = []
    for role, ws in sorted(worlds.items()):
        ws.sort(key=lambda x: (x["rank"] if x["rank"] is not None else -1))
        dbs = [w["db"] for w in ws]
        ranks = sorted(x["rank"] for x in ws if x["rank"] is not None)
        wins = [w["signals"]["win_ns"] for w in ws if w["signals"]["win_ns"]]
        win_ns = (min(w[0] for w in wins), max(w[1] for w in wins)) if wins else None
        note = []
        if ranks and len(ranks) < len(ws):
            note.append("部分 rank 无法从文件名解析")
        result.append({
            "role": role,
            "dbs": dbs,
            "ranks": ranks,
            "workers": [w["worker_dir"] for w in ws],
            "win_ns": win_ns,
            "note": "; ".join(note),
        })
    if len(result) >= 2:
        for i in range(len(result)):
            for j in range(i + 1, len(result)):
                ov = _window_overlap(result[i]["win_ns"], result[j]["win_ns"])
                if ov is True:
                    result[i]["note"] = (result[i]["note"] + "; " if result[i]["note"] else "") + \
                        f"与 {result[j]['role']} 时间窗重叠, 请人工确认分组"
                    result[j]["note"] = (result[j]["note"] + "; " if result[j]["note"] else "") + \
                        f"与 {result[i]['role']} 时间窗重叠, 请人工确认分组"
    return result

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    worlds = build_role_worlds(root)
    if worlds:
        for w in worlds:
            print(f"\n[{w['role']}]  {len(w['dbs'])} rank")
            print(f"    ranks={w['ranks']}  窗口={_fmt_win(w['win_ns'])}  {w['note']}")
            for worker_dir in w["workers"]:
                print(f"    {os.path.basename(worker_dir)}")
        return
    dbs = sorted(glob.glob(os.path.join(
        root, "worker*_*_ascend_pt", "ASCEND_PROFILER_OUTPUT",
        "ascend_pytorch_profiler_*.db")))
    if not dbs:
        sys.exit("未找到 ascend_pytorch_profiler_*.db, 请在数据根目录运行")
    by_role = {}
    for db in dbs:
        sig = gather_signals(db)
        sig["role"] = classify(sig)
        by_role.setdefault(sig["role"], []).append(sig)
    for role, items in sorted(by_role.items()):
        print(f"\n[{role}]  {len(items)} rank")
        for i in items:
            d = os.path.basename(os.path.dirname(os.path.dirname(i["db"])))
            print(f"    {d[:44]:44s} engine={i['engine'] or 'n/a':8s} "
                  f"backward={i['has_backward']} infer_ops={i['infer_ops']} "
                  f"gemv={i['decode_gemv']} fsdp_comm={i['fsdp_comm']} "
                  f"dur={i['dur_s'] and round(i['dur_s'], 2)}s")

def _fmt_win(win_ns) -> str:
    if not win_ns:
        return "n/a"
    return f"{win_ns[0]/1e9:.2f}s~{win_ns[1]/1e9:.2f}s"
if __name__ == "__main__":
    main()
