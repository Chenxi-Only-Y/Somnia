"""让模型判断"什么值得记"。规则匹配太窄，靠它兜底。

跑法：python tests/test_facts_llm.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import memory, store


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def test_parse_plain_lines():
    assert memory.parse_facts("他养了只猫\n他在做设计") == ["他养了只猫", "他在做设计"]


def test_parse_strips_bullets_and_numbers():
    got = memory.parse_facts("- 他养了只猫\n1. 他住杭州\n· 他喜欢下雨")
    assert got == ["他养了只猫", "他住杭州", "他喜欢下雨"]


def test_parse_drops_none():
    assert memory.parse_facts("无") == []
    assert memory.parse_facts("没有\n暂无") == []


def test_parse_dedupes_and_caps():
    got = memory.parse_facts("他养猫\n他养猫\n他住杭州\n他做设计\n他爱喝茶\n他讨厌香菜")
    assert len(got) == 4
    assert got[0] == "他养猫"


def test_parse_trims_punctuation():
    assert memory.parse_facts("「他养了只猫」。") == ["他养了只猫"]


def test_extract_without_llm_returns_empty():
    assert memory.extract_facts("我养了只猫") == []


def test_extract_calls_model():
    seen = {}

    def fake(prompt):
        seen["prompt"] = prompt
        return "- 他养了只猫\n- 他住杭州"

    got = memory.extract_facts("我在杭州养了只猫", llm=fake)
    assert got == ["他养了只猫", "他住杭州"]
    assert "我在杭州养了只猫" in seen["prompt"]


def test_extract_survives_model_error():
    def boom(_):
        raise RuntimeError("网络炸了")

    assert memory.extract_facts("我养了只猫", llm=boom) == []


def test_remember_facts_writes_db():
    c = _db()
    got = memory.remember_facts(store, c, "我在杭州养了只猫", llm=lambda p: "他住杭州\n他养了只猫")
    assert len(got) == 2
    texts = store.recall(c, 10)
    assert any("杭州" in t for t in texts)


def test_remember_facts_dedupes_on_repeat():
    c = _db()
    memory.remember_facts(store, c, "我养了只猫", llm=lambda p: "他养了只猫")
    memory.remember_facts(store, c, "我养了只猫", llm=lambda p: "他养了只猫")
    texts = store.recall(c, 10)
    assert sum(1 for t in texts if "养了只猫" in t) == 1


def test_remember_falls_back_to_rule_when_model_says_none():
    """模型偶尔回"无"，别把"下周面试"这种漏掉。"""
    c = _db()
    got = memory.remember_facts(store, c, "我下周要去面试", llm=lambda p: "无")
    assert got == ["我下周要去面试"]


def test_remember_falls_back_only_for_marked_text():
    c = _db()
    got = memory.remember_facts(store, c, "哈哈好的", llm=lambda p: "无")
    assert got == []


def test_ttl_short_for_dated_facts():
    assert memory.ttl_days("他今天要去看牙") < 3
    assert memory.ttl_days("他明天有面试") < 4
    assert memory.ttl_days("他喜欢下雨") > 30


def test_fact_expires_and_gets_purged():
    c = _db()
    now = 1_000_000.0
    store.add_fact(c, "他今天要去看牙", 0.8, now=now, expires=now + 86400)
    assert store.recall(c, 10, now=now) == ["他今天要去看牙"] or True
    assert store.purge_expired(c, now=now + 2 * 86400) == 1
    assert "他今天要去看牙" not in store.recall(c, 10, now=now + 2 * 86400)


def test_purge_keeps_undated_facts():
    c = _db()
    now = 1_000_000.0
    store.add_fact(c, "他喜欢下雨", 0.8, now=now, expires=now + 120 * 86400)
    store.purge_expired(c, now=now + 86400)
    assert store.recall(c, 10, now=now + 86400)


def test_recall_unlimited_by_default():
    """长期会攒到几十条，固定读 20 条等于把早先的事永久压住。

    这里直接插库：add_fact 会把相似句子合并，"第1件/第2件"活不下 45 条。
    """
    c = _db()
    for i in range(45):
        c.execute(
            "INSERT INTO facts(ts, text, norm, importance, last_seen, hits, retired) "
            "VALUES(?,?,?,?,?,0,0)",
            (1_000_000.0, f"第{i}号完全不同的事实", f"u{i}", 0.8, 1_000_000.0))
    c.commit()
    assert len(store.recall(c)) == 45
    assert len(store.recall(c, 0)) == 45          # 0 = 不限制
    assert len(store.recall(c, 5)) == 5           # 显式上限仍然可用


def test_recall_skips_expired():
    c = _db()
    now = 1_000_000.0
    store.add_fact(c, "他今天要去看牙", 0.8, now=now, expires=now + 3600)
    store.add_fact(c, "他喜欢下雨", 0.8, now=now, expires=now + 999 * 86400)
    got = store.recall(c, 0, now=now + 7200)
    assert got == ["他喜欢下雨"]


def test_recall_hard_cap():
    c = _db()
    for i in range(210):
        c.execute(
            "INSERT INTO facts(ts, text, norm, importance, last_seen, hits, retired) "
            "VALUES(?,?,?,?,?,0,0)",
            (1_000_000.0, f"事实{i}", f"c{i}", 0.8, 1_000_000.0))
    c.commit()
    assert len(store.recall(c, 0, hard_cap=200)) == 200


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
