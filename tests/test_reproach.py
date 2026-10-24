"""抱怨「你怎么不找我」可以，但一天只准一次。

跑法：python tests/test_reproach.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store
from somnia.app import REPROACH_WORDS, _extra_note


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


class D:
    reason = "planned"


def test_counts_todays_reproach():
    c = _db()
    assert store.count_said_today(c, REPROACH_WORDS) == 0
    store.add_message(c, "assistant", "你怎么不找我")
    assert store.count_said_today(c, REPROACH_WORDS) == 1


def test_ignores_other_messages():
    c = _db()
    store.add_message(c, "assistant", "今天好累")
    store.add_message(c, "user", "你怎么不找我")     # 他说的不算
    assert store.count_said_today(c, REPROACH_WORDS) == 0


def test_extra_note_blocks_second_time():
    c = _db()
    store.add_message(c, "assistant", "你怎么不找我")
    note = _extra_note(None, D(), c)
    assert "已经抱怨过一次" in note


def test_extra_note_silent_when_clean():
    c = _db()
    assert _extra_note(None, D(), c) == ""


def test_wake_note_still_works_together():
    c = _db()
    store.add_message(c, "assistant", "你怎么才回我")

    class W:
        reason = "woken"

    note = _extra_note(None, W(), c)
    assert "被这条消息吵醒" in note
    assert "已经抱怨过一次" in note


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
