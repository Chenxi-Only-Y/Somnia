"""微信 4.x 通道测试：库行解析、增量识别、发送、失败告警。

用假 db / 假 sender，不需要装 wechatauto，也不需要开微信。
跑法：python tests/test_wechatauto.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia.channel import wechatauto as W

W.DEFAULT_PACE = (0, 0, 0)      # 测试不等待
W.DEFAULT_VERIFY_TIMEOUT = 0    # 回查不等待


class FakeDB:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def get_messages(self, target, limit=30):
        return list(self.rows)

    def push(self, content, local_id, shard="message_0.db", self_sent=False, sender="peer"):
        self.rows.append({
            "content": content,
            "local_id": local_id,
            "shard": shard,
            "local_type": 1,
            "sender_username": sender,
            "is_sender": self_sent,
            "create_time": time.time(),
        })


class FakeSender:
    def __init__(self, fail=False, resp_ok=True, flaky=0):
        self.sent = []
        self.fail = fail
        self.resp_ok = resp_ok
        self.flaky = flaky          # 前 N 次抛 COM 类错误
        self.attempts = 0

    def send_msg(self, text, who):
        self.attempts += 1
        if self.flaky and self.attempts <= self.flaky:
            raise OSError("模拟 COMError：事件无法调用任何订户")
        if self.fail:
            raise RuntimeError("窗口不可用")
        self.sent.append((text, who))
        return _Resp(self.resp_ok)


class _Resp:
    def __init__(self, ok):
        self.is_success = ok

    def __str__(self):
        return f"WxResponse(success={self.is_success})"


def _rows():
    return [
        {"content": "旧消息", "local_id": 1, "shard": "m0", "type": "文本", "is_sender": False},
        {"content": "我发的", "local_id": 2, "shard": "m0", "type": "文本", "is_sender": True},
        {"content": "", "local_id": 3, "shard": "m0", "type": "文本", "is_sender": False},
        {"content": "<img/>", "local_id": 4, "shard": "m0", "type": "图片", "is_sender": False},
    ]


# ── 解析 ─────────────────────────────────────────────────

def test_parse_skips_empty_text_but_keeps_media_placeholder():
    msgs = W.parse_messages(_rows())
    # 空文本丢掉；图片不再丢，换成占位——她得知道你发过东西
    assert msgs[0].text == "旧消息"
    assert msgs[1].text == "我发的"
    assert msgs[2].text == "（发来一张图片）"


def test_parse_marks_self_sent():
    msgs = W.parse_messages(_rows())
    assert msgs[0].self_sent is False
    assert msgs[1].self_sent is True


def test_parse_self_by_username():
    rows = [{"content": "hi", "local_id": 1, "shard": "m0", "type": "文本",
             "sender_username": "wxid_me"}]
    msgs = W.parse_messages(rows, my_username="wxid_me")
    assert msgs[0].self_sent is True


def test_chinese_type_is_text():
    assert W.is_text({"type": "文本"})
    assert not W.is_text({"type": "图片"})
    assert not W.is_text({"type": "语音"})
    assert W.is_text({"type": 1})
    assert not W.is_text({"type": 3})


def test_send_unconfirmed_response_raises():
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=FakeSender(resp_ok=False), pace=(0,0,0))
    try:
        ch.send("x")
    except RuntimeError as e:
        assert "未被确认" in str(e)
    else:
        raise AssertionError("应当抛出")


def test_msg_key_includes_shard():
    msgs = W.parse_messages([
        {"content": "a", "local_id": 7, "shard": "m0", "local_type": 1},
        {"content": "a", "local_id": 7, "shard": "m1", "local_type": 1},
    ])
    assert msgs[0].key != msgs[1].key


# ── 增量 ─────────────────────────────────────────────────

def test_first_poll_is_baseline():
    db = FakeDB(_rows())
    ch = W.WechatAutoChannel("她", db=db, sender=FakeSender(), poll_interval=0.02).start()
    try:
        time.sleep(0.15)
        assert ch.recv_timeout(0.2) is None      # 历史不当新消息
    finally:
        ch.close()


def test_new_peer_message_delivered():
    db = FakeDB(_rows())
    ch = W.WechatAutoChannel("她", db=db, sender=FakeSender(), poll_interval=0.02).start()
    try:
        time.sleep(0.15)
        db.push("新消息", local_id=9)
        inb = ch.recv_timeout(3)
        assert inb is not None and inb.text == "新消息"
        assert inb.msg_id.startswith("wa_")
    finally:
        ch.close()


def test_new_self_message_not_delivered():
    db = FakeDB(_rows())
    ch = W.WechatAutoChannel("她", db=db, sender=FakeSender(), poll_interval=0.02).start()
    try:
        time.sleep(0.15)
        db.push("我自己发的", local_id=10, self_sent=True)
        assert ch.recv_timeout(0.5) is None
    finally:
        ch.close()


# ── 发送 ─────────────────────────────────────────────────

def test_send_uses_target():
    sender = FakeSender()
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=sender, pace=(0,0,0))
    ch.send("你好")
    assert sender.sent == [("你好", "测试")]


def test_send_failure_alerts_and_raises():
    alerts = []
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=FakeSender(fail=True),
                             alert_hook=alerts.append)
    try:
        ch.send("x")
    except RuntimeError:
        pass
    else:
        raise AssertionError("应当抛出")
    assert len(alerts) == 1
    assert "发送失败" in alerts[0]


def test_send_failure_alerts_only_once():
    alerts = []
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=FakeSender(fail=True),
                             alert_hook=alerts.append)
    for _ in range(3):
        try:
            ch.send("x")
        except RuntimeError:
            pass
    assert len(alerts) == 1          # 不刷屏


def test_send_retries_transient_uia_error():
    """真机：UIA 调用间歇性抛 COMError，重试一次通常就成功。"""
    sender = FakeSender(flaky=1)
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=sender, pace=(0,0,0))
    ch.send("你好")
    assert sender.attempts == 2
    assert sender.sent == [("你好", "测试")]


def test_send_raises_after_all_attempts():
    sender = FakeSender(flaky=99)
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=sender, pace=(0,0,0))
    try:
        ch.send("x")
    except Exception:
        pass
    else:
        raise AssertionError("应当抛出")
    assert sender.attempts == 3


def test_resolve_account_accepts_bare_wxid():
    accounts = [
        {"account": "wxid_aaa_2a90", "wxid": "wxid_aaa"},
        {"account": "wxid_bbb_42fd", "wxid": "wxid_bbb"},
    ]
    assert W.resolve_account("wxid_bbb", accounts) == "wxid_bbb_42fd"
    assert W.resolve_account("wxid_aaa_2a90", accounts) == "wxid_aaa_2a90"
    assert W.resolve_account("", accounts) == ""
    assert W.resolve_account("unknown", accounts) == "unknown"


def test_send_verifies_message_in_db():
    """发送后应能在钉死的库里查到，否则告警账号可能串了。"""
    db = FakeDB(_rows())
    ch = W.WechatAutoChannel("测试", db=db, sender=FakeSender())
    ch.send("落库校验")          # FakeDB 不会自动加，所以查不到
    # 首次查不到会触发一次告警（这里只断言行为不崩、且缓存了告警状态）
    assert ch._account_warned is True


def test_send_skip_verify_when_disabled():
    db = FakeDB(_rows())
    ch = W.WechatAutoChannel("测试", db=db, sender=FakeSender())
    ch.verify = False
    ch.send("不校验")
    assert ch._account_warned is False


def test_typing_is_noop():
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=FakeSender(), pace=(0,0,0))
    assert ch.typing(True) is None


def test_close_is_sticky():
    ch = W.WechatAutoChannel("测试", db=FakeDB(), sender=FakeSender(), pace=(0,0,0))
    ch.close()
    assert ch.recv() is None
    assert ch.recv_timeout(0.05) is None


def test_non_text_messages_become_placeholders():
    """图片/表情读不出内容，但不能当没收到 —— 她至少要知道你发了东西。"""
    from somnia.channel.wechatauto import parse_messages
    rows = [
        {"local_id": 1, "type": "文本", "content": "在吗", "is_sender": 0},
        {"local_id": 2, "type": "动画表情", "content": "<msg><emoji/></msg>", "is_sender": 0},
        {"local_id": 3, "type": "图片", "content": "<msg><img/></msg>", "is_sender": 0},
        {"local_id": 4, "type": "语音", "content": "<msg><voice/></msg>", "is_sender": 0},
    ]
    texts = [m.text for m in parse_messages(rows)]
    assert texts[0] == "在吗"
    assert "表情" in texts[1]
    assert "图片" in texts[2]
    assert "语音" in texts[3]


def test_unknown_message_type_is_skipped():
    from somnia.channel.wechatauto import parse_messages
    rows = [{"local_id": 9, "type": "系统消息", "content": "x", "is_sender": 0}]
    assert parse_messages(rows) == []


def test_recent_self_text_detects_just_sent():
    ch = W.WechatAutoChannel("对方", db=FakeDB(), sender=FakeSender(),
                             my_username="me", target_id="wxid_x")
    ch._said = [("走吧，海边晚风正好", time.time())]
    assert ch._recent_self_text() == "走吧，海边晚风正好"


def test_recent_self_text_ignores_old_says():
    """40 秒前说过"嗯"，现在再说一次是正常的，不能被当成重复吞掉。"""
    ch = W.WechatAutoChannel("对方", db=FakeDB(), sender=FakeSender(),
                             my_username="me", target_id="wxid_x")
    ch._said = [("嗯", time.time() - 40)]
    assert ch._recent_self_text() == ""


def test_send_skips_identical_repeat():
    """连着发同一句，第二次跳过（重试最容易造成重复）。"""
    sender = FakeSender()
    ch = W.WechatAutoChannel("对方", db=FakeDB(), sender=sender,
                             my_username="me", target_id="wxid_x")
    ch.verify = False
    ch.send("走吧，海边晚风正好")
    ch.send("走吧，海边晚风正好")
    assert len(sender.sent) == 1        # 只发了一次


class BlindSender:
    """能粘贴、能回车，但读不到输入框内容（UIA 读回为空值）。"""

    def __init__(self):
        self.sent = []
        self._last_input_box = (0, 0, 10, 10)

        class Keys:
            def key(self, *a, **k):
                pass

        self._input = Keys()

    def get_input_box(self):
        return (0, 0, 10, 10)

    def focus_input(self, box):
        return True

    def set_clipboard(self, text):
        return True

    def _get_uia(self):
        class Uia:
            def _chat_input(self):
                return object()

            def _read_edit_value(self, ctrl):
                return None

        return Uia()

    def send_msg(self, text, who=None, verify=False):
        self.sent.append(text)

    def release_foreground(self):
        pass


def test_unknown_send_state_does_not_resend():
    """回车按了、但读不到框 —— 绝不能退回库的发送流程再发一遍。"""
    sender = BlindSender()
    ch = W.WechatAutoChannel("对方", db=FakeDB(), sender=sender,
                             my_username="me", target_id="wxid_x")
    ch.verify = False
    ch.send("在吗")
    assert sender.sent == []          # 没有第二遍


class StuckSender:
    """文字进了输入框，但第一次回车没生效的那种发送器。"""

    def __init__(self):
        self.sent = []
        self.clicked = 0
        self.focused = 0
        self._last_input_box = (0, 0, 10, 10)

    def send_msg(self, text, who=None, verify=False):
        self.sent.append(text)
        raise RuntimeError("发送点击没生效（输入框没清空）")

    def focus_input(self, box):
        self.focused += 1
        return True

    def click_send(self, fast=False):
        self.clicked += 1
        return self.clicked >= 1          # 补按一次就成功

    def release_foreground(self):
        pass


def test_nudge_send_rescues_when_enter_missed():
    """回车没落地时，只补按发送，不重新粘一遍（否则会重复）。"""
    db = FakeDB()
    sender = StuckSender()
    ch = W.WechatAutoChannel("对方", db=db, sender=sender,
                             my_username="me", target_id="wxid_x")
    ch.verify = False
    ch.send("晚安")
    assert sender.clicked == 1        # 补按了发送
    assert len(sender.sent) == 1      # 没有再来一次 send_msg（不会重复）


def test_box_text_none_without_uia():
    ch = W.WechatAutoChannel("对方", db=FakeDB(), sender=object(), target_id="x")
    assert ch._box_text(object()) is None      # 没有 UIA 就返回 None，不瞎猜


def test_send_direct_without_box_is_false():
    ch = W.WechatAutoChannel("对方", db=FakeDB(), sender=object(), target_id="x")
    assert ch._send_direct(object(), "在吗") is False


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")

