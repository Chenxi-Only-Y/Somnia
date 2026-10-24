"""补回复：她当时没回，过几分钟自己补一句。

跑法：python tests/test_catchup.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store
from somnia.app import maybe_catchup, needs_catchup
from somnia.config import Config

CFG = Config(quiet_hours=(0, 0), catchup_delay_s=240,
             catchup_max_age_s=3 * 3600, seg_gap=(0, 0))


class FakeCh:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def _seed(conn, age_s, answered=False):
    now = time.time()
    conn.execute("INSERT INTO messages(msg_id, role, ts, text) VALUES(?,?,?,?)",
                 ("u1", "user", now - age_s, "今天好累"))
    if answered:
        conn.execute("INSERT INTO messages(msg_id, role, ts, text) VALUES(?,?,?,?)",
                     (None, "assistant", now - age_s + 3, "那就早点睡"))
    conn.commit()


# ── 判断 ─────────────────────────────────────────────────

def test_no_message_no_catchup():
    assert not needs_catchup(None, None, time.time(), 240, 10800)


def test_already_answered():
    now = time.time()
    assert not needs_catchup(now - 100, now - 50, now, 240, 10800)


def test_too_early():
    now = time.time()
    assert not needs_catchup(now - 100, now - 500, now, 240, 10800)


def test_due():
    now = time.time()
    assert needs_catchup(now - 300, now - 500, now, 240, 10800)


def test_too_old():
    now = time.time()
    assert not needs_catchup(now - 5 * 3600, now - 6 * 3600, now, 240, 10800)


# ── 真的补 ───────────────────────────────────────────────

def test_catchup_sends_and_records():
    c = _db()
    _seed(c, 600)
    ch = FakeCh()
    ok = maybe_catchup(CFG, c, lambda p: "刚在忙|怎么了", ch,
                       threading.Lock(), now=time.time())
    assert ok is True
    assert ch.sent == ["刚在忙", "怎么了"]
    # 补完就算"她这边有回应了"，不会重复补
    assert store.last_assistant_ts(c) >= store.last_user_ts(c)
    assert not maybe_catchup(CFG, c, lambda p: "再来一句", ch,
                            threading.Lock(), now=time.time() + 9000)


def test_catchup_logs_decision():
    """补回复也要留记录，否则面板上像"凭空冒出一条消息"。"""
    c = _db()
    _seed(c, 600)
    maybe_catchup(CFG, c, lambda p: "刚看到", FakeCh(), threading.Lock(),
                  now=time.time())
    row = c.execute("SELECT act, reason FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["act"] == "reply"
    assert row["reason"] == "catchup"


def test_catchup_skips_when_answered():
    c = _db()
    _seed(c, 600, answered=True)
    ch = FakeCh()
    assert maybe_catchup(CFG, c, lambda p: "不该发", ch,
                         threading.Lock(), now=time.time()) is False
    assert ch.sent == []


def test_catchup_skips_when_too_late():
    c = _db()
    _seed(c, 5 * 3600)
    ch = FakeCh()
    assert maybe_catchup(CFG, c, lambda p: "不该发", ch,
                         threading.Lock(), now=time.time()) is False


def test_catchup_tolerates_empty_generation():
    c = _db()
    _seed(c, 600)
    ch = FakeCh()
    assert maybe_catchup(CFG, c, lambda p: "", ch,
                         threading.Lock(), now=time.time()) is False
    assert ch.sent == []


def test_catchup_respects_quiet_hours():
    c = _db()
    _seed(c, 600)
    ch = FakeCh()
    quiet = Config(quiet_hours=(0, 24), catchup_delay_s=240)   # 全天静默
    assert maybe_catchup(quiet, c, lambda p: "不该发", ch,
                         threading.Lock(), now=time.time()) is False


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
