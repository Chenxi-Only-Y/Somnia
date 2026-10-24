"""配置测试：toml 覆盖、类型转换、坏文件不崩。

跑法：python tests/test_config.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia.config import Config


def _toml(text):
    p = Path(tempfile.mkdtemp()) / "c.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_sane():
    c = Config()
    assert 0 < c.reply_rate < 1
    assert c.quiet_hours == (2, 7)
    assert c.max_len > 10
    assert "听起来" in c.banned


def test_overrides_behavior():
    c = Config().with_file(_toml("""
[behavior]
reply_rate = 0.5
delay_median = 3.0
max_len = 12
"""))
    assert c.reply_rate == 0.5
    assert c.delay_median == 3.0
    assert c.max_len == 12
    assert c.quiet_hours == (2, 7)      # 没写的保留默认


def test_lists_become_tuples():
    c = Config().with_file(_toml("""
[behavior]
quiet_hours = [1, 7]
seg_choices = [1, 2]
banned = ["啊这"]
"""))
    assert isinstance(c.quiet_hours, tuple)
    assert c.quiet_hours == (1, 7)
    assert c.seg_choices == (1, 2)
    assert c.banned == ("啊这",)


def test_identity_name():
    c = Config().with_file(_toml("""
[identity]
name = "阿七"
"""))
    assert c.name == "阿七"


def test_missing_file_is_noop():
    c = Config()
    assert c.with_file(Path(tempfile.mkdtemp()) / "nope.toml") == c


def test_broken_toml_is_noop():
    c = Config()
    bad = _toml("这不是 toml [[[")
    assert c.with_file(bad) == c


def test_wrong_type_is_ignored():
    c = Config()
    out = c.with_file(_toml("""
[behavior]
reply_rate = "很多"
max_len = 20
"""))
    assert out.reply_rate == c.reply_rate     # 类型不对，忽略
    assert out.max_len == 20                  # 其余照常生效


def test_unknown_keys_ignored():
    c = Config()
    out = c.with_file(_toml("""
[behavior]
不存在的字段 = 1
[unknown_section]
x = 2
"""))
    assert out == c


def test_repo_toml_parses():
    """仓库里那份 somnia.toml 必须能解析。

    这条是补回来的：之前一次编辑把它写坏了（表头后面跟了半句注释），
    tomllib 直接抛错，而加载器把异常吞掉 —— 于是文件里所有设置都没生效，
    谁也没发现，因为默认值和文件里的值刚好一样。
    """
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    with open(root / "somnia.toml", "rb") as f:
        data = tomllib.load(f)
    for section in ("identity", "behavior", "memory", "proactive", "model"):
        assert section in data, f"somnia.toml 缺少 [{section}]"
    assert data["memory"]["hot_days"] >= 1
    assert data["behavior"]["reply_rate"] <= 1.0


def test_toml_values_reach_config():
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp()) / "somnia.toml"
    d.write_text('[identity]\nname = "张三"\n[memory]\nhot_days = 5\n[behavior]\nreply_rate = 0.5\n',
                 encoding="utf-8")
    c = Config().with_file(str(d))
    assert c.name == "张三"
    assert c.hot_days == 5
    assert c.reply_rate == 0.5


def test_bad_value_is_ignored_not_fatal():
    """写错类型应该退回默认，而不是让程序起不来。"""
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp()) / "somnia.toml"
    d.write_text('[memory]\nhot_days = "三天"\n', encoding="utf-8")
    c = Config().with_file(str(d))
    assert c.hot_days == Config().hot_days


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
