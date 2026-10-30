#!/bin/sh
# register_instance.sh —— webui 实例登记工具（对 configs/webui.yaml 的 instances 段做
# add/remove/update/list；文本级操作，保留文件其余段与注释原样）
#
# 配置热重载：webui 对 instances 段支持热重载（见 webui-guide.md），本工具改完即生效，
# 无需重启 webui 进程。
#
# 用法:
#   sh register_instance.sh [--docker NAME] --config <webui.yaml路径> list
#   sh register_instance.sh [--docker NAME] --config <webui.yaml路径> add    --name N --url U [--paused]
#   sh register_instance.sh [--docker NAME] --config <webui.yaml路径> update --name N --url U [--paused]
#   sh register_instance.sh [--docker NAME] --config <webui.yaml路径> remove --name N
#     --docker NAME   webui.yaml 在容器内：--config 为【容器内路径】；工具先 docker cp 拷出、
#                     改完校验通过后 docker cp 回写（容器内先做 .bak.<ts> 备份）
#     add    幂等：同名实例已存在且 URL 相同 → 跳过（rc=0）；URL 不同 → 冲突（rc=5，改用 update）
#     update 仅改该实例的 url/paused；实例不存在 → rc=5
#     remove 实例不存在 → rc=5
#     写前自动备份 <config>.bak.<时间戳>；pyyaml 可用时做 safe_load 语义校验，
#     校验失败不写入（原文件保持原样）；不可用时退化为基础格式校验
#
# 退出码: 0=成功  2=用法错误  3=配置文件不存在  4=写入/校验失败  5=实例不存在或冲突
# 输出:   过程行 + 末行纯 JSON

set -u
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "错误：目标机无 python3/python（webui.yaml 文本级操作依赖）" >&2; exit 2; }

CFG=""; DOCKER=""; ACTION=""
# 只读遍历解析（不 shift，保留 "$@" 原样传给 python）
_prev=""
for a in "$@"; do
  [ "$_prev" = "--config" ] && CFG="$a"
  [ "$_prev" = "--docker" ] && DOCKER="$a"
  case "$a" in list|add|update|remove) ACTION="$a" ;; esac
  _prev="$a"
done
[ -n "$CFG" ] || { echo "错误：缺少 --config" >&2; exit 2; }
[ -n "$ACTION" ] || { echo "错误：缺少动作（list/add/update/remove）" >&2; exit 2; }

# ---- 内嵌 python 逻辑落临时文件（容器/非容器两分支共用）----
PYCODE="$(mktemp "${TMPDIR:-/tmp}/reginst.XXXXXX")"
trap 'rm -f "${PYCODE:-}" "${TMPF:-}" 2>/dev/null' EXIT
cat > "$PYCODE" <<'PYEOF'
import sys, os, re, json, shutil, time

def die(rc, msg):
    print(f"错误：{msg}", file=sys.stderr)
    print(json.dumps({"rc": rc, "msg": msg}))
    sys.exit(rc)

args = sys.argv[1:]
cfg = action = name = url = None
paused = None
i = 0
while i < len(args):
    a = args[i]
    if a == "--config":
        i + 1 < len(args) or die(2, "--config 缺参数值"); cfg = args[i+1]; i += 2
    elif a in ("add", "remove", "update", "list"):
        action = a; i += 1
    elif a == "--name":
        i + 1 < len(args) or die(2, "--name 缺参数值"); name = args[i+1]; i += 2
    elif a == "--url":
        i + 1 < len(args) or die(2, "--url 缺参数值"); url = args[i+1]; i += 2
    elif a == "--paused":
        paused = "yes"; i += 1
    elif a == "--docker":
        # sh 层已消费（决定拷出/回写通道），此处仅跳过其值
        i + 1 < len(args) or die(2, "--docker 缺参数值"); i += 2
    else:
        die(2, f"未知参数: {a}")
if not cfg or not action:
    die(2, "用法: register_instance.sh [--docker NAME] --config <webui.yaml> list|add|update|remove [--name N] [--url U] [--paused]")
if not os.path.isfile(cfg):
    die(3, f"配置文件不存在: {cfg}")

with open(cfg, encoding="utf-8") as f:
    lines = f.read().splitlines(keepends=True)

# ---- 解析 instances 段（仅文本定位：块起点 / 各 item 行区间）----
hdr = None
hdr_empty = False            # instances: [] 形态（空列表）
for idx, ln in enumerate(lines):
    m_hdr = re.match(r'^instances:\s*(\[\s*\])?\s*(#.*)?$', ln)
    if m_hdr:
        hdr = idx
        hdr_empty = bool(m_hdr.group(1))
        break
if hdr is None:
    die(4, "webui.yaml 中未找到 instances: 段（文件布局与预期不符，不擅改）")

items = []          # 每个 item: {"start":行号,"end":行号(不含),"name":..,"url":..,"paused":..}
cur = None
for idx in range(hdr + 1, len(lines)):
    ln = lines[idx]
    if re.match(r'^\S', ln):              # 下一个顶层键 → instances 段结束
        break
    m = re.match(r'^  - name:\s*(.+?)\s*(#.*)?$', ln)
    if m:
        if cur: items.append(cur)
        cur = {"start": idx, "end": None, "name": m.group(1).strip("'\"")}
    elif cur is not None:
        m = re.match(r'^    url:\s*(.+?)\s*(#.*)?$', ln)
        if m: cur["url"] = m.group(1).strip("'\"")
        m = re.match(r'^    paused:\s*(\S+)', ln)
        if m: cur["paused"] = m.group(1)
if cur: items.append(cur)
seg_end = len(lines)
for idx in range(hdr + 1, len(lines)):
    if re.match(r'^\S', lines[idx]):
        seg_end = idx; break
for k, it in enumerate(items):
    it["end"] = items[k+1]["start"] if k + 1 < len(items) else seg_end
    while it["end"] - 1 > it["start"] and lines[it["end"]-1].strip() == "":
        it["end"] -= 1

def find_item(n):
    for it in items:
        if it["name"] == n: return it
    return None

if action == "list":
    print(json.dumps({"rc": 0, "instances": [
        {"name": it["name"], "url": it.get("url"), "paused": it.get("paused")} for it in items]}))
    sys.exit(0)

if not name:
    die(2, f"{action} 需要 --name")

if action == "add":
    if not url: die(2, "add 需要 --url")
    if not re.match(r'^[A-Za-z0-9_.\-]+$', name): die(2, f"实例名含非法字符: {name}")
    if not re.match(r'^https?://\S+$', url): die(2, f"URL 格式非法: {url}")
    ex = find_item(name)
    if ex:
        if ex.get("url") == url:
            print(json.dumps({"rc": 0, "msg": "already-exists-same-url", "name": name, "url": url}))
            sys.exit(0)
        die(5, f"实例 {name} 已存在但 URL 不同（{ex.get('url')}），如需变更请用 update")
    item = f"  - name: {name}\n    url: {url}\n    paused: {'true' if paused else 'false'}\n"
    if hdr_empty:
        # instances: [] → 展开为块列表
        new_lines = lines[:hdr] + ["instances:\n", item] + lines[hdr+1:]
    else:
        insert_at = items[-1]["end"] if items else hdr + 1
        new_lines = lines[:insert_at] + [item] + lines[insert_at:]
elif action == "update":
    ex = find_item(name)
    if not ex: die(5, f"实例不存在: {name}")
    out = []
    for ln in lines[ex["start"]:ex["end"]]:
        if re.match(r'^    url:', ln) and url:
            ln = f"    url: {url}\n"
        if re.match(r'^    paused:', ln) and paused is not None:
            ln = "    paused: true\n"
        out.append(ln)
    new_lines = lines[:ex["start"]] + out + lines[ex["end"]:]
else:  # remove
    ex = find_item(name)
    if not ex: die(5, f"实例不存在: {name}")
    remaining = [it for it in items if it["name"] != name]
    if not remaining:
        # 移除最后一个实例：写成合法的 instances: []（空段会被 YAML 解析为 null，
        # webui 读配置时崩 / 校验器拒绝——两者都不可接受）
        hdr_ln = lines[hdr]
        if hdr_ln.rstrip("\n").strip() == "instances:":
            hdr_ln = "instances: []\n"
        new_lines = lines[:hdr] + [hdr_ln] + lines[seg_end:]
    else:
        new_lines = lines[:ex["start"]] + lines[ex["end"]:]

# ---- 校验（pyyaml 可用 → 语义校验；不可用 → 仅确认 instances 段仍在）----
new_text = "".join(new_lines)
try:
    import yaml
    data = yaml.safe_load(new_text)
    if not isinstance(data, dict) or not isinstance(data.get("instances"), list):
        die(4, "校验失败：修改后的 YAML instances 段结构异常（不写入，原文件保持原样）")
    names = [d.get("name") for d in data["instances"] if isinstance(d, dict)]
    if len(names) != len(set(names)):
        die(4, f"校验失败：实例名重复 {names}（不写入）")
except ImportError:
    if not re.search(r'^instances:', new_text, flags=re.M):
        die(4, "校验失败：instances 段丢失（不写入）")

bak = f"{cfg}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
shutil.copy2(cfg, bak)
with open(cfg, "w", encoding="utf-8") as f:
    f.write(new_text)
print(json.dumps({"rc": 0, "action": action, "name": name, "url": url,
                  "backup": bak, "note": "instances 段热重载，改完即生效"}))
PYEOF

# ---- 执行（容器模式：拷出 → 改副本 → 校验通过后回写）----
rc=0
if [ -n "$DOCKER" ]; then
  command -v docker >/dev/null 2>&1 || { echo "错误：--docker 需要本机 docker CLI" >&2; exit 2; }
  docker inspect "$DOCKER" >/dev/null 2>&1 || { echo "错误：容器不存在或未运行: $DOCKER" >&2; exit 2; }
  TMPF="$(mktemp "${TMPDIR:-/tmp}/reginst.XXXXXX")"
  docker cp "$DOCKER:$CFG" "$TMPF" >/dev/null 2>&1 \
    || { echo "错误：docker cp 拷出失败: $DOCKER:$CFG（检查 --config 是否为容器内路径）" >&2; exit 3; }
  "$PY" "$PYCODE" "$@" --config "$TMPF"    # 覆盖值在后：python 解析以最后一次 --config 为准
  rc=$?
  if [ "$rc" -eq 0 ] && [ "$ACTION" != "list" ]; then
    TS=$(date +%Y%m%d_%H%M%S)
    docker exec "$DOCKER" sh -c "cp '$CFG' '$CFG.bak.$TS'" 2>/dev/null \
      || echo "[register] 警告：容器内备份失败（继续回写）" >&2
    if docker cp "$TMPF" "$DOCKER:$CFG" >/dev/null 2>&1; then
      echo "[register] 已回写容器内 $CFG"
    else
      echo "错误：docker cp 回写失败（容器内备份 $CFG.bak.$TS；本地副本 $TMPF 可人工恢复）" >&2
      exit 4
    fi
  fi
else
  "$PY" "$PYCODE" "$@"
  rc=$?
fi
exit "$rc"
