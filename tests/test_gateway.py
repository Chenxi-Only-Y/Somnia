"""Gateway 测试：连发合并与去重。

用假通道 + 假时钟，完全确定性，不依赖真实等待。
跑法：python tests/test_gateway.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store
from somnia.gateway import Gateway
from somnia.channel.base import Inbound


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeChannel:
    """items: [(到达时刻, 文本, msg_id)]，按时间升序。"""

    def __init__(self, clock, items):
        self.clock = clock
        self.items = list(items)
        self.closed = False

    def recv(self):
        if not self.items:
            return None
        at, text, mid = self.items.pop(0)
        self.clock.t = max(self.clock.t, at)
        return Inbound(text=text, msg_id=mid)

    def recv_timeout(self, seconds):
        if not self.items:
            self.clock.t += seconds
            return None
        at, text, mid = self.items[0]
        if at <= self.clock.t + seconds:
            self.clock.t = at
            self.items.pop(0)
            return Inbound(text=text, msg_id=mid)
        self.clock.t += seconds
        return None

    def send(self, text):
        pass

    def typing(self, on):
        pass


def _env():
    d = tempfile.mkdtemp()
    conn = store.connect(str(Path(d) / "t.db"))
    clock = FakeClock()
    return conn, clock


def test_burst_within_window_is_merged():
    conn, clock = _env()
    ch = FakeChannel(clock, [(0.0, "在吗", "m1"), (0.5, "今天好累", "m2"), (1.0, "算了", "m3")])
    gw = Gateway(conn, store, window=2.5, clock=clock)
    b = gw.collect(ch)
    assert b.texts == ["在吗", "今天好累", "算了"], b.texts
    assert b.is_burst
    assert b.text.count("\n") == 2


def test_slow_messages_not_merged():
    conn, clock = _env()
    ch = FakeChannel(clock, [(0.0, "在吗", "m1"), (5.0, "今天好累", "m2")])
    gw = Gateway(conn, store, window=2.5, clock=clock)
    b = gw.collect(ch)
    assert b.texts == ["在吗"], b.texts
    assert not b.is_burst


def test_duplicate_msg_id_dropped():
    conn, clock = _env()
    store.add_message(conn, "user", "在吗", "m1")
    ch = FakeChannel(clock, [(0.0, "在吗", "m1"), (0.2, "新的", "m2")])
    gw = Gateway(conn, store, window=2.5, clock=clock)
    b = gw.collect(ch)
    assert b.texts == ["新的"], b.texts


def test_duplicate_within_same_batch_dropped():
    """长轮询会把同一条消息投两次，同一批里也要去重。"""
    conn, clock = _env()
    ch = FakeChannel(clock, [(0.0, "在吗", "m1"), (0.1, "在吗", "m1")])
    gw = Gateway(conn, store, window=2.5, clock=clock)
    b = gw.collect(ch)
    assert b.texts == ["在吗"], b.texts


def test_all_duplicates_gives_empty_batch():
    conn, clock = _env()
    store.add_message(conn, "user", "在吗", "m1")
    ch = FakeChannel(clock, [(0.0, "在吗", "m1")])
    gw = Gateway(conn, store, window=2.5, clock=clock)
    b = gw.collect(ch)
    assert not b
    assert b.texts == []


def test_closed_channel_returns_none():
    conn, clock = _env()
    ch = FakeChannel(clock, [])
    gw = Gateway(conn, store, window=2.5, clock=clock)
    assert gw.collect(ch) is None


def test_zero_window_never_merges():
    conn, clock = _env()
    ch = FakeChannel(clock, [(0.0, "a", "m1"), (0.0, "b", "m2")])
    gw = Gateway(conn, store, window=0.0, clock=clock)
    b = gw.collect(ch)
    assert b.texts == ["a"], b.texts


CLOSE = object()


class StickyChannel:
    """模拟"关闭信号被合并窗口取走"的情形。

    回归用例：一旦关闭，后续 recv/recv_timeout 必须立即返回 None，
    否则主循环会在窗口内吞掉 EOF 之后永远阻塞。
    """

    def __init__(self, items):
        self.items = list(items)
        self._closed = False

    def _take(self, item):
        if item is CLOSE:
            self._closed = True
            return None
        return item

    def recv(self):
        if self._closed:
            return None
        return self._take(self.items.pop(0) if self.items else CLOSE)

    def recv_timeout(self, seconds):
        if self._closed:
            return None
        if not self.items:
            return None
        return self._take(self.items.pop(0))

    def send(self, text):
        pass

    def typing(self, on):
        pass


def test_close_during_window_is_sticky():
    conn, clock = _env()
    ch = StickyChannel([Inbound(text="a", msg_id="m1"), CLOSE])
    gw = Gateway(conn, store, window=2.5, clock=clock)

    first = gw.collect(ch)
    assert first.texts == ["a"]

    # 关键：这里必须立刻返回 None，不能挂住
    assert gw.collect(ch) is None


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
