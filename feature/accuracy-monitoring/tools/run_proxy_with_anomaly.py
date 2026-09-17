#!/usr/bin/env python3
"""独立启动器：零修改运行 vllm-ascend 的 load_balance_proxy 脚本并附加精度异常检测。

vllm-ascend 各版本的 load_balance_proxy_*.py 差异较大，逐版本手工合入检测挂钩
成本高。本启动器不改 proxy 脚本任何一行：按路径导入 proxy 模块 → 用 ASGI
中间件包裹其 FastAPI app → uvicorn 启动（design_pd_proxy.md §5.11 集成模式 B）。

用法（proxy 原参数原样透传，--anomaly-* 由本启动器消费并转为环境变量）：

    python run_proxy_with_anomaly.py load_balance_proxy_server_example.py \
        --host 10.0.0.1 --port 9000 \
        --prefiller-hosts 10.0.0.2 --prefiller-ports 8100 \
        --decoder-hosts 10.0.0.3 10.0.0.4 --decoder-ports 8200 8201 \
        --anomaly-token2category /opt/anomaly/token2category/Qwen3-0.6B_151669.json \
        --anomaly-monitor-rate 0.3

不传 --anomaly-token2category（或 --anomaly-enabled false）→ 不启用检测，
proxy 行为与原版完全一致。

注意：
- 与"脚本内挂钩"方式（ProxyAnomalyController）二选一，不可叠加——若检测到
  proxy 脚本已含挂钩（_init_anomaly）会告警退出。
- 依赖：anomaly_middleware 包需在 proxy 机器上可导入（pip install -e 或
  PYTHONPATH）。

--anomaly-* 参数映射：
  --anomaly-token2category <file>  → VLLM_ANOMALY_TOKEN2CATEGORY（启用检测必填）
  --anomaly-token-text <file>      → VLLM_ANOMALY_TOKEN_TEXT（默认兄弟目录同名文件）
  --anomaly-monitor-rate <float>   → VLLM_ANOMALY_MONITOR_RATE
  --anomaly-workers <int>          → VLLM_ANOMALY_DETECTOR_WORKERS
  --anomaly-save-path <path>       → VLLM_ANOMALY_SAVE_PATH
  --anomaly-enabled true|false     → VLLM_ANOMALY_ENABLED（--no-anomaly-enabled 等价 false）
"""
from __future__ import annotations

import importlib.util
import os
import sys

# anomaly_middleware 本地包优先（避免 site-packages 旧副本缺 proxy_integration）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

ANOMALY_ENV_MAP = {
    "--anomaly-token2category": "VLLM_ANOMALY_TOKEN2CATEGORY",
    "--anomaly-token-text": "VLLM_ANOMALY_TOKEN_TEXT",
    "--anomaly-monitor-rate": "VLLM_ANOMALY_MONITOR_RATE",
    "--anomaly-workers": "VLLM_ANOMALY_DETECTOR_WORKERS",
    "--anomaly-save-path": "VLLM_ANOMALY_SAVE_PATH",
}


def split_argv(argv):
    """拆分 proxy 参数与 --anomaly-* 参数（支持 --flag value 与 --flag=value）。"""
    proxy_args = []
    for i in range(len(argv)):
        a = argv[i]
        if a is None:
            continue  # 已被前一参数消费的值
        if a == "--anomaly-enabled":
            val = argv[i + 1].strip().lower() if i + 1 < len(argv) else "true"
            os.environ["VLLM_ANOMALY_ENABLED"] = (
                "1" if val not in {"0", "false", "no", "off"} else "0"
            )
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                argv[i + 1] = None  # 消费其值
        elif a == "--no-anomaly-enabled":
            os.environ["VLLM_ANOMALY_ENABLED"] = "0"
        elif a.startswith("--anomaly-"):
            if "=" in a:
                flag, val = a.split("=", 1)
            else:
                flag = a
                if i + 1 >= len(argv) or argv[i + 1] is None:
                    raise SystemExit(f"缺少参数值: {a}")
                val = argv[i + 1]
                argv[i + 1] = None  # 消费其值
            env = ANOMALY_ENV_MAP.get(flag)
            if env is None:
                raise SystemExit(f"未知参数: {flag}")
            os.environ[env] = val
        else:
            proxy_args.append(a)
    return proxy_args


def load_proxy_module(script_path: str):
    """按路径导入 proxy 脚本为模块（不执行其 __main__ 块）。"""
    script_path = os.path.abspath(script_path)
    if not os.path.isfile(script_path):
        raise SystemExit(f"proxy 脚本不存在: {script_path}")
    # 脚本所在目录加入 sys.path（兼容脚本 import 同目录邻居的情况）
    script_dir = os.path.dirname(script_path)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    name = "lb_proxy_" + os.path.splitext(os.path.basename(script_path))[0]
    spec = importlib.util.spec_from_file_location(name, script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # 模块级 FastAPI app 在此构建
    return mod


def main():
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        raise SystemExit(__doc__)
    script_path, rest = argv[0], argv[1:]

    proxy_args = split_argv(rest)

    mod = load_proxy_module(script_path)
    if hasattr(mod, "_init_anomaly"):
        raise SystemExit(
            "检测到该 proxy 脚本已内置检测挂钩（_init_anomaly），与本启动器的"
            " ASGI 中间件方式不可叠加（会双重采样/注入）。请直接运行该脚本，"
            "或换用未挂钩的上游原版脚本。"
        )

    # 注入 proxy 的全局参数（等效原 __main__ 块，但不启动 uvicorn）
    saved_argv = sys.argv
    try:
        sys.argv = [script_path] + proxy_args
        mod.global_args = mod.parse_args()
    finally:
        sys.argv = saved_argv
    ga = mod.global_args

    # 包裹 ASGI 中间件（未配置词表文件时原样返回 app）
    from anomaly_middleware.proxy_integration import build_proxy_middleware

    app = build_proxy_middleware(mod.app)

    import uvicorn

    try:
        uvicorn.run(app, host=ga.host, port=ga.port)
    finally:
        mw = getattr(app, "shutdown", None)
        if callable(mw) and app is not mod.app:
            try:
                app.shutdown()
            except Exception as exc:  # noqa: BLE001
                print(f"anomaly shutdown 警告: {exc}")


if __name__ == "__main__":
    main()
