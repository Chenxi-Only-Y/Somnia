"""重启后的记忆：停机期间的消息要补回来，删过的不能再复活。

跑法：python tests/test_restore.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store
from somnia.channel.wechatauto import RawMsg, select_replay


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def _msg(key, ts, self_sent=False):
    return RawMsg(text=f"t{key}", key=key, self_sent=self_sent, ts=ts)


# ── 删掉的不能复活 ───────────────────────────────────────

def test_deleted_stays_deleted():
    c = _db()
    assert store.add_message(c, "user", "别记这句", "wx:1") is True
    row = c.execute("select id from messages where msg_id='wx:1'").fetchone()
    assert store.delete_message(c, row["id"]) is True
    # 通道重启后重新读到同一条，不该再写回来
    assert store.add_message(c, "user", "别记这句", "wx:1") is False
    assert c.execute("select count(*) n from messages").fetchone()["n"] == 0


def test_tombstone_is_per_message():
    c = _db()
    store.add_message(c, "user", "a", "wx:1")
    store.add_message(c, "user", "b", "wx:2")
    row = c.execute("select id from messages where msg_id='wx:1'").fetchone()
    store.delete_message(c, row["id"])
    assert store.add_message(c, "user", "b", "wx:2") is False   # msg_id 已存在
    assert store.is_deleted(c, "wx:1") is True
    assert store.is_deleted(c, "wx:2") is False


def test_delete_missing_returns_false():
    c = _db()
    assert store.delete_message(c, 12345) is False


# ── 停机期间的消息要补回 ─────────────────────────────────

def test_replay_picks_messages_after_watermark():
    now = time.time()
    since = now - 3600
    msgs = [_msg("a", now - 7200), _msg("b", now - 600), _msg("c", now - 60)]
    got = select_replay(msgs, since, now=now)
    assert [m.key for m in got] == ["b", "c"]


def test_replay_skips_self_sent():
    now = time.time()
    msgs = [_msg("b", now - 600), _msg("mine", now - 300, self_sent=True)]
    got = select_replay(msgs, now - 3600, now=now)
    assert [m.key for m in got] == ["b"]


def test_replay_respects_limit():
    now = time.time()
    msgs = [_msg(str(i), now - 100 + i) for i in range(20)]
    got = select_replay(msgs, now - 3600, now=now, limit=5)
    assert len(got) == 5
    assert got[-1].key == "19"          # 取最近的


def test_replay_skips_too_old():
    now = time.time()
    msgs = [_msg("old", now - 3 * 86400), _msg("new", now - 60)]
    got = select_replay(msgs, now - 7 * 86400, now=now, age=12 * 3600)
    assert [m.key for m in got] == ["new"]


def test_no_watermark_means_no_replay():
    """第一次跑、库里什么都没有时，不能把历史全当新消息。"""
    now = time.time()
    msgs = [_msg("a", now - 60)]
    assert select_replay(msgs, 0.0, now=now) == []


def test_last_message_ts():
    c = _db()
    assert store.last_message_ts(c) == 0.0
    store.add_message(c, "user", "hi", "wx:1")
    assert store.last_message_ts(c) > time.time() - 5


def test_bulk_delete():
    c = _db()
    for i in range(3):
        store.add_message(c, "user", f"m{i}", f"wx:{i}")
    ids = [r["id"] for r in c.execute("select id from messages")]
    assert store.delete_messages(c, ids) == 3
    assert c.execute("select count(*) n from messages").fetchone()["n"] == 0
    # 删掉的同样不许复活
    assert store.add_message(c, "user", "m0", "wx:0") is False


def test_bulk_delete_ignores_bad_ids():
    c = _db()
    store.add_message(c, "user", "x", "wx:1")
    ids = [r["id"] for r in c.execute("select id from messages")]
    assert store.delete_messages(c, ids + [999999, "bad", None]) == 1


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
