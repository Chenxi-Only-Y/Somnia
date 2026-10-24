"""调度器测试：唤醒条件、打分、Outbox 生命周期。

跑法：python tests/test_scheduler.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store, scheduler
from somnia.config import Config

CFG = Config()


class Rng:
    def __init__(self, value=0.9, pick=0):
        self.value = value
        self.pick = pick

    def random(self):
        return self.value

    def choice(self, seq):
        return seq[self.pick % len(seq)]


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def _at(h, m=0):
    return datetime(2026, 10, 20, h, m)


def test_quiet_hours_block_wake():
    wake, why = scheduler.should_wake(CFG, _at(3), None, None, Rng())
    assert not wake and why == "quiet"


def test_cooldown_blocks_wake():
    now = _at(15)
    wake, why = scheduler.should_wake(
        CFG, now, now.timestamp() - 60, now.timestamp() - 3600, Rng())
    assert not wake and why == "cooldown"


def test_silence_triggers_wake():
    now = _at(15)
    wake, why = scheduler.should_wake(
        CFG, now, now.timestamp() - 7 * 3600, None, Rng(0.01))
    assert wake and why == "silence"


def test_silence_threshold_is_two_hours():
    now = _at(15)
    # 还没到 2 小时：连"想开口"的阶段都没进
    w1, why1 = scheduler.should_wake(CFG, now, now.timestamp() - 1 * 3600, None, Rng(0.5))
    assert not w1 and why1 == "idle"
    # 刚过 2 小时：进入概率阶段，掷得低才会发
    w2, why2 = scheduler.should_wake(CFG, now, now.timestamp() - 2 * 3600 - 1, None, Rng(0.01))
    assert w2 and why2 == "silence"
    w2b, why2b = scheduler.should_wake(CFG, now, now.timestamp() - 2 * 3600 - 1, None, Rng(0.5))
    assert not w2b and why2b == "silence_wait"


def test_silence_chance_rises_with_time():
    """沉默越久，单次检查就越容易开口。"""
    now = _at(15)
    near = now.timestamp() - int(2.1 * 3600)
    far = now.timestamp() - 5 * 3600
    # 0.60 这个掷值：刚满 2 小时不会发，沉默 5 小时就会发
    w_near, _ = scheduler.should_wake(CFG, now, near, None, Rng(0.60))
    w_far, _ = scheduler.should_wake(CFG, now, far, None, Rng(0.60))
    assert not w_near
    assert w_far


def test_six_hours_is_guaranteed():
    """6 小时必发 —— 掷什么都会发。"""
    now = _at(15)
    for roll in (0.99, 0.5, 0.01):
        wake, why = scheduler.should_wake(
            CFG, now, now.timestamp() - 6 * 3600 - 1, None, Rng(roll))
        assert wake and why == "silence", roll


def test_idle_does_not_wake():
    now = _at(15)
    wake, why = scheduler.should_wake(
        CFG, now, now.timestamp() - 30 * 60, None, Rng(0.99))
    assert not wake and why == "idle"


def test_drift_wakes_on_low_roll():
    now = _at(15)
    # 没到沉默门槛时，还有每小时 2% 的"忽然想起你"
    wake, why = scheduler.should_wake(
        CFG, now, now.timestamp() - 30 * 60, None, Rng(0.0))
    assert wake and why == "drift"


def test_grade_penalizes_questions():
    assert scheduler.grade("不知道他今天忙不忙") > scheduler.grade("你在干嘛?")


def test_grade_bounds():
    for t in ("", "嗯", "刚才看到一只很胖的猫", "x" * 100):
        assert 0.0 <= scheduler.grade(t) <= 1.0


def test_daily_cap_blocks_wake():
    """一天说太多次就烦了。"""
    now = _at(15)
    old = now.timestamp() - 7 * 3600
    wake, why = scheduler.should_wake(CFG, now, old, None, Rng(0.01),
                                      wakes_today=CFG.proactive_max_per_day)
    assert not wake and why == "daily_cap"


def test_daily_cap_allows_under_limit():
    now = _at(15)
    old = now.timestamp() - 7 * 3600
    wake, why = scheduler.should_wake(CFG, now, old, None, Rng(0.01), wakes_today=0)
    assert wake and why == "silence"


def test_thought_prompt_carries_context():
    """主动开口要根据记忆和最近的对话找话题，不是凭空一句。"""
    seen = {}

    def fake(prompt):
        seen["p"] = prompt
        return "刚画完一张画，手上全是颜料。"

    got = scheduler.make_thought(CFG, _at(15), Rng(0.5), fake,
                                 memory="你记得关于他的事：\n- 他下周要面试\n",
                                 chat="你们最近聊到：\n他: 我在忙\n")
    assert "颜料" in got
    assert "面试" in seen["p"]
    assert "我在忙" in seen["p"]


def test_thought_is_first_line_only():
    got = scheduler.make_thought(CFG, _at(15), Rng(0.5),
                                 lambda p: "刚画完一张画。\n第二行不该出现")
    assert got == "刚画完一张画。"


def test_greeting_passes_threshold():
    """允许"在吗""干嘛呢"这种开场 —— 它们不到 4 个字，不能按长度误杀。"""
    for g in ("在吗", "在么", "干嘛呢", "睡了吗", "在吗？"):
        assert scheduler.grade(g) >= CFG.outbox_threshold, g


def test_greeting_not_confused_with_normal_text():
    assert scheduler.grade("在吗我在楼下") < 1.0


def test_question_penalty_is_mild():
    """问句略扣分，但不至于让"你午饭吃辣没"被否掉。"""
    assert scheduler.grade("刚偷吃辣条，你午饭吃辣没？") >= CFG.outbox_threshold


def test_tick_writes_outbox():
    c = _db()
    now = _at(15)
    r = scheduler.tick(store, c, CFG, now, rng=Rng(0.01, 0))
    assert r is not None
    thought, score, reason, oid = r
    assert store.outbox_pending_count(c) == 1
    assert isinstance(oid, int) and oid > 0
    assert reason in ("silence", "drift")


def test_tick_respects_cooldown():
    c = _db()
    now = _at(15)
    scheduler.tick(store, c, CFG, now, rng=Rng(0.01))
    again = scheduler.tick(store, c, CFG, now, rng=Rng(0.01))
    assert again is None
    assert store.outbox_pending_count(c) == 1


def test_outbox_take_does_not_mark():
    c = _db()
    store.outbox_add(c, "想起他说的那家店", 0.8)
    taken = store.outbox_take(c, 0.6)
    assert len(taken) == 1
    # 没标记，所以还在
    assert store.outbox_pending_count(c) == 1
    store.outbox_deliver(c, [taken[0][0]])
    assert store.outbox_pending_count(c) == 0


def test_outbox_threshold_filters():
    c = _db()
    store.outbox_add(c, "低分的念头", 0.2)
    assert store.outbox_take(c, 0.6) == []


def test_pending_block_allows_mild_complaint():
    block = scheduler.pending_block(["刚才看到一只猫"])
    assert "刚才看到一只猫" in block
    assert "一天最多一次" in block      # 抱怨可以，但要限量
    assert "不要威胁" in block
    assert scheduler.pending_block([]) == ""


def test_evening_checkin_fires_when_silent_all_day():
    """一整天没聊，八点她主动来问一句 —— 这条不走概率。"""
    now = _at(20, 30)
    wake, why = scheduler.should_wake(
        CFG, now, None, None, Rng(0.99), talked_today=False)
    assert wake and why == "evening"


def test_evening_checkin_skipped_if_talked():
    now = _at(20, 30)
    wake, why = scheduler.should_wake(
        CFG, now, None, None, Rng(0.99), talked_today=True)
    assert why != "evening"


def test_evening_checkin_only_once():
    now = _at(20, 30)
    wake, why = scheduler.should_wake(
        CFG, now, None, None, Rng(0.99), talked_today=False, evening_done=True)
    assert why != "evening"


def test_evening_checkin_respects_window():
    now = _at(15, 0)
    wake, why = scheduler.should_wake(
        CFG, now, None, None, Rng(0.99), talked_today=False)
    assert why != "evening"


def test_evening_checkin_can_be_disabled():
    now = _at(20, 30)
    cfg = Config(evening_checkin=False)
    wake, why = scheduler.should_wake(
        cfg, now, None, None, Rng(0.99), talked_today=False)
    assert why != "evening"


def test_wake_loop_exits_when_sleeping():
    """按下"睡下"之后，后台唤醒线程必须退出 —— 否则等于睡着了还被叫醒。"""
    import threading
    from somnia import app as A

    stop = threading.Event()
    stop.set()                                  # 一开始就是"睡下"状态
    th = threading.Thread(target=A._wake_loop,
                          args=(CFG, None, lambda m: None, None, None, stop),
                          daemon=True)
    th.start()
    th.join(timeout=3)
    assert not th.is_alive()                    # 退出了


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")

