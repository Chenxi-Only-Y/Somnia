"""记忆测试：L2 日摘要、事实判定、上下文组装。

跑法：python tests/test_memory.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store, memory
from somnia.config import Config

CFG = Config()


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def _put(conn, role, text, ts):
    conn.execute("INSERT INTO messages(role, ts, text) VALUES(?,?,?)",
                 (role, ts, text))
    conn.commit()


def _ts(y, m, d, h=12):
    return time.mktime((y, m, d, h, 0, 0, 0, 0, -1))


def test_worth_remembering_marks_facts():
    assert memory.worth_remembering("我明天要面试")
    assert memory.worth_remembering("我讨厌香菜")
    assert not memory.worth_remembering("嗯")
    assert not memory.worth_remembering("今天天气不错啊哈哈哈")


def test_fallback_summary_deterministic():
    msgs = [("user", "我今天面试砸了"), ("assistant", "啊？"),
            ("user", "就是那个算法题没写出来")]
    a = memory._fallback(msgs)
    b = memory._fallback(msgs)
    assert a == b
    assert a.startswith("他说：")
    assert "面试" in a


def test_summarize_day_persists():
    c = _db()
    day = "2026-10-19"
    _put(c, "user", "我今天面试砸了", _ts(2026, 10, 19))
    text = memory.summarize_day(store, c, CFG, day)
    assert "面试" in text
    assert store.has_summary(c, day)


def test_summarize_empty_day_returns_blank():
    c = _db()
    assert memory.summarize_day(store, c, CFG, "1999-01-01") == ""
    assert not store.has_summary(c, "1999-01-01")


def test_catch_up_skips_today():
    c = _db()
    today = memory.day_key()
    _put(c, "user", "昨天的具体事", time.time() - 86400)
    _put(c, "user", "今天的具体事", time.time())
    done = memory.catch_up(store, c, CFG)
    assert today not in done
    assert not store.has_summary(c, today)
    assert len(done) <= 1


def test_context_returns_three_layers():
    c = _db()
    store.add_message(c, "user", "在吗")
    store.add_fact(c, "他喜欢辣")
    store.add_summary(c, "2026-10-18", "他说要早起")
    history, facts, summaries, recall = memory.context(store, c, CFG)
    assert history and facts
    assert summaries and summaries[0][0] == "2026-10-18"


def test_summaries_since_is_chronological():
    c = _db()
    for d in ("2026-10-17", "2026-10-18", "2026-10-19"):
        store.add_summary(c, d, f"{d} 的事")
    got = store.summaries_since(c, 2)
    assert [d for d, _ in got] == ["2026-10-18", "2026-10-19"]


# ── 不重要的事别反复提 ───────────────────────────────────

def test_trim_drops_recently_mentioned():
    facts = ["他今天要去看牙", "他住杭州"]
    history = [("assistant", "你那个牙看了没")]
    got = memory.trim_facts(facts, history=history, user_text="嗯")
    assert "他今天要去看牙" not in got
    assert "他住杭州" in got


def test_trim_prioritizes_relevant():
    facts = ["他喜欢吃辣的", "他养了只橘猫", "他住杭州"]
    got = memory.trim_facts(facts, history=[], user_text="你那只猫怎么样了")
    assert got[0] == "他养了只橘猫"


def test_trim_caps_length():
    facts = [f"事实第{i}号内容" for i in range(30)]
    assert len(memory.trim_facts(facts, keep=12)) == 12


def test_trim_empty():
    assert memory.trim_facts([]) == []
    assert memory.trim_facts(["他住杭州"], keep=5) == ["他住杭州"]


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
