"""反 AI 味检查表。这张表必须一直是绿的。

跑法：
    python tests/test_lint.py
    pytest tests/ -q          （如果装了 pytest）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia.composer import lint, split
from somnia.config import Config

CFG = Config()


def test_period_removed():
    assert "。" not in lint("今天好累。", CFG)
    assert lint("今天好累。", CFG) == "今天好累"


def test_banned_words_removed():
    for bad in ("听起来", "我理解", "你辛苦了", "抱抱你"):
        out = lint(f"{bad}你", CFG)
        assert bad not in out, out


def test_length_capped():
    long = "我今天真的特别特别累从早上一直忙到现在连饭都没吃上"
    out = lint(long, CFG)
    assert len(out) <= CFG.max_len, (len(out), out)


def test_short_survives():
    assert lint("嗯", CFG) == "嗯"


def test_split_respects_count():
    assert len(split("一|二|三|四", 2)) == 2
    assert split("一|二", 3) == ["一", "二"]


def test_split_drops_empty():
    assert split(" | |x| ", 3) == ["x"]


def test_newline_treated_as_separator():
    assert split("a\nb", 3) == ["a", "b"]


def test_lint_never_returns_full_stop():
    for s in ["好。", "好吧！！", "行吧。"]:
        assert "。" not in lint(s, CFG)


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
