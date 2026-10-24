"""面板测试：设置读写、状态、启停。

不起 HTTP 服务，直接测 PanelApp。
跑法：python tests/test_panel.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import settings as S
from somnia import panel as P
from somnia.config import Config


def _isolate():
    d = Path(tempfile.mkdtemp())
    S.FILE = d / "settings.json"
    S.DATA_DIR = d
    P.PERSONA_FILE = d / "persona.md"
    return d


def test_settings_defaults_and_roundtrip():
    _isolate()
    d = S.load()
    assert d["channel"] == "cli"
    assert d["model"] == "deepseek-flash"

    S.save({"api_key": "sk-abcdef123456", "model": "deepseek-flash", "channel": "wechatauto"})
    d2 = S.load()
    assert d2["api_key"] == "sk-abcdef123456"
    assert d2["channel"] == "wechatauto"


def test_unknown_keys_ignored():
    _isolate()
    S.save({"nope": 1, "model": "m2"})
    d = S.load()
    assert d["model"] == "m2"
    assert "nope" not in d


def test_mask_hides_middle():
    assert S.mask("") == ""
    assert S.mask("short") .endswith("…")
    m = S.mask("sk-1234567890abcdef")
    assert m.startswith("sk-123")
    assert "567890" not in m


def test_state_never_returns_plain_key():
    _isolate()
    S.save({"api_key": "sk-secret-value-123"})
    st = P.PanelApp().state()
    assert st["api_key"] == ""
    assert "sk-secret" not in str(st)
    assert st["api_key_masked"]


def test_empty_key_does_not_overwrite():
    _isolate()
    S.save({"api_key": "sk-keep-me"})
    app = P.PanelApp()
    app.update_settings({"api_key": "", "model": "x"})
    assert S.load()["api_key"] == "sk-keep-me"


def test_save_persona_writes_file():
    _isolate()
    app = P.PanelApp()
    assert app.save_persona("你是{name}，一个画画的。")
    assert "画画的" in P.PERSONA_FILE.read_text(encoding="utf-8")
    assert "人设" in app.state()["log"][-1]


def test_start_rejects_cli_without_console():
    """没有控制台时，cli 不能启动 —— 但要给一句能照做的话。"""
    from somnia import panel as P
    app = P.PanelApp()
    old = P.cli_interactive
    P.cli_interactive = lambda: False
    try:
        ok, msg = app.start()
    finally:
        P.cli_interactive = old
    assert not ok and "cli" in msg

def test_start_rejects_missing_target():
    _isolate()
    S.save({"channel": "wechatauto", "target": ""})
    app = P.PanelApp(runner=lambda *a: None)
    ok, msg = app.start()
    assert not ok and "聊天对象" in msg


def test_start_and_stop_lifecycle():
    _isolate()
    S.save({"channel": "wechatauto", "target": "对方", "target_id": "wxid_x"})

    seen = {"kind": None, "closed": False}

    class FakeChannel:
        def close(self):
            seen["closed"] = True

    def fake_run(cfg, fast, offline, kind, window, sched, stop_event, on_channel):
        seen["kind"] = kind
        on_channel(FakeChannel())
        while not stop_event.is_set():
            time.sleep(0.01)

    app = P.PanelApp(runner=fake_run)
    ok, msg = app.start()
    assert ok, msg
    time.sleep(0.15)
    assert app.running
    assert seen["kind"] == "wechatauto"

    ok2, _ = app.stop()
    assert ok2
    assert seen["closed"] is True
    assert not app.running
    assert app.stop()[0] is False        # 重复停止返回 False


def test_proactive_defaults_and_toggle():
    _isolate()
    d = S.load()
    assert d["proactive"] is True
    assert d["silence_hours"] == 6.0

    S.save({"proactive": False, "silence_hours": 12.0})
    d2 = S.load()
    assert d2["proactive"] is False
    assert d2["silence_hours"] == 12.0


def test_config_applies_proactive_settings():
    _isolate()
    S.save({"proactive": False, "silence_hours": 9.0})
    cfg = Config().with_settings()
    assert cfg.proactive is False
    assert cfg.silence_hours == 9.0


def test_port_taken_detects_listener():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert P.port_taken("127.0.0.1", port) is True
    finally:
        s.close()


def test_broken_settings_json_falls_back_to_defaults():
    """settings.json 写坏了：用默认值，不抛异常，但要出声提醒。"""
    import tempfile
    from pathlib import Path
    from somnia import settings as S

    tmp = Path(tempfile.mkdtemp()) / "settings.json"
    old_file, old_dir = S.FILE, S.DATA_DIR
    try:
        S.FILE, S.DATA_DIR = tmp, tmp.parent
        tmp.write_text("{ 坏的 json", encoding="utf-8")
        d = S.load()
        assert d["channel"] == S.DEFAULTS["channel"]
        tmp.write_text("[1,2,3]", encoding="utf-8")
        assert S.load()["name"] == S.DEFAULTS["name"]
    finally:
        S.FILE, S.DATA_DIR = old_file, old_dir


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
