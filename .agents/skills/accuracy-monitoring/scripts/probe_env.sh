#!/bin/sh
# probe_env.sh —— 只读探测目标节点事实（幂等，可重复执行；不做任何变更）
#
# 用法:
#   sh probe_env.sh [--tp N] [--port P] [--save-path DIR] [--discovery]
#                   [--docker NAME] [--vllm-bin PATH] [--model PATH]
#     --tp N           期望的 tensor parallel 大小（用于资源校验判定，可选）
#     --port P         期望服务端口：实测占用状态（requested_port_free），供部署前发现冲突
#     --model PATH     模型路径：实测存在性（--docker 时为容器内路径并在容器内检查）
#     --save-path DIR  拟用的 pkl 落盘路径（文件模式 *.pkl 查父目录；--docker 时为
#                      容器内路径并在容器内检查）
#     --discovery      轻量服务发现：仅列运行中 vllm 服务（限 8xxx 端口），查完即退——
#                      跳过 vllm --help / 包导入 / 硬件 / 落盘路径等慢检查
#     --docker NAME    目标为容器：python/vllm/进程检查经 docker exec 在容器内执行；
#                      硬件/卡数/端口映射仍为宿主机视角
#     --vllm-bin PATH  容器内 vllm 不在 PATH（venv）时的绝对路径
#
# 退出码: 0=探测完成（facts 内容好坏由调用方判定）  1=目标错误（docker/容器不可用）  2=用法错误
# 输出:   过程证据行 + 末行纯 JSON {"facts":{...}} / {"discovery":{...}}（供 Agent 程序化解析）
#
# 事实项: python/anomaly_middleware 安装状态、vLLM 版本与 --middleware 支持（判据：
#         serve --help 输出 grep --middleware，未命中时补查 serve --help=all——部分构建
#         （如 vllm-ascend v0.18.0）默认 help 不展示该参数、仅全量 help 可见，保留证据）、
#         现有 vllm 进程与启动命令（是否已挂 middleware）、硬件类型（NPU/GPU，附属获取物）
#         与加速卡总数/空闲数（宿主机视角）、监听端口、SAVE_PATH 存在性/可写性
#         （--discovery 模式仅返回服务清单）

set -u

TP=""; PORT=""; SAVE_PATH=""; DISCOVERY="no"; DOCKER=""; VLLM_BIN="vllm"; MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tp)        [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; TP="${2}";        shift 2 ;;
    --port)      [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; PORT="${2}";      shift 2 ;;
    --save-path) [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; SAVE_PATH="${2}"; shift 2 ;;
    --model)     [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; MODEL="${2}";     shift 2 ;;
    --discovery) DISCOVERY="yes"; shift ;;
    --docker)    [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; DOCKER="${2}";    shift 2 ;;
    --vllm-bin)  [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; VLLM_BIN="${2}";  shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r\t' | cut -c1-800
}

# ---- 服务发现模式（秒级返回，查完即退）----
if [ "$DISCOVERY" = "yes" ]; then
  PROCS=$(ps -eo pid,args 2>/dev/null | grep 'vllm serve' | grep -v grep || true)
  SRV=""
  FIRST=1
  if [ -n "$PROCS" ]; then
    TMP=$(mktemp 2>/dev/null) || TMP="./.probe_disc_tmp"
    : > "$TMP"
    printf '%s\n' "$PROCS" | while IFS= read -r line; do
      pid=$(printf '%s' "$line" | awk '{print $1}')
      args=$(printf '%s' "$line" | cut -d' ' -f2-)
      port=$(printf '%s' "$args" | grep -oE '(--port[ =])[0-9]+' | head -n 1 | grep -oE '[0-9]+$')
      [ -n "$port" ] || port=8000
      case "$port" in 8*) ;; *) continue ;; esac        # 仅保留 8xxx 端口
      mw=$(printf '%s' "$args" | grep -q -- '--middleware' && echo yes || echo no)
      model=$(printf '%s' "$args" | sed -n 's/.*vllm serve[ ]\+\([^ -][^ ]*\).*/\1/p' | head -n 1)
      printf '%s|%s|%s|%s\n' "$pid" "$port" "$mw" "${model:-?}" >> "$TMP"
    done
    while IFS='|' read -r pid port mw model; do
      [ -n "$pid" ] || continue
      listen="no"
      if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${port}[[:space:]]"; then
        listen="yes"
      fi
      echo "[discovery] port=$port model=$model middleware=$mw listening=$listen pid=$pid"
      [ $FIRST -eq 1 ] || SRV="$SRV,"
      FIRST=0
      SRV="$SRV{\"pid\":\"$pid\",\"port\":\"$port\",\"model\":\"$(json_escape "$model")\",\"middleware\":\"$mw\",\"listening\":\"$listen\"}"
    done < "$TMP"
    rm -f "$TMP" 2>/dev/null
  fi
  # vllm 容器（进程对宿主机 ps 不可见时兜底）：列发布 8xxx 端口的容器
  CTL=""
  if command -v docker >/dev/null 2>&1; then
    CTMP=$(mktemp 2>/dev/null) || CTMP="./.probe_disc_ct"
    docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null \
      | grep -E '8[0-9]{3}' > "$CTMP" || true
    CFIRST=1
    while IFS='|' read -r cname ports; do
      [ -n "$cname" ] || continue
      plist=$(printf '%s' "$ports" | grep -oE ':8[0-9]{3}->' | tr -d '->' | tr '\n' ' ')
      echo "[discovery] container=$cname ports=$plist"
      [ $CFIRST -eq 1 ] || CTL="$CTL,"
      CFIRST=0
      CTL="$CTL{\"name\":\"$(json_escape "$cname")\",\"ports\":\"$(json_escape "$plist")\"}"
    done < "$CTMP"
    rm -f "$CTMP" 2>/dev/null
  fi
  printf '{"discovery":{"port_range":"8000-8999","services":[%s],"containers":[%s]}}\n' "$SRV" "$CTL"
  exit 0
fi

# ---- 目标上下文（--docker 时 python/vllm/进程检查在容器内执行）----
TARGET_DESC="host"
if [ -n "$DOCKER" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：--docker 需要本机 docker CLI" >&2
    printf '{"facts":{"error":"docker cli not found"}}\n'
    exit 1
  fi
  if ! docker inspect "$DOCKER" >/dev/null 2>&1; then
    echo "错误：容器不存在或未运行: $DOCKER" >&2
    printf '{"facts":{"error":"container not found"}}\n'
    exit 1
  fi
  TARGET_DESC="docker:$DOCKER"
  echo "[probe] target=$TARGET_DESC"
fi

# ---- 1) python 与 anomaly_middleware ----
# 解释器口径：优先用 vllm 可执行文件的 shebang 解释器测包 import——默认 python3 与 vllm
# 运行环境可能不同（editable 装进别的解释器、或包仅靠 cwd/PYTHONPATH 加载），只测 python3
# 会误报 missing（与"现役服务带 middleware 正常运行"的进程事实自相矛盾）。
PKG="unknown"; PKG_ERR=""; PKG_PY=""
resolve_vllm_py() {  # $1=执行器前缀（如 "docker exec <C> sh -c"）；→ 打印 vllm shebang 解释器（空=失败）
  _rpy_abs=$( $1 "command -v '$VLLM_BIN' 2>/dev/null" 2>/dev/null | tail -n 1 )
  [ -n "$_rpy_abs" ] || return 0
  _rpy_py=$( $1 "head -1 '$_rpy_abs' 2>/dev/null | sed 's/^#!//'" 2>/dev/null | tail -n 1 )
  [ -n "$_rpy_py" ] && printf '%s\n' "$_rpy_py"
}
if [ -n "$DOCKER" ]; then
  PY="python3(容器内)"
  PKG_PY=$(resolve_vllm_py "docker exec $DOCKER sh -c")
  if [ -n "$PKG_PY" ]; then
    PY="python3(容器内) pkg_python=$PKG_PY(vllm解释器)"
  else
    PKG_PY="python3"
  fi
  if docker exec "$DOCKER" sh -c "$PKG_PY -c 'import anomaly_middleware'" >/dev/null 2>&1; then
    PKG="installed"
  else
    PKG="missing"
    PKG_ERR=$(docker exec "$DOCKER" sh -c "$PKG_PY -c 'import anomaly_middleware'" 2>&1 | tail -n 1)
  fi
else
  PY=""; for p in python3 python; do
    if command -v "$p" >/dev/null 2>&1; then PY="$p"; break; fi
  done
  PKG_PY=$(resolve_vllm_py "sh -c")
  [ -n "$PKG_PY" ] || PKG_PY="$PY"
  if [ -n "$PKG_PY" ]; then
    if sh -c "$PKG_PY -c 'import anomaly_middleware'" >/dev/null 2>&1; then
      PKG="installed"
    else
      PKG="missing"
      PKG_ERR=$(sh -c "$PKG_PY -c 'import anomaly_middleware'" 2>&1 | tail -n 1)
    fi
  else
    PKG_ERR="python not found"
  fi
fi
echo "[probe] python=$PY anomaly_middleware=$PKG"

# ---- 2) vLLM CLI 与 --middleware 支持（判据：serve --help 输出 grep --middleware，
#         未命中时补查 serve --help=all——部分构建默认 help 不展示该参数；保留证据）----
VLLM_VER="unknown"; VLLM_SUPPORT="unknown"; VLLM_HELP_EVID=""; HELP_VARIANT=""
HELP_OUT=""
TWRAP=""
command -v timeout >/dev/null 2>&1 && TWRAP="timeout 180"
if [ -n "$DOCKER" ]; then
  # 部分构建（如 vllm-ascend）--version 会在末尾附 DeprecationWarning，过滤后再取末行
  RAW_VER=$(docker exec "$DOCKER" sh -c "$VLLM_BIN --version" 2>&1)
  VLLM_VER=$(printf '%s\n' "$RAW_VER" | grep -viE 'warn' | tail -n 1)
  [ -n "$VLLM_VER" ] || VLLM_VER=$(printf '%s\n' "$RAW_VER" | tail -n 1)
  HELP_OUT=$($TWRAP docker exec "$DOCKER" sh -c "$VLLM_BIN serve --help" 2>&1)
else
  if command -v "$VLLM_BIN" >/dev/null 2>&1; then
    RAW_VER=$("$VLLM_BIN" --version 2>/dev/null)
    VLLM_VER=$(printf '%s\n' "$RAW_VER" | grep -viE 'warn' | tail -n 1)
    [ -n "$VLLM_VER" ] || VLLM_VER=$(printf '%s\n' "$RAW_VER" | tail -n 1)
    HELP_OUT=$($TWRAP "$VLLM_BIN" serve --help 2>&1)
  fi
fi
if [ -n "$HELP_OUT" ]; then
  if printf '%s' "$HELP_OUT" | grep -q -- '--middleware'; then
    VLLM_SUPPORT="yes"; HELP_VARIANT="--help"
    VLLM_HELP_EVID=$(printf '%s' "$HELP_OUT" | grep -- '--middleware' | head -n 1)
  else
    # 回退：全量 help（部分构建仅在 --help=all 中展示 --middleware）
    HELP_ALL_OUT=""
    if [ -n "$DOCKER" ]; then
      HELP_ALL_OUT=$($TWRAP docker exec "$DOCKER" sh -c "$VLLM_BIN serve --help=all" 2>&1)
    elif command -v "$VLLM_BIN" >/dev/null 2>&1; then
      HELP_ALL_OUT=$($TWRAP "$VLLM_BIN" serve --help=all 2>&1)
    fi
    if printf '%s' "$HELP_ALL_OUT" | grep -q -- '--middleware'; then
      VLLM_SUPPORT="yes"; HELP_VARIANT="--help=all"
      VLLM_HELP_EVID=$(printf '%s' "$HELP_ALL_OUT" | grep -- '--middleware' | head -n 1)
    else
      VLLM_SUPPORT="no"
    fi
  fi
else
  VLLM_SUPPORT="vllm-not-found"
fi
echo "[probe] vllm=$VLLM_VER middleware_support=$VLLM_SUPPORT${HELP_VARIANT:+ evidence_variant=$HELP_VARIANT}"
[ -n "$VLLM_HELP_EVID" ] && echo "[probe] evidence: $(printf '%s' "$VLLM_HELP_EVID" | cut -c1-200)"
[ "$VLLM_SUPPORT" = "vllm-not-found" ] && echo "[probe] 注意：目标${DOCKER:+容器内}未找到 vllm CLI（venv 场景请提供 --vllm-bin 绝对路径）"

# ---- 3) 现有 vllm 进程与启动命令 ----
if [ -n "$DOCKER" ]; then
  VLLM_PROCS=$(docker exec "$DOCKER" sh -c "ps -eo pid,args 2>/dev/null | grep -i vllm | grep -v grep" 2>/dev/null || true)
else
  VLLM_PROCS=$(ps -eo pid,args 2>/dev/null | grep -i 'vllm' | grep -v grep || true)
fi
VLLM_PROCS_COUNT=$(printf '%s\n' "$VLLM_PROCS" | grep -c . 2>/dev/null || true)
VLLM_HAS_MW="no"; VLLM_CMD_SAMPLE=""
if [ "$VLLM_PROCS_COUNT" -gt 0 ] 2>/dev/null; then
  if printf '%s\n' "$VLLM_PROCS" | grep -q -- '--middleware'; then VLLM_HAS_MW="yes"; fi
  # 取第一条含 "serve" 的进程命令行作为样本（可能是脚本路径形态，仅作识别参考）
  VLLM_CMD_SAMPLE=$(printf '%s\n' "$VLLM_PROCS" | grep 'serve' | head -n 1 | cut -c1-800)
  [ -n "$VLLM_CMD_SAMPLE" ] || VLLM_CMD_SAMPLE=$(printf '%s\n' "$VLLM_PROCS" | head -n 1 | cut -c1-800)
fi
echo "[probe] vllm_procs=$VLLM_PROCS_COUNT has_middleware=$VLLM_HAS_MW"

# ---- 3b) 包级 vs 进程级分歧（重启决策关键：进程在跑 ≠ 新拉起能 import）----
PKG_PROC_MISMATCH="no"
if [ "$VLLM_HAS_MW" = "yes" ] && [ "$PKG" != "installed" ]; then
  PKG_PROC_MISMATCH="yes"
  echo "[probe] 注意：现役进程已挂 middleware，但包级 import 失败（解释器=${PKG_PY:-?}）——" >&2
  echo "        包大概率经 cwd/PYTHONPATH 加载；重启前必须补装（cd <仓库目录> && pip install -e .）" >&2
  echo "        或完全复现原加载方式，否则新拉起会因 import 失败起不来" >&2
fi

# ---- 4) 硬件类型与加速卡 ----
# GPU：nvidia-smi 标准查询。NPU：单次 npu-smi info 解析出——
#   ① 卡号清单（物理号，即 npu-smi 显示号）② 逐卡占用（进程表有进程=占用）③ 空闲数
#   ④ 逐卡 HBM 已用 ⑤ /dev/davinci* 挂载清单（--docker 时为容器内视角）
#   ⑥ torch_npu 逻辑卡数（device_count，即 ASCEND_RT_VISIBLE_DEVICES 的合法取值域）
# ⚠️ 卡号语义（真实事故沉淀）：npu-smi 显示的是【物理卡号】（如 2/5），而
#   ASCEND_RT_VISIBLE_DEVICES 要的是【逻辑卡号】（torch_npu 枚举序 0..N-1）。容器按物理号
#   挂载 davinci2/davinci5 时逻辑号是 0/1——把物理 5 填进该 env 会在 aclInit 报 107001
#   "Invalid device ID"，引擎启动即崩。部署指定卡号必须以 npu_logical_count 为合法值域。
HW="-"; GPU_TOTAL=""; GPU_FREE=""; NPU_TOTAL=""; NPU_FREE=""
NPU_IDS=""; NPU_BUSY=""; NPU_FREE_IDS=""; NPU_HBM=""; DAVINCI=""; NPU_LOGICAL=""
px_exec() {  # 容器感知执行（davinci 清单与 torch_npu 计数须取目标环境视角）
  if [ -n "$DOCKER" ]; then docker exec "$DOCKER" sh -c "$1" 2>/dev/null; else sh -c "$1" 2>/dev/null; fi
}
if command -v nvidia-smi >/dev/null 2>&1; then
  HW="GPU"
  GPU_TOTAL=$(nvidia-smi --list-gpus 2>/dev/null | wc -l | tr -d ' ')
  # 空闲判据：显存占用 < 1024 MiB（可按站点口径调整）
  GPU_FREE=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
             | awk '$1 < 1024 {c++} END {print c+0}')
elif command -v npu-smi >/dev/null 2>&1; then
  HW="NPU"
  NPU_SMIP=$(npu-smi info 2>/dev/null)
  # 物理卡号：摘要表行 "| <id> <型号>"（型号 token 必含至少一个字母，如 910B4/910B1/310P；
  # ⚠️ 不能要求 token 以字母开头——910B4 首字符是数字。芯片行 id 后紧跟 '|'、进程表行
  # id 后为纯数字芯片号，均不含字母，不匹配）。第二分支兼容无竖线的旧版输出。
  NPU_IDS=$(printf '%s\n' "$NPU_SMIP" \
            | sed -n -e 's/^| *\([0-9][0-9]*\)  *[0-9A-Za-z]*[A-Za-z][0-9A-Za-z]*.*/\1/p' \
                     -e 's/^[[:space:]]*\([0-9][0-9]*\)[[:space:]]\+[0-9A-Za-z]*[A-Za-z][0-9A-Za-z][[:space:]].*/\1/p' \
            | sort -un | tr '\n' ' ' | sed 's/ $//')
  # 占用卡：进程表区（"Process id" 表头之后）形如 "| <npu> <chip> | <pid> | <名> | <mem> |" 的行
  NPU_BUSY=$(printf '%s\n' "$NPU_SMIP" \
             | awk -F'|' '/Process id/{f=1;next} f{ if ($2 ~ /^[ ]*[0-9]/) {split($2,a," "); if (a[1] ~ /^[0-9]+$/) print a[1]} }' \
             | sort -un | tr '\n' ' ' | sed 's/ $//')
  # 逐卡 HBM 已用：摘要表 NPU 行(id) + 紧随的芯片行(末列 m/n 取末组)配对；
  # ⚠️ 保留原始空格做匹配（去空格会使相邻数字粘连，如 "0   0" + "/ 0" 粘成 00/0）
  NPU_HBM=$(printf '%s\n' "$NPU_SMIP" | awk -F'|' '
    $2 ~ /^[ ]*[0-9]+[ ]+[0-9A-Za-z]*[A-Za-z][0-9A-Za-z]/ { split($2,a," "); id=a[1]; next }
    $2 ~ /^[ ]*[0-9]+[ ]*$/ && $3 ~ /[0-9a-fA-F][.:]/ {
      s=$4; used=""
      while (match(s, /[0-9]+[ ]*\/[ ]*[0-9]+/)) { used=substr(s,RSTART,RLENGTH); s=substr(s,RSTART+RLENGTH) }
      if (used != "") { gsub(/[ ]+/,"",used); split(used,b,"/"); printf "%s:%s ", id, b[1] }
    }' | sed 's/ $//')
  # 空闲卡号 = 全量 − 占用
  NPU_FREE_IDS=""
  for _id in $NPU_IDS; do
    case " $NPU_BUSY " in *" $_id "*) ;; *) NPU_FREE_IDS="${NPU_FREE_IDS}${_id} " ;; esac
  done
  NPU_FREE_IDS=$(printf '%s' "$NPU_FREE_IDS" | sed 's/ $//')
  NPU_TOTAL=$(printf '%s' "$NPU_IDS" | wc -w | tr -d ' ')
  NPU_FREE=$(printf '%s' "$NPU_FREE_IDS" | wc -w | tr -d ' ')
  # davinci 挂载清单（目标环境视角）
  DAVINCI=$(px_exec "ls /dev 2>/dev/null | grep -oE 'davinci[0-9]+' | sort -u" | tr '\n' ' ' | sed 's/ $//')
  # torch_npu 逻辑卡数（ASCEND_RT_VISIBLE_DEVICES 的合法取值域 0..N-1；尽力而为，超时 60s）
  _TNPY=""
  [ -n "$PKG_PY" ] && _TNPY="$PKG_PY"
  [ -n "$_TNPY" ] || _TNPY=$(px_exec "command -v python3 || command -v python" | tail -n 1)
  if [ -n "$_TNPY" ]; then
    NPU_LOGICAL=$(px_exec "command -v timeout >/dev/null 2>&1 && timeout 60 $_TNPY -c 'import torch_npu;print(torch_npu.npu.device_count())' 2>/dev/null || $_TNPY -c 'import torch_npu;print(torch_npu.npu.device_count())' 2>/dev/null" \
                  | tail -n 1)
    case "$NPU_LOGICAL" in ''|*[!0-9]*) NPU_LOGICAL="" ;; esac
  fi
  echo "[probe] npu_ids(物理)=$NPU_IDS busy=$NPU_BUSY free=$NPU_FREE_IDS hbm_used=$NPU_HBM"
  echo "[probe] npu_davinci=$DAVINCI npu_logical_count=${NPU_LOGICAL:-unknown}"
  [ -z "$NPU_LOGICAL" ] && echo "[probe] 注意：未能获取 torch_npu 逻辑卡数（ASCEND_RT_VISIBLE_DEVICES 合法值域未知，部署时 deploy 将给出强告警）"
fi
echo "[probe] hardware=$HW gpu_total=${GPU_TOTAL:-} gpu_free=${GPU_FREE:-} npu_total=${NPU_TOTAL:-} npu_free=${NPU_FREE:-}"

# ---- 5) tp 资源校验判定 ----
TP_CHECK="not_requested"
if [ -n "$TP" ]; then
  FREE=""
  [ "$HW" = "GPU" ] && FREE="$GPU_FREE"
  [ "$HW" = "NPU" ] && FREE="$NPU_FREE"
  if [ -n "$FREE" ] && [ "$FREE" != "unknown" ] && [ "$FREE" != "" ]; then
    if [ "$FREE" -ge "$TP" ] 2>/dev/null; then TP_CHECK="enough"; else TP_CHECK="insufficient"; fi
  else
    TP_CHECK="unknown"
  fi
fi
echo "[probe] tp_required=${TP:-} tp_check=$TP_CHECK"

# ---- 6) 监听端口（尽力而为，需要 root 才能看到进程名）----
LISTEN=""
if command -v ss >/dev/null 2>&1; then
  LISTEN=$(ss -ltn 2>/dev/null | tail -n +2 | awk '{print $4}' | tr '\n' ' ' | cut -c1-500)
fi

# ---- 7) SAVE_PATH 检查（文件模式 *.pkl → 查父目录 + 目标文件本身；--docker → 容器内执行）----
# 只探测不动作：目标 pkl 已存在 = 用户决策点（沿用/改名/删除/换路径），本脚本绝不增删改任何 pkl
SP_OK=""; SP_MSG="not_requested"; SP_FILE_EXISTS=""; SP_FILE_INFO=""; SP_DIR_PKLS="none"
if [ -n "$SAVE_PATH" ]; then
  sp_exec() {
    if [ -n "$DOCKER" ]; then docker exec "$DOCKER" sh -c "$1" 2>/dev/null; else sh -c "$1" 2>/dev/null; fi
  }
  SP_DIR="$SAVE_PATH"; SP_PRE="dir"
  case "$SAVE_PATH" in
    *.pkl) SP_DIR=$(dirname "$SAVE_PATH"); SP_PRE="parent dir" ;;
  esac
  if sp_exec "[ -d '$SP_DIR' ]"; then
    if sp_exec "touch '$SP_DIR/.probe_write_test'"; then
      sp_exec "rm -f '$SP_DIR/.probe_write_test'"
      SP_OK="yes"; SP_MSG="$SP_PRE exists and writable"
    else
      SP_OK="no"; SP_MSG="$SP_PRE exists but not writable"
    fi
  else
    SP_OK="no"; SP_MSG="$SP_PRE not exists"
  fi
  # 目标 pkl 已存在检测（文件模式=目标本身；目录模式=目录内既有 *.pkl 清单）——仅报告，不动作
  case "$SAVE_PATH" in
    *.pkl)
      if sp_exec "[ -f '$SAVE_PATH' ]"; then
        SP_FILE_EXISTS="yes"
        SP_FILE_INFO=$(sp_exec "stat -c '%s bytes, mtime %y' '$SAVE_PATH'" || true)
        [ -z "$SP_FILE_INFO" ] && SP_FILE_INFO=$(sp_exec "ls -l '$SAVE_PATH' | awk '{print \$5\" bytes\"}'")
      else
        SP_FILE_EXISTS="no"
      fi
      ;;
    *)
      _pkl_n=$(sp_exec "ls -1 '$SP_DIR'/*.pkl 2>/dev/null | wc -l" || true)
      _pkl_n=$(printf '%s' "$_pkl_n" | tr -d ' ')
      if [ -n "$_pkl_n" ] && [ "$_pkl_n" -gt 0 ] 2>/dev/null; then
        SP_DIR_PKLS="count=$_pkl_n: $(sp_exec "cd '$SP_DIR' && ls -1 *.pkl 2>/dev/null | head -20" | tr '\n' ' ' | cut -c1-300)"
      else
        SP_DIR_PKLS="none"
      fi
      ;;
  esac
fi
echo "[probe] save_path=${SAVE_PATH:-} ok=$SP_OK msg=$SP_MSG file_exists=${SP_FILE_EXISTS:-n/a}${SP_FILE_INFO:+ ($SP_FILE_INFO)} dir_pkls=${SP_DIR_PKLS}"

# ---- 8) 模型路径存在性（--docker → 容器内检查）与请求端口占用实测 ----
MP_OK=""; MP_MSG="not_requested"
if [ -n "$MODEL" ]; then
  if [ -n "$DOCKER" ]; then
    if docker exec "$DOCKER" sh -c "[ -e '$MODEL' ]" 2>/dev/null; then
      MP_OK="yes"; MP_MSG="exists"
    else
      MP_OK="no"; MP_MSG="not exists（--docker 时为容器内路径，确认是否已挂载/拷入容器）"
    fi
  else
    if [ -e "$MODEL" ]; then MP_OK="yes"; MP_MSG="exists"; else MP_OK="no"; MP_MSG="not exists"; fi
  fi
fi
echo "[probe] model_path=${MODEL:-} ok=$MP_OK msg=$MP_MSG"

# 端口占用：容器模式在容器 netns 内查（host 网络容器与宿主机同 netns，结果一致）；
# ss/netstat 均缺失时回退 curl 探测（检测 HTTP 型监听）
PORT_FREE=""; PORT_LISTENER=""
if [ -n "$PORT" ]; then
  PORT_OUT=""
  if [ -n "$DOCKER" ]; then
    PORT_OUT=$(docker exec "$DOCKER" sh -c "ss -ltn 2>/dev/null | grep ':$PORT ' ; netstat -ltn 2>/dev/null | grep ':$PORT '" 2>/dev/null)
    if [ -z "$PORT_OUT" ] && ! docker exec "$DOCKER" sh -c "command -v ss >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1" >/dev/null 2>&1; then
      _prc=$(docker exec "$DOCKER" sh -c "curl -s -o /dev/null --max-time 1 'http://127.0.0.1:$PORT/' >/dev/null 2>&1; echo \$?" 2>/dev/null | tail -n 1)
      case "$_prc" in 0|52|56) PORT_OUT="curl-probe:listening" ;; esac
    fi
  else
    PORT_OUT=$(ss -ltn 2>/dev/null | grep ":$PORT " ; netstat -ltn 2>/dev/null | grep ":$PORT ")
    if [ -z "$PORT_OUT" ] && ! command -v ss >/dev/null 2>&1 && ! command -v netstat >/dev/null 2>&1; then
      _prc=$(curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; echo $?)
      case "$_prc" in 0|52|56) PORT_OUT="curl-probe:listening" ;; esac
    fi
  fi
  if [ -n "$PORT_OUT" ]; then
    PORT_FREE="no"
    PORT_LISTENER=$(printf '%s' "$PORT_OUT" | head -n 1 | cut -c1-200)
  else
    PORT_FREE="yes"
  fi
fi
echo "[probe] requested_port=${PORT:-} free=${PORT_FREE:-not_requested}${PORT_LISTENER:+ listener=$PORT_LISTENER}"

# ---- 末行 JSON ----
printf '{"facts":{"target":"%s","python":"%s","pkg_python":"%s","pkg":"%s","pkg_err":"%s","pkg_proc_mismatch":"%s","vllm_support_middleware":"%s","vllm_version":"%s","vllm_help_evid":"%s","hardware":"%s","gpu_total":"%s","gpu_free":"%s","npu_total":"%s","npu_free":"%s","npu_ids":"%s","npu_busy_ids":"%s","npu_free_ids":"%s","npu_hbm_used":"%s","npu_davinci":"%s","npu_logical_count":"%s","tp_required":"%s","tp_check":"%s","vllm_procs_count":"%s","vllm_has_middleware":"%s","vllm_cmd_sample":"%s","requested_port":"%s","requested_port_free":"%s","requested_port_listener":"%s","model_path":"%s","model_path_ok":"%s","model_path_msg":"%s","listen_addrs":"%s","save_path":"%s","save_path_ok":"%s","save_path_msg":"%s","save_path_file_exists":"%s","save_path_file_info":"%s","save_path_dir_pkls":"%s"}}\n' \
  "$(json_escape "$TARGET_DESC")" "$(json_escape "$PY")" "$(json_escape "$PKG_PY")" "$(json_escape "$PKG")" "$(json_escape "$PKG_ERR")" \
  "$(json_escape "$PKG_PROC_MISMATCH")" \
  "$(json_escape "$VLLM_SUPPORT")" "$(json_escape "$VLLM_VER")" "$(json_escape "$VLLM_HELP_EVID")" "$(json_escape "$HW")" \
  "$(json_escape "$GPU_TOTAL")" "$(json_escape "$GPU_FREE")" "$(json_escape "$NPU_TOTAL")" \
  "$(json_escape "$NPU_FREE")" "$(json_escape "$NPU_IDS")" "$(json_escape "$NPU_BUSY")" \
  "$(json_escape "$NPU_FREE_IDS")" "$(json_escape "$NPU_HBM")" "$(json_escape "$DAVINCI")" "$(json_escape "$NPU_LOGICAL")" \
  "$(json_escape "$TP")" "$(json_escape "$TP_CHECK")" \
  "$(json_escape "$VLLM_PROCS_COUNT")" "$(json_escape "$VLLM_HAS_MW")" \
  "$(json_escape "$VLLM_CMD_SAMPLE")" "$(json_escape "$PORT")" "$(json_escape "$PORT_FREE")" "$(json_escape "$PORT_LISTENER")" \
  "$(json_escape "$MODEL")" "$(json_escape "$MP_OK")" "$(json_escape "$MP_MSG")" \
  "$(json_escape "$LISTEN")" \
  "$(json_escape "$SAVE_PATH")" "$(json_escape "$SP_OK")" "$(json_escape "$SP_MSG")" \
  "$(json_escape "$SP_FILE_EXISTS")" "$(json_escape "$SP_FILE_INFO")" "$(json_escape "$SP_DIR_PKLS")"
exit 0
