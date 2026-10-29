#!/bin/sh
# stop_service.sh —— 精准停服：TERM → 宽限 → 仅同批残留 KILL（容器模式全程容器内执行）
#
# ⚠️ 属变更执行（会停止推理服务）：必须先征得用户同意再投递（铁律 6/7）
# ⚠️ 只杀"本服务"进程：目标批 = cmdline 命中 vllm serve / --middleware 的进程；
#    其 spawn 残留（EngineCore/worker）仅当 PPID 落在目标批内才列入第二批收割。
#    绝不扩大击杀范围——宿主机多实例并存时防误伤其他 vllm 服务（铁律 7）。
#    grep 模式用括号技巧（vllm[ ]serve）排除 sh -c 包装进程的自匹配。
#
# 用法:
#   sh stop_service.sh [--docker NAME] [--grace S] [--yes]
#     --docker NAME  容器内停服（目标批采集/信号均在容器内执行）
#     --grace S      TERM 后宽限秒数，默认 15（每秒轮询，全退即提前结束等待）
#     --yes          跳过交互确认（经 ansible 投递必须带；无 tty 时自动跳过）
#
# 退出码: 0=目标批全部退出  1=有残留（不自动扩大击杀，附 PID 交人工处理）
#         2=用法/目标错误（docker CLI/容器不可用）  3=目标作用域无 vllm 进程  4=确认被拒绝
# 输出:   过程行 + 末行纯 JSON {"rc":..,"batch1":[..],"killed":[..],"residual":[..],"card_clear":..}
# 拉起前注意：停服后须确认加速卡已释放（本脚本尽力复核，最终以 probe/npu-smi 实测为准，铁律 7）

set -u

DOCKER=""; GRACE=15; ASSUME_YES="no"
while [ $# -gt 0 ]; do
  case "$1" in
    --docker) [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; DOCKER="${2}"; shift 2 ;;
    --grace)  [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; GRACE="${2}";  shift 2 ;;
    --yes)    ASSUME_YES="yes"; shift ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

EXEC() {  # EXEC <cmd串>：在目标作用域（容器内/宿主机）执行
  if [ -n "$DOCKER" ]; then docker exec "$DOCKER" sh -c "$1"; else sh -c "$1"; fi
}

# ---- 守卫 ----
if [ -n "$DOCKER" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：--docker 需要本机 docker CLI" >&2
    printf '{"rc":2,"msg":"docker cli not found"}\n'; exit 2
  fi
  if ! docker inspect "$DOCKER" >/dev/null 2>&1; then
    echo "错误：容器不存在或未运行: $DOCKER" >&2
    printf '{"rc":2,"msg":"container not found"}\n'; exit 2
  fi
  echo "[stop] target=docker:$DOCKER"
else
  echo "[stop] target=host"
fi

# ---- 目标批采集（进程级特征；不含其未启动的子进程）----
B1=$(EXEC "ps -eo pid,args 2>/dev/null | grep -E 'vllm[ ]serve|[-]-middleware' | grep -v grep | grep -v stop_service | awk '{print \$1}' | tr '\n' ' ' " 2>/dev/null)
B1=$(printf '%s' "$B1" | tr -s ' ' '\n' | sed '/^$/d' | tr '\n' ' ')
if [ -z "$B1" ]; then
  echo "[stop] 目标作用域内无 vllm 服务进程"
  printf '{"rc":3,"msg":"no vllm process in target scope","docker":"%s"}\n' "$DOCKER"
  exit 3
fi
echo "[stop] 目标批:$B1"

# ---- 交互确认（无 tty 自动跳过，ansible 投递场景不受阻）----
if [ -t 0 ] && [ "$ASSUME_YES" != "yes" ]; then
  printf '确认停止上述进程？[y/N] '
  read -r _ans
  [ "$_ans" = "y" ] || { echo "已取消"; printf '{"rc":4,"msg":"declined by user"}\n'; exit 4; }
fi

# ---- TERM → 宽限（每秒轮询提前退出）----
EXEC "kill -TERM $B1" 2>/dev/null
_i=0
while [ $_i -lt "$GRACE" ]; do
  ALIVE=$(EXEC "for p in $B1; do kill -0 \$p 2>/dev/null && printf '%s ' \$p; done" 2>/dev/null)
  [ -z "$ALIVE" ] && break
  _i=$((_i + 1))
  sleep 1
done

# ---- 同批残留 KILL（只杀目标批内 PID，绝不扩批）----
KILLED=""
if [ -n "${ALIVE:-}" ]; then
  echo "[stop] 宽限 ${GRACE}s 后残留（TERM 未退，KILL 同批）:$ALIVE"
  EXEC "kill -KILL $ALIVE" 2>/dev/null
  KILLED="$ALIVE"
  sleep 3
fi

# ---- 第二批：目标批的 spawn 残留（PPID ∈ 目标批才收割；EngineCore/worker 等）----
B2=$(EXEC "ps -eo pid,ppid,args 2>/dev/null | grep -E 'spawn_main|EngineCore|vllm' | grep -v grep | grep -v stop_service | awk -v ps='$B1 $KILLED' 'BEGIN{n=split(ps,P,\" \"); for(i=1;i<=n;i++) PP[P[i]]=1} {if (!(\$1 in PP) && (\$2 in PP)) print \$1}' | tr '\n' ' '" 2>/dev/null)
B2=$(printf '%s' "$B2" | tr -s ' ' '\n' | sed '/^$/d' | tr '\n' ' ')
if [ -n "$B2" ]; then
  echo "[stop] 收割目标批子进程:$B2"
  EXEC "kill -TERM $B2" 2>/dev/null
  _j=0
  while [ $_j -lt 5 ]; do
    B2_ALIVE=$(EXEC "for p in $B2; do kill -0 \$p 2>/dev/null && printf '%s ' \$p; done" 2>/dev/null)
    [ -z "$B2_ALIVE" ] && break
    _j=$((_j + 1))
    sleep 1
  done
  if [ -n "${B2_ALIVE:-}" ]; then
    echo "[stop] 子进程 TERM 未退，KILL:$B2_ALIVE"
    EXEC "kill -KILL $B2_ALIVE" 2>/dev/null
    KILLED="$KILLED $B2_ALIVE"
    sleep 2
  fi
fi

# ---- 终态核查 ----
RESIDUAL=$(EXEC "for p in $B1 ${KILLED:-}; do [ -n \"\$p\" ] && kill -0 \$p 2>/dev/null && printf '%s ' \$p; done" 2>/dev/null)
RESIDUAL=$(printf '%s' "$RESIDUAL" | tr -s ' ' '\n' | sed '/^$/d' | tr '\n' ' ')

# ---- 加速卡释放复核（尽力而为，宿主机视角；最终以 probe 实测为准）----
CARD="unknown"
if command -v nvidia-smi >/dev/null 2>&1; then
  _np=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c . || true)
  [ "$_np" = "0" ] && CARD="yes" || CARD="no($_np procs)"
elif command -v npu-smi >/dev/null 2>&1; then
  npu-smi info 2>/dev/null | grep -q "No running processes" && CARD="yes" || CARD="check-npu-smi"
fi
echo "[stop] 卡释放复核: $CARD（拉起前仍须按铁律 7 以 probe 实测为准）"

if [ -n "$RESIDUAL" ]; then
  echo "[stop] 存在残留进程（不自动扩大击杀范围，交人工处理）:$RESIDUAL" >&2
  printf '{"rc":1,"msg":"residual processes remain","batch1":"%s","killed":"%s","residual":"%s","card_clear":"%s"}\n' \
    "$(printf '%s' "$B1" | tr -s ' ')" "$(printf '%s' "$KILLED" | tr -s ' ')" \
    "$(printf '%s' "$RESIDUAL" | tr -s ' ')" "$CARD"
  exit 1
fi

echo "[stop] 全部退出"
printf '{"rc":0,"msg":"all stopped","batch1":"%s","killed":"%s","residual":"","card_clear":"%s"}\n' \
  "$(printf '%s' "$B1" | tr -s ' ')" "$(printf '%s' "$KILLED" | tr -s ' ')" "$CARD"
exit 0
