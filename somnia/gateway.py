"""Gateway：消息进入大脑之前的闸门。

两件事：
  1. 去重 —— 长连接重连会重投同一条，不去重她会回两遍
  2. 合并 —— 连发三条只回一次。真人不会逐条回，她等你发完

合并窗口默认 2.5 秒：比打字快，比犹豫短。
"""

import time
from dataclasses import dataclass, field


@dataclass
class Batch:
    texts: list = field(default_factory=list)
    msg_ids: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.texts)

    @property
    def is_burst(self) -> bool:
        return len(self.texts) > 1

    def __bool__(self):
        return bool(self.texts)


class Gateway:
    def __init__(self, conn, store, window: float = 2.5, clock=time.monotonic):
        self.conn = conn
        self.store = store
        self.window = window
        self.clock = clock

    def collect(self, channel):
        """阻塞取第一条，然后在窗口内尽量多收。

        返回 Batch；通道结束返回 None。
        """
        first = channel.recv()
        if first is None:
            return None

        raw = [first]
        deadline = self.clock() + self.window
        while True:
            remain = deadline - self.clock()
            if remain <= 0:
                break
            nxt = channel.recv_timeout(remain)
            if nxt is None:
                break
            raw.append(nxt)

        batch = Batch()
        seen_here = set()
        for inb in raw:
            if inb.msg_id:
                # 同一批里也可能重复（长轮询重投），先查本批再查库
                if inb.msg_id in seen_here or self.store.seen(self.conn, inb.msg_id):
                    continue
                seen_here.add(inb.msg_id)
            batch.texts.append(inb.text)
            batch.msg_ids.append(inb.msg_id)
        return batch
