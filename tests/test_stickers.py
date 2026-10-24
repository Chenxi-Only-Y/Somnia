"""表情包库测试：上传、备注、删除、选图、安全名。

跑法：python tests/test_stickers.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia.stickers import StickerBook, safe_name
from somnia.channel.wechatauto import WechatAutoChannel


class Rng:
    def __init__(self, idx=0):
        self.idx = idx

    def choice(self, seq):
        return seq[self.idx % len(seq)]


def _book():
    d = Path(tempfile.mkdtemp())
    return StickerBook(d / "stickers", meta_path=d / "data" / "stickers.json")


def test_add_and_list():
    b = _book()
    b.add("嗯.png", b"img", note="答应的时候")
    items = b.items()
    assert len(items) == 1
    assert items[0]["name"] == "嗯.png"
    assert items[0]["note"] == "答应的时候"


def test_note_roundtrip():
    b = _book()
    b.add("a.png", b"x")
    assert b.set_note("a.png", "无语")
    assert b.items()[0]["note"] == "无语"
    # 备注落盘
    meta = json.loads((Path(b.meta_path)).read_text(encoding="utf-8"))
    assert meta["a.png"] == "无语"


def test_note_missing_file():
    b = _book()
    assert b.set_note("nope.png", "x") is False


def test_delete_removes_file_and_note():
    b = _book()
    b.add("a.png", b"x", note="n")
    assert b.delete("a.png")
    assert b.items() == []
    assert json.loads(Path(b.meta_path).read_text(encoding="utf-8")) == {}


def test_rejects_non_image():
    b = _book()
    try:
        b.add("evil.exe", b"x")
    except ValueError:
        pass
    else:
        raise AssertionError("应当拒绝")


def test_rejects_oversize():
    b = _book()
    try:
        b.add("big.png", b"x" * (5 * 1024 * 1024 + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("应当拒绝")


def test_path_traversal_is_stripped():
    assert "/" not in safe_name("../../etc/passwd")
    assert ".." not in safe_name("../../etc/passwd")
    assert safe_name("../../a.png").endswith(".png")


def test_pick_avoids_repeat():
    b = _book()
    b.add("a.png", b"x")
    b.add("b.png", b"y")
    b.rng = Rng(0)
    first = b.pick()
    second = b.pick()
    assert first.name != second.name


def test_pick_on_empty():
    assert _book().pick() is None


# ── 由她决定发哪张 ───────────────────────────────────────

def test_extract_sticker_bracket():
    from somnia.composer import extract_sticker
    body, st = extract_sticker("行吧\n[表情:无语]")
    assert st == "无语"
    assert "表情" not in body


def test_extract_sticker_cn_bracket():
    from somnia.composer import extract_sticker
    body, st = extract_sticker("【表情：抱抱】你还好吗")
    assert st == "抱抱"
    assert "抱抱】" not in body


def test_extract_sticker_absent():
    from somnia.composer import extract_sticker
    body, st = extract_sticker("嗯")
    assert st is None and body == "嗯"


def test_menu_shows_notes():
    b = _book()
    b.add("a.png", b"x", note="无语")
    b.add("b.png", b"y")            # 没备注 → 不进清单
    text = b.menu_text()
    assert "无语" in text
    assert "b.png" not in text


def test_menu_groups_and_caps():
    b = _book()
    for i in range(3):
        b.add(f"c{i}.png", b"x", note="委屈")
    b.add("d.png", b"x", note="生气")
    for i in range(20):
        b.add(f"n{i}.png", b"x", note=f"含义{i}")
    text = b.menu_text(limit=5)
    assert "委屈（3 张）" in text          # 同义合并带数量
    assert len(text.splitlines()) == 5     # 有条数上限


def test_unlabeled_never_found():
    b = _book()
    b.add("a.png", b"x")                  # 无备注
    assert b.find("a") is None            # 只能按含义找
    assert b.labels() == {}


def test_find_by_note():
    b = _book()
    b.add("a.png", b"x", note="无语")
    b.add("b.png", b"y", note="抱抱")
    assert b.find("抱抱").name == "b.png"


def test_find_by_name_not_allowed():
    """只能按含义找；文件名不是含义。"""
    b = _book()
    b.add("a.png", b"x")
    assert b.find("a.png") is None


def test_find_partial_and_missing():
    b = _book()
    b.add("a.png", b"x", note="摸摸头")
    assert b.find("摸头").name == "a.png"      # 包含关系
    assert b.find("不存在") is None


def test_generate_turn_returns_sticker():
    from somnia import composer
    from somnia.config import Config
    out, st, ph = composer.generate_turn(
        Config(), [], [], "在吗", 2,
        llm=lambda p: "嗯|行吧\n[表情:无语]",
        sticker_menu="- 无语",
    )
    assert st == "无语"
    assert ph is None
    assert out == ["嗯", "行吧"]


def test_generate_turn_returns_photo():
    from somnia import composer
    from somnia.config import Config
    out, st, ph = composer.generate_turn(
        Config(), [], [], "你在干嘛", 2,
        llm=lambda p: "在阳台上\n[照片:晚霞]",
        photo_menu="- 晚霞",
    )
    assert ph == "晚霞"
    assert st is None


def test_extract_media_both():
    from somnia.composer import extract_media
    body, st, ph = extract_media("嗯\n[表情:委屈]\n[照片:猫]")
    assert (st, ph) == ("委屈", "猫")
    assert "[" not in body


def test_prompt_contains_sticker_menu():
    from somnia import composer
    from somnia.config import Config
    p = composer.build_prompt(Config(), [], [], "在吗", 1, sticker_menu="- 无语")
    assert "你能发的表情" in p
    assert "[表情:" in p


# ── 通道发送 ─────────────────────────────────────────────

class FakeSender:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send_image(self, path, who):
        self.sent.append((path, who))
        return _Resp(self.ok)


class _Resp:
    def __init__(self, ok):
        self.ok = ok
        self.is_success = ok

    def __str__(self):
        return f"WxResponse({self.ok})"


def test_channel_send_image():
    sender = FakeSender()
    ch = WechatAutoChannel("对方", sender=sender, pace=(0, 0, 0))
    ch.send_image("/tmp/a.png")
    assert sender.sent == [("/tmp/a.png", "对方")]


def test_channel_send_image_unconfirmed_raises():
    ch = WechatAutoChannel("对方", sender=FakeSender(ok=False), pace=(0, 0, 0))
    try:
        ch.send_image("/tmp/a.png")
    except RuntimeError as e:
        assert "未被确认" in str(e)
    else:
        raise AssertionError("应当抛出")


def test_channel_declares_image_capability():
    assert WechatAutoChannel.can_send_image is True


# ── 照片的时间归属 ───────────────────────────────────────

def test_time_tag_reads_note():
    from somnia.stickers import time_tag
    assert time_tag("凌晨三点的窗外") == "deep_night"
    assert time_tag("中午吃的外卖") == "noon"
    assert time_tag("下班地铁上的倒影") == "evening"
    assert time_tag("窗台上那只猫") is None       # 没时间也能用


def test_slot_of_hour():
    from somnia.stickers import slot_of_hour
    assert slot_of_hour(2) == "deep_night"
    assert slot_of_hour(8) == "morning"
    assert slot_of_hour(12) == "noon"
    assert slot_of_hour(15) == "afternoon"
    assert slot_of_hour(19) == "evening"
    assert slot_of_hour(22) == "night"


def test_menu_filters_by_time():
    """半夜不该给她"中午的外卖"这种选项。"""
    b = _book()
    b.add("a.png", b"x", note="凌晨三点的窗外")
    b.add("b.png", b"x", note="中午吃的外卖")
    b.add("c.png", b"x", note="窗台上那只猫")
    night = b.menu_text(limit=20, slot="deep_night")
    noon = b.menu_text(limit=20, slot="noon")
    assert "凌晨三点的窗外" in night and "中午吃的外卖" not in night
    assert "中午吃的外卖" in noon and "凌晨三点的窗外" not in noon
    # 没写时间的通用图两边都在
    assert "窗台上那只猫" in night and "窗台上那只猫" in noon


def test_menu_allows_neighbor_slots():
    """傍晚的图给晚饭后看也合理，别卡太死。"""
    b = _book()
    b.add("a.png", b"x", note="傍晚路上买的奶茶")
    assert "傍晚路上买的奶茶" in b.menu_text(limit=10, slot="night")


def test_menu_without_slot_returns_everything():
    b = _book()
    b.add("a.png", b"x", note="凌晨三点的窗外")
    b.add("b.png", b"x", note="中午吃的外卖")
    text = b.menu_text(limit=10)
    assert "凌晨" in text and "中午" in text


# ── 图和话对得上吗 ───────────────────────────────────────

def test_matches_text_rejects_unrelated():
    from somnia.stickers import matches_text
    assert matches_text("窗台上那只猫", "今天累死了躺了一天") is False
    assert matches_text("下班地铁车窗上的倒影", "我在家写代码") is False


def test_matches_text_accepts_related():
    from somnia.stickers import matches_text
    assert matches_text("窗台上那只猫", "刚才有只猫蹲在窗台上") is True
    assert matches_text("中午吃的外卖", "我在吃外卖，难吃") is True


def test_matches_text_allows_pointing():
    """说了"给你看"，就是明确在分享，别卡太死。"""
    from somnia.stickers import matches_text
    assert matches_text("中午吃的外卖", "给你看看这个") is True


def test_matches_text_empty_is_permissive():
    from somnia.stickers import matches_text
    assert matches_text("", "随便说说") is True
    assert matches_text("窗台的猫", "") is True


def test_note_of_returns_meta():
    b = _book()
    b.add("a.png", b"x", note="窗台上那只猫")
    p = next(iter(b.files()))
    assert b.note_of(p) == "窗台上那只猫"


def test_shipped_notes_work_without_user_file():
    """随包发的 stickers/notes.json 要能直接用 —— 否则别人拿到手全是没有备注的图。"""
    d = Path(tempfile.mkdtemp())
    (d / "stickers").mkdir()
    (d / "stickers" / "a.gif").write_bytes(b"GIF89a")
    (d / "stickers" / "notes.json").write_text(
        json.dumps({"a.gif": "卖萌"}, ensure_ascii=False), encoding="utf-8")
    book = StickerBook(d / "stickers")
    assert book.labels() == {"卖萌": [d / "stickers" / "a.gif"]}


def test_user_note_overrides_shipped_note():
    d = Path(tempfile.mkdtemp())
    (d / "stickers").mkdir()
    (d / "stickers" / "a.gif").write_bytes(b"GIF89a")
    (d / "stickers" / "notes.json").write_text(
        json.dumps({"a.gif": "卖萌"}, ensure_ascii=False), encoding="utf-8")
    book = StickerBook(d / "stickers")
    book.set_note("a.gif", "生气")
    assert "生气" in book.labels() and "卖萌" not in book.labels()
    # 随包那份不能被改写，否则下次升级就丢了默认值
    shipped = json.loads((d / "stickers" / "notes.json").read_text(encoding="utf-8"))
    assert shipped == {"a.gif": "卖萌"}


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
