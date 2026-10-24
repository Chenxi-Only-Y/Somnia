"""存储层测试：幂等、顺序、事实命中、决策日志。

跑法：python tests/test_store.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store


def _fresh():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def test_msg_id_is_idempotent():
    c = _fresh()
    assert store.add_message(c, "user", "在吗", "m1") is True
    assert store.add_message(c, "user", "在吗", "m1") is False
    assert store.seen(c, "m1") is True
    assert store.seen(c, "m2") is False


def test_message_without_id_always_inserts():
    c = _fresh()
    assert store.add_message(c, "user", "a") is True
    assert store.add_message(c, "user", "a") is True


def test_recent_is_chronological():
    c = _fresh()
    for i in range(5):
        store.add_message(c, "user", f"m{i}")
    texts = [t for _, t in store.recent(c, 3)]
    assert texts == ["m2", "m3", "m4"], texts


def test_facts_recall_bumps_hits():
    c = _fresh()
    store.add_fact(c, "她怕黑")
    store.add_fact(c, "她讨厌香菜")
    store.recall(c, 1)
    store.recall(c, 1)
    row = c.execute("SELECT text, hits FROM facts ORDER BY hits DESC LIMIT 1").fetchone()
    assert row["hits"] >= 1


def test_decisions_logged():
    c = _fresh()
    store.log_decision(c, "m1", "reply", 8.3, 2, "planned")
    store.log_decision(c, "m2", "skip", 0, 1, "reply_rate_sample")
    assert store.decision_stats(c) == {"reply": 1, "skip": 1}


def test_quiet_counts_as_decision():
    c = _fresh()
    store.log_decision(c, None, "quiet", 0, 1, "quiet_hours 2-9")
    assert store.decision_stats(c)["quiet"] == 1


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
