"""启停生命周期：按一次停，线程要真的走干净，连开三次不能越积越多。

跑法：python tests/test_lifecycle.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import app as A
from somnia.config import Config


class DummyChannel:
    """像一个真通道：recv 一直等着，close 之后返回 None。"""

    can_initiate = False
    can_send_image = False

    def __init__(self):
        self.closed = False
        self.sent = []

    def recv(self):
        while not self.closed:
            time.sleep(0.02)
        return None

    def recv_timeout(self, seconds):
        time.sleep(seconds)
        return None

    def send(self, text):
        self.sent.append(text)

    def typing(self, on):
        pass

    def close(self):
        self.closed = True


def _cfg(tmp):
    import dataclasses
    c = Config.from_env()
    return dataclasses.replace(c,
                               db_path=str(Path(tmp) / "somnia.db"),
                               api_key="", catchup_enabled=False,
                               scheduler_interval_min=1)


def _run_once(hold=0.25):
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    stop = threading.Event()
    box = {}

    def on_channel(ch):
        box["ch"] = ch

    th = threading.Thread(target=A.run,
                          kwargs=dict(cfg=cfg, fast=True, offline=True,
                                      kind="cli", stop_event=stop,
                                      on_channel=on_channel, scheduler_on=True),
                          daemon=True)
    th.start()
    time.sleep(hold)
    stop.set()
    box["ch"].close()          # 和面板的停止动作一致：先置停止，再关通道
    th.join(timeout=5)
    return th


def test_run_exits_when_stopped():
    th = _run_once()
    assert not th.is_alive(), "按下停止后主循环还在跑"


def test_repeated_start_stop_no_leak():
    before = len(threading.enumerate())
    for _ in range(3):
        th = _run_once()
        assert not th.is_alive()
    time.sleep(1.2)            # 给收尾线程一点时间
    after = len(threading.enumerate())
    assert after - before <= 1, f"线程从 {before} 涨到 {after}，说明有泄漏"


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            bad += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - bad} passed" + (f", {bad} failed" if bad else ""))
    sys.exit(1 if bad else 0)
