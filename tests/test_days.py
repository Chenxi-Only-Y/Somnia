"""记忆按天分开：L1 按天数取，面板按天分组。

跑法：python tests/test_days.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import memory, store
from somnia.config import Config

DAY = 86400


def _db_with_history():
    d = tempfile.mkdtemp()
    c = store.connect(str(Path(d) / "t.db"))
    now = time.time()
    for i in range(2):
        c.execute("INSERT INTO messages(msg_id,role,ts,text) VALUES(?,?,?,?)",
                  (f"t{i}", "user", now - 60 * i, f"今天{i}"))
    for i in range(2):
        c.execute("INSERT INTO messages(msg_id,role,ts,text) VALUES(?,?,?,?)",
                  (f"y{i}", "user", now - DAY - 60 * i, f"昨天{i}"))
    c.execute("INSERT INTO messages(msg_id,role,ts,text) VALUES(?,?,?,?)",
              ("o1", "user", now - 3 * DAY, "三天前"))
    c.execute("INSERT INTO messages(msg_id,role,ts,text) VALUES(?,?,?,?)",
              ("o2", "user", now - 10 * DAY, "十天前"))
    c.commit()
    return c


def test_recent_days_cuts_by_date():
    c = _db_with_history()
    got = [t for _, t, _ in store.recent_days(c, days=2)]
    assert got == ["今天0", "今天1", "昨天0", "昨天1"]
    assert "三天前" not in got


def test_recent_days_three():
    """days=3 是"今天 + 昨天 + 前天"，三天前的不算。"""
    c = _db_with_history()
    got = [t for _, t, _ in store.recent_days(c, days=3)]
    assert "昨天0" in got
    assert "三天前" not in got


def test_recent_days_four_includes_older():
    c = _db_with_history()
    got = [t for _, t, _ in store.recent_days(c, days=4)]
    assert "三天前" in got
    assert "十天前" not in got


def test_recent_days_respects_limit():
    c = _db_with_history()
    assert len(store.recent_days(c, days=30, limit=3)) == 3


def test_messages_by_day_groups_and_offsets():
    c = _db_with_history()
    groups = store.messages_by_day(c, days=7)
    assert [g["count"] for g in groups] == [2, 2, 1]     # 今天 / 昨天 / 三天前
    assert groups[0]["offset"] == 0
    assert groups[1]["offset"] == 1
    assert groups[2]["offset"] == 3                       # 前天没消息，跳过
    assert groups[0]["items"][0]["text"] == "今天0"
    assert "time" in groups[0]["items"][0]


def test_context_uses_days_window():
    c = _db_with_history()
    cfg = Config(recent_days=2, recent_n=200, recall_k=0)
    history, _facts, _summaries, _recall = memory.context(store, c, cfg, user_text="在吗")
    texts = [t for _, t, _ in history]
    assert "今天0" in texts and "昨天0" in texts
    assert "三天前" not in texts


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
