"""事实记忆测试：去重、冲突退休、时间衰减、老库迁移。

跑法：python tests/test_facts.py
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store

DAY = 86400.0


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def _count(conn, where="retired = 0"):
    return conn.execute(f"SELECT COUNT(*) FROM facts WHERE {where}").fetchone()[0]


def test_exact_duplicate_merges():
    c = _db()
    assert store.add_fact(c, "我喜欢吃辣的") == "insert"
    assert store.add_fact(c, "我喜欢吃辣的") == "merge"
    assert _count(c) == 1


def test_near_duplicate_merges():
    c = _db()
    store.add_fact(c, "我喜欢吃辣的")
    assert store.add_fact(c, "我喜欢吃辣") == "merge"
    assert _count(c) == 1


def test_merge_keeps_higher_importance():
    c = _db()
    store.add_fact(c, "我明天要面试", importance=0.5)
    store.add_fact(c, "我明天要面试", importance=0.9)
    imp = c.execute("SELECT importance FROM facts").fetchone()[0]
    assert imp == 0.9


def test_punctuation_ignored_in_dedup():
    c = _db()
    store.add_fact(c, "我喜欢吃辣的")
    assert store.add_fact(c, "我喜欢吃辣的！！") == "merge"


def test_opposite_retires_old():
    c = _db()
    store.add_fact(c, "我喜欢香菜")
    assert store.add_fact(c, "我讨厌香菜") == "retire+insert"
    assert _count(c) == 1
    assert _count(c, "retired = 1") == 1
    assert store.recall(c, 5) == ["我讨厌香菜"]


def test_opposite_does_not_retire_unrelated():
    c = _db()
    store.add_fact(c, "我喜欢香菜")
    store.add_fact(c, "我讨厌下雨")
    assert _count(c) == 2


def test_recall_prefers_recent():
    c = _db()
    now = time.time()
    store.add_fact(c, "他三个月前说过怕黑", importance=0.8, now=now - 90 * DAY)
    store.add_fact(c, "他昨天说喜欢猫", importance=0.8, now=now - DAY)
    got = store.recall(c, 2, now=now)
    assert got[0] == "他昨天说喜欢猫"


def test_recall_prefers_important():
    c = _db()
    now = time.time()
    store.add_fact(c, "随便一句", importance=0.2, now=now)
    store.add_fact(c, "他讨厌香菜", importance=0.9, now=now)
    assert store.recall(c, 1, now=now) == ["他讨厌香菜"]


def test_decay_can_demote_old_important_fact():
    c = _db()
    now = time.time()
    store.add_fact(c, "很久以前的重要事", importance=0.85, now=now - 180 * DAY)
    store.add_fact(c, "昨天的小事", importance=0.5, now=now - DAY)
    assert store.recall(c, 1, now=now) == ["昨天的小事"]


def test_retired_never_recalled():
    c = _db()
    store.add_fact(c, "我喜欢香菜")
    store.add_fact(c, "我讨厌香菜")
    assert "我喜欢香菜" not in store.recall(c, 10)


def test_old_schema_is_migrated():
    d = tempfile.mkdtemp()
    p = Path(d) / "old.db"
    raw = sqlite3.connect(p)
    raw.executescript("""
    CREATE TABLE facts(
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, text TEXT NOT NULL,
        hits INTEGER DEFAULT 0);
    INSERT INTO facts(ts, text) VALUES(0, '旧数据');
    """)
    raw.commit()
    raw.close()

    conn = store.connect(str(p))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(facts)")}
    assert {"norm", "importance", "last_seen", "retired"} <= cols
    row = conn.execute("SELECT norm, retired FROM facts").fetchone()
    assert row["norm"] == "旧数据"
    assert row["retired"] == 0


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
