"""分层记忆：三天细节 / 一周印象 / 一个月档案。

跑法：python tests/test_archive.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import aging, archive, store
from somnia.config import Config

DAY = 86400


def _rows(now, days_ago, n=4, base="聊天"):
    t0 = now - days_ago * DAY
    return [("user" if i % 2 == 0 else "assistant", f"{base}{i}", t0 + i * 60)
            for i in range(n)]


def _db():
    d = Path(tempfile.mkdtemp())
    c = store.connect(str(d / "t.db"))
    return c, d


def _add(c, rows):
    for i, (role, text, ts) in enumerate(rows):
        c.execute("INSERT INTO messages(msg_id,role,ts,text) VALUES(?,?,?,?)",
                  (f"m{ts}-{i}", role, ts, text))
    c.commit()


# ── 分段 ────────────────────────────────────────────────

def test_split_segments_by_gap():
    rows = [("user", "a", 0), ("assistant", "b", 60),
            ("user", "c", 60 + 31 * 60)]        # 隔了 31 分钟
    segs = archive.split_segments(rows)
    assert len(segs) == 2


def test_split_single_segment():
    rows = [("user", "a", 0), ("assistant", "b", 60)]
    assert len(archive.split_segments(rows)) == 1


# ── 档案读写 ────────────────────────────────────────────

def test_append_and_load(tmpdir=None):
    d = Path(tempfile.mkdtemp())
    rows = _rows(time.time(), 5)
    segs = archive.split_segments(rows)
    n = archive.append_segments(segs, ["那天他加班到很晚"], base=d)
    assert n == 1
    got = archive.load(base=d)
    assert len(got) == 1
    assert got[0]["lines"][0][1] == rows[0][1]
    assert got[0]["sum"] == "那天他加班到很晚"


def test_search_by_keyword():
    d = Path(tempfile.mkdtemp())
    rows = [("user", "我今天面试完了", 1000),
            ("assistant", "怎么样", 1060),
            ("user", "还行，等通知", 1120)]
    archive.append_segments(archive.split_segments(rows), ["他面试完了"], base=d)
    assert archive.search("面试怎么样", base=d)
    assert archive.search("完全不相关的天气", base=d, min_hit=3) == []


def test_find_segment():
    d = Path(tempfile.mkdtemp())
    rows = [("user", "猫又吐了", 5000), ("assistant", "带去看看吧", 5060)]
    archive.append_segments(archive.split_segments(rows), ["猫吐了"], base=d)
    seg = archive.load(base=d)[0]
    assert archive.find(seg["seg"], base=d)["sum"] == "猫吐了"


def test_render_has_sum_and_lines():
    d = Path(tempfile.mkdtemp())
    rows = [("user", "在吗", 1), ("assistant", "在", 2)]
    archive.append_segments(archive.split_segments(rows), ["打了个招呼"], base=d)
    text = archive.render(archive.load(base=d)[0])
    assert "打了个招呼" in text
    assert "你：在吗" in text or "ta：在" in text


# ── 归档（三天前的原文搬走）────────────────────────────

def test_roll_archives_old_and_keeps_recent():
    c, d = _db()
    now = time.time()
    _add(c, _rows(now, 5))          # 5 天前
    _add(c, _rows(now, 1, base="新"))  # 1 天前

    out = aging.roll(store, c, Config(hot_days=3), llm=None, now=now, base=d)
    assert out["archived"] == 1
    left = [t for _, t in store.recent(c, 50)]
    assert all(t.startswith("新") for t in left)      # 近的还在
    assert archive.load(base=d)                        # 远的进档案


def test_roll_writes_impression():
    c, d = _db()
    now = time.time()
    _add(c, _rows(now, 4))
    aging.roll(store, c, Config(hot_days=3), llm=None, now=now, base=d)
    imps = store.summaries_since(c, 10)
    assert imps and imps[-1][1].strip()


def test_roll_prunes_old_summaries():
    c, d = _db()
    now = time.time()
    store.add_summary(c, "2020-01-01", "很久以前")
    aging.roll(store, c, Config(hot_days=3, summary_keep_days=30),
               llm=None, now=now, base=d)
    days = [day for day, _ in store.summaries_since(c, 100)]
    assert "2020-01-01" not in days


def test_roll_uses_model_impression():
    c, d = _db()
    now = time.time()
    _add(c, _rows(now, 6))
    aging.roll(store, c, Config(hot_days=3), llm=lambda p: "他说面试还行", now=now, base=d)
    assert "面试" in store.summaries_since(c, 5)[-1][1]


# ── 检索（想起来了）─────────────────────────────────────

def test_context_recalls_old_segment():
    from somnia import memory
    c, d = _db()
    now = time.time()
    _add(c, [("user", "我家猫又吐毛球了", now - 20 * DAY),
             ("assistant", "喂点化毛膏", now - 20 * DAY + 60)])
    aging.roll(store, c, Config(hot_days=3), llm=None, now=now, base=d)

    _h, _f, _s, recall = memory.context(
        store, c, Config(hot_days=3), user_text="还记得我家猫吗", base=d)
    assert "猫" in recall


def test_context_no_recall_for_small_talk():
    from somnia import memory
    c, d = _db()
    _h, _f, _s, recall = memory.context(
        store, c, Config(hot_days=3), user_text="吃了没", base=d)
    assert recall == ""


def test_index_cache_sees_new_archives():
    """缓存不能让新写的档案看不见 —— 追加之后必须能立刻搜到。"""
    import tempfile
    from pathlib import Path
    base = Path(tempfile.mkdtemp())

    seg = [[("user", "我家的猫又吐毛球了", 1792000000.0),
            ("assistant", "喂点化毛膏吧", 1792000060.0)]]
    archive.append_segments(seg, ["他家的猫吐毛球"], base=base)
    assert archive.search("还记得我家猫吗", base=base), "第一段应该能搜到"

    seg2 = [[("user", "今天去打了羽毛球，累死了", 1792100000.0),
             ("assistant", "羽毛球别一下打太猛", 1792100060.0)]]
    archive.append_segments(seg2, ["他打了羽毛球"], base=base)
    hit = archive.search("上次那个羽毛球", base=base)
    assert hit, "新追加的段落必须能被搜到（缓存要失效）"
    assert any("羽毛球" in (x.get("sum") or "") for x in hit), "搜到的应该是新那段"


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
