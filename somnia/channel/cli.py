"""本地终端通道。开发期用这个，不需要任何 secret。

用后台线程把 stdin 泵进队列，这样才支持 recv_timeout——
合并窗口在终端里也必须成立，否则测不出真实节奏。
"""

import queue
import threading

from .base import Inbound

_SENTINEL = object()


class CliChannel:
    can_initiate = False
    can_send_image = False

    def __init__(self, name: str = "她"):
        self.name = name
        self._q: "queue.Queue" = queue.Queue()
        self._closed = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                line = input("你: ")
            except (EOFError, KeyboardInterrupt):
                self._q.put(_SENTINEL)
                return
            line = line.strip()
            if line:
                self._q.put(Inbound(text=line))

    def _unwrap(self, item):
        # 关闭信号一旦被合并窗口取走，必须记住，否则下一轮 recv 会永远阻塞
        if item is _SENTINEL:
            self._closed = True
            return None
        return item

    def recv(self):
        if self._closed:
            return None
        return self._unwrap(self._q.get())

    def recv_timeout(self, seconds: float):
        if self._closed:
            return None
        try:
            item = self._q.get(timeout=seconds)
        except queue.Empty:
            return None
        return self._unwrap(item)

    def send(self, text: str) -> None:
        print(f"{self.name}: {text}")

    def typing(self, on: bool) -> None:
        if on:
            print(f"  {self.name} 正在输入", end="", flush=True)

    def close(self):
        """停：标记关闭，并塞一个哨兵把正等着的 recv 立刻放出来。

        之前没有这个方法 —— 面板"睡下"对终端通道直接抛 AttributeError，
        主循环还堵在 recv 上，等于停不掉。
        """
        self._closed = True
        self._q.put(_SENTINEL)

    def typing_dot(self) -> None:
        print(".", end="", flush=True)

    def typing_end(self) -> None:
        print()
