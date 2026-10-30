#!/bin/sh
# webui_control.sh —— 控制节点 webui 生命周期管理（首选端口 + 顺位探测策略）
#
# 策略（SKILL.md W1 / webui-guide.md）：webui 只在控制节点拉起，首选端口 9092，
# bind 默认 0.0.0.0（用户确认）；9092 被无关进程占用时不擅杀，顺位 +1 重试
# （至多 +20），成功拉起后回显实际端口；控制节点无代码 → Agent 经 ansible 从被测
# 节点拷回最小集（webui/ + configs/）后再 start；实例端口对控制节点不可达（安全组）时
# 先经 tunnel 子命令建 ssh 隧道，再以隧道地址注册——webui 靠 HTTP 轮询实例，
# 注册一个不可达的 URL 只会得到永久离线卡片。
#
# 用法:
#   webui_control.sh status   [--code-path P] [--port N]
#   webui_control.sh start    --code-path P [--host H] [--port N] [--python PY]
#                             # N 为首选端口（默认 9092），被无关进程占用时顺位 +1
#   webui_control.sh register --config <webui.yaml> --name N --url U [--force]
#                             [--register-script PATH]
#   webui_control.sh tunnel   --ssh-host H --target-port T [--ssh-port P] [--ssh-user U]
#                             [--local-port L] [--local-bind B] [--pass-file F | --key-path K]
#                             # 本地端默认绑 0.0.0.0（用户决策，2026-10-28）
#   webui_control.sh stop     [--port N] [--code-path P]
#
# 子命令语义:
#   status    只读报告：端口占用者身份 / 代码完备性 / 依赖四件套 /（末行 JSON）
#   start     守卫（空闲或已是本 webui 则幂等成功）→ 代码校验 → 依赖校验（缺则 rc=6
#             给安装命令，由 Agent 尝试一次、失败交用户）→ daemonize 拉起 → 探活 200
#   register  先探 <url>/anomaly/metrics 可达性（不可达默认拒绝，--force 逃生）→
#             委托 register_instance.sh 写配置（热重载即生效）
#   tunnel    建-daemonize-探活 一体的本地端口转发（凭据只经 --pass-file 路径引用，
#             本体绝不入命令行/日志——铁律 10）；幂等：隧道已通则跳过
#   stop      按 pidfile/进程特征精准 TERM→宽限→同批 KILL
#
# 退出码: 0=成功/已就绪  1=启动失败或未就绪  2=用法错误  5=代码/配置不完备
#         6=依赖缺失（附安装命令）  7=端口被无关服务占用（不擅杀，交用户处置）
#         8=实例 URL 不可达（默认拒绝注册，--force 可越）
# 输出:   过程行 + 末行纯 JSON

set -u

DEFAULT_PORT=9092
DEFAULT_BIND="0.0.0.0"
DEPS="fastapi uvicorn httpx prometheus_client"

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r\t' | cut -c1-400; }

usage_exit() { echo "错误：$1" >&2; echo "用法见脚本头注释" >&2; printf '{"rc":2,"msg":"usage"}\n'; exit 2; }

# ---- 公共解析（全部子命令共用的可选参数）----
PORT="$DEFAULT_PORT"; BIND="$DEFAULT_BIND"; CODE=""; PYBIN=""
ACTION="${1:-}"; [ -n "$ACTION" ] || usage_exit "缺少子命令（status/start/register/tunnel/stop）"
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --port)            [ $# -ge 2 ] || usage_exit "$1 缺参数值"; PORT="$2"; shift 2 ;;
    --host)            [ $# -ge 2 ] || usage_exit "$1 缺参数值"; BIND="$2"; shift 2 ;;
    --code-path)       [ $# -ge 2 ] || usage_exit "$1 缺参数值"; CODE="$2"; shift 2 ;;
    --python)          [ $# -ge 2 ] || usage_exit "$1 缺参数值"; PYBIN="$2"; shift 2 ;;
    *) break ;;   # 子命令专属参数留给各自分支解析
  esac
done

port_pid() {  # 监听 <PORT> 的进程 pid（取不到则空；ss 缺失时回退 netstat——部分控制节点无 iproute2）
  P="${1:-$PORT}"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep ":${P}[[:space:]]" | head -n 1 | grep -oE 'pid=[0-9]+' | head -n 1 | cut -d= -f2
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltnp 2>/dev/null | grep ":${P}[[:space:]]" | head -n 1 | grep -oE '[0-9]+/' | head -n 1 | tr -d '/'
  fi
}

# webui 身份：cmdline 同时含 webui.main:app 与目标端口的进程
webui_identity() {
  P="${1:-$PORT}"
  ps -eo pid,args 2>/dev/null | grep 'webui\.main:app' | grep -v grep | grep -- "--port[ =]*${P}\$" \
    || ps -eo pid,args 2>/dev/null | grep 'webui\.main:app' | grep -v grep | grep -- "--port $P"
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null; }

deps_missing() {  # → 打印缺失模块名（空=齐全）；依赖调用方先在父 shell 解析好 PYBIN
  [ -n "$PYBIN" ] || { for p in python3 python; do command -v "$p" >/dev/null 2>&1 && { PYBIN="$p"; break; }; done; }
  [ -n "$PYBIN" ] || { echo "python3"; return; }
  for m in $DEPS; do
    "$PYBIN" -c "import $m" >/dev/null 2>&1 || echo "$m"
  done
}
resolve_py() {  # 在父 shell 解析 PYBIN（子 shell 内赋值不回传——$( ) 陷阱）
  if [ -z "$PYBIN" ]; then
    for p in python3 python; do
      command -v "$p" >/dev/null 2>&1 && { PYBIN="$p"; break; }
    done
  fi
}

# =============== status ===============
if [ "$ACTION" = "status" ]; then
  PPID_=$(port_pid)
  IDENT="none"; WPID=""; WCWD=""
  if [ -n "$PPID_" ]; then
    WLINE=$(webui_identity | head -n 1)
    if [ -n "$WLINE" ]; then
      WPID=$(printf '%s' "$WLINE" | awk '{print $1}')
      IDENT="our-webui"; WCWD=$(readlink "/proc/$WPID/cwd" 2>/dev/null || echo "?")
    else
      IDENT="foreign"
    fi
  fi
  CODE_STATE="unset"
  [ -n "$CODE" ] || CODE="$HOME/accuracy-monitor-control"
  if [ -d "$CODE" ]; then
    if [ -f "$CODE/webui/main.py" ] && [ -f "$CODE/configs/webui.yaml" ]; then
      CODE_STATE="ready"
    elif [ -d "$CODE" ]; then
      CODE_STATE="incomplete"
    fi
  fi
  resolve_py
  MISS=$(deps_missing)
  [ -n "$MISS" ] && DEP_STATE="missing:$(printf '%s' "$MISS" | tr '\n' ',')" || DEP_STATE="ok"
  echo "[webui] port=$PORT ident=$IDENT${WPID:+ pid=$WPID}${WCWD:+ cwd=$WCWD}"
  echo "[webui] code=$CODE state=$CODE_STATE"
  echo "[webui] deps=$DEP_STATE (python=${PYBIN:-auto})"
  printf '{"rc":0,"port":"%s","ident":"%s","pid":"%s","cwd":"%s","code_path":"%s","code_state":"%s","deps":"%s"}\n' \
    "$(json_escape "$PORT")" "$(json_escape "$IDENT")" "$(json_escape "$WPID")" \
    "$(json_escape "$WCWD")" "$(json_escape "$CODE")" "$(json_escape "$CODE_STATE")" "$(json_escape "$DEP_STATE")"
  exit 0
fi

# =============== start ===============
if [ "$ACTION" = "start" ]; then
  [ -n "$CODE" ] || CODE="$HOME/accuracy-monitor-control"
  REQ_PORT="$PORT"
  # 守卫 + 端口顺位探测：首选端口被无关进程占用 → 不擅杀，顺位 +1（至多 +20）；
  # 被本 webui 占用 → 幂等成功；耗尽 → rc=7 交用户处置
  _cand="$PORT"; _n=0
  while :; do
    PPID_=$(port_pid "$_cand")
    if [ -z "$PPID_" ]; then break; fi
    if [ -n "$(webui_identity "$_cand")" ]; then
      echo "[webui] 已有本 webui 在 ${_cand} 运行（幂等成功，无需重复拉起）"
      printf '{"rc":0,"msg":"already-running","port":"%s","requested_port":"%s"}\n' \
        "$(json_escape "$_cand")" "$(json_escape "$REQ_PORT")"
      exit 0
    fi
    echo "[webui] 端口 $_cand 被无关进程(pid=$PPID_)占用——不擅杀，顺位尝试 $((_cand + 1))"
    _cand=$((_cand + 1)); _n=$((_n + 1))
    if [ "$_n" -gt 20 ]; then
      echo "错误：端口 $REQ_PORT 起顺位 +20 内均被占用——交用户处置" >&2
      printf '{"rc":7,"msg":"no free port within +20","requested_port":"%s"}\n' "$(json_escape "$REQ_PORT")"
      exit 7
    fi
  done
  PORT="$_cand"
  if [ "$PORT" != "$REQ_PORT" ]; then
    echo "[webui] 端口顺位调整: $REQ_PORT → $PORT"
  fi
  # 代码校验
  [ -f "$CODE/webui/main.py" ] || { echo "错误：$CODE 下无 webui/main.py（控制节点无代码时，先由 Agent 经 ansible 从被测节点拷回最小集 webui/ + configs/）" >&2; printf '{"rc":5,"msg":"webui code missing"}\n'; exit 5; }
  [ -f "$CODE/configs/webui.yaml" ] || { echo "错误：$CODE 下无 configs/webui.yaml（webui 启动依赖；可从被测节点仓库 configs/ 一并拷回）" >&2; printf '{"rc":5,"msg":"webui.yaml missing"}\n'; exit 5; }
  # 依赖校验（缺则交 Agent 尝试一次安装；脚本不代装）
  resolve_py
  [ -n "$PYBIN" ] || { echo "错误：未找到 python3/python" >&2; printf '{"rc":6,"msg":"python not found"}\n'; exit 6; }
  MISS=$(deps_missing)
  if [ -n "$MISS" ]; then
    PIP=$(command -v pip3 || command -v pip || echo "pip")
    echo "错误：webui 依赖缺失: $(printf '%s' "$MISS" | tr '\n' ' ')" >&2
    echo "安装命令（Agent 尝试一次，失败交用户）：$PYBIN -m pip install $(printf '%s' "$MISS" | tr '\n' ' ')" >&2
    printf '{"rc":6,"msg":"deps missing","missing":"%s"}\n' "$(json_escape "$(printf '%s' "$MISS" | tr '\n' ' ')")"
    exit 6
  fi
  # daemonize 拉起（cwd=代码根：webui.main 相对导入 + configs/ 相对路径均依赖它）
  LOG="$CODE/webui_${PORT}.log"; PIDF="$CODE/webui_${PORT}.pid"
  mkdir -p "$CODE" 2>/dev/null
  ( cd "$CODE" && setsid "$PYBIN" -m uvicorn webui.main:app --host "$BIND" --port "$PORT" >>"$LOG" 2>&1 </dev/null & echo $! >"$PIDF" )
  echo "[webui] 已下发拉起 pid=$(cat "$PIDF" 2>/dev/null) bind=$BIND port=$PORT log=$LOG"
  # 探活
  _n=0
  while [ $_n -lt 30 ]; do
    C=$(http_code "http://127.0.0.1:${PORT}/")
    [ "$C" != "000" ] && break
    _n=$((_n + 1)); sleep 1
  done
  if [ "${C:-000}" != "000" ] && [ "${C:-000}" != "500" ]; then
    echo "[webui] 就绪 http_code=${C} 实际端口=${PORT}${PORT:+"（请求端口 $REQ_PORT，占用时已顺位调整）"}"
    printf '{"rc":0,"msg":"started","pid":"%s","port":"%s","requested_port":"%s","http_code":"%s","log":"%s"}\n' \
      "$(json_escape "$(cat "$PIDF" 2>/dev/null)")" "$(json_escape "$PORT")" "$(json_escape "$REQ_PORT")" "$(json_escape "$C")" "$(json_escape "$LOG")"
    exit 0
  fi
  echo "启动失败：30s 未就绪，日志末 5 行：" >&2
  tail -n 5 "$LOG" 2>/dev/null >&2
  printf '{"rc":1,"msg":"not ready","log":"%s"}\n' "$(json_escape "$LOG")"
  exit 1
fi

# =============== register ===============
if [ "$ACTION" = "register" ]; then
  REGCFG=""; REGNAME=""; REGURL=""; FORCE="no"; REGSCRIPT=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --config)          [ $# -ge 2 ] || usage_exit "$1 缺参数值"; REGCFG="$2"; shift 2 ;;
      --name)            [ $# -ge 2 ] || usage_exit "$1 缺参数值"; REGNAME="$2"; shift 2 ;;
      --url)             [ $# -ge 2 ] || usage_exit "$1 缺参数值"; REGURL="$2"; shift 2 ;;
      --force)           FORCE="yes"; shift ;;
      --register-script) [ $# -ge 2 ] || usage_exit "$1 缺参数值"; REGSCRIPT="$2"; shift 2 ;;
      *) usage_exit "register 未知参数: $1" ;;
    esac
  done
  [ -n "$REGCFG" ] && [ -n "$REGNAME" ] && [ -n "$REGURL" ] || usage_exit "register 需要 --config/--name/--url"
  C=$(http_code "${REGURL}/anomaly/metrics")
  if [ "$C" != "200" ]; then
    if [ "$FORCE" != "yes" ]; then
      echo "错误：实例不可达（${REGURL}/anomaly/metrics → ${C:-超时}）——注册只会得到离线卡片。" >&2
      echo "处理：先建隧道（tunnel 子命令）再以隧道地址注册；或请用户放行安全组后重试；坚持注册加 --force" >&2
      printf '{"rc":8,"msg":"instance unreachable","url":"%s","http_code":"%s"}\n' "$(json_escape "$REGURL")" "$(json_escape "$C")"
      exit 8
    fi
    echo "[webui] 警告：实例不可达仍强制注册（--force）"
  fi
  if [ -z "$REGSCRIPT" ]; then
    SD=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
    [ -f "$SD/register_instance.sh" ] && REGSCRIPT="$SD/register_instance.sh"
  fi
  [ -n "$REGSCRIPT" ] && [ -f "$REGSCRIPT" ] || { echo "错误：找不到 register_instance.sh（用 --register-script 指定，或与本脚本同目录部署）" >&2; printf '{"rc":2,"msg":"register_instance.sh not found"}\n'; exit 2; }
  echo "[webui] 登记实例 $REGNAME -> $REGURL"
  sh "$REGSCRIPT" --config "$REGCFG" add --name "$REGNAME" --url "$REGURL"
  exit $?
fi

# =============== tunnel ===============
if [ "$ACTION" = "tunnel" ]; then
  SHOST=""; SPORT=22; SUSER="root"; TPORT=""; LPORT=""; PASSF=""; KEYF=""; LBIND="0.0.0.0"
  while [ $# -gt 0 ]; do
    case "$1" in
      --ssh-host)    [ $# -ge 2 ] || usage_exit "$1 缺参数值"; SHOST="$2"; shift 2 ;;
      --ssh-port)    [ $# -ge 2 ] || usage_exit "$1 缺参数值"; SPORT="$2"; shift 2 ;;
      --ssh-user)    [ $# -ge 2 ] || usage_exit "$1 缺参数值"; SUSER="$2"; shift 2 ;;
      --target-port) [ $# -ge 2 ] || usage_exit "$1 缺参数值"; TPORT="$2"; shift 2 ;;
      --local-port)  [ $# -ge 2 ] || usage_exit "$1 缺参数值"; LPORT="$2"; shift 2 ;;
      --local-bind)  [ $# -ge 2 ] || usage_exit "$1 缺参数值"; LBIND="$2"; shift 2 ;;
      --stop)        STOPFLAG="yes"; shift ;;
      --pass-file)   [ $# -ge 2 ] || usage_exit "$1 缺参数值"; PASSF="$2"; shift 2 ;;
      --key-path)    [ $# -ge 2 ] || usage_exit "$1 缺参数值"; KEYF="$2"; shift 2 ;;
      *) usage_exit "tunnel 未知参数: $1" ;;
    esac
  done
  LOGF="/tmp/webui_tunnel_${LPORT}.log"; PIDF="/tmp/webui_tunnel_${LPORT}.pid"

  # --stop：精准拆除本条隧道（pidfile 真身 + 进程表按转发特征兜底，含 sshpass/ssh 一对），
  # 防包装层泄漏；不要求 --ssh-host
  if [ "${STOPFLAG:-}" = "yes" ]; then
    [ -n "$LPORT" ] || [ -n "$TPORT" ] || usage_exit "tunnel --stop 需要 --local-port 或 --target-port"
    [ -n "$LPORT" ] || LPORT="1${TPORT}"
    _killed=""
    [ -f "$PIDF" ] && { _p=$(cat "$PIDF" 2>/dev/null); [ -n "$_p" ] && kill "$_p" 2>/dev/null && _killed="$_p"; }
    # 转发特征匹配：-L 后是空格（"−L 18010:127.0.0.1:8010"）；首字符括号化防 grep 自匹配
    _pat="${LPORT}:127.0.0.1:${TPORT}"
    _scan_pat="[${_pat%"${_pat#?}"}]${_pat#?}"
    for _p in $(ps -eo pid,args 2>/dev/null | grep -- "$_scan_pat" | grep -v grep | awk '{print $1}'); do
      kill "$_p" 2>/dev/null && _killed="$_killed $_p"
    done
    [ -n "$_killed" ] && sleep 2
    rm -f "$PIDF"
    echo "[tunnel] 已拆除: ${_killed:-无活动进程}"
    printf '{"rc":0,"msg":"tunnel-stopped","killed":"%s"}\n' "$(json_escape "$_killed")"
    exit 0
  fi

  [ -n "$SHOST" ] && [ -n "$TPORT" ] || usage_exit "tunnel 需要 --ssh-host/--target-port"
  [ -n "$LPORT" ] || LPORT="1${TPORT}"           # 默认隧道本地端口 = 1<目标端口>（8xxx→18xxx，如 8010→18010）
  [ -n "$PASSF" ] || [ -n "$KEYF" ] || usage_exit "tunnel 需要 --pass-file（路径引用）或 --key-path"
  LOGF="/tmp/webui_tunnel_${LPORT}.log"; PIDF="/tmp/webui_tunnel_${LPORT}.pid"

  # 幂等：本地端口已通 → 视为隧道已建立
  C=$(http_code "http://127.0.0.1:${LPORT}/")
  if [ "$C" != "000" ]; then
    echo "[tunnel] 本地 ${LPORT} 已有通路（http_code=$C），视为隧道已建立（幂等跳过；如需重建请先 --stop）"
    printf '{"rc":0,"msg":"tunnel-alive","local_port":"%s"}\n' "$(json_escape "$LPORT")"
    exit 0
  fi
  # 凭据（本体不落命令行：密码经 -f 文件引用）
  if [ -n "$PASSF" ]; then
    [ -f "$PASSF" ] || { echo "错误：密码文件不存在: $PASSF（路径引用，本体不入对话）" >&2; printf '{"rc":2,"msg":"pass file missing"}\n'; exit 2; }
    command -v sshpass >/dev/null 2>&1 || { echo "错误：sshpass 未安装（Agent 可自装：apt/yum install sshpass 或 pipx 环境）" >&2; printf '{"rc":6,"msg":"sshpass missing"}\n'; exit 6; }
  fi
  # daemonize：不经 sh -c 包装——$! 即 sshpass/ssh 真身，stop 时 kill 一次到位
  # 本地端按 --local-bind 绑定（默认 0.0.0.0，用户决策）；ExitOnForwardFailure 防假活
  if [ -n "$PASSF" ]; then
    setsid sshpass -f "$PASSF" ssh -N -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -p "$SPORT" "$SUSER@$SHOST" -L "$LBIND:$LPORT:127.0.0.1:$TPORT" >>"$LOGF" 2>&1 </dev/null &
  else
    setsid ssh -i "$KEYF" -N -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -p "$SPORT" "$SUSER@$SHOST" -L "$LBIND:$LPORT:127.0.0.1:$TPORT" >>"$LOGF" 2>&1 </dev/null &
  fi
  echo $! >"$PIDF"
  echo "[tunnel] 已下发 ${LBIND}:${LPORT} → ${SHOST}:127.0.0.1:${TPORT} pid=$(cat "$PIDF") log=$LOGF"
  sleep 3
  # 端到端验证：优先打实例指标端点 /anomaly/metrics（200=强通过）；
  # 其他 HTTP 响应说明隧道已通但该路径非 200（可能实例 metrics 路径非默认，注册前 register 会再验）
  MC=$(http_code "http://127.0.0.1:${LPORT}/anomaly/metrics")
  if [ "$MC" = "200" ]; then
    echo "[tunnel] 端到端验证通过：/anomaly/metrics=200（本地 ${LPORT} → 目标 ${TPORT}）"
    printf '{"rc":0,"msg":"tunnel-up","local_port":"%s","target_port":"%s","metrics_code":"200"}\n' \
      "$(json_escape "$LPORT")" "$(json_escape "$TPORT")"
    exit 0
  fi
  C=$(http_code "http://127.0.0.1:${LPORT}/")
  if [ "$C" != "000" ]; then
    echo "[tunnel] 通路已建立（http_code=$C），但 /anomaly/metrics=${MC:-000}——非默认指标路径或实例未就绪时会出现，register 时会复验"
    printf '{"rc":0,"msg":"tunnel-up","local_port":"%s","target_port":"%s","metrics_code":"%s"}\n' \
      "$(json_escape "$LPORT")" "$(json_escape "$TPORT")" "$(json_escape "${MC:-000}")"
    exit 0
  fi
  echo "隧道建立失败，日志末 5 行：" >&2; tail -n 5 "$LOGF" 2>/dev/null >&2
  printf '{"rc":1,"msg":"tunnel failed","log":"%s"}\n' "$(json_escape "$LOGF")"
  exit 1
fi

# =============== stop ===============
if [ "$ACTION" = "stop" ]; then
  WLINE=$(webui_identity | head -n 1)
  [ -n "$WLINE" ] || { echo "[webui] 端口 ${PORT} 无本 webui 进程"; printf '{"rc":0,"msg":"not-running"}\n'; exit 0; }
  WPID=$(printf '%s' "$WLINE" | awk '{print $1}')
  echo "[webui] 停止 pid=$WPID"
  kill -TERM "$WPID" 2>/dev/null; sleep 5
  if kill -0 "$WPID" 2>/dev/null; then echo "[webui] TERM 未退，KILL"; kill -KILL "$WPID" 2>/dev/null; sleep 2; fi
  kill -0 "$WPID" 2>/dev/null && { echo "错误：pid=$WPID 残留，交人工" >&2; printf '{"rc":1,"msg":"residual","pid":"%s"}\n' "$(json_escape "$WPID")"; exit 1; }
  # 清理 pidfile
  for f in "${CODE:-$HOME/accuracy-monitor-control}/webui_${PORT}.pid" "$HOME/accuracy-monitor-control/webui_${PORT}.pid"; do
    [ -f "$f" ] && rm -f "$f"
  done
  printf '{"rc":0,"msg":"stopped","pid":"%s"}\n' "$(json_escape "$WPID")"
  exit 0
fi

usage_exit "未知子命令: $ACTION"
