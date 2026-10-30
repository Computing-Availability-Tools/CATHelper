#!/bin/sh
# verify_anomaly.sh —— V1-V4 取证（支持 --wait 活性轮询：进度/停滞/致命错误三判定）
#
# 在目标节点本机执行（127.0.0.1 回环）；幂等，可重复投递。
#
# 用法:
#   sh verify_anomaly.sh [--host H] [--port P] [--metrics-path MP] [--model NAME]
#                        [--docker NAME] [--no-request] [--timeout S]
#                        [--wait SECONDS] [--interval SECONDS]
#                        [--log PATH] [--stall SECONDS]
#     --host/--port/--metrics-path  同旧版
#     --docker NAME  目标为容器：HTTP 探测与日志读取均经 docker exec 在容器内执行；
#                    此时 --host/--port 为容器内可达地址（host 网络容器用默认 127.0.0.1 即可），
#                    --log 为容器内日志路径（deploy_monitor.sh --docker 返回的 log）
#     --model        测试请求的 model 名（缺省自动从 /v1/models 取第一个）
#     --no-request   跳过 V3/V4（不发测试请求；仅 V1/V2 可判）
#     --timeout      单次 HTTP 超时秒数，默认 30（测试请求固定 10s）
#     --wait         单次调用内总等待秒数（默认 0=一次性检查，兼容旧用法）
#     --interval     等待期检查间隔，默认 15s
#     --log          vllm 启动日志路径（deploy_monitor.sh 返回的 log；--docker 时为容器内路径）。提供后启用：
#                    ① 致命错误早期检测（末 300 行滚动扫描：Traceback/引擎启动失败/端口占用/OOM/
#                       缺模块/昇腾 ERR 码/RuntimeError 等 → 立即失败返回）
#                    ② 进度证据（日志末行，随加载推进）
#                    ③ 停滞判定（日志 --stall 秒无增长 → 判定加载停滞；停滞前先做致命扫描，
#                       命中则 rc=3 优先于 rc=4）
#                    ④ 窗口耗尽兜底：rc=1 返回前对【全量日志】再做一次致命扫描——历史事故中
#                       Traceback 字样滚出末 50 行窗口、末行是崩溃后打印的 DeprecationWarning，
#                       曾被误判为"未就绪"白等满窗口。窗口耗尽 + 全量扫描仍无命中才真正 rc=1
#
# 退出码: 0=就绪且 V1-V4 全过      1=等待窗口内未就绪且全量日志无致命特征（Agent 自动续投下一轮；
#         若调用时未提供 --log，Agent 续投前必须自行 grep 一次日志 ERROR/Traceback）
#         3=检测到致命启动错误（终止，附日志原文；rc=3 只代表日志出现致命特征，Agent 汇报前
#           应把脚本附上的日志末 25 行过目一眼——极少数场景存在已捕获/可重试的 RuntimeError）
#         4=加载停滞（终止，附进度末行——Agent 报告用户确认）
#         2=用法错误
# 输出:   过程证据行 + 末行纯 JSON

set -u

HOST="127.0.0.1"; PORT="8000"; MPATH="/anomaly/metrics"; MODEL=""; NO_REQ="no"; TMO=30
WAIT=0; INTERVAL=15; LOGP=""; STALL=300; DOCKER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host)         [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; HOST="${2}";         shift 2 ;;
    --port)         [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; PORT="${2}";         shift 2 ;;
    --metrics-path) [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; MPATH="${2}";        shift 2 ;;
    --model)        [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; MODEL="${2}";        shift 2 ;;
    --docker)       [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; DOCKER="${2}";       shift 2 ;;
    --no-request)   NO_REQ="yes"; shift ;;
    --timeout)      [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; TMO="${2}";          shift 2 ;;
    --wait)         [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; WAIT="${2}";         shift 2 ;;
    --interval)     [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; INTERVAL="${2}";     shift 2 ;;
    --log)          [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; LOGP="${2}";         shift 2 ;;
    --stall)        [ $# -ge 2 ] || { echo "缺少参数值: $1" >&2; exit 2; }; STALL="${2}";        shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

BASE="http://$HOST:$PORT"
METRICS_URL="$BASE$MPATH"

# ---- --docker 守卫（容器不可用属目标错误 → rc=2，调用方不得按"未就绪"续投）----
if [ -n "$DOCKER" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：--docker 需要本机 docker CLI" >&2
    printf '{"rc":2,"msg":"docker cli not found"}\n'
    exit 2
  fi
  if ! docker inspect "$DOCKER" >/dev/null 2>&1; then
    echo "错误：容器不存在或未运行: $DOCKER" >&2
    printf '{"rc":2,"msg":"container not found"}\n'
    exit 2
  fi
fi

# ---- 执行通道（--docker 时经 docker exec 在容器内执行；注意 JSON body 内不能含单引号）----
hc() {  # hc <url> <max_time> → http code
  if [ -n "$DOCKER" ]; then
    docker exec "$DOCKER" sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time $2 '$1'" 2>/dev/null
  else
    curl -s -o /dev/null -w '%{http_code}' --max-time "$2" "$1" 2>/dev/null
  fi
}
hg() {  # hg <url> <max_time> → body
  if [ -n "$DOCKER" ]; then
    docker exec "$DOCKER" sh -c "curl -s --max-time $2 '$1'" 2>/dev/null
  else
    curl -s --max-time "$2" "$1" 2>/dev/null
  fi
}
hpost_hdr() {  # hpost_hdr <url> <max_time> <json_body> → 响应头
  if [ -n "$DOCKER" ]; then
    docker exec "$DOCKER" sh -c "curl -s -D - -o /dev/null --max-time $2 -X POST '$1' -H 'Content-Type: application/json' -d '$3'" 2>/dev/null
  else
    curl -s -D - -o /dev/null --max-time "$2" -X POST "$1" -H "Content-Type: application/json" -d "$3" 2>/dev/null
  fi
}
log_tail() {  # log_tail <file> <lines> → 日志末 N 行
  if [ -n "$DOCKER" ]; then
    docker exec "$DOCKER" sh -c "tail -n $2 '$1' 2>/dev/null"
  else
    tail -n "$2" "$1" 2>/dev/null
  fi
}
log_size() {  # log_size <file> → 字节数（不可读时 -1）
  if [ -n "$DOCKER" ]; then
    docker exec "$DOCKER" sh -c "stat -c %s '$1' 2>/dev/null" 2>/dev/null || echo -1
  else
    stat -c %s "$1" 2>/dev/null || echo -1
  fi
}
log_cat() {  # log_cat <file> → 全量日志（窗口耗尽/停滞前的兜底全量扫描用）
  if [ -n "$DOCKER" ]; then
    docker exec "$DOCKER" sh -c "cat '$1' 2>/dev/null"
  else
    cat "$1" 2>/dev/null
  fi
}

# 致命启动错误特征库（真实事故沉淀）：
#   - Traceback                          —— Python 崩溃栈（含未捕获异常）
#   - EngineCore failed to start         —— vllm v1 引擎核心启动失败（APIServer 侧栈前标志）
#   - Engine core initialization failed  —— APIServer 收敛后的最终错误行
#   - unrecognized arguments             —— deploy 透传参数拼写错误（verify rc=3 的典型来源）
#   - address already in use             —— 端口冲突
#   - out of memory / OOM                —— 显存/内存不足
#   - No module named / ImportError / ModuleNotFoundError —— 包缺失/加载方式不一致
#   - ERR[0-9]{5}                        —— 昇腾 CANN 错误码（如 ERR99999/ERR00100）
#   - Invalid device ID                  —— 昇腾卡号非法（物理号误当逻辑号的真实事故特征）
#   - RuntimeError                       —— 兜底捕获（极少数场景为已捕获/可重试输出，
#                                           Agent 汇报 rc=3 前过目日志末 25 行确认）
FATAL_PAT='Traceback|EngineCore failed to start|Engine core initialization failed|unrecognized arguments|address already in use|[Oo]ut of [Mm]emory|No module named|ImportError|ModuleNotFoundError|ERR[0-9]{5}|Invalid device ID|RuntimeError'

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r\t' | cut -c1-500
}

v1_probe() {
  hc "$METRICS_URL" 5
}

# ---- 完整 V1-V4（就绪后执行；末行 JSON + exit rc 0/1）----
full_check() {
  # V1
  V1=$(hc "$METRICS_URL" "$TMO")
  echo "[verify] V1 metrics http_code=$V1 ($METRICS_URL)"
  # V2
  BODY=$(hg "$METRICS_URL" "$TMO")
  V2_FOUND="false"; RATE=""; RATE_POS="false"; REQ_BEFORE=""
  if printf '%s' "$BODY" | grep -q '^vllm_anomaly_requests_total'; then
    V2_FOUND="true"
    REQ_BEFORE=$(printf '%s' "$BODY" | grep '^vllm_anomaly_requests_total' | tail -n 1 | awk '{print $2}')
  fi
  RATE=$(printf '%s' "$BODY" | grep '^vllm_anomaly_monitor_rate' | tail -n 1 | awk '{print $2}')
  if [ -n "$RATE" ]; then
    if [ "$(awk -v r="$RATE" 'BEGIN {print (r > 0) ? 1 : 0}')" = "1" ]; then RATE_POS="true"; fi
  fi
  echo "[verify] V2 requests_total=${REQ_BEFORE:-missing} monitor_rate=${RATE:-missing} rate_positive=$RATE_POS"
  # V3
  V3="skipped"; V3_REASON=""
  if [ "$NO_REQ" = "yes" ]; then
    V3_REASON="skipped by --no-request"
  else
    if [ -z "$MODEL" ]; then
      MODEL=$(hg "$BASE/v1/models" 10 \
              | grep -o '"id":"[^"]*"' | head -n 1 | cut -d'"' -f4)
    fi
    if [ -n "$MODEL" ]; then
      HDRS=$(hpost_hdr "$BASE/v1/chat/completions" 10 \
             "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1,\"stream\":false}")
      if printf '%s' "$HDRS" | grep -qi '^x-anomaly-request-id'; then
        V3="true"
      else
        V3="false"; V3_REASON="header missing（可能未命中采样或请求未进检测链路）"
      fi
    else
      V3="false"; V3_REASON="无法确定 model 名（/v1/models 不可用且未指定 --model）"
    fi
  fi
  echo "[verify] V3 header=$V3 ${V3_REASON:+reason=$V3_REASON}"
  # V4（短轮询：中间件计数在检测完成后异步落账——响应返回 ≠ 计数已 +1，最多等 5s 防
  # "rc 即裁决"把异步延迟误判为真失败）
  V4="false"; GROWTH=""
  REQ_AFTER=""
  if [ "$NO_REQ" = "yes" ]; then
    REQ_AFTER=$(hg "$METRICS_URL" "$TMO" \
                | grep '^vllm_anomaly_requests_total' | tail -n 1 | awk '{print $2}')
  else
    _n=0
    while [ $_n -lt 6 ]; do
      REQ_AFTER=$(hg "$METRICS_URL" "$TMO" \
                  | grep '^vllm_anomaly_requests_total' | tail -n 1 | awk '{print $2}')
      if [ -n "$REQ_BEFORE" ] && [ -n "$REQ_AFTER" ]; then
        GROWTH=$(awk -v a="$REQ_BEFORE" -v b="$REQ_AFTER" 'BEGIN {print b - a}')
        if [ "$(awk -v g="$GROWTH" 'BEGIN {print (g >= 1) ? 1 : 0}')" = "1" ]; then
          V4="true"; break
        fi
      fi
      _n=$((_n + 1))
      [ $_n -lt 6 ] && sleep 1
    done
  fi
  echo "[verify] V4 growth=${GROWTH:-unknown} pass=$V4"
  # 裁决
  RC=0
  [ "$V1" = "200" ]        || RC=1
  [ "$V2_FOUND" = "true" ] || RC=1
  [ "$RATE_POS" = "true" ] || RC=1
  [ "$NO_REQ" = "yes" ]    || { [ "$V3" = "true" ] || RC=1; }
  [ "$NO_REQ" = "yes" ]    || { [ "$V4" = "true" ] || RC=1; }
  printf '{"rc":%s,"ready":true,"v1_http_code":"%s","v2_counters_found":"%s","monitor_rate":"%s","requests_total_before":"%s","requests_total_after":"%s","v4_growth":"%s","v3_header":"%s","v3_reason":"%s"}\n' \
    "$RC" "$(json_escape "$V1")" "$(json_escape "$V2_FOUND")" "$(json_escape "$RATE")" \
    "$(json_escape "$REQ_BEFORE")" "$(json_escape "$REQ_AFTER")" "$(json_escape "$GROWTH")" \
    "$(json_escape "$V3")" "$(json_escape "$V3_REASON")"
  exit $RC
}

# ---- --wait > 0：活性轮询（就绪即返 / 致命即败 / 停滞即停 / 窗口耗尽续投）----
fatal_report() {  # $1=命中行 $2=来源说明 → 打印证据 + JSON，exit 3
  echo "[verify] 检测到致命启动错误（$2），终止等待；命中行："
  printf '%s\n' "$1"
  echo "[verify] ---- 日志末 25 行（$LOGP；Agent 汇报 rc=3 前过目确认非可重试输出）----"
  log_tail "$LOGP" 25
  echo "[verify] ---- 日志结束 ----"
  printf '{"rc":3,"msg":"fatal startup error","source":"%s","fatal":"%s","progress_line":"%s","log":"%s"}\n' \
    "$(json_escape "$2")" "$(json_escape "$1")" "$(json_escape "$PROG")" "$(json_escape "$LOGP")"
  exit 3
}
if [ "$WAIT" -gt 0 ] 2>/dev/null; then
  START=$(date +%s); LAST_SIZE=-1; LAST_CHANGE=$START; PROG=""
  echo "[verify] 活性轮询：wait=${WAIT}s interval=${INTERVAL}s log=${LOGP:-未提供} stall=${LOGP:+$STALL}"
  while :; do
    NOW=$(date +%s)
    [ $((NOW - START)) -ge "$WAIT" ] && break
    CODE=$(v1_probe)
    if [ "$CODE" = "200" ]; then
      echo "[verify] 服务已就绪，执行完整 V1-V4"
      full_check
    fi
    if [ -n "$LOGP" ]; then
      FATAL=$(log_tail "$LOGP" 300 | grep -aE "$FATAL_PAT" | tail -n 3)
      [ -n "$FATAL" ] && fatal_report "$FATAL" "末 300 行滚动扫描"
      SIZE=$(log_size "$LOGP")
      if [ "$SIZE" != "$LAST_SIZE" ]; then
        LAST_SIZE=$SIZE; LAST_CHANGE=$NOW
        PROG=$(log_tail "$LOGP" 1 | cut -c1-200)
        echo "[verify] 等待中 progress: $PROG"
      fi
      if [ $((NOW - LAST_CHANGE)) -ge "$STALL" ]; then
        # 停滞 ≠ 一定失败（个别阶段编译可达数分钟），但先做全量致命扫描再定性
        FF=$(log_cat "$LOGP" | grep -aE "$FATAL_PAT" | tail -n 3)
        if [ -n "$FF" ]; then
          fatal_report "$FF" "日志停滞 ${STALL}s 且全量扫描命中致命特征"
        fi
        echo "[verify] 加载停滞：日志 ${STALL}s 无增长（全量扫描无致命特征），终止等待" >&2
        printf '{"rc":4,"msg":"log stalled","stall_seconds":"%s","progress_line":"%s"}\n' \
          "$(json_escape "$STALL")" "$(json_escape "$PROG")"
        exit 4
      fi
    fi
    sleep "$INTERVAL"
  done
  # 窗口耗尽：续投 rc=1 前必须全量扫描一次——历史事故中 Traceback 滚出末行窗口、
  # 末行是崩溃后的 DeprecationWarning，曾被误判"未就绪"白等满窗口
  if [ -n "$LOGP" ]; then
    FF=$(log_cat "$LOGP" | grep -aE "$FATAL_PAT" | tail -n 3)
    if [ -n "$FF" ]; then
      echo "[verify] 窗口耗尽，全量日志扫描命中致命特征（滚动扫描未覆盖到）："
      fatal_report "$FF" "窗口耗尽全量扫描"
    fi
  fi
  echo "[verify] 等待窗口 ${WAIT}s 内未就绪（全量日志${LOGP:+已扫描、}无致命特征；Agent 应自动续投下一轮，无需询问用户；${LOGP:-未提供 --log 时 Agent 续投前必须自行 grep 日志 ERROR/Traceback}）"
  printf '{"rc":1,"msg":"not ready within wait window","waited_seconds":"%s","progress_line":"%s","full_log_scanned":"%s"}\n' \
    "$(json_escape "$WAIT")" "$(json_escape "$PROG")" "$([ -n "$LOGP" ] && echo true || echo false)"
  exit 1
fi

# ---- 一次性模式（--wait 0，兼容旧用法）：完整 V1-V4 ----
full_check
