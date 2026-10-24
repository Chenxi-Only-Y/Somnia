"""承诺：她说了"晚上找你"，就得真的到点来找。

真人不会说一句"晚点跟你说"然后消失 —— 那是机器人。
这里只做两件事：从她的话里认出承诺，和一个大致该兑现的时间。
"""

import datetime
import re
import time

# "晚上/晚点/明天…" 后面跟着 "找你/跟你说/发你" 这类，就是一个承诺
PATTERN = re.compile(
    r"(今晚|晚上|晚点|一会儿|等下|回头|待会儿|明天|后天)"
    r"[^，。！？!?\n]{0,8}"
    r"(找你|和你说|跟你说|告诉你|聊|发你|给你看|给你拍|说一声)"
)

SOON = {"一会儿", "等下", "待会儿"}
LATER = {"晚点", "回头"}


def due_at(token: str, now: float = None) -> float:
    """这句话大约什么时候兑现。"""
    now = time.time() if now is None else now
    dt = datetime.datetime.fromtimestamp(now)

    if token in SOON:
        return now + 45 * 60
    if token in LATER:
        return now + 90 * 60
    if token == "明天":
        t = (dt + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0)
        return t.timestamp()
    if token == "后天":
        t = (dt + datetime.timedelta(days=2)).replace(hour=10, minute=0, second=0)
        return t.timestamp()
    # 今晚 / 晚上
    target = dt.replace(hour=20, minute=30, second=0, microsecond=0)
    if dt >= target:
        return now + 30 * 60          # 已经过了这个点，半小时后
    return target.timestamp()


def detect(text: str):
    """返回 (承诺原句, 兑现时间) 列表。"""
    if not text:
        return []
    out = []
    for m in PATTERN.finditer(text):
        token = m.group(1)
        out.append((m.group(0).strip(), due_at(token)))
    return out


def first(text: str):
    got = detect(text)
    return got[0] if got else None
