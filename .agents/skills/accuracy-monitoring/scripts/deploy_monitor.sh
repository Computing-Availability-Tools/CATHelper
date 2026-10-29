#!/bin/sh
# deploy_monitor.sh —— 变更执行：预建落盘目录 + 写 env + 后台拉起 vllm serve --middleware
#
# ⚠️ 仅在用户确认部署/重启方案后投递执行（本脚本会拉起/重启服务）
# ⚠️ daemonize 后立即返回"已下发"——模型加载耗时 1~10 分钟，健康等待由 verify_anomaly.sh 轮询
#
# 用法:
#   sh deploy_monitor.sh --model <模型路径或名称> [--port P] [--tp N]
#                        [--save-path DIR|.pkl] [--monitor-rate R] [--top-logprobs N]
#                        [--workers N] [--tokenizer-model M] [--metrics-path P]
#                        [--launch-script S] [--log-dir DIR] [--docker NAME]
#                        [--config-file F] [--extra-args "VLLM_SERVE 参数串"] [--dry-run]
#                        [--env KEY=VALUE]... [--force]
#                        [其余未知参数原样透传给 vllm serve]
#     --model          模型路径/名称（与 --launch-script 二选一）
#     --port P         期望服务端口（缺省 8000）。拉起前实测占用：被占用则顺位 +1
#                      重试（至多 +20）直至空闲端口，成功拉起后回显实际端口
#                      （JSON 含 requested_port/port 两字段；launch-script 模式
#                      改写脚本内 --port 为顺位端口后以脚本副本拉起）
#     --dry-run        只做预检（import/幂等/端口/卡况）+ 组装命令并打印，不做任何变更、不拉起
#                      ——变更清单确认环节与测试解析用
#     --launch-script  现有启动脚本路径：先设置好 env 再以 sh 执行该脚本（env 对脚本内命令生效）。
#                      ⚠️ 脚本内必须自带 --middleware anomaly_middleware.AnomalyMiddleware
#                      （本模式只注入 env，不改写脚本 CLI 参数）——缺失时默认拒绝（rc=3），
#                      确认要裸拉起才加 --force
#     --env KEY=VALUE  附加环境变量（可重复），以 export 注入启动环境（如
#                      --env ASCEND_RT_VISIBLE_DEVICES=1 指定昇腾卡；--env CUDA_VISIBLE_DEVICES=0）
#     --force          逃生开关：① 跳过"已存在挂 --middleware 的 vllm"重复接入防护
#                      （同容器/同机拉起第二个监控实例时用，端口/卡分配由调用方自证）；
#                      ② launch-script 缺 --middleware 时仍继续（不推荐）
#     --config-file    容器模式：宿主机上的 configs/detector.yaml——安装后 docker cp 进容器
#                      工作目录（wheel/exec 安装不含 configs/，缺失必触发启动期 fail-fast）
#     vllm serve 参数透传（三选一，可混用，均拼在 --middleware 之后）：
#       ① 未知 --flag VALUE 直接写在命令行：自动带值透传（下一个参数不以 - 开头即视为
#          其值，如 --served-model-name Qwen3-0.6B --max-model-len 8192）；
#          布尔开关（后跟另一个 - 开头参数）只透传开关本身；
#       ② --extra-args "--flag value --flag2"：整串原样拼接（含负数值等歧义场景用这个）；
#       ③ 位置参数：非 - 开头的裸词原样透传（旧用法兼容）。
#       注意：部署自有选项拼写错误不会报"未知选项"，而是被透传给 vllm serve，
#       在启动期报 unrecognized arguments——由 verify_anomaly.sh rc=3 捕获。
#
# 退出码: 0=已下发  1=执行失败/端口耗尽  3=参数缺失/launch-script 缺 --middleware
#         4=重复接入拒绝（已存在挂 --middleware 的 vllm；--force 可越）
# 输出:   过程行 + 末行纯 JSON {"rc":..,"pid":..,"log":..,"port":..}

set -u

MODEL=""; PORT=""; TP=""; SAVE_PATH=""; RATE=""; TOPK=""; WORKERS=""; TK=""; MPATH=""
SCRIPT=""; LOG_DIR="/tmp"; EXTRA=""; DOCKER=""; CONFIG_FILE=""; DRYRUN="no"; FORCE="no"; ENVLIST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)        DRYRUN="yes"; shift ;;
    --force)          FORCE="yes"; shift ;;
    --env)            [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }
                      _ek="${2%%=*}"; _ev="${2#*=}"
                      case "$_ek" in
                        ""|*[!A-Za-z0-9_]*|[0-9]*)
                          echo "错误：--env 非法变量名: $_ek（须为 KEY=VALUE，KEY 为合法环境变量名）" >&2
                          echo '{"rc":3,"msg":"invalid env key"}'; exit 3 ;;
                      esac
                      ENVLIST="${ENVLIST} export ${_ek}='$_ev';"; shift 2 ;;
    --model)          [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; MODEL="${2}";          shift 2 ;;
    --port)           [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; PORT="${2}";           shift 2 ;;
    --tp)             [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; TP="${2}";             shift 2 ;;
    --save-path)      [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; SAVE_PATH="${2}";      shift 2 ;;
    --monitor-rate)   [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; RATE="${2}";           shift 2 ;;
    --top-logprobs)   [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; TOPK="${2}";           shift 2 ;;
    --workers)        [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; WORKERS="${2}";        shift 2 ;;
    --tokenizer-model) [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; TK="${2}";           shift 2 ;;
    --metrics-path)   [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; MPATH="${2}";          shift 2 ;;
    --launch-script)  [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; SCRIPT="${2}";         shift 2 ;;
    --log-dir)        [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; LOG_DIR="${2}";        shift 2 ;;
    --docker)         [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; DOCKER="${2}";         shift 2 ;;
    --config-file)    [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; CONFIG_FILE="${2}";    shift 2 ;;
    --extra-args)     [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 3; }; EXTRA="$EXTRA ${2}";   shift 2 ;;
    --*)  # 未知 --flag：透传给 vllm serve。下一个参数存在且不以 - 开头 → 视为该 flag 的值
      if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then
        EXTRA="$EXTRA $1 $2"; shift 2
      else
        EXTRA="$EXTRA $1"; shift
      fi ;;
    *) EXTRA="$EXTRA $1"; shift ;;
  esac
done

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r\t'
}

# ---- 拉起前置：anomaly_middleware 必须已装入目标环境（用户安装，Agent 不代装）----
# 缺失时拒绝拉起并给出安装命令；用户装好后重跑本脚本即可（会自动复检）。
PKG_GUIDE="在目标环境内安装 anomaly_middleware（任选其一，由用户执行）：
  ① 源码目录（推荐）：在目标环境（容器则先 docker exec -it <容器名> bash 进入）内执行：
     cd /path/to/accuracy-monitoring/ && pip install -e .
  ② wheel 离线装：管理节点 pip wheel . 打包 → 传入目标环境 → pip install <wheel 路径>
  ③ 容器长期方案：Dockerfile 加 RUN pip install /path/to/accuracy-monitoring 后重建镜像
装好后重跑本脚本，将自动复检。"
if [ -n "$DOCKER" ]; then
  if ! docker exec "$DOCKER" sh -c "python3 -c 'import anomaly_middleware'" >/dev/null 2>&1; then
    echo "错误：容器 $DOCKER 内未安装 anomaly_middleware（已实测 import 失败）" >&2
    echo "$PKG_GUIDE" >&2
    echo '{"rc":1,"msg":"anomaly_middleware missing in container"}'
    exit 1
  fi
else
  _PY=""; for p in python3 python; do
    command -v "$p" >/dev/null 2>&1 && { _PY="$p"; break; }
  done
  if [ -z "$_PY" ] || ! "$_PY" -c "import anomaly_middleware" >/dev/null 2>&1; then
    echo "错误：目标环境未安装 anomaly_middleware（已实测 import 失败）" >&2
    echo "$PKG_GUIDE" >&2
    echo '{"rc":1,"msg":"anomaly_middleware missing on host"}'
    exit 1
  fi
fi

# ---- 幂等防护：已存在挂 middleware 的 vllm 进程 → 拒绝重复接入（--force 可越：
#      同容器/同机多实例场景，第二实例由调用方自证端口与卡分配不冲突）----
# 容器模式：进程检查在容器内执行（宿主机 ps 看不到/不可靠）
if [ "$FORCE" = "yes" ]; then
  echo "[deploy] --force：跳过重复接入防护（多实例场景，请确认端口/卡不与现有实例冲突）"
elif [ -n "$DOCKER" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：--docker 需要本机 docker CLI" >&2
    echo '{"rc":1,"msg":"docker cli not found"}'
    exit 1
  fi
  if ! docker inspect "$DOCKER" >/dev/null 2>&1; then
    echo "错误：容器不存在或未运行: $DOCKER" >&2
    echo '{"rc":1,"msg":"container not found"}'
    exit 1
  fi
  if docker exec "$DOCKER" sh -c "ps -eo args 2>/dev/null | grep -i vllm | grep -v grep | grep -q -- '--middleware'" 2>/dev/null; then
    echo "拒绝：容器 $DOCKER 内已存在挂载 --middleware 的 vllm 进程（重复接入/双重挂载）" >&2
    echo '{"rc":4,"msg":"already integrated"}'
    exit 4
  fi
else
  if ps -eo args 2>/dev/null | grep -i 'vllm' | grep -v grep | grep -q -- '--middleware'; then
    echo "拒绝：目标机已存在挂载 --middleware 的 vllm 进程（重复接入/双重挂载）" >&2
    echo '{"rc":4,"msg":"already integrated"}'
    exit 4
  fi
fi

# ---- 检测器配置随装补齐（容器模式：wheel/exec 安装不含 configs/，缺失必触发 fail-fast）----
# （dry-run 跳过：docker cp 属变更）
if [ "$DRYRUN" != "yes" ] && [ -n "$DOCKER" ] && [ -n "$CONFIG_FILE" ]; then
  if [ ! -f "$CONFIG_FILE" ]; then
    echo "错误：--config-file 文件不存在: $CONFIG_FILE" >&2
    echo '{"rc":1,"msg":"config file not found"}'
    exit 1
  fi
  WORKDIR=$(docker exec "$DOCKER" sh -c "pwd" 2>/dev/null | tail -n 1)
  if [ -z "$WORKDIR" ]; then
    echo "错误：无法获取容器工作目录" >&2
    echo '{"rc":1,"msg":"cannot resolve container workdir"}'
    exit 1
  fi
  docker exec "$DOCKER" sh -c "mkdir -p '$WORKDIR/configs'" 2>/dev/null
  if ! docker cp "$CONFIG_FILE" "$DOCKER:$WORKDIR/configs/detector.yaml"; then
    echo "错误：detector.yaml 拷贝进容器失败" >&2
    echo '{"rc":1,"msg":"config copy failed"}'
    exit 1
  fi
  echo "[deploy] 已补齐容器内检测器配置: $WORKDIR/configs/detector.yaml（vllm serve 需在该工作目录启动）"
fi

# ---- 参数缺失检查 ----
if [ -z "$MODEL" ] && [ -z "$SCRIPT" ]; then
  echo "缺少 --model 或 --launch-script（二选一）" >&2
  echo '{"rc":3,"msg":"missing model or launch-script"}'
  exit 3
fi

# ---- 预建落盘目录（文件夹模式建目录；文件模式建父目录；dry-run 跳过建目录，仅报状态）----
# 容器模式：SAVE_PATH 视为容器内路径，在容器内预创建；宿主机 bind mount 由用户编排保证
ENV_CMDS=""
[ -n "$SAVE_PATH" ] && ENV_CMDS="${ENV_CMDS} export VLLM_ANOMALY_SAVE_PATH='$SAVE_PATH';"
if [ -n "$SAVE_PATH" ] && [ "$DRYRUN" != "yes" ]; then
  case "$SAVE_PATH" in
    *.pkl) TARGET_DIR=$(dirname "$SAVE_PATH") ;;
    *)     TARGET_DIR="$SAVE_PATH" ;;
  esac
  if [ -n "$DOCKER" ]; then
    if ! docker exec "$DOCKER" sh -c "mkdir -p '$TARGET_DIR' && touch '$TARGET_DIR/.deploy_write_test'" 2>/dev/null; then
      echo "容器内创建/写入落盘目录失败: $TARGET_DIR（检查容器内权限或 bind mount）" >&2
      echo '{"rc":1,"msg":"save-path not writable in container"}'
      exit 1
    fi
    docker exec "$DOCKER" sh -c "rm -f '$TARGET_DIR/.deploy_write_test'" 2>/dev/null
  else
    if ! mkdir -p "$TARGET_DIR" 2>/dev/null; then
      echo "创建落盘目录失败: $TARGET_DIR" >&2
      echo '{"rc":1,"msg":"mkdir save-path failed"}'
      exit 1
    fi
    if ! touch "$TARGET_DIR/.deploy_write_test" 2>/dev/null; then
      echo "落盘目录不可写: $TARGET_DIR" >&2
      echo '{"rc":1,"msg":"save-path not writable"}'
      exit 1
    fi
    rm -f "$TARGET_DIR/.deploy_write_test" 2>/dev/null
  fi
  echo "[deploy] 落盘目录就绪: $TARGET_DIR"
fi
[ -n "$RATE" ]   && ENV_CMDS="${ENV_CMDS} export VLLM_ANOMALY_MONITOR_RATE='$RATE';"
[ -n "$TOPK" ]   && ENV_CMDS="${ENV_CMDS} export VLLM_ANOMALY_TOP_LOGPROBS='$TOPK';"
[ -n "$WORKERS" ] && ENV_CMDS="${ENV_CMDS} export VLLM_ANOMALY_DETECTOR_WORKERS='$WORKERS';"
[ -n "$TK" ]     && ENV_CMDS="${ENV_CMDS} export VLLM_ANOMALY_TOKENIZER_MODEL='$TK';"
[ -n "$MPATH" ]  && ENV_CMDS="${ENV_CMDS} export VLLM_ANOMALY_METRICS_PATH='$MPATH';"
[ -n "$ENVLIST" ] && ENV_CMDS="${ENV_CMDS}${ENVLIST}"

# ---- ASCEND_RT_VISIBLE_DEVICES 逻辑卡号校验（dry-run 同样执行；纯检查，零变更）----
# ⚠️ 该 env 在 NPU 环境取【逻辑卡号】（torch_npu 枚举序，0..N-1）。npu-smi 显示的是
# 【物理卡号】——容器按物理号挂载 davinci2/davinci5 时，逻辑号是 0/1。真实事故：把物理 5
# 填入该 env → aclInit 报 107001 "Invalid device ID"，引擎核心启动即崩。
# 校验依据：torch_npu.npu.device_count()（主）→ /dev/davinci* 数量（兜底）→ 都取不到则强告警放行。
AVD_VAL=$(printf '%s' "$ENV_CMDS" | sed -n "s/.*export ASCEND_RT_VISIBLE_DEVICES='\([^']*\)'.*/\1/p" | head -n 1)
if [ -n "$AVD_VAL" ]; then
  avd_exec() {
    if [ -n "$DOCKER" ]; then docker exec "$DOCKER" sh -c "$1" 2>/dev/null; else sh -c "$1" 2>/dev/null; fi
  }
  avd_exec_docker_guard() {
    if [ -n "$DOCKER" ]; then
      command -v docker >/dev/null 2>&1 && docker inspect "$DOCKER" >/dev/null 2>&1
    else
      return 0
    fi
  }
  NLOG=""; DVC=""
  if avd_exec_docker_guard; then
    _avd_py=""
    for _p in python3 python; do
      avd_exec "command -v $_p" >/dev/null 2>&1 && { _avd_py="$_p"; break; }
    done
    if [ -n "$_avd_py" ]; then
      NLOG=$(avd_exec "command -v timeout >/dev/null 2>&1 && timeout 60 $_avd_py -c 'import torch_npu;print(torch_npu.npu.device_count())' 2>/dev/null || $_avd_py -c 'import torch_npu;print(torch_npu.npu.device_count())' 2>/dev/null" | tail -n 1)
      case "$NLOG" in ''|*[!0-9]*) NLOG="" ;; esac
    fi
    DVC=$(avd_exec "ls /dev 2>/dev/null | grep -cE '^davinci[0-9]+$'" | tail -n 1)
    case "$DVC" in ''|*[!0-9]*) DVC="" ;; esac
  fi
  UPPER=""; UPPER_SRC=""
  [ -n "$NLOG" ] && { UPPER="$NLOG"; UPPER_SRC="torch_npu.device_count"; }
  [ -z "$UPPER" ] && [ -n "$DVC" ] && { UPPER="$DVC"; UPPER_SRC="/dev/davinci* 数量"; }
  _bad=""
  for _id in $(printf '%s' "$AVD_VAL" | tr ', ' '  '); do
    case "$_id" in
      ''|*[!0-9]*) _bad="$_bad '$_id'(非数字)" ;;
      *) [ -n "$UPPER" ] && [ "$_id" -ge "$UPPER" ] 2>/dev/null && _bad="$_bad $_id(>=上限$UPPER)" ;;
    esac
  done
  if [ -n "$_bad" ]; then
    echo "拒绝：ASCEND_RT_VISIBLE_DEVICES='$AVD_VAL' 含非法逻辑卡号:$_bad" >&2
    echo "       该 env 要的是【逻辑卡号】(0..N-1)，不是 npu-smi 的物理卡号！" >&2
    echo "       依据：$UPPER_SRC=$UPPER。请先跑 probe_env.sh 取 npu_logical_count/npu_davinci，" >&2
    echo "       用空闲物理卡对应的逻辑号（物理→逻辑按挂载顺序映射，如 davinci2,5 → 逻辑 0,1）" >&2
    echo '{"rc":3,"msg":"invalid ascend logical device id"}'
    exit 3
  fi
  if [ -n "$UPPER" ]; then
    echo "[deploy] 卡号校验通过：ASCEND_RT_VISIBLE_DEVICES='$AVD_VAL'（逻辑号，依据 $UPPER_SRC=$UPPER）"
  else
    echo "[deploy] ⚠️ 强告警：无法获取逻辑卡数（torch_npu 计数与 davinci 清单均失败），" >&2
    echo "          无法校验 ASCEND_RT_VISIBLE_DEVICES='$AVD_VAL' 是否为逻辑号。" >&2
    echo "          若值来自 npu-smi 物理卡号，拉起后将报 aclInit 107001 'Invalid device ID'；" >&2
    echo "          建议先跑 probe_env.sh 确认 npu_logical_count 与空闲卡映射" >&2
  fi
fi

# ---- 端口占用实测 + 顺位探测（用户指定或默认端口被占用 → +1 重试至多 20 次）----
# 容器模式在容器 netns 内检查（host 网络容器与宿主机同 netns，结果一致）；
# ss/netstat 均缺失时回退 curl 探测（覆盖 HTTP 型监听，即 vllm/webui 场景）。
port_busy() {  # $1=port → rc0=占用 rc1=空闲
  _pb_out=""
  if [ -n "$DOCKER" ]; then
    _pb_out=$(docker exec "$DOCKER" sh -c "ss -ltn 2>/dev/null | grep ':$1 ' ; netstat -ltn 2>/dev/null | grep ':$1 '" 2>/dev/null)
    if [ -n "$_pb_out" ]; then return 0; fi
    if ! docker exec "$DOCKER" sh -c "command -v ss >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1" >/dev/null 2>&1; then
      _pb_rc=$(docker exec "$DOCKER" sh -c "curl -s -o /dev/null --max-time 1 'http://127.0.0.1:$1/' >/dev/null 2>&1; echo \$?" 2>/dev/null | tail -n 1)
      case "$_pb_rc" in 0|52|56) return 0 ;; esac
    fi
    return 1
  else
    _pb_out=$(ss -ltn 2>/dev/null | grep ":$1 " ; netstat -ltn 2>/dev/null | grep ":$1 ")
    [ -n "$_pb_out" ] && return 0
    if ! command -v ss >/dev/null 2>&1 && ! command -v netstat >/dev/null 2>&1; then
      _pb_rc=$(curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$1/" >/dev/null 2>&1; echo $?)
      case "$_pb_rc" in 0|52|56) return 0 ;; esac
    fi
    return 1
  fi
}
REQ_PORT="${PORT:-8000}"
CHOSEN_PORT=""
pick_port() {  # $1=起始端口 → 全局 CHOSEN_PORT；至多顺位 +20
  _pp="$1"; _n=0
  while [ "$_n" -le 20 ]; do
    if port_busy "$_pp"; then
      echo "[deploy] 端口 $_pp 被占用，顺位尝试 $((_pp + 1))"
      _pp=$((_pp + 1)); _n=$((_n + 1))
    else
      CHOSEN_PORT="$_pp"; return 0
    fi
  done
  return 1
}

# ---- 拉起前卡况复核（缩 TOCTOU 窗口：probe 时刻与拉起时刻之间卡可能被抢占）----
if [ -n "$TP" ] && [ "$TP" -ge 1 ] 2>/dev/null; then
  FREE=""
  if command -v nvidia-smi >/dev/null 2>&1; then
    FREE=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
           | awk '$1 < 1024 {c++} END {print c+0}')
  elif command -v npu-smi >/dev/null 2>&1; then
    FREE="unknown"   # NPU 空闲数解析版本差异大：兜底核查见 troubleshooting.md「NPU 卡况兜底」
  fi
  if [ -n "$FREE" ] && [ "$FREE" != "unknown" ]; then
    if [ "$FREE" -ge "$TP" ] 2>/dev/null; then
      echo "[deploy] 卡况复核通过：空闲 $FREE >= 需求 $TP"
    else
      echo "拒绝：拉起前卡况复核失败（空闲 $FREE < 需求 $TP，可能已被其他进程抢占）——不抢卡（铁律 7）" >&2
      echo '{"rc":1,"msg":"insufficient cards at launch"}'
      exit 1
    fi
  else
    echo "[deploy] 卡况复核不可用（NPU 兜底核查见 troubleshooting.md），跳过复核继续"
  fi
fi

# ---- 组装启动命令 ----
if [ -n "$SCRIPT" ]; then
  # launch-script 模式：① 拦截缺 --middleware 的脚本（env≠CLI 参数，缺了=静默裸拉起）；
  # ② 提取脚本内 --port 做占用实测，被占用则改写脚本副本顺位端口后拉起
  if [ -n "$DOCKER" ]; then
    SCRIPT_BODY=$(docker exec "$DOCKER" cat "$SCRIPT" 2>/dev/null)
  else
    SCRIPT_BODY=$(cat "$SCRIPT" 2>/dev/null)
  fi
  if ! printf '%s' "$SCRIPT_BODY" | grep -q -- '--middleware'; then
    if [ "$FORCE" != "yes" ]; then
      echo "拒绝：launch-script 内未检测到 --middleware anomaly_middleware.AnomalyMiddleware" >&2
      echo "       （本模式只注入 env，不改写脚本 CLI 参数——直接拉起将得到无监控的裸 vllm）" >&2
      echo "修复：在脚本内 vllm serve 命令后追加 --middleware anomaly_middleware.AnomalyMiddleware；" >&2
      echo "       确认要裸拉起才加 --force" >&2
      echo '{"rc":3,"msg":"launch-script missing --middleware"}'
      exit 3
    fi
    echo "[deploy] --force：launch-script 缺 --middleware 仍继续（将得到无监控的裸 vllm）"
  fi
  SCRIPT_PORT=$(printf '%s' "$SCRIPT_BODY" | grep -oE '(--port[ =])[0-9]+' | head -n 1 | grep -oE '[0-9]+$')
  if [ -n "$SCRIPT_PORT" ]; then
    REQ_PORT="$SCRIPT_PORT"
    if pick_port "$SCRIPT_PORT"; then
      if [ "$CHOSEN_PORT" != "$SCRIPT_PORT" ]; then
        echo "[deploy] 端口顺位调整: $SCRIPT_PORT → $CHOSEN_PORT（改写启动脚本副本后拉起）"
        if [ "$DRYRUN" != "yes" ]; then
          NEWSCRIPT="/tmp/deploy_port${CHOSEN_PORT}.sh"
          printf '%s' "$SCRIPT_BODY" | sed "s/--port[ =]*${SCRIPT_PORT}/--port ${CHOSEN_PORT}/" > "$NEWSCRIPT"
          chmod 755 "$NEWSCRIPT" 2>/dev/null
          if [ -n "$DOCKER" ]; then
            docker cp "$NEWSCRIPT" "$DOCKER:$NEWSCRIPT" >/dev/null 2>&1 \
              || { echo "错误：改写后的启动脚本拷入容器失败: $NEWSCRIPT" >&2; echo '{"rc":1,"msg":"script copy failed"}'; exit 1; }
          fi
          SCRIPT="$NEWSCRIPT"
        else
          echo "[deploy] DRY-RUN：将改写启动脚本副本为 /tmp/deploy_port${CHOSEN_PORT}.sh（--port $CHOSEN_PORT）"
        fi
      fi
      PORT="$CHOSEN_PORT"
    else
      echo "错误：端口 $SCRIPT_PORT 起顺位 +20 内均被占用，放弃拉起" >&2
      echo '{"rc":1,"msg":"no free port within +20"}'
      exit 1
    fi
  else
    echo "[deploy] launch-script 内未发现 --port，跳过端口占用检查（端口由脚本自行决定）"
  fi
  CMD="sh '$SCRIPT'"
else
  if pick_port "$REQ_PORT"; then
    if [ "$CHOSEN_PORT" != "$REQ_PORT" ]; then
      echo "[deploy] 端口顺位调整: $REQ_PORT → $CHOSEN_PORT"
    fi
    PORT="$CHOSEN_PORT"
  else
    echo "错误：端口 $REQ_PORT 起顺位 +20 内均被占用，放弃拉起" >&2
    echo '{"rc":1,"msg":"no free port within +20"}'
    exit 1
  fi
  CMD="vllm serve '$MODEL'"
  CMD="$CMD --port '$PORT'"
  [ -n "$TP" ]   && CMD="$CMD --tensor-parallel-size '$TP'"
  CMD="$CMD --middleware anomaly_middleware.AnomalyMiddleware"
  [ -n "$EXTRA" ] && CMD="$CMD$EXTRA"
fi

# ---- DRY-RUN：预检+组装输出，不做任何变更 ----
if [ "$DRYRUN" = "yes" ]; then
  echo "[deploy] DRY-RUN：预检通过，未做任何变更"
  echo "[deploy] env:${ENV_CMDS:- （无）}"
  echo "[deploy] 将执行命令: $CMD"
  echo "[deploy] 实际端口: $PORT（请求端口 $REQ_PORT${PORT:+}）"
  printf '{"rc":0,"dry_run":true,"cmd":"%s","env":"%s","requested_port":"%s","port":"%s"}\n' \
    "$(json_escape "$CMD")" "$(json_escape "$ENV_CMDS")" "$(json_escape "$REQ_PORT")" "$(json_escape "$PORT")"
  exit 0
fi

# ---- 后台拉起（宿主机：setsid 脱离会话；容器：docker exec -d；日志重定向；立即返回）----
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="."
LOG="$LOG_DIR/anomaly_deploy_$TS.log"
if [ -n "$DOCKER" ]; then
  # 容器生命周期即服务生命周期：不做 setsid；日志写容器内
  docker exec -d "$DOCKER" sh -c "$ENV_CMDS $CMD >$LOG 2>&1 </dev/null"
  PID="(容器内后台)"
  echo "[deploy] 已在容器 $DOCKER 内下发后台拉起（exec -d）"
else
  setsid sh -c "$ENV_CMDS $CMD" >"$LOG" 2>&1 </dev/null &
  PID=$!
  echo "[deploy] 已下发后台拉起 pid=$PID"
fi
echo "[deploy] 命令: $CMD"
echo "[deploy] 实际端口: $PORT${PORT:+"（请求端口 $REQ_PORT，占用时已顺位调整）"} —— 请以该端口访问服务与 /anomaly/metrics"
echo "[deploy] 日志: $LOG（模型加载约 1~10 分钟，请用 verify_anomaly.sh 轮询健康）"
printf '{"rc":0,"pid":"%s","log":"%s","cmd":"%s","target":"%s","requested_port":"%s","port":"%s"}\n' \
  "$(json_escape "$PID")" "$(json_escape "$LOG")" "$(json_escape "$CMD")" \
  "$(json_escape "${DOCKER:+docker:$DOCKER}")" "$(json_escape "$REQ_PORT")" "$(json_escape "$PORT")"
exit 0
