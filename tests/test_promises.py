"""承诺：她说了"晚上找你"，到点必须来。

跑法：python tests/test_promises.py
"""

import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import promises, scheduler, store
from somnia.config import Config


class Rng:
    def __init__(self, value=0.9):
        self.value = value

    def random(self):
        return self.value

    def choice(self, seq):
        return seq[0]


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


NOW = datetime.datetime(2026, 10, 21, 15, 0).timestamp()


# ── 认得出承诺 ───────────────────────────────────────────

def test_detects_evening_promise():
    got = promises.detect("行，晚上找你")
    assert got and "晚上" in got[0][0]


def test_detects_later_promise():
    got = promises.detect("晚点跟你说个事")
    assert got and got[0][0].startswith("晚点")


def test_ignores_plain_chat():
    assert promises.detect("我先去忙了") == []
    assert promises.detect("嗯好") == []
    assert promises.detect("") == []


# ── 兑现时间 ─────────────────────────────────────────────

def test_evening_due_is_tonight():
    due = promises.due_at("晚上", now=NOW)
    t = datetime.datetime.fromtimestamp(due)
    assert t.hour == 20 and t.minute == 30
    assert t.date() == datetime.date(2026, 10, 21)


def test_evening_due_after_window_rolls_forward():
    """晚上 21 点还说"晚上找你"，就是半小时后。"""
    late = datetime.datetime(2026, 10, 21, 21, 0).timestamp()
    due = promises.due_at("晚上", now=late)
    assert due - late == 30 * 60


def test_soon_due_is_under_an_hour():
    assert promises.due_at("一会儿", now=NOW) - NOW == 45 * 60


def test_tomorrow_due_is_ten_am():
    t = datetime.datetime.fromtimestamp(promises.due_at("明天", now=NOW))
    assert t.day == 22 and t.hour == 10


# ── 存储 ─────────────────────────────────────────────────

def test_add_and_due():
    c = _db()
    store.add_promise(c, "晚上找你", NOW - 1)
    got = store.due_promise(c, NOW)
    assert got and got["text"] == "晚上找你"


def test_not_due_yet():
    c = _db()
    store.add_promise(c, "明天告诉你", NOW + 3600)
    assert store.due_promise(c, NOW) is None


def test_close_promise():
    c = _db()
    store.add_promise(c, "晚上找你", NOW - 1)
    got = store.due_promise(c, NOW)
    store.close_promise(c, got["id"])
    assert store.due_promise(c, NOW) is None


def test_user_message_cancels_promises():
    """她已经在跟他讲了，"晚上找你"就不必再补。"""
    c = _db()
    store.add_promise(c, "晚上找你", NOW + 3600)
    assert store.cancel_promises(c) == 1
    assert store.pending_promises(c) == []


# ── 到点真的会说 ─────────────────────────────────────────

def test_tick_fires_promise_even_without_silence():
    c = _db()
    now = datetime.datetime(2026, 10, 21, 15, 0)
    # 他刚刚才说过话（远没到沉默门槛），但承诺到点了
    store.add_message(c, "user", "我先忙", "wx:1")
    store.add_promise(c, "晚上找你", now.timestamp() - 60)

    r = scheduler.tick(store, c, Config(), now, rng=Rng(0.5))
    assert r is not None
    thought, score, reason, oid = r
    assert reason == "promise"
    assert store.pending_promises(c) == []


def test_promise_waits_for_quiet_hours():
    """睡觉时间不兑现承诺，醒了再说。"""
    c = _db()
    night = datetime.datetime(2026, 10, 21, 3, 0)
    store.add_promise(c, "晚上找你", night.timestamp() - 60)
    r = scheduler.tick(store, c, Config(), night, rng=Rng(0.99))
    assert r is None
    assert len(store.pending_promises(c)) == 1


def test_promise_fires_ninety_percent():
    """掷 0.5（< 0.9）按时兑现。"""
    c = _db()
    now = datetime.datetime(2026, 10, 21, 15, 0)
    store.add_promise(c, "晚上找你", now.timestamp() - 60)
    r = scheduler.tick(store, c, Config(), now, rng=Rng(0.5))
    assert r is not None and r[2] == "promise"


def test_promise_may_be_late():
    """掷 0.95（>= 0.9）：这次想不起来，往后推，标记成迟了。"""
    c = _db()
    now = datetime.datetime(2026, 10, 21, 15, 0)
    # 他刚说过话，排除"沉默触发"的干扰
    store.add_message(c, "user", "我在忙", "wx:9")
    store.add_promise(c, "晚上找你", now.timestamp() - 60)
    r = scheduler.tick(store, c, Config(), now, rng=Rng(0.95))
    assert r is None
    left = store.pending_promises(c)
    assert len(left) == 1
    assert left[0]["due"] > now.timestamp()
    row = c.execute("SELECT late FROM promises WHERE id=?", (left[0]["id"],)).fetchone()
    assert row["late"] == 1


def test_late_promise_still_fires_eventually():
    c = _db()
    now = datetime.datetime(2026, 10, 21, 15, 0)
    store.add_promise(c, "晚上找你", now.timestamp() - 60)
    scheduler.tick(store, c, Config(), now, rng=Rng(0.95))     # 推后
    later = datetime.datetime(2026, 10, 21, 20, 0)
    r = scheduler.tick(store, c, Config(), later, rng=Rng(0.5))
    assert r is not None and r[2] == "promise"
    assert store.pending_promises(c) == []


def test_late_promise_blames_business_not_memory():
    """迟到的那次要说是被事情绊住了，不是"忘了"。"""
    c = _db()
    now = datetime.datetime(2026, 10, 21, 15, 0)
    store.add_message(c, "user", "我在忙", "wx:9")
    store.add_promise(c, "晚上找你", now.timestamp() - 60)
    scheduler.tick(store, c, Config(), now, rng=Rng(0.95))     # 先推后

    seen = {}

    def fake(prompt):
        seen["p"] = prompt
        return "刚忙完，你睡了吗"

    later = datetime.datetime(2026, 10, 21, 20, 0)
    r = scheduler.tick(store, c, Config(), later, rng=Rng(0.5), llm=fake)
    assert r is not None and r[2] == "promise"
    assert "绊着" in seen["p"]
    assert "忘了" in seen["p"]          # 只在"别说自己忘了"里出现
    assert "别说自己忘了" in seen["p"]


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
