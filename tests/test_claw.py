"""Claw 通道测试：协议构造、错误码、消息解析、通道契约。

全程用假 transport，不碰网络。
跑法：python tests/test_claw.py
"""

import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia.channel import claw
from somnia.channel.claw import (
    ClawAuthExpired, ClawChannel, ClawClient, ClawError,
    build_body, build_headers, check, parse_updates,
)


class FakeTransport:
    def __init__(self, responder=None, responses=None):
        self.calls = []
        self.responder = responder
        self.responses = list(responses or [])

    def post(self, path, body, headers, params=None):
        self.calls.append({"path": path, "body": body, "headers": headers, "params": params})
        if self.responder:
            return self.responder(path, body)
        return self.responses.pop(0) if self.responses else {}

    def paths(self):
        return [c["path"] for c in self.calls]


# ── 协议构造 ─────────────────────────────────────────────

def test_headers_shape():
    h = build_headers("TOKEN", uin=123456)
    assert h["Authorization"] == "Bearer TOKEN"
    assert h["AuthorizationType"] == "ilink_bot_token"
    assert h["iLink-App-Id"] == "bot"
    assert h["iLink-App-ClientVersion"] == claw.CLIENT_VERSION
    assert base64.b64decode(h["X-WECHAT-UIN"]).decode().isdigit()


def test_body_has_base_info():
    b = build_body({"text": "x"})
    assert b["base_info"]["channel_version"]
    assert b["base_info"]["bot_agent"].startswith("somnia/")


def test_body_keeps_payload():
    assert build_body({"a": 1})["a"] == 1


# ── 错误码 ───────────────────────────────────────────────

def test_ok_response_passes():
    assert check({"ret": 0}) == {"ret": 0}
    assert check({"errcode": 0}) == {"errcode": 0}
    assert check({}) == {}


def test_ret_minus_14_is_auth_expired():
    try:
        check({"ret": -14})
    except ClawAuthExpired as e:
        assert e.ret == -14
    else:
        raise AssertionError("应当抛 ClawAuthExpired")


def test_other_ret_is_error():
    try:
        check({"ret": 10086, "msg": "boom"})
    except ClawError as e:
        assert not isinstance(e, ClawAuthExpired)
    else:
        raise AssertionError("应当抛 ClawError")


# ── 解析 ─────────────────────────────────────────────────

REAL_FRAME = {
    "seq": 1,
    "message_id": 7518278190366719752,
    "from_user_id": "o9cq808qXXPAb2YNQm2TR7gBx8Og@im.wechat",
    "to_user_id": "cd6f59453277@im.bot",
    "message_type": 1,
    "message_state": 2,
    "item_list": [{
        "type": 1,
        "msg_id": "v1:4427254513849133461",
        "text_item": {"text": "在吗"},
    }],
    "context_token": "AARzJWAFAAABAAAA",
}


def test_parse_real_frame_shape():
    """真机帧：item_list[0].text_item.text。"""
    msgs, _ = parse_updates({"msgs": [REAL_FRAME], "get_updates_buf": "B"})
    assert len(msgs) == 1
    assert msgs[0].text == "在吗"
    assert msgs[0].msg_id == "v1:4427254513849133461"
    assert msgs[0].context_token == "AARzJWAFAAABAAAA"


def test_parse_skips_non_text_message():
    """图片消息没有 text_item，应当忽略而不是瞎猜。"""
    frame = {"message_id": 1, "message_type": 2,
             "item_list": [{"type": 2, "image_item": {"url": "x"}}]}
    assert parse_updates({"msgs": [frame]})[0] == []


def test_parse_falls_back_to_message_id():
    frame = {"message_id": 999, "context_token": "C",
             "msg": {"text": "兜底"}}
    msgs, _ = parse_updates({"msgs": [frame]})
    assert msgs[0].msg_id == "999"
    assert msgs[0].text == "兜底"


def test_parse_updates_basic():
    msgs, buf = parse_updates({
        "msgs": [{"text": "在吗", "msg_id": "m1", "context_token": "CTX"}],
        "get_updates_buf": "BUF2",
    })
    assert len(msgs) == 1
    assert msgs[0].text == "在吗"
    assert msgs[0].msg_id == "m1"
    assert msgs[0].context_token == "CTX"
    assert buf == "BUF2"


def test_parse_updates_nested_text():
    msgs, _ = parse_updates({"messages": [{"msg": {"content": "嵌套"}}]})
    assert msgs and msgs[0].text == "嵌套"


def test_parse_updates_empty():
    assert parse_updates({}) == ([], "")
    assert parse_updates({"msgs": []}) == ([], "")


def test_parse_updates_skips_textless():
    msgs, _ = parse_updates({"msgs": [{"image": "x"}, {"text": "有字"}]})
    assert [m.text for m in msgs] == ["有字"]


# ── 客户端 ───────────────────────────────────────────────

def test_sendmessage_payload():
    t = FakeTransport()
    c = ClawClient("TOKEN", t, user_id="U1")
    c.sendmessage("你好", "CTX")
    call = t.calls[0]
    assert call["path"] == claw.P_SENDMESSAGE
    msg = call["body"]["msg"]
    # 真机验证：文本必须放在 item_list[].text_item.text
    assert msg["item_list"][0]["text_item"]["text"] == "你好"
    assert msg["item_list"][0]["type"] == 1
    assert msg["context_token"] == "CTX"
    assert msg["client_id"]
    assert msg["message_type"] == 1
    assert msg["to_user_id"] == "U1"
    assert "text" not in msg          # 顶层 text 会被服务端拒绝


def test_paths_have_ilink_prefix():
    for p in (claw.P_GETUPDATES, claw.P_SENDMESSAGE, claw.P_SENDTYPING,
              claw.P_NOTIFYSTART, claw.P_GETCONFIG):
        assert p.startswith("/ilink/bot/"), p


def test_user_id_injected_when_present():
    t = FakeTransport()
    c = ClawClient("TOKEN", t, user_id="U1")
    c.sendtyping(1, "CTX")
    assert t.calls[0]["body"]["ilink_user_id"] == "U1"


def test_user_id_absent_when_empty():
    t = FakeTransport()
    c = ClawClient("TOKEN", t)
    c.sendtyping(1, "CTX")
    assert "ilink_user_id" not in t.calls[0]["body"]


def test_load_credentials(tmpdir=None):
    import json
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "cred.json"
    p.write_text(json.dumps({"bot_token": "T", "ilink_user_id": "U"}), encoding="utf-8")
    assert claw.load_credentials(p) == ("T", "U")
    missing = Path(tempfile.mkdtemp()) / "nope.json"
    assert claw.load_credentials(missing) == ("", "")


def test_sendtyping_status():
    t = FakeTransport()
    c = ClawClient("TOKEN", t)
    c.sendtyping(1, "CTX")
    c.sendtyping(2)
    assert t.calls[0]["body"]["status"] == 1
    assert t.calls[1]["body"]["status"] == 2


def test_getupdates_sends_buf_and_timeout():
    t = FakeTransport()
    c = ClawClient("TOKEN", t)
    c.getupdates("BUF1", 5000)
    b = t.calls[0]["body"]
    assert b["get_updates_buf"] == "BUF1"
    assert b["longpolling_timeout_ms"] == 5000


# ── 通道契约 ─────────────────────────────────────────────

def test_send_without_context_raises():
    ch = ClawChannel("TOKEN", transport=FakeTransport())
    try:
        ch.send("在吗")
    except RuntimeError as e:
        assert "context_token" in str(e)
    else:
        raise AssertionError("没有 context_token 时不该能发")


def test_send_uses_last_context():
    t = FakeTransport()
    ch = ClawChannel("TOKEN", transport=t)
    ch._ctx = "CTX9"
    ch.send("在")
    assert t.calls[-1]["body"]["msg"]["context_token"] == "CTX9"


def test_typing_swallows_errors():
    ch = ClawChannel("TOKEN", transport=FakeTransport(lambda p, b: (_ for _ in ()).throw(IOError())))
    ch.typing(True)          # 不该抛


def test_channel_receives_and_sets_context():
    state = {"sent": False}

    def responder(path, body):
        if path == claw.P_NOTIFYSTART:
            return {}
        if path == claw.P_GETUPDATES:
            if not state["sent"]:
                state["sent"] = True
                return {"msgs": [{"text": "在吗", "msg_id": "m1",
                                  "context_token": "CTX"}], "get_updates_buf": "B2"}
            time.sleep(0.01)
            return {}
        return {}

    ch = ClawChannel("TOKEN", transport=FakeTransport(responder)).start()
    try:
        inb = ch.recv_timeout(3)
        assert inb is not None and inb.text == "在吗"
        assert inb.msg_id == "m1"
        assert ch._ctx == "CTX"
    finally:
        ch.close()


def test_closed_channel_returns_none():
    ch = ClawChannel("TOKEN", transport=FakeTransport())
    ch.close()
    assert ch.recv() is None
    assert ch.recv_timeout(0.1) is None


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
