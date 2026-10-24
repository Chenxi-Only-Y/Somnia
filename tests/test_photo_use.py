"""照片用完即删，以及她复读自己的检测。

跑法：python tests/test_photo_use.py
"""

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import composer
from somnia.app import _fire_sticker
from somnia.config import Config
from somnia.stickers import StickerBook


class FakeCh:
    def __init__(self):
        self.images = []

    def send_image(self, p):
        self.images.append(str(p))


def _setup():
    d = Path(tempfile.mkdtemp())
    photos = d / "photos"
    photos.mkdir()
    book = StickerBook(photos)
    book.add("a.png", b"fake")
    cfg = Config(photo_dir=str(photos), db_path=str(d / "t.db"))
    return cfg, book, photos


def test_photo_is_deleted_after_send():
    cfg, book, photos = _setup()
    ch = FakeCh()
    busy = threading.Event()
    busy.set()
    _fire_sticker(cfg, ch, photos / "a.png", threading.Lock(),
                  lambda m: None, busy, "照片", True)
    assert ch.images, "应该发出去"
    assert not (photos / "a.png").exists(), "发过就该删掉"
    assert book.items() == []


def test_photo_kept_when_not_deleting():
    cfg, book, photos = _setup()
    busy = threading.Event()
    busy.set()
    _fire_sticker(cfg, FakeCh(), photos / "a.png", threading.Lock(),
                  lambda m: None, busy, "表情", False)
    assert (photos / "a.png").exists(), "表情不是一次性的"


def test_send_failure_does_not_delete():
    """没发出去就不能删，否则这张图白白丢了。"""
    cfg, book, photos = _setup()
    busy = threading.Event()
    busy.set()

    class Broken(FakeCh):
        def send_image(self, p):
            raise RuntimeError("发不出去")

    _fire_sticker(cfg, Broken(), photos / "a.png", threading.Lock(),
                  lambda m: None, busy, "照片", True)
    assert (photos / "a.png").exists()


# ── 复读检测 ─────────────────────────────────────────────

def test_repeats_detects_same_line():
    hist = [("assistant", "我快到办公室了，你晚上别又只吃零食")]
    assert composer.repeats(["我快到办公室了，你晚上别又只吃零食"], hist)


def test_repeats_allows_new_line():
    hist = [("assistant", "我快到办公室了，你晚上别又只吃零食")]
    assert not composer.repeats(["知道了，那我先忙"], hist)


def test_repeats_ignores_empty_history():
    assert not composer.repeats(["随便说点"], [])
    assert not composer.repeats([], [("assistant", "x")])


def test_repeats_ignores_media_artifacts():
    hist = [("assistant", "[照片] a.png")]
    assert not composer.repeats(["a.png"], hist)


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
