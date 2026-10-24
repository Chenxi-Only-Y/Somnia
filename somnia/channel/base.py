"""Channel 抽象：把"平台细节"关在门外。

只要换掉这一层，CLI / Claw / 将来的小程序都不用改核心逻辑。
"""

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Inbound:
    text: str
    msg_id: Optional[str] = None


class Channel(Protocol):
    """最小接口。typing 可以是空实现。

    契约：通道关闭是**粘性**的。recv() 或 recv_timeout() 一旦返回 None
    表示关闭，此后两者都必须立即返回 None，不得再次阻塞。
    """

    def recv(self) -> Optional[Inbound]:
        """阻塞直到有一条消息；返回 None 表示通道结束（粘性）。"""
        ...

    def recv_timeout(self, seconds: float) -> Optional[Inbound]:
        """最多等 seconds 秒。用于合并窗口。关闭时同样返回 None（粘性）。"""
        ...

    def send(self, text: str) -> None:
        ...

    def typing(self, on: bool) -> None:
        ...
