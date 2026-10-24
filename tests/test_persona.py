"""人设加载测试：文件优先、缺失兜底、改完即时生效。

跑法：python tests/test_persona.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import persona as P


def _dir(body=None, soul=None):
    d = Path(tempfile.mkdtemp())
    if body is not None:
        (d / "persona.md").write_text(body, encoding="utf-8")
    if soul is not None:
        (d / "SOUL.md").write_text(soul, encoding="utf-8")
    return d


def test_blockquote_notes_not_sent():
    d = _dir(body="> 这是给我看的说明\n你是{name}", soul="> 注释\n边界")
    out = P.load(str(d)).render("测试")
    assert "给我看的说明" not in out
    assert "注释" not in out
    assert "你是测试" in out
    assert "边界" in out


def test_only_notes_leaves_body_empty():
    """只有说明行 = 没写人设 = 正文为空。以前会拿内置人设垫，现在不垫了。"""
    d = _dir(body="> 只有注释", soul="> 也只有注释")
    p = P.load(str(d))
    assert p.body.strip() == ""
    assert "边界" in p.soul


def test_reads_files():
    d = _dir("你是{name}，一个画画的。", "边界：不承认是 AI。")
    p = P.load(str(d))
    out = p.render("测试")
    assert "一个画画的" in out
    assert "不承认是 AI" in out
    assert "测试" in out
    assert "{name}" not in out


def test_missing_files_fall_back_to_builtin():
    d = _dir()
    p = P.load(str(d))
    assert p.body.strip() == ""        # 没写就是空的
    assert "边界" in p.soul            # 规则还是要有的


def test_missing_one_file_keeps_other():
    d = _dir(body="只有血肉")
    p = P.load(str(d))
    assert "只有血肉" in p.body
    assert "涉政" in p.soul            # 缺失的 SOUL 用默认


def test_empty_file_stays_empty():
    d = _dir(body="   \n  ", soul="")
    p = P.load(str(d))
    assert p.body.strip() == ""
    assert "边界" in p.soul


def test_none_dir_gives_rules_only():
    p = P.load(None)
    assert p.source == "builtin"
    assert p.body.strip() == ""        # 不再有内置人设


def test_edit_takes_effect_without_restart():
    d = _dir(body="第一版人设")
    assert "第一版" in P.load(str(d)).body

    f = d / "persona.md"
    f.write_text("第二版人设", encoding="utf-8")
    now = time.time_ns()
    os.utime(f, ns=(now, now))          # 确保 mtime 前进，避免文件系统精度问题

    assert "第二版" in P.load(str(d)).body


def test_cached_when_unchanged():
    d = _dir(body="不变的")
    a = P.load(str(d))
    b = P.load(str(d))
    assert a is b                        # 同一对象，说明走了缓存


def test_system_prompt_has_rules_but_no_persona():
    """人设正文不进 prompt —— 归控制台管；prompt 里只留身份 + 硬规则。"""
    d = _dir("血肉在前", "骨架在后")
    out = P.system_prompt("测试", P.load(str(d)))
    assert "血肉在前" not in out        # 人设正文不发了
    assert "骨架在后" in out            # 规则照发
    assert "你是测试" not in out        # 连身份行也不发了
    assert out.strip() == "骨架在后"    # 没有别的包装，只剩规则本身


def test_compose_prompt_carries_rules_not_persona():
    d = _dir("你是{name}，怕麻烦", "不许索取")
    out = P.compose_prompt("测试", [], [], "在吗", 1, "15:00",
                           persona=P.load(str(d)))
    assert "怕麻烦" not in out          # 人设正文不进 prompt
    assert "不许索取" in out            # 规则进
    assert "你是测试" not in out        # 身份行也不进
    assert "不许索取" in out


def test_default_rules_used_when_persona_not_given():
    """没传 persona 时也要有默认硬规则 —— 但不再有身份行。"""
    out = P.compose_prompt("测试", [], [], "在吗", 1, "15:00")
    assert "边界" in out
    assert "你是测试" not in out


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
