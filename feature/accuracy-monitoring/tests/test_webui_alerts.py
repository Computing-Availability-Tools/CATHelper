"""alerts 单元测试：滑动窗口、阈值触发、窗口重置去重、Webhook 分发与失败容错。"""
from __future__ import annotations

import asyncio

import pytest

from webui.alerts import Alert, AlertEngine
from webui.config import AlertRule, StoreConfig
from webui.events import AnomalyEvent
from webui.store import RingBuffer, Store


def _engine(rules, store=None, sender=None, default_webhook=""):
    st = store or Store(
        StoreConfig(event_capacity=1000, alert_capacity=200,
                    raw_trend_window_seconds=3600, trend_bucket_seconds=60,
                    trend_horizon_seconds=86400)
    )
    eng = AlertEngine(st.alerts, id_allocer=st.alloc_alert_id, sender=sender,
                      default_webhook_url=default_webhook)
    eng.set_rules(rules, default_webhook)
    return eng, st


def _evt(instance, model, ill, ts):
    return AnomalyEvent(id=ts * 10, ts=ts, instance=instance, model=model,
                        ill_type=ill, choice_index="0")


def test_threshold_not_reached_no_alert():
    eng, st = _engine([AlertRule(name="r", ill_type=None, threshold=3, window_seconds=300)])
    for i in range(2):
        eng.ingest(_evt("a", "m", "garbled", ts=100 + i))
    assert len(st.recent_alerts(10)) == 0


def test_sliding_window_trigger_and_reset():
    """达到阈值触发一次并重置窗口队列（去重）。"""
    eng, st = _engine([AlertRule(name="r", ill_type=None, threshold=3, window_seconds=300)])
    for i in range(3):
        eng.ingest(_evt("a", "m", "garbled", ts=100 + i))
    alerts = st.recent_alerts(10)
    assert len(alerts) == 1
    assert alerts[0].count == 3
    assert alerts[0].instance == "a" and alerts[0].ill_type == "garbled"
    # 窗口已重置：再进 1 条不触发
    eng.ingest(_evt("a", "m", "garbled", ts=200))
    assert len(st.recent_alerts(10)) == 1


def test_window_expiry_prunes_old_events():
    """窗口过期事件丢弃：跨窗口计数。"""
    eng, st = _engine([AlertRule(name="r", ill_type=None, threshold=2, window_seconds=60)])
    eng.ingest(_evt("a", "m", "garbled", ts=100))
    # 61s 后的事件：前一条已出窗口
    eng.ingest(_evt("a", "m", "garbled", ts=170))
    assert len(st.recent_alerts(10)) == 0
    eng.ingest(_evt("a", "m", "garbled", ts=171))
    assert len(st.recent_alerts(10)) == 1


def test_per_rule_per_key_isolation():
    """不同 (instance, model, ill_type) 键独立计数。"""
    eng, st = _engine([AlertRule(name="r", ill_type=None, threshold=2, window_seconds=60)])
    eng.ingest(_evt("a", "m", "garbled", ts=1))
    eng.ingest(_evt("b", "m", "garbled", ts=2))
    assert len(st.recent_alerts(10)) == 0
    eng.ingest(_evt("a", "m", "garbled", ts=3))
    assert len(st.recent_alerts(10)) == 1
    assert st.recent_alerts(10)[0].instance == "a"


def test_rule_filtering_by_instance_and_ill_type():
    rule = AlertRule(name="r", instance="a", model="*", ill_type="garbled", threshold=1, window_seconds=60)
    eng, st = _engine([rule])
    eng.ingest(_evt("a", "m", "rare_character", ts=1))  # 类型不匹配
    eng.ingest(_evt("b", "m", "garbled", ts=2))  # 实例不匹配
    assert len(st.recent_alerts(10)) == 0
    eng.ingest(_evt("a", "m", "garbled", ts=3))
    assert len(st.recent_alerts(10)) == 1


def test_disabled_rule_skipped():
    eng, st = _engine([AlertRule(name="r", ill_type=None, threshold=1, window_seconds=60, enabled=False)])
    eng.ingest(_evt("a", "m", "garbled", ts=1))
    assert len(st.recent_alerts(10)) == 0


# ------------------------------------------------------------------ #
# Webhook
# ------------------------------------------------------------------ #
class RecordingSender:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, payload):
        self.calls.append((url, payload))


def test_webhook_called_with_payload():
    async def run():
        sender = RecordingSender()
        eng, st = _engine(
            [AlertRule(name="r", ill_type="garbled", threshold=1, window_seconds=60)],
            sender=sender,
            default_webhook="http://hook.example/x",
        )
        eng.ingest(_evt("a", "m", "garbled", ts=123.5))
        await eng.wait_pending()
        assert len(sender.calls) == 1
        url, payload = sender.calls[0]
        assert url == "http://hook.example/x"
        assert payload["rule_name"] == "r"
        assert payload["instance"] == "a"
        assert payload["model"] == "m"
        assert payload["ill_type"] == "garbled"
        assert payload["count"] == 1
        assert payload["ts"] == 123.5

    asyncio.run(run())


def test_rule_webhook_overrides_default():
    async def run():
        sender = RecordingSender()
        eng, st = _engine(
            [AlertRule(name="r", ill_type=None, threshold=1, window_seconds=60,
                       webhook_url="http://rule.example/y")],
            sender=sender,
            default_webhook="http://default.example/z",
        )
        eng.ingest(_evt("a", "m", "repetition", ts=1))
        await eng.wait_pending()
        assert sender.calls[0][0] == "http://rule.example/y"

    asyncio.run(run())


def test_webhook_same_window_sent_once():
    """窗口重置语义 → 同一窗口只发一次。"""
    async def run():
        sender = RecordingSender()
        eng, st = _engine(
            [AlertRule(name="r", ill_type=None, threshold=2, window_seconds=60)],
            sender=sender,
            default_webhook="http://hook.example/x",
        )
        for i in range(3):
            eng.ingest(_evt("a", "m", "garbled", ts=10 + i))
        await eng.wait_pending()
        assert len(sender.calls) == 1

    asyncio.run(run())


def test_webhook_failure_does_not_raise():
    async def run():
        async def failing_sender(url, payload):
            raise RuntimeError("boom")

        eng, st = _engine(
            [AlertRule(name="r", ill_type=None, threshold=1, window_seconds=60)],
            sender=failing_sender,
            default_webhook="http://hook.example/x",
        )
        eng.ingest(_evt("a", "m", "garbled", ts=1))
        await eng.wait_pending()
        # 主流程不受影响：告警已入缓冲
        assert len(st.recent_alerts(10)) == 1

    asyncio.run(run())


def test_no_webhook_when_no_url():
    sender = RecordingSender()
    eng, st = _engine([AlertRule(name="r", ill_type=None, threshold=1, window_seconds=60)], sender=sender)
    eng.ingest(_evt("a", "m", "garbled", ts=1))
    assert sender.calls == []


def test_feishu_webhook_uses_feishu_format():
    """飞书机器人地址 → 自动转换为飞书文本消息格式。"""
    async def run():
        sender = RecordingSender()
        eng, st = _engine(
            [AlertRule(name="r", ill_type="garbled", threshold=1, window_seconds=60)],
            sender=sender,
            default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/fake-token",
        )
        eng.ingest(_evt("a", "m", "garbled", ts=123.5))
        await eng.wait_pending()
        assert len(sender.calls) == 1
        url, payload = sender.calls[0]
        assert url.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/")
        assert payload["msg_type"] == "text"
        text = payload["content"]["text"]
        assert "告警规则: r" in text   # 规则名
        assert "乱码" in text          # 异常类型中文标签
        assert "实例: a" in text       # 实例
        assert "模型: m" in text       # 模型

    asyncio.run(run())


def test_feishu_detection():
    from webui.alerts import feishu_payload, is_feishu_webhook

    assert is_feishu_webhook("https://open.feishu.cn/open-apis/bot/v2/hook/x")
    assert is_feishu_webhook("https://open.larksuite.com/open-apis/bot/v2/hook/x")
    assert not is_feishu_webhook("http://hook.example/x")
    assert not is_feishu_webhook("")
    alert = Alert(id=1, rule_name="r", ts=1.0, instance="a", model="m",
                  ill_type="nan_value", count=2)
    p = feishu_payload(alert)
    assert p["content"]["text"].startswith("【推理精度异常监控】")
    assert "NaN 值" in p["content"]["text"]


# ------------------------------------------------------------------ #
# 多渠道 Webhook：钉钉 / 企业微信 / 通用 JSON
# ------------------------------------------------------------------ #
def test_dingtalk_and_wechat_payloads():
    from webui.alerts import dingtalk_payload, wechat_payload

    alert = Alert(id=1, rule_name="r", ts=1.0, instance="a", model="m",
                  ill_type="garbled", count=2)
    d = dingtalk_payload(alert)
    assert d["msgtype"] == "text"
    text = d["text"]["content"]
    assert "告警规则: r" in text and "乱码" in text and "实例: a" in text
    w = wechat_payload(alert)
    assert w["msgtype"] == "text"
    assert "乱码" in w["text"]["content"]


def test_webhook_payload_auto_detects_channel():
    from webui.alerts import webhook_payload

    alert = Alert(id=1, rule_name="r", ts=1.0, instance="a", model="m",
                  ill_type="garbled", count=2)
    body = webhook_payload("https://oapi.dingtalk.com/robot/send?access_token=x", alert)
    assert body["msgtype"] == "text" and "text" in body
    body = webhook_payload("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", alert)
    assert body["msgtype"] == "text" and "text" in body
    body = webhook_payload("http://hook.example/x", alert)
    assert body["rule_name"] == "r" and body["ill_type"] == "garbled"
    assert "msgtype" not in body


def test_dingtalk_webhook_roundtrip_via_engine():
    """钉钉地址经引擎分发 → 自动转钉钉格式。"""
    async def run():
        sender = RecordingSender()
        eng, st = _engine(
            [AlertRule(name="r", ill_type="garbled", threshold=1, window_seconds=60)],
            sender=sender,
            default_webhook="https://oapi.dingtalk.com/robot/send?access_token=fake",
        )
        eng.ingest(_evt("a", "m", "garbled", ts=123.5))
        await eng.wait_pending()
        assert len(sender.calls) == 1
        url, payload = sender.calls[0]
        assert "oapi.dingtalk.com/robot/send" in url
        assert payload["msgtype"] == "text"
        assert "乱码" in payload["text"]["content"]
        assert "告警规则: r" in payload["text"]["content"]

    asyncio.run(run())


def test_email_channel_dispatch_and_failure_is_silent(monkeypatch):
    """邮件通道：触发时调度 SMTP 发送；SMTP 失败仅记日志不影响主流程。"""
    from webui import alerts as alerts_mod
    from webui.config import EmailConfig

    # mock 掉真实 SMTP 网络发送：直接模拟"连接被拒"，
    # 避免部分机器上防火墙/安全软件 DROP 连接导致 10s 超时 > wait_pending 的 5s
    def fake_smtp_send(email_cfg, subject, body):
        raise ConnectionRefusedError("mocked: connection refused")

    monkeypatch.setattr(alerts_mod, "_smtp_send_sync", fake_smtp_send)

    async def run():
        st = Store(StoreConfig(event_capacity=100, alert_capacity=10,
                               raw_trend_window_seconds=3600, trend_bucket_seconds=60,
                               trend_horizon_seconds=86400))
        eng = AlertEngine(
            st.alerts,
            id_allocer=st.alloc_alert_id,
            email_cfg=EmailConfig(
                enabled=True, smtp_host="smtp.test.invalid", smtp_port=465,
                from_addr="a@b.c", to_addrs=("x@y.z",),
            ),
        )
        eng.set_rules([AlertRule(name="r", ill_type=None, threshold=1, window_seconds=60)])
        eng.ingest(_evt("a", "m", "garbled", ts=1))
        await eng.wait_pending()
        # SMTP 发送被拒（mock 抛异常）→ 异常被吞，告警仍在缓冲
        assert len(st.recent_alerts(10)) == 1

    asyncio.run(run())


def test_email_disabled_no_email_task():
    from webui.config import EmailConfig

    st = Store(StoreConfig(event_capacity=100, alert_capacity=10,
                           raw_trend_window_seconds=3600, trend_bucket_seconds=60,
                           trend_horizon_seconds=86400))
    eng = AlertEngine(st.alerts, id_allocer=st.alloc_alert_id, email_cfg=EmailConfig())
    eng.set_rules([AlertRule(name="r", ill_type=None, threshold=1, window_seconds=60)])
    rule = eng.rules[0]
    eng._dispatch(rule, Alert(id=1, rule_name="r", ts=1.0, instance="a", model="m",
                              ill_type="garbled", count=1))
    assert eng._pending == set()
    assert len(st.recent_alerts(10)) == 0  # dispatch 只发通知，不入缓冲（由 ingest 处理）


def test_email_config_validation():
    from webui.config import ConfigError, EmailConfig

    # enabled 缺 smtp_host / from_addr / to_addrs → 拒绝
    with pytest.raises(ConfigError, match="smtp_host"):
        EmailConfig.from_dict({"enabled": True, "from_addr": "a@b.c", "to_addrs": ["x@y.z"]})
    with pytest.raises(ConfigError, match="from_addr"):
        EmailConfig.from_dict({"enabled": True, "smtp_host": "h"})
    with pytest.raises(ConfigError, match="to_addrs"):
        EmailConfig.from_dict({"enabled": True, "smtp_host": "h", "from_addr": "a@b.c"})
    with pytest.raises(ConfigError, match="smtp_port"):
        EmailConfig.from_dict({"enabled": True, "smtp_host": "h", "from_addr": "a@b.c",
                               "to_addrs": ["x@y.z"], "smtp_port": -1})
    # 合法配置
    cfg = EmailConfig.from_dict({
        "enabled": True, "smtp_host": "smtp.x.com", "smtp_port": 465,
        "smtp_user": "u", "smtp_password": "p", "from_addr": "a@b.c",
        "to_addrs": ["x@y.z", "q@w.e"],
    })
    assert cfg.is_ready()
    assert cfg.to_addrs == ("x@y.z", "q@w.e")
    # 未启用时缺省合法
    assert not EmailConfig.from_dict({}).enabled


def test_alert_buffer_capacity():
    st = Store(StoreConfig(event_capacity=100, alert_capacity=3,
                           raw_trend_window_seconds=3600, trend_bucket_seconds=60,
                           trend_horizon_seconds=86400))
    eng = AlertEngine(st.alerts, id_allocer=st.alloc_alert_id)
    eng.set_rules([AlertRule(name="r", ill_type=None, threshold=1, window_seconds=60)])
    for i in range(5):
        eng.ingest(_evt("a", "m", "garbled", ts=i))
    alerts = st.recent_alerts(10)
    assert len(alerts) == 3  # 最旧两条被淘汰
    assert [a.id for a in alerts] == [5, 4, 3]  # 最新在前