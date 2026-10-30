# Web 看板使用指南（C4 / W3）

独立的 Web 服务，聚合多实例实时展示推理精度异常检测现象，并支持可配置阈值告警
（界面告警 + Webhook + 邮箱通知）。

## 安装与启动（控制节点，webui_control.sh 管理）

```bash
pip install fastapi uvicorn httpx prometheus_client   # 依赖四件套（缺时 Agent 尝试一次）
sh <SKILL_DIR>/scripts/webui_control.sh start --code-path <仓库根>   # 首选 9092，被无关进程占用自动顺位 +1，bind 0.0.0.0
```

启动后浏览器访问 `http://localhost:<实际端口>`（远程先
`ssh -L <实际端口>:localhost:<实际端口> <控制节点>`；start 成功后回显实际端口，JSON 含
requested_port/port 两字段）→ 登录 → 看板。完整决策树（端口占用验身份/代码拷回/依赖分级/
隧道注册）见下文「部署策略」节。

## 配置（configs/webui.yaml）

```yaml
auth:
  username: admin
  password: admin@123          # 或 SHA-256 hex 的 password_hash（推荐）
  token_ttl_hours: 24          # 登录有效时间 0-24 小时

poll:
  interval_seconds: 3          # 轮询间隔 2-5s
  http_timeout_seconds: 2

instances:                     # 被监控的推理服务（可 Web 界面运行中增删）
  - name: vllm-prod-1
    url: http://10.0.0.1:8000  # 计算节点服务地址（PD 模式填 proxy 地址）
    paused: false

store:
  event_capacity: 1000         # 事件环形容量
  trend_horizon_seconds: 2592000

webhooks:
  default: ""                  # 全局默认 Webhook URL（自动识别渠道）

email:
  enabled: false               # 邮件告警（可选），smtp_host/from_addr/to_addrs 必填

alerts:
  - name: garbled_frequent
    instance: "*"              # * = 全部实例
    model: "*"
    ill_type: garbled          # rare_character | garbled | repetition | nan_value；缺省=任一
    window_seconds: 300        # 滑动窗口（以当前异常事件为准向前数）
    threshold: 3               # 窗口内计数 ≥ 阈值触发
    webhook_url: ""            # 可选，覆盖全局
    enabled: true
```

注意：

- 看板趋势为**轮询期内的内存数据**——推理服务运行期间 webui 未启动的时段趋势**永久丢失、
  无法事后回补**；webui 进程重启同样清零（全局累计计数除外）。因此 webui 随接入**默认拉起**；
- 用户名/密码变更即时生效；实例增删/暂停/恢复经 Web 界面或编辑 yaml 即时生效（热重载）；
  告警规则变更需重启服务。

## 告警

- 规则驱动 + 滑动窗口去抖：`window_seconds` 内同一（实例、模型、异常类型）计数 ≥ `threshold`
  才触发；触发后窗口清零，同一时段不重复轰炸；
- 渠道自动识别（按 URL 前缀）：飞书（`https://open.feishu.cn/open-apis/bot/v2/hook/<token>`）、
  钉钉（`https://oapi.dingtalk.com/robot/send?access_token=<token>`）、
  企业微信（`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>`）、
  其它地址发通用 JSON；webhook 与邮件可并行。

## 部署策略（仅控制节点，首选端口 9092 + 顺位探测）

webui 只在控制节点拉起（`--host 0.0.0.0`，首选 `--port 9092`），**不在被测/
推理节点跑**（管理负载不挤占推理资源，且避免双看板数据割裂——趋势为轮询期内存数据，
多实例各自累积、无法合并）。生命周期一律经 `webui_control.sh`（status/start/register/
tunnel/stop），决策树：

```
start（守卫 + 端口顺位探测：请求端口被无关进程占用 → 顺位 +1 至多 +20，不擅杀；
      成功拉起后回显实际端口）
├─ 9092 已被本 webui 占用 → 幂等成功，直接 register 注册实例（复用）
├─ 9092 被无关服务占用 → 顺位尝试 9093/9094/…（至多 +20）；耗尽 → rc=7 交用户处置
└─ 空闲 → start --code-path <仓库根>
     ├─ 代码取优先级：档案/用户给定路径 → 控制节点无代码则从被测节点拷回最小集（见下）
     ├─ 代码校验（webui/main.py + configs/webui.yaml）不过 → rc=5
     └─ 依赖校验（fastapi/uvicorn/httpx/prometheus_client）缺 → rc=6 附安装命令
        → Agent pip 安装一次（失败即停交用户，不换源不重试——铁律 9）
```

**实例可达性（关键）**：webui 靠 HTTP 轮询实例；推理服务端口常被安全组挡住（控制节点
探不通）——**先建隧道再注册**，注册不可达 URL 只会得到永久离线卡片（register 默认拒绝，
`--force` 逃生）：

```bash
sh <SKILL_DIR>/scripts/webui_control.sh tunnel --ssh-host <被测节点IP> --ssh-port 2123 \
   --target-port 8010 --pass-file ~/.ansible_cpass       # 本地端口默认 1<目标端口>（8010→18010）；
                                                          # 本地端默认绑 0.0.0.0（用户决策），--local-bind 可改
sh <SKILL_DIR>/scripts/webui_control.sh register --config <webui.yaml> \
   --name <实例名> --url http://127.0.0.1:18010          # 轮询 URL 是拨号目标，用 127.0.0.1（同机隧道入口）
# 拆除/重建：tunnel ... --stop（精准杀 sshpass/ssh 一对，防包装层泄漏）后重跑 tunnel
```

**控制节点无代码 → 从被测节点拷回最小集**（一次性，秒级；只拷 webui/ + configs/ + tools/ +
anomaly_middleware/，绝不整仓——仓库可能含模型/构建产物；**含 tools/ + anomaly_middleware/
是 W4 报告的来源前提**：控制节点副本完整性以被测节点检出为源，被测节点当年就没有检出则
双侧皆缺，W4 只能请用户检出仓库）：

```bash
# ① 被测节点（经 ansible；容器部署先在容器内打包再拷出宿主机）：
docker exec <容器名> sh -c 'tar czf /tmp/webui_min.tgz -C <仓库根> webui configs tools anomaly_middleware --exclude=__pycache__'
docker cp <容器名>:/tmp/webui_min.tgz /tmp/webui_min.tgz
# ② fetch 回控制节点并解压到 $HOME/accuracy-monitor-control（webui_control.sh 默认根）：
ansible <目标主机> -m fetch -a "src=/tmp/webui_min.tgz dest=/tmp/opencode/webui_min.tgz flat=yes"
mkdir -p ~/accuracy-monitor-control && tar xzf /tmp/opencode/webui_min.tgz -C ~/accuracy-monitor-control
```

**访问**：用户与控制节点同机 → `http://localhost:9092`；远程 →
`ssh -L 9092:localhost:9092 <控制节点>` 后开 `http://localhost:9092`。
首次拉起若 configs/webui.yaml 仍为默认口令 `admin@123`，**必须提醒用户改密**（弱口令 +
0.0.0.0 = 任何可达者可试登录）。被测节点若有遗留 webui（历史部署），在 9092 就绪后
**建议用户关闭**（需确认），趋势数据无法迁移、自 9092 启动起累积。

## 实例登记（register_instance.sh）

对 configs/webui.yaml 的 instances 段做 add/update/remove/list（文本级操作保留注释，
写前备份 `.bak.<ts>`，pyyaml 可用时语义校验、校验失败不写入）：

```bash
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/register_instance.sh \
  --config <webui.yaml路径> add --name <实例名> --url http://127.0.0.1:<端口>" -b
# 容器内 webui.yaml：加 --docker <容器名>（--config 填容器内路径）

ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/register_instance.sh \
  --config <webui.yaml路径> remove --name <实例名>" -b                  # 注销登记（停服默认动作）
```

**停服/下线默认注销登记（双轨约定）**：实例停止 = ① remove 本登记 + 拆对应隧道
（`tunnel ... --stop`）——webui.yaml 只留活实例，防灰色离线卡片累积；② instances.yaml
条目**保留** + 结构化 stopped 标记（身份参数是重启复用的第一数据源，见 report-tool.md
档案守则）。重新拉起已停实例：verify 通过后重新 add 登记（隧道按需重建）。

## 看板内容

累计统计（总请求/总异常/累计异常率/检测错误）、异常检出趋势（1h/4h/8h/16h/24h 时间窗）、
异常类型与按模型累计分布、实例状态卡（在线/离线/暂停，支持运行中增删/暂停/恢复）、
界面右上角告警提醒。

## 后端 API（密度/趋势问题由 Agent 程序化读取用）

| 端点 | 说明 |
|---|---|
| `POST /api/login` | 登录取 token（body: username/password） |
| `GET /api/summary` | 全局累计统计 |
| `GET /api/instances` | 实例清单与状态 |
| `GET /api/trends` | 全局趋势（时间窗分桶） |
| `GET /api/events` | 异常事件流 |
| `GET /api/alerts` | 告警记录 |
| `GET /api/instances/{name}/summary|trends|events` | 单实例维度 |
| `POST /api/instances` / `POST /api/instances/{name}/pause|resume` | 运行中注册/暂停/恢复实例 |
