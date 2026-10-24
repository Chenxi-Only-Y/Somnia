"""决策层测试。这层不调模型，所以完全可测。

跑法：python tests/test_brain.py
"""

import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia.brain import plan
from somnia.config import Config


def test_quiet_hours_silent():
    d = plan(Config(reply_rate=1.0), datetime(2026, 1, 1, 3, 0), random.Random(0))
    assert d.act == "quiet", d


def test_quiet_hours_boundary_exit():
    # 7 点整应该已经醒了
    d = plan(Config(reply_rate=1.0), datetime(2026, 1, 1, 7, 0), random.Random(0))
    assert d.act != "quiet", d


def test_quiet_hours_ends_at_seven():
    d = plan(Config(reply_rate=1.0), datetime(2026, 1, 1, 6, 59), random.Random(0))
    assert d.act == "quiet", d


def test_never_replies_when_rate_zero():
    for seed in range(30):
        d = plan(Config(reply_rate=0.0), datetime(2026, 1, 1, 15, 0), random.Random(seed))
        assert d.act == "skip", (seed, d)


def test_always_replies_when_rate_one():
    for seed in range(30):
        d = plan(Config(reply_rate=1.0), datetime(2026, 1, 1, 15, 0), random.Random(seed))
        assert d.act == "reply", (seed, d)


def test_delay_within_bounds():
    for seed in range(100):
        d = plan(Config(reply_rate=1.0), datetime(2026, 1, 1, 15, 0), random.Random(seed))
        assert 3.0 <= d.delay <= Config().delay_max, d.delay
        assert 1 <= d.segments <= 3, d.segments


def test_delay_respects_cap():
    cfg = Config(reply_rate=1.0, delay_median=30.0, delay_max=12.0)
    for seed in range(100):
        d = plan(cfg, datetime(2026, 1, 1, 15, 0), random.Random(seed))
        assert d.delay <= 12.0, d.delay


def test_delay_has_spread():
    # 延迟必须是分布的，不能是常数；但也不该长尾到没有上限
    ds = [plan(Config(reply_rate=1.0), datetime(2026, 1, 1, 15, 0), random.Random(s)).delay
          for s in range(200)]
    assert max(ds) - min(ds) > 5, (min(ds), max(ds))
    assert max(ds) <= Config().delay_max


def test_consecutive_skip_is_prevented():
    """连着两次不回，看起来就是坏了，不是"在忙"。"""
    class R:
        def random(self): return 0.99
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    now = datetime(2026, 1, 1, 15, 0)
    cfg = Config(reply_rate=0.88)
    assert plan(cfg, now, R()).act == "skip"
    assert plan(cfg, now, R(), last_act="skip").act == "reply"
    assert plan(cfg, now, R(), last_act="reply").act == "skip"


def test_question_gets_higher_rate():
    class R:
        def __init__(self, v): self.v = v
        def random(self): return self.v
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    now = datetime(2026, 1, 1, 15, 0)
    cfg = Config(reply_rate=0.88)
    # 0.93 在 reply_rate(0.88) 之上、提问加成后(0.96) 之下
    assert plan(cfg, now, R(0.93)).act == "skip"
    assert plan(cfg, now, R(0.93), asking=True).act == "reply"


def test_woken_at_night_by_chance():
    """睡着也可能被吵醒，但只醒一次。"""
    class R:
        def __init__(self, v): self.v = v
        def random(self): return self.v
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    cfg = Config(wake_chance=0.06, wake_max_per_night=1)
    night = datetime(2026, 1, 1, 3, 30)
    d = plan(cfg, night, R(0.01))
    assert d.act == "reply" and d.reason == "woken"
    assert d.segments == 1 and d.delay >= 8

    # 没被吵醒就继续睡
    assert plan(cfg, night, R(0.99)).act == "quiet"
    # 一晚只醒一次
    assert plan(cfg, night, R(0.01), woken_tonight=1).act == "quiet"


def test_no_night_wake_when_disabled():
    class R:
        def random(self): return 0.0
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    cfg = Config(wake_chance=0.0)
    assert plan(cfg, datetime(2026, 1, 1, 3, 30), R()).act == "quiet"


def test_night_wake_unlimited_by_default():
    """wake_max_per_night=0 表示不限次数。"""
    class R:
        def __init__(self, v): self.v = v
        def random(self): return self.v
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    cfg = Config(wake_chance=0.10, wake_max_per_night=0)
    night = datetime(2026, 1, 1, 3, 30)
    for _ in range(10):
        assert plan(cfg, night, R(0.05), woken_tonight=5).act == "reply"


def test_wake_chance_is_ten_percent():
    """0.10 的概率：掷 0.09 醒，掷 0.11 继续睡。"""
    class R:
        def __init__(self, v): self.v = v
        def random(self): return self.v
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    cfg = Config(wake_chance=0.10, wake_max_per_night=0)
    night = datetime(2026, 1, 1, 3, 30)
    assert plan(cfg, night, R(0.09)).act == "reply"
    assert plan(cfg, night, R(0.11)).act == "quiet"


def test_chance_rises_each_time():
    """每回一次，下一次更容易醒：0.10 → 0.30 → 0.50 …"""
    class R:
        def __init__(self, v): self.v = v
        def random(self): return self.v
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    cfg = Config(wake_chance=0.10, wake_chance_step=0.20,
                 wake_chance_max=0.90, wake_max_per_night=0)
    night = datetime(2026, 1, 1, 3, 30)
    # 掷 0.15：第一次（0.10）醒不了，第二次（0.30）能醒
    assert plan(cfg, night, R(0.15), woken_tonight=0).act == "quiet"
    assert plan(cfg, night, R(0.15), woken_tonight=1).act == "reply"
    # 掷 0.45：第 2 次（0.50）才够
    assert plan(cfg, night, R(0.45), woken_tonight=1).act == "quiet"
    assert plan(cfg, night, R(0.45), woken_tonight=2).act == "reply"
    # 上限 0.90：掷 0.95 永远醒不了
    assert plan(cfg, night, R(0.95), woken_tonight=99).act == "quiet"


def test_streak_raises_rate():
    """连着回得越多，越不可能忽然不回。"""
    class R:
        def __init__(self, v): self.v = v
        def random(self): return self.v
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    cfg = Config(reply_rate=0.88, reply_rate_step=0.03, reply_rate_max=0.98)
    now = datetime(2026, 1, 1, 15, 0)
    # 0.92：没连击时漏（0.88），连回两次后（0.94）就接
    assert plan(cfg, now, R(0.92), streak=0).act == "skip"
    assert plan(cfg, now, R(0.92), streak=2).act == "reply"
    # 上限 0.98，不会变成 100%
    assert plan(cfg, now, R(0.99), streak=99).act == "skip"


def test_skip_still_forces_next_reply():
    class R:
        def __init__(self, v): self.v = v
        def random(self): return self.v
        def lognormvariate(self, m, s): return m
        def choice(self, seq): return seq[0]

    cfg = Config(reply_rate=0.88, reply_rate_step=0.03)
    now = datetime(2026, 1, 1, 15, 0)
    assert plan(cfg, now, R(0.99), last_act="skip", streak=0).act == "reply"


def test_reply_streak_counter():
    import tempfile
    from pathlib import Path
    from somnia import store

    d = tempfile.mkdtemp()
    c = store.connect(str(Path(d) / "t.db"))
    assert store.reply_streak(c) == 0
    store.log_decision(c, None, "reply", 1, 1, "planned")
    store.log_decision(c, None, "reply", 1, 1, "planned")
    assert store.reply_streak(c) == 2
    store.log_decision(c, None, "skip", 0, 1, "reply_rate_sample")
    assert store.reply_streak(c) == 0


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
