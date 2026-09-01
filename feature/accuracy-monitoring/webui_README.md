# 推理精度异常监控 Web 界面（webui）

> 独立 Web 服务，多 vLLM 实例聚合可视化推理精度异常检测现象，并支持可配置阈值告警
> （界面告警 + Webhook(钉钉、飞书、企业微信) + 邮箱通知）。

## 1. 概述

`accuracy-monitoring`（推理精度异常检测工具）已在推理链路中实时检测四类异常
（生僻字 / 乱码 / 重复 / NaN），并通过独立 Prometheus 端点 `GET /anomaly/metrics`
暴露聚合指标。本服务通过轮询各实例 `/anomaly/metrics` 聚合展示：

- 累计统计看板（总请求 / 总异常 / 累计异常率 / 检测错误）
- 异常检出趋势（1h / 4h / 8h / 16h / 24h 时间窗，分层存储控制内存）
- 异常类型 / 按模型累计分布
- 实例状态卡（在线 / 离线 / 暂停），运行中增删 / 暂停 / 恢复
- 滑动窗口阈值告警 + 界面铃铛 + 多渠道通知（飞书 / 钉钉 / 企业微信 / 邮件）
- 配置热重载（Web 界面操作或直接编辑 yaml 均即时生效，无需重启）


## 2. 快速开始
### 2.1 安装依赖

```bash
pip install fastapi uvicorn httpx prometheus_client
```

### 2.2 配置（configs/webui.yaml）
```yaml
auth:
  username: admin
  # 二选一：明文 password 或 SHA-256 hex 的 password_hash（推荐，避免明文落盘）
  password: admin@123 # 默认
  token_ttl_hours: 24 # 网页用户登录有效时间，可选时间 0-24 小时，数值取正整数

poll:
  interval_seconds: 3        # 2-5s
  http_timeout_seconds: 2

# 监控的推理服务，可配置多个，支持在本文件和 Web 界面两种配置方式
instances:
  - name: vllm-prod-1
    url: http://10.0.0.1:8000
  - name: vllm-prod-2
    url: http://10.0.0.2:8000
    paused: false            # 可选，默认 false

store:
  event_capacity: 1000
  alert_capacity: 200
  raw_trend_window_seconds: 3600
  trend_bucket_seconds: 60
  trend_horizon_seconds: 2592000

webhooks:
  # 全局默认 Webhook URL（可空）。自动识别渠道并转换消息格式：
  #   - 飞书（Lark）机器人:   https://open.feishu.cn/open-apis/bot/v2/hook/<token>
  #   - 钉钉自定义机器人:     https://oapi.dingtalk.com/robot/send?access_token=<token>
  #   - 企业微信群机器人:     https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>
  #   - 其它地址: 发送通用 JSON（rule_name/instance/model/ill_type/count/ts）
  default: ""

email:
  enabled: false             # 邮件告警（可选）
  smtp_host: "smtp.example.com"
  smtp_port: 465             # 465=SSL；587 请设 use_ssl: false
  smtp_user: "alert@example.com"
  smtp_password: "xxx"
  use_ssl: true
  from_addr: "alert@example.com"
  to_addrs:
    - "ops@example.com"

alerts:
  - name: garbled_frequent
    instance: "*"            # * = 全部
    model: "*"
    ill_type: garbled        # rare_character | garbled | repetition | nan_value；缺省 = 任一异常
    window_seconds: 300      # 滑动窗口时长(单位：秒)，以当前异常事件事件为准，往前数window_seconds内，同一个（实例、模型、异常类型）检出累计达到 threshold ，则触发告警
    threshold: 3             # 窗口内事件数阈值，超过阈值则告警
    webhook_url: ""          # 可选，覆盖全局 webhook
    enabled: true
```



> [!NOTE]
>
> (1) 实例 name 唯一且非空、url 为合法 http/https
> 
> (2) 用户名或密码变更即时生效，已登录的 Web 页面不强制失效


### 2.3 启动服务

```bash
# 启动服务, host 和 port 可配置
uvicorn webui.main:app --host 0.0.0.0 --port 9090
```

启动后浏览器访问 `http://<host>:<port>` → 登录 → 看板。


## 3. 告警

configs/webui.yaml 可配置告警信息和告警方式，当前支持`界面告警` + `Webhook(钉钉、飞书、企业微信) `+ `邮箱通知`。

### 3.1 告警信息配置
```yaml
alerts:
  - name: garbled_frequent
    instance: "*"            # * = 全部
    model: "*"
    ill_type: garbled        # rare_character | garbled | repetition | nan_value；缺省 = 任一异常
    window_seconds: 300      # 滑动窗口时长(单位：秒)，以当前异常事件事件为准，往前数window_seconds内，同一个（实例、模型、异常类型）检出累计达到 threshold ，则触发告警
    threshold: 3             # 窗口内事件数阈值，超过阈值则告警
    webhook_url: ""          # 可选，覆盖全局 webhook
    enabled: true
```


- 规则驱动+滑动窗口去抖：window_seconds 窗口内计数 ≥ threeshold 才触发，避免单次抖动误报
- 触发后窗口清零，同一时段不会重复轰炸
- 渠道自动识别：按 URL 前缀自动转成飞书/钉钉/企微/通用JSON
- 多渠道并行+隔离：webhook 与邮件同时发
- 匹配灵活：规则可用*通配实例/模型/类型，一条规则覆盖整个集群 



### 3.2 界面告警

当触发告警时，在 Web 界面右上角会有告警信息提醒，告知用户具体告警信息。

### 3.2 Webhook 渠道（飞书 / 钉钉 / 企业微信 / 通用 JSON）

Webhook URL 自动识别渠道并转换消息格式：设置全局默认或单条规则 `webhook_url` 即可。
同一告警可同时配置多个渠道（全局默认 + 各规则覆盖，若规则配置了则只发规则的地址）。

```yaml
webhooks:
  default: "https://open.feishu.cn/open-apis/bot/v2/hook/<你的机器人token>"
```

| 渠道 | Webhook URL 格式 | 说明 |
|---|---|---|
| 飞书 / Lark | `https://open.feishu.cn/open-apis/bot/v2/hook/<token>`（国际版 `open.larksuite.com/...`） | 群机器人，复制地址填入即可 |
| 钉钉 | `https://oapi.dingtalk.com/robot/send?access_token=<token>` | 机器人安全设置选「自定义关键词」，关键词设为 `异常监控` 等消息内文本 |
| 企业微信 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>` | 群机器人 |
| 其它地址 | 任意 http/https | 发送通用 JSON |

各渠道告警消息内容统一为：

```
【推理精度异常监控】
告警规则: garbled_frequent
实例: vllm-prod-1
模型: Qwen3-0.6B
异常类型: 乱码
窗口计数: 3
触发时间: 2026-08-13 10:00:00
```

**通用 JSON Webhook**（非飞书/钉钉/企微地址）发送内容：

```json
{"rule_name": "...", "instance": "...", "model": "...", "ill_type": "...", "count": 3, "ts": 1718...}
```

### 3.3 邮件告警（SMTP）

配置 `email` 段即可在触发告警时同时发送邮件（不影响 Webhook）：

```yaml
email:
  enabled: true
  smtp_host: "smtp.example.com"
  smtp_port: 465            # 465=SSL；587 请设 use_ssl: false（SMTP+STARTTLS）
  smtp_user: "alert@example.com"
  smtp_password: "xxx"
  use_ssl: true
  from_addr: "alert@example.com"   # 发件人（缺省用 smtp_user）
  to_addrs:                          # 收件人，可多组
    - "ops@example.com"
```

- `enabled: true` 时 `smtp_host` / `from_addr` / `to_addrs` 必填，否则启动校验失败。
- 邮件主题：`【推理精度异常监控】<规则> · <实例> · <异常类型>`；正文同 Webhook 文本格式。
- 发送在独立线程执行，失败仅记日志，不影响主流程。



## 4. 注意

- 进程重启后历史数据会被清空，请谨慎执行。
- 删除实例不会清除全局累计统计数。
- 告警规则变更需重启服务。
