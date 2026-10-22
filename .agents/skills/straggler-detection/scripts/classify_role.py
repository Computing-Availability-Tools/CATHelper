#!/usr/bin/env python3
"""区分 verl colocate 场景下 profiler 数据是训练(FSDP)还是 rollout(推理引擎)。
引擎无关版本: 不依赖 vLLM/SGLang 特定算子名, 换 rollout 引擎依然有效。

用法: 在包含 worker*_*_ascend_pt 目录的根目录下运行:
    python classify_role.py

判据分三层, 上层优先:
    L1 反向传播存在性  —— 引擎无关的决定性判据(rollout 永远没有 backward)
    L2 推理引擎指纹词表 —— 可选加速项, 换引擎只需加词表, 缺失不影响正确性
    L3 引擎无关行为特征 —— 兜底: 采样算子/KV cache 落盘核/GEMV 形状/通信模式
"""
import glob
import json
import os
import re
import sqlite3
import sys

# ---------- L1: 训练侧不变判据 (autograd 存在即训练) ----------
BACKWARD_PATTERNS = [
    "Backward", "backward",            # autograd 节点/算子, 如 MatmulBackward0
    "AccumulateGrad", "RmsNormGrad",   # 梯度累积 / 融合反核
    "clip_grad", "optimizer", "Adam", "adam",
]

# ---------- L2: 推理引擎专属词表 (可选, 按引擎增删) ----------
ENGINE_VOCAB = {
    "vllm": [
        "vllm::", "unified_attention",
    ],
    "sglang": [
        "sglang::", "sgl_kernel", "RadixAttention", "radix",
        "flashinfer",                                  # CUDA 后端
    ],
    # 未来加 trt-llm / trl 等均在此扩展
}

# ---------- L3: 引擎无关的推理行为指纹 ----------
INFERENCE_OP_PATTERNS = [
    # 推理融合 attention: vllm-ascend 与 sglang-on-Ascend 共用 ATB/opapi 核
    "FusedInferAttention", "fused_infer_attention",
    "BatchDecode", "BatchPrefill", "single_prefill", "single_decode",
    # KV cache 读写 (任何 paged attention 引擎都要落 cache)
    "ReshapeAndCache", "reshape_and_cache", "set_kv_buffer", "StoreKV",
    # 采样: 出 token 必经之路
    "topk", "top_p", "top_k", "multinomial", "gumbel",
    "RandomSample", "Penalty",
]
FSDP_COMM_PATTERNS = ["ReduceScatter"]          # FSDP 参数分片特征通信


def q(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def gather_signals(db_path):
    con = sqlite3.connect(db_path)
    sig = {"db": db_path}

    # 1) 训练侧: autograd 指纹 (主机侧 PYTORCH_API)
    api = {v[0] for v in q(con, """select distinct s.value from PYTORCH_API p
                                   join STRING_IDS s on p.name=s.id""")}
    api_all = " | ".join(api)
    sig["has_backward"] = any(p in api_all for p in BACKWARD_PATTERNS)

    # 2) 引擎词表命中
    sig["engine"] = None
    for eng, pats in ENGINE_VOCAB.items():
        if any(p in api_all for p in pats):
            sig["engine"] = eng
            break

    # 3) 设备侧算子名 (COMPUTE_TASK_INFO, 引擎无关) + 采样算子
    dev = {v[0] for v in q(con, """select distinct s.value from COMPUTE_TASK_INFO c
                                   join STRING_IDS s on c.name=s.id""")}
    dev_all = " | ".join(dev)
    sig["infer_ops"] = sorted({p for p in INFERENCE_OP_PATTERNS
                               if p.lower() in dev_all.lower() or p.lower() in api_all.lower()})

    # 4) decode 期 GEMV 形状: matmul 的 M 维为 1 (逐 token 解码)
    gemv = q(con, """select sh.value from COMPUTE_TASK_INFO c
                     join STRING_IDS s on c.name=s.id
                     join STRING_IDS sh on c.inputShapes=sh.id
                     where s.value like '%MatMul%' or s.value like '%Matmul%' limit 50""")
    sig["decode_gemv"] = any(re.search(r'(^|[\[,])\s*1\s*[,\]]', r[0] or "")
                             for r in gemv)

    # 5) FSDP 通信特征 (MSTX 的 HCCL 消息)
    mstx = " ".join(str(r[0]) for r in q(
        con, """select coalesce(s.value, '') from MSTX_EVENTS m
                left join STRING_IDS s on m.message=s.id"""))
    sig["fsdp_comm"] = any(p in mstx for p in FSDP_COMM_PATTERNS)

    # 6) session 时长 / 时间窗（交叉验证用：colocate 下训练与 rollout 窗口应错开）
    rows = q(con, "select min(startNs), max(endNs) from TASK")
    sig["win_ns"] = (rows[0][0], rows[0][1]) if rows and rows[0][0] is not None \
        and rows[0][1] is not None else None
    sig["dur_s"] = (rows[0][1] - rows[0][0]) / 1e9 if sig["win_ns"] else None
    con.close()
    return sig


def classify(sig):
    # L1: 决定性 —— 有 backward 必为训练, 与引擎无关
    if sig["has_backward"]:
        return "TRAINING"
    # L2 + L3: 无 backward, 综合推理特征打分
    score = (2 if sig["engine"] else 0) + (2 if sig["infer_ops"] else 0) \
            + (1 if sig["decode_gemv"] else 0) + (2 if sig["fsdp_comm"] is False else 0)
    if sig["fsdp_comm"]:
        return "TRAINING (forward-only? 检查采样窗口)"
    return "ROLLOUT" if score >= 2 else "ROLLOUT (低置信, 需人工确认)"


# ---------- 目录级分组: verl colocate 多 worker*_ascend_pt 同层, 每 worker = 一个 rank db ----------
ROLE_TRAINING = "training"
ROLE_ROLLOUT = "rollout"


def _normalize_role(role: str):
    """把 classify() 的字符串标签归一成 'training'/'rollout'；非两者返回 None。"""
    if role.startswith("TRAINING"):
        return ROLE_TRAINING
    if role.startswith("ROLLOUT"):
        return ROLE_ROLLOUT
    return None


def _rank_from_db_filename(db_path: str):
    """从 ascend_pytorch_profiler_<rank>.db 文件名提取 rank。"""
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
    """
    第 0 步 · 进程分组（目录层面）。

    在 root 下枚举同层的 worker*_ascend_pt 目录，每个 worker 目录预期恰好
    一个 rank 的 profiler db（ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_{rank}.db）。

    返回: [{worker_dir, db, rank}]；无 worker*_ascend_pt 目录时返回 []。
    """
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
        search_dirs.append(wdir)  # 兜底: worker 目录自身直接含 db

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
    """两个 (startNs, endNs) 窗口是否重叠。任一为 None 视为未知(不判重叠)。"""
    if not a or not b:
        return None
    return a[0] < b[1] and b[0] < a[1]


def build_role_worlds(root: str):
    """
    第 1/2 步 · 按角色把 worker db 分成“世界”并做时间窗交叉验证。

    返回: [{role, dbs, ranks, workers, win_ns, note}]，每个角色一个世界。
    同一世界里所有 db 应角色一致、时间窗基本重叠；跨世界窗口应错开。
    """
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

        # 世界内时间窗 = 覆盖所有 rank 的 [最早 start, 最晚 end]
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

    # 交叉验证: 训练与 rollout 窗口不应重叠（colocate 下生成/训练错开）
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

    # 若 root 是 verl colocate 布局（同层多 worker*_ascend_pt），直接按世界输出
    worlds = build_role_worlds(root)
    if worlds:
        for w in worlds:
            print(f"\n[{w['role']}]  {len(w['dbs'])} rank")
            print(f"    ranks={w['ranks']}  窗口={_fmt_win(w['win_ns'])}  {w['note']}")
            for worker_dir in w["workers"]:
                print(f"    {os.path.basename(worker_dir)}")
        return

    # 回退：无 worker 目录结构，按单个 db 逐条定性（原行为）
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
