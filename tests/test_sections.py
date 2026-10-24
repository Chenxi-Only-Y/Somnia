"""人设按块编辑：解析、写回、空块不落地。

跑法：python tests/test_sections.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import sections

SAMPLE = """# 开场白
> 这行是说明

## 身份
> 她是谁
你是测试。
二十四岁。

## 说话方式
一句话就一句话

## 示例
好：嗯
坏：好的呢~

> ## 没启用的块
> 整段都被注释掉了
"""


def test_parse_splits_hint_and_body():
    head, blocks = sections.parse(SAMPLE)
    titles = [b["title"] for b in blocks]
    assert titles == ["身份", "说话方式", "示例"]
    body = "\n".join(blocks[0]["body"])
    assert "你是测试" in body
    assert "她是谁" not in body            # 说明没混进内容
    assert any("她是谁" in h for h in blocks[0]["hint"])


def test_to_json_keeps_framework_order():
    items = sections.to_json(SAMPLE)
    ids = [i["id"] for i in items]
    assert ids[:3] == ["identity", "relation", "partner"]
    assert "life" in ids and "mood" in ids      # 新增的建议块也在框架里
    identity = next(i for i in items if i["id"] == "identity")
    assert "你是测试" in identity["body"]
    # 文件里没有的块也要出现，body 为空
    relation = next(i for i in items if i["id"] == "relation")
    assert relation["body"] == ""


def test_render_skips_empty_sections():
    items = sections.to_json(SAMPLE)
    items = [dict(i, body="") if i["id"] == "voice" else i for i in items]
    out = sections.render(items)
    assert "## 身份" in out
    assert "## 说话方式" not in out        # 清空了就不写
    assert "你是测试" in out


def test_render_keeps_hint_lines():
    items = sections.to_json(SAMPLE)
    out = sections.render(items)
    assert "> 她是谁" in out               # 说明原样保留
    assert "一句话就一句话" in out


def test_custom_section_roundtrip():
    items = sections.to_json(SAMPLE)
    items.append({"id": "x1", "title": "她的生活", "hint": "",
                  "body": "在广告公司做设计，养了只猫叫豆豆。"})
    out = sections.render(items)
    assert "## 她的生活" in out
    assert "豆豆" in out

    again = sections.to_json(out)
    assert any(i["title"] == "她的生活" and "豆豆" in i["body"] for i in again)


def test_strip_empty_sections():
    text = "## 甲\n有内容\n## 乙\n## 丙\n也有"
    out = sections.strip_empty_sections(text)
    assert "## 甲" in out and "## 丙" in out
    assert "## 乙" not in out


def test_persona_load_drops_empty_section():
    from somnia.persona import load
    d = Path(tempfile.mkdtemp())
    (d / "persona.md").write_text(
        "## 身份\n你是测试\n\n## 空块\n\n## 示例\n好：嗯\n", encoding="utf-8")
    (d / "SOUL.md").write_text("红线", encoding="utf-8")
    p = load(str(d))
    assert "## 身份" in p.body
    assert "## 空块" not in p.body


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
