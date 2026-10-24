"""时间感：她知道今天是几号、昨天说过什么、隔了多久。

跑法：python tests/test_time.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import composer
from somnia.config import Config
from somnia.persona import day_label, elapsed_text, format_history

BASE = time.mktime((2026, 10, 21, 10, 0, 0, 0, 0, -1))
NOW = time.localtime(BASE)


def _at(days_ago=0, hour=10):
    t = time.mktime((2026, 10, 21, hour, 0, 0, 0, 0, -1))
    return t - days_ago * 86400


def test_day_label_today():
    assert day_label(_at(0, 9), NOW) == "今天"


def test_day_label_yesterday():
    assert day_label(_at(1, 22), NOW) == "昨天"


def test_day_label_before_that():
    assert day_label(_at(2, 22), NOW) == "前天"


def test_day_label_older_shows_date_and_weekday():
    label = day_label(_at(5), NOW)
    assert "月" in label and "周" in label


def test_history_has_day_boundaries():
    hist = [
        ("user", "我睡了", _at(1, 22)),
        ("assistant", "嗯 晚安", _at(1, 22) + 3),
        ("user", "早", _at(0, 9)),
    ]
    text = format_history(hist, NOW)
    assert "—— 昨天 ——" in text
    assert "—— 今天 ——" in text
    # 日界线在对应的对话之前
    assert text.index("—— 昨天 ——") < text.index("我睡了")
    assert text.index("—— 今天 ——") < text.index("他: 早")


def test_history_without_ts_still_works():
    assert format_history([("user", "在吗")]) == "他: 在吗"


def test_elapsed_hours():
    hist = [("user", "我睡了", BASE - 13 * 3600)]
    assert "13 小时前" in elapsed_text(hist, now_ts=BASE)


def test_elapsed_minutes():
    hist = [("user", "在吗", BASE - 30 * 60)]
    assert "30 分钟前" in elapsed_text(hist, now_ts=BASE)


def test_elapsed_days():
    hist = [("user", "好久没聊", BASE - 3 * 86400)]
    assert "3 天前" in elapsed_text(hist, now_ts=BASE)


def test_elapsed_ignores_just_now():
    hist = [("user", "在吗", BASE - 10)]
    assert elapsed_text(hist, now_ts=BASE) == ""


def test_prompt_has_full_date():
    p = composer.build_prompt(Config(), [], [], "在吗", 1)
    line = next(l for l in p.splitlines() if l.startswith("现在是"))
    assert "年" in line and "月" in line and "日" in line and "周" in line


# ── 她说的话要接得上 ─────────────────────────────────────

def test_artifacts_not_in_history():
    """发表情/照片的记录不是"话"，不该混进对话。"""
    hist = [
        ("user", "在吗", _at(0, 9)),
        ("assistant", "[表情] 呆猫八条_009.gif", _at(0, 9) + 1),
        ("assistant", "在", _at(0, 9) + 2),
    ]
    text = format_history(hist, NOW)
    assert "呆猫八条" not in text
    assert "你: 在" in text


def test_self_recall_block():
    from somnia.persona import self_recall
    hist = [("assistant", "我躺着呢", None), ("assistant", "[照片] a.png", None),
            ("assistant", "困", None)]
    block = self_recall(hist)
    assert "我躺着呢" in block and "困" in block
    assert "a.png" not in block


def test_prompt_contains_self_recall():
    hist = [("user", "在吗", _at(0, 9)), ("assistant", "我躺着呢", _at(0, 9) + 1)]
    p = composer.build_prompt(Config(), hist, [], "干嘛呢", 1)
    assert "你最近说过的话" in p
    assert "我躺着呢" in p
    assert "不要重复" in p


def test_self_recall_keeps_twelve():
    from somnia.persona import self_recall
    hist = [("assistant", f"第{i}句", None) for i in range(20)]
    block = self_recall(hist)
    assert "第19句" in block          # 最近的
    assert "第7句" not in block       # 太早的不占位置
    assert block.count("\n- ") == 12


def test_recent_n_covers_enough():
    assert Config().recent_n >= 30


def test_period_covers_whole_day():
    from somnia.persona import period
    seen = []
    for h in range(24):
        label, doing = period(h)
        assert label and doing
        seen.append(label)
    assert "深夜" in seen and "上午" in seen and "晚上" in seen


def test_period_says_what_shes_doing():
    from somnia.persona import period
    label, doing = period(10)
    assert label == "上午"
    assert doing                      # 默认给的是中性描述，不预设职业


def test_persona_rhythm_overrides_default():
    """人设里写了作息就以人设为准 —— 换职业不用改代码。"""
    from somnia.persona import period, parse_rhythm

    class Office:
        rhythm = parse_rhythm("## 作息\n- 09:00-18:00 在上班\n- 23:30-07:30 睡觉\n")

    assert period(10, 0, Office())[1] == "在上班"
    assert period(2, 0, Office())[1] == "睡觉"      # 跨零点
    assert period(10)[1] != "在上班"


def test_parse_rhythm_handles_cross_midnight():
    from somnia.persona import parse_rhythm
    blocks = parse_rhythm("## 作息\n- 23:30-07:30 睡觉\n")
    assert blocks and blocks[0][0] == 1410 and blocks[0][1] == 1890


def test_parse_rhythm_ignores_other_sections():
    from somnia.persona import parse_rhythm
    text = "- 09:00-10:00 不该被读\n## 作息\n- 10:00-11:00 在开会\n"
    assert parse_rhythm(text) == ((600, 660, "在开会"),)


def test_prompt_has_rhythm_hint():
    p = composer.build_prompt(Config(), [], [], "在吗", 1)
    assert "说的内容要配得上这个时间" in p
    assert "早上别提月亮" in p


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
