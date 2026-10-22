#!/usr/bin/env python3
"""
Slow Node Detection - 主入口

用法:
    python main.py path=/xxx compute=1.3 io=2.5 comm=1.3
    或作为 skill 被 Claude Code 调用
"""

import os
import sys
import logging
from typing import Dict, Any, List

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import profilingdataparse
import nodelevel
import nodelevel_data_handler
import utils
import visualizer
import summary_table

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(message)s'
)
logger = logging.getLogger("[SLOWNODE ALGO]")


def parse_args(args: list) -> Dict[str, Any]:
    """
    解析命令行参数

    支持的参数:
        path=xxx     - 数据目录路径（必需）
        compute=xxx  - 计算类阈值（可选，默认 1.3）
        io=xxx       - IO/CPU 类阈值（可选，默认 2.5）
        comm=xxx     - 通信类阈值（可选，默认 1.3）
        no-align     - 关闭慢通信带宽跨 rank 对齐（可选；也可写 no-align=true）
        clean=xxx    - yes/no/ask
    """
    result = {
        'path': None,
        'compute': 1.3,
        'io': 2.5,
        'comm': 1.3,
        'no_align': False,   # 慢通信带宽是否关闭跨 rank 对齐
        'clean': 'ask'       # ask / yes / no
    }

    def _bad(name, val, default):
        logger.warning(f"Invalid {name} value '{val}', using default {default}")

    for arg in args:
        if arg == 'no-align':        # 裸参数形式 --no-align
            result['no_align'] = True
            continue
        if '=' not in arg:
            continue  # 忽略无效参数

        parts = arg.split('=', 1)
        if len(parts) != 2:
            continue

        key, val = parts[0], parts[1]

        if key == 'path':
            result['path'] = val
        elif key in ('compute', 'io', 'comm'):
            try:
                f = float(val)
                if f > 0:
                    result[key] = f
                else:
                    _bad(key, val, result[key])
            except ValueError:
                _bad(key, val, result[key])
        elif key == 'no-align':
            if val.lower() in ('true', 'yes', '1'):
                result['no_align'] = True
            elif val.lower() in ('false', 'no', '0'):
                result['no_align'] = False
            else:
                logger.warning(f"Invalid no-align value '{val}', using default false")
        elif key == 'clean':
            if val.lower() in ('yes', 'no', 'ask'):
                result['clean'] = val.lower()
            else:
                logger.warning(f"Invalid clean value '{val}', using default 'ask'")

    return result


def _find_job_subdirs(parent_path: str) -> List[str]:
    """
    查找父目录下直接包含 ascend_pytorch_profiler_*.db 文件的子目录（作为独立 job）。

    用于“父目录含多 job 子目录”场景，逐个子目录执行检测。

    返回:
        满足条件的子目录绝对路径列表（不包含父目录本身直接含 db 的情况）
    """
    jobs = []
    try:
        for entry in sorted(os.listdir(parent_path)):
            sub = os.path.join(parent_path, entry)
            if not os.path.isdir(sub):
                continue
            try:
                names = os.listdir(sub)
            except OSError:
                continue
            if any(
                f.startswith("ascend_pytorch_profiler_") and f.endswith(".db")
                for f in names
            ):
                jobs.append(sub)
    except OSError as e:
        logger.warning(f"[SLOWNODE ALGO] 扫描父目录 job 子目录失败：{e}")
    return jobs


def _dir_has_db_directly(path: str) -> bool:
    """判断目录自身是否直接包含 ascend_pytorch_profiler_*.db 文件"""
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(
        f.startswith("ascend_pytorch_profiler_") and f.endswith(".db")
        for f in names
    )


def _safe_print(text: str):
    """向 stdout 打印汇总表（兼容 Windows GBK 等非 UTF-8 控制台，避免 UnicodeEncodeError）"""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            sys.stdout.write(text.encode(enc, errors="replace").decode(enc))
        except Exception:
            sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii"))


def _backfill_slow_domain(job_path: str, db_files: list = None):
    """慢通信带宽 / PP 等待回填（附加步骤，失败仅告警不中断检测）。"""
    try:
        profilingdataparse.backfill_slow_domain_bandwidth(job_path, db_files)
    except Exception as e:
        logger.warning(f"慢通信带宽回填失败：{e}")
    try:
        profilingdataparse.backfill_pp_wait(job_path, db_files)
    except Exception as e:
        logger.warning(f"PP 等待回填失败：{e}")


def _find_colocate_worlds(input_path: str) -> List[dict]:
    """
    探测 verl colocate 输入：root 下同层多个 worker*_ascend_pt（每 worker = 一个 rank db），
    且角色分类同时出现 training 与 rollout。

    返回满足条件的“世界”列表；不是 colocate 场景时返回 []，走原单/multi-job 流程。
    """
    if not input_path or not os.path.isdir(input_path):
        return []
    try:
        import classify_role
    except Exception as e:
        logger.warning(f"[SLOWNODE ALGO] 导入 classify_role 失败，跳过 colocate 分组：{e}")
        return []
    try:
        worlds = classify_role.build_role_worlds(input_path)
    except Exception as e:
        logger.warning(f"[SLOWNODE ALGO] colocate 角色分组失败：{e}")
        return []
    if not worlds:
        return []
    roles = {w["role"] for w in worlds}
    if len(roles) < 2:
        # 只有一种角色，不是训练/rollout 混合场景，走原流程
        return []
    if any(len(w["ranks"]) < 2 for w in worlds):
        logger.warning("[SLOWNODE ALGO] colocate 世界内 rank 数 <2，无法做跨卡检测，按普通流程处理")
        return []
    return worlds


def _process_colocate_root(input_path: str, worlds: List[dict], thresholds: tuple,
                           clean_mode: str, skip_parsing: bool) -> Dict[str, Any]:
    """
    verl colocate 场景主流程：训练/rollout 各自一个“世界”，分别解析+检测，
    结果输出到 <root>/detection_output/training 与 <root>/detection_output/rollout。
    只在入口确认一次阈值（计算/IO/通信）与 clean，避免每个世界重复提问。
    """
    config.set_file_path(input_path)
    thresholds = utils.confirm_thresholds(thresholds)
    config.set_thresholds(*thresholds)

    if skip_parsing:
        clean_decision = False
    elif clean_mode == 'yes':
        clean_decision = True
    elif clean_mode == 'no':
        clean_decision = False
    else:
        clean_decision = utils.confirm_clean(input_path)

    results = {}
    for world in worlds:
        role = world["role"]
        out_path = os.path.join(input_path, "detection_output", role)
        logger.info(f"===== 处理 {role} 世界：{len(world['dbs'])} rank，ranks={world['ranks']} =====")
        if world.get("note"):
            logger.warning(f"[SLOWNODE ALGO] {role} 世界备注：{world['note']}")
        try:
            results[role] = _process_single_job(
                input_path, thresholds, clean_mode, output_path=out_path,
                db_files=world["dbs"], skip_confirm=True, clean_decision=clean_decision)
        except Exception as e:
            logger.error(f"[SLOWNODE ALGO] {role} 世界检测失败：{e}")
            results[role] = {}
    return results


def _threshold_ask_keys(level: str) -> tuple:
    """按数据级别决定要询问哪些阈值组：cluster 只需 comm；levelnone 跳过 compute（那三类不检测）。"""
    if level == "cluster":
        return ("comm",)
    if level == "levelnone":
        return ("io", "comm")
    return ("compute", "io", "comm")   # level0 / level1


def _process_single_job(job_path: str, thresholds: tuple, clean_mode: str, output_path: str = None,
                        db_files: list = None, skip_confirm: bool = False, clean_decision: bool = None):
    """
    对单个 job 目录执行完整检测流程（解析 → 并行域 → 定界检测 → 结果 → 报告 → 可视化）。
    对应原 main() 的主体逻辑，供多 job / colocate 世界场景逐个调用。

    参数:
        job_path: 输入数据目录（含原始 db）
        thresholds: (计算类, IO/CPU 类, 通信类) 阈值元组
        output_path: 检测结果输出目录；为 None 时输出到 job_path 自身（单 job 场景）
        db_files: 显式指定待解析的 db 列表（colocate 场景按角色世界传入）；None 时递归 job_path
        skip_confirm: True 时不重复提问阈值（调用方已确认一次）
        clean_decision: 已确定的清理结果（True=清理重解析 / False=保留）；None 时按 clean_mode 处理
    """
    # 设置全局配置（含 job 类型检测所需的 FilePath）
    config.set_file_path(job_path)
    if output_path:
        config.set_output_path(output_path)

    # === 数据级别识别（levelnone / level0 / level1 / cluster） ===
    level = nodelevel_data_handler.detect_profiling_level(job_path)
    config.set_profiling_level(level)

    if not skip_confirm:
        # 向用户提问阈值（传入值作为默认/回退）；按级别决定问哪几组
        thresholds = utils.confirm_thresholds(thresholds, keys=_threshold_ask_keys(level))
    config.set_thresholds(*thresholds)

    logger.info(f"开始慢节点检测 - 路径：{job_path}, 阈值(计算/IO/通信)：{thresholds}")
    logger.info(f"数据级别：{level}")
    logger.info(f"检测结果输出目录：{config.get_output_path()}")

    # 检测结果清理与输出目录（output_path），而非原始数据目录
    out_path = config.get_output_path()

    # === cluster_analysis.db 单文件：跳过解析，直接走新两级带宽口径 ===
    if level == "cluster":
        # 输入是文件，结果输出到其所在目录下的 detection_output（避免写到文件本身）
        out_dir = os.path.join(os.path.dirname(os.path.abspath(job_path)), "detection_output")
        os.makedirs(out_dir, exist_ok=True)
        config.set_output_path(out_dir)
        parallels, comm = nodelevel_data_handler.load_cluster_comm(job_path)
        if not parallels or not comm:
            logger.error("cluster 数据为空，无法检测")
            return {}
        valid_ranks = sorted({int(r) for gs in parallels.values() for g in gs for r in g})
        result = nodelevel.delimit_detection({}, parallels, valid_ranks, comm=comm)
        utils.write_result(result, parallels)
        # 生成文本报告（仅含 comm 两级口径段）
        try:
            visualizer.run_visualization(
                {}, parallels, valid_ranks, out_dir,
                detection_result=result, data_path=job_path)
        except Exception as e:
            logger.warning(f"生成报告失败：{e}")
        try:
            summary = summary_table.build_summary_table(result, parallels, {})
            _safe_print(summary)
        except Exception as e:
            logger.warning(f"生成汇总表失败：{e}")
        return result

    # === 步骤 1: 清理/解析（清理决策优先取调用方已确认值） ===
    if clean_decision is None:
        if clean_mode == 'yes':
            clean_decision = True
        elif clean_mode == 'no':
            clean_decision = False
        else:
            clean_decision = utils.confirm_clean(out_path)

    if clean_decision:
        logger.info("开始 Profiling 数据解析（清理旧 op_metric 后重新解析）...")
        utils.clean_detection_outputs(out_path)
        if db_files:
            profilingdataparse.data_parsing_paths(db_files, job_path)
        else:
            profilingdataparse.data_parsing(job_path)
        logger.info("数据解析完成")

        # 慢通信带宽回填（解析后、检测前）：把 "<domain>_<opType>_<count>" 带宽列写回 CSV
        _backfill_slow_domain(job_path, db_files)
    else:
        logger.info("跳过清理和重新解析，直接使用已有的 op_metric 数据")

    # === 步骤 2: 获取并行域和有效 ranks ===
    parallels, valid_ranks = nodelevel_data_handler.get_cur_detection_info(job_path)

    if not parallels or not valid_ranks:
        logger.error("获取并行域/卡数失败")
        return {}

    logger.info(f"并行域：{list(parallels.keys())}")
    logger.info(f"有效 Ranks: {valid_ranks}")

    # === 步骤 3: 获取最新 step 数据 ===
    last_step_data = nodelevel_data_handler.get_cur_job_last_step_data(valid_ranks)

    # level1：装载新两级带宽口径数据（analysis.db 的 CommAnalyzerBandwidth），
    # 传给定界检测替换原 comm 检测。
    comm = None
    if level == "level1":
        comm = nodelevel_data_handler.load_level1_comm(job_path, parallels)

    # === 步骤 4: 执行定界检测 ===
    result = nodelevel.delimit_detection(last_step_data, parallels, valid_ranks, comm=comm)

    # === 步骤 5: 输出结果 ===
    # 始终输出 JSON 结果文件（即使没有异常）
    utils.write_result(result, parallels)

    # === 步骤 6: 可视化 ===
    if last_step_data:
        visualizer.run_visualization(
            last_step_data, parallels, valid_ranks, config.get_output_path(),
            detection_result=result,
            data_path=job_path,
        )

    if not result:
        logger.info("未检测到异常节点")

    # === 步骤 7: 最终输出逐类别汇总表（渲染到调用方 agent 的 stdout） ===
    try:
        summary = summary_table.build_summary_table(
            result, parallels, last_step_data)
        _safe_print(summary)
    except Exception as e:  # 汇总表为附加展示，失败不应中断检测
        logger.warning(f"生成汇总表失败：{e}")

    return result


def main():
    """
    主入口函数
    """
    args = sys.argv[1:]

    if len(args) < 1:
        logger.error("[SLOWNODE ALGO] Usage: python main.py path=<dir | cluster.db> [compute=1.3] [io=2.5] [comm=1.3] [no-align] [clean=ask]")
        sys.exit(1)

    # 解析参数
    parsed = parse_args(args)
    input_path = parsed['path']
    thresholds = (parsed['compute'], parsed['io'], parsed['comm'])
    clean_mode = parsed['clean']
    config.set_comm_no_align(parsed['no_align'])

    if not input_path:
        logger.error("[SLOWNODE ALGO] Missing required parameter: path=/your/data/dir")
        sys.exit(1)

    # 校验输入：目录，或可被 sqlite 打开且含所需表的单文件 db（cluster，不限制文件名）
    if not os.path.isdir(input_path) and not (
        os.path.isfile(input_path) and nodelevel_data_handler.is_cluster_db(input_path)
    ):
        logger.error(f"[SLOWNODE ALGO] Invalid input (need a directory, or a cluster db file): {input_path}")
        sys.exit(1)

    # 校验阈值范围
    for name, v in zip(("compute", "io", "comm"), thresholds):
        if v <= 0:
            logger.warning(f"[WARN] {name} threshold must be > 0. Please verify.")

    # === 单文件 cluster db：跳过 colocate / 子 job 扫描，直接处理 ===
    if os.path.isfile(input_path):
        config.set_output_path("")
        return _process_single_job(input_path, thresholds, clean_mode)

    # === verl colocate：同层多 worker*_ascend_pt 且含训练+rollout，先分组再分开检测 ===
    colocate_worlds = _find_colocate_worlds(input_path)
    if colocate_worlds:
        logger.info(f"[SLOWNODE ALGO] 检测到 verl colocate：训练/rollout 分开检测 "
                    f"{[w['role'] for w in colocate_worlds]}")
        return _process_colocate_root(input_path, colocate_worlds, thresholds, clean_mode, False)

    # === 判断是否为“父目录含多 job 子目录”场景 ===
    sub_jobs = _find_job_subdirs(input_path)
    parent_has_db = _dir_has_db_directly(input_path)

    if sub_jobs and not parent_has_db:
        logger.info(f"[SLOWNODE ALGO] 检测到 {len(sub_jobs)} 个子 job，逐个执行检测：{sub_jobs}")
        # 检测结果统一输出到父目录下的独立结果目录，原始数据目录（含 db）不写入任何结果
        output_root = os.path.join(input_path, "detection_output")
        results = {}
        for job in sub_jobs:
            logger.info(f"===== 处理 job：{job} =====")
            job_output = os.path.join(output_root, os.path.basename(job))
            results[job] = _process_single_job(job, thresholds, clean_mode, output_path=job_output)
        return results

    # === 默认：单个 job 目录直接处理（输出到输入目录自身，向后兼容） ===
    config.set_output_path("")
    return _process_single_job(input_path, thresholds, clean_mode)


def run_detection(input_path: str, compute: float = 1.3, io: float = 2.5, comm: float = 1.3,
                  skip_parsing: bool = False, clean: str = 'ask',
                  no_align: bool = None) -> Dict[str, Dict[str, float]]:
    """
    作为 skill 被调用的入口函数

    参数:
        input_path: 数据目录路径
        compute: 计算类阈值（KERNEL_AICORE / kernel_aivec，默认 1.3）
        io: IO/CPU 类阈值（cpu / memcpy_async，默认 2.5）
        comm: 通信类阈值（comm / pp_comm，默认 1.3）
        skip_parsing: 是否跳过清理和重新解析，直接使用已有的 op_metric 数据（默认 False）
                      当 skip_parsing=True 时相当于 clean='no'
        clean: 清理模式（'ask' / 'yes' / 'no'），仅在 skip_parsing=False 时生效
               'ask' - 交互式询问（默认，需终端 TTY）
               'yes' - 强制清理并重新解析
               'no'  - 跳过清理，使用已有数据
        no_align: 慢通信带宽是否关闭跨 rank 对齐（True=扁平池）；为 None 且数据级别为 levelnone
                  时交互式询问；level1/cluster 用新两级带宽口径，不询问

    返回:
        检测结果：{"KERNEL_AICORE": {"0": 1.5}, "comm": {"0,1": 1.8}, "cpu": {"5": 2.1}}
    """
    # === 数据级别识别（levelnone / level0 / level1 / cluster）===
    level = nodelevel_data_handler.detect_profiling_level(input_path)
    config.set_profiling_level(level)

    # 慢通信带宽口径：显式传入优先；no-align 仅对 levelnone 的旧带宽口径有意义，
    # level1/cluster 用新两级带宽口径，不询问（直接置 False）。
    if no_align is not None:
        config.set_comm_no_align(bool(no_align))
    elif level == "levelnone":
        config.set_comm_no_align(utils.confirm_no_align())
    else:
        config.set_comm_no_align(False)

    thresholds = (compute, io, comm)

    # === verl colocate：同层多 worker*_ascend_pt 且含训练+rollout，先分组再分开检测 ===
    colocate_worlds = _find_colocate_worlds(input_path)
    if colocate_worlds:
        logger.info(f"[SLOWNODE ALGO] 检测到 verl colocate：训练/rollout 分开检测 "
                    f"{[w['role'] for w in colocate_worlds]}")
        return _process_colocate_root(input_path, colocate_worlds, thresholds, clean, skip_parsing)

    # 设置全局配置（先设置，后续步骤可能用到）
    config.set_file_path(input_path)
    # 向用户提问阈值（传入值作为默认/回退）；按级别决定问哪几组
    thresholds = utils.confirm_thresholds(thresholds, keys=_threshold_ask_keys(level))
    config.set_thresholds(*thresholds)

    logger.info(f"数据级别：{level}")

    # === cluster_analysis.db 单文件：跳过解析，直接走新两级带宽口径 ===
    if level == "cluster":
        # 输入是文件，结果输出到其所在目录下的 detection_output（避免写到文件本身）
        out_dir = os.path.join(os.path.dirname(os.path.abspath(input_path)), "detection_output")
        os.makedirs(out_dir, exist_ok=True)
        config.set_output_path(out_dir)
        parallels, comm = nodelevel_data_handler.load_cluster_comm(input_path)
        if not parallels or not comm:
            logger.error("cluster 数据为空，无法检测")
            return {}
        valid_ranks = sorted({int(r) for gs in parallels.values() for g in gs for r in g})
        result = nodelevel.delimit_detection({}, parallels, valid_ranks, comm=comm)
        utils.write_result(result, parallels)
        # 生成文本报告（仅含 comm 两级口径段）
        try:
            visualizer.run_visualization(
                {}, parallels, valid_ranks, out_dir,
                detection_result=result, data_path=input_path)
        except Exception as e:
            logger.warning(f"生成报告失败：{e}")
        try:
            summary = summary_table.build_summary_table(result, parallels, {})
            _safe_print(summary)
        except Exception as e:
            logger.warning(f"生成汇总表失败：{e}")
        return result

    if skip_parsing:
        logger.info("跳过数据解析，直接使用已有的 op_metric 数据")
    else:
        if clean == 'yes':
            # 强制清理并重新解析
            utils.clean_detection_outputs(input_path)
            profilingdataparse.data_parsing(input_path)
            _backfill_slow_domain(input_path)
        elif clean == 'no':
            logger.info("跳过清理和重新解析（clean=no），直接使用已有的 op_metric 数据")
        else:
            # clean == 'ask': 交互式询问
            should_clean = utils.confirm_clean(input_path)
            if should_clean:
                utils.clean_detection_outputs(input_path)
                profilingdataparse.data_parsing(input_path)
                _backfill_slow_domain(input_path)
            else:
                logger.info("跳过清理和重新解析，直接使用已有的 op_metric 数据")

    logger.info(f"开始慢节点检测 - 路径：{input_path}, 阈值(计算/IO/通信)：{thresholds}")

    # 步骤 2: 获取并行域和有效 ranks
    parallels, valid_ranks = nodelevel_data_handler.get_cur_detection_info(input_path)

    if not parallels or not valid_ranks:
        logger.error("获取并行域/卡数失败")
        return {}

    # 步骤 3: 获取最新 step 数据
    last_step_data = nodelevel_data_handler.get_cur_job_last_step_data(valid_ranks)

    # level1：装载新两级带宽口径数据（analysis.db 的 CommAnalyzerBandwidth），
    # 传给定界检测替换原 comm 检测。
    comm = None
    if level == "level1":
        comm = nodelevel_data_handler.load_level1_comm(input_path, parallels)

    # 步骤 4: 执行定界检测
    result = nodelevel.delimit_detection(last_step_data, parallels, valid_ranks, comm=comm)

    # 步骤 5: 输出结果
    # 始终输出 JSON 结果文件（即使没有异常）
    utils.write_result(result, parallels)

    # 步骤 6: 可视化
    if last_step_data:
        visualizer.run_visualization(
            last_step_data, parallels, valid_ranks, input_path,
            detection_result=result,
            data_path=input_path,
        )

    if not result:
        logger.info("未检测到异常节点")

    # 步骤 7: 最终输出逐类别汇总表（渲染到调用方 agent 的 stdout）
    try:
        summary = summary_table.build_summary_table(
            result, parallels, last_step_data)
        _safe_print(summary)
    except Exception as e:  # 汇总表为附加展示，失败不应中断检测
        logger.warning(f"生成汇总表失败：{e}")

    return result


if __name__ == "__main__":
    main()