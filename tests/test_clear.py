"""清空记忆：先备份，再删干净。

跑法：python tests/test_clear.py
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store


def _db():
    d = Path(tempfile.mkdtemp())
    c = store.connect(str(d / "t.db"))
    store.add_message(c, "user", "你好", "wx:1")
    store.add_message(c, "assistant", "嗯", "wx:2")
    store.add_fact(c, "他养了只猫")
    store.log_decision(c, None, "reply", 1.0, 1, "planned")
    store.outbox_add(c, "想问问他今天怎么样", 0.8)
    store.add_promise(c, "晚上找你", 0)
    store.add_summary(c, "2026-10-23", "聊了猫")
    return c, d


def test_clear_empties_everything():
    c, _ = _db()
    n = store.clear_all(c)
    assert n["messages"] == 2
    assert n["facts"] == 1
    for t in store.TABLES:
        assert c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] == 0


def test_backup_keeps_old_data():
    c, d = _db()
    bk = d / "backup"
    n = store.clear_all(c, backup_dir=str(bk))
    path = Path(n["_backup"])
    assert path.exists() and path.parent == bk
    bc = sqlite3.connect(str(path))
    try:
        assert bc.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert bc.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    finally:
        bc.close()


def test_clear_is_idempotent():
    c, _ = _db()
    store.clear_all(c)
    again = store.clear_all(c)
    assert again["messages"] == 0 and again["facts"] == 0


def test_after_clear_no_since_ts():
    """清空后 last_message_ts 归零 —— 通道不会把旧消息补回来。"""
    c, _ = _db()
    store.clear_all(c)
    assert store.last_message_ts(c) == 0.0


def test_clear_sets_watermark():
    """清空后要留水位线，否则重启期间的消息会被当成"已经处理过"。"""
    c, _ = _db()
    store.clear_all(c)
    assert store.watermark(c) > 0
    assert store.since_ts(c) == store.watermark(c)


def test_since_ts_takes_later_of_two():
    d = tempfile.mkdtemp()
    c = store.connect(str(Path(d) / "empty.db"))       # 一张干净的库，没有消息
    now = time.time()
    store.set_watermark(c, now - 100)
    assert abs(store.since_ts(c) - (now - 100)) < 5    # 没有消息时用水位线
    store.add_message(c, "user", "新的", "wx:new")
    assert abs(store.since_ts(c) - time.time()) < 5    # 有消息时用消息时间


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
