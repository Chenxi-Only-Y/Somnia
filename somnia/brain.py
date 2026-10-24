"""决策层：要不要回、多久回、回几条。

这里不调用模型。所有判断都是规则 + 采样。
理由：一旦让模型决定"要不要回"，延迟、成本、可测性全部失控。
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime

from .config import Config


@dataclass
class Decision:
    act: str            # reply | skip | quiet
    delay: float = 0.0
    segments: int = 1
    reason: str = ""


def plan(cfg: Config, now: datetime, rng=random, last_act: str = None,
         asking: bool = False, woken_tonight: int = 0,
         recently_woken: bool = False, streak: int = 0) -> Decision:
    lo, hi = cfg.quiet_hours
    if lo <= now.hour < hi:
        # 睡着也可能被吵醒。每回一次就更难睡回去，所以概率逐次提高。
        cap = getattr(cfg, "wake_max_per_night", 0)
        base = getattr(cfg, "wake_chance", 0)
        step = getattr(cfg, "wake_chance_step", 0)
        top = getattr(cfg, "wake_chance_max", 0.9)
        chance = min(top, base + step * max(0, woken_tonight))
        if (chance > 0
                and (not cap or woken_tonight < cap)
                and rng.random() < chance):
            return Decision(
                "reply",
                delay=max(8.0, min(cfg.delay_max, rng.lognormvariate(
                    math.log(cfg.delay_median * 2), cfg.delay_sigma))),
                segments=1,
                reason="woken",
            )
        return Decision("quiet", reason=f"quiet_hours {lo}-{hi}")

    rate = cfg.reply_rate
    # 连着回得越多，下一次越可能继续回：聊得正顺的时候忽然消失很假
    step = getattr(cfg, "reply_rate_step", 0)
    top = getattr(cfg, "reply_rate_max", 0.98)
    if step and streak > 0:
        rate = min(top, rate + step * streak)
    # 连着两次不回，看起来就是"坏了"，不是"在忙"
    if last_act == "skip":
        rate = 1.0
    # 对方在问问题，比陈述句更该接
    if asking:
        rate = min(1.0, rate + 0.08)

    if rng.random() > rate:
        return Decision("skip", reason="reply_rate_sample")

    delay = rng.lognormvariate(math.log(cfg.delay_median), cfg.delay_sigma)
    delay = max(3.0, min(getattr(cfg, "delay_max", 20.0), delay))
    return Decision(
        "reply",
        delay=delay,
        segments=rng.choice(cfg.seg_choices),
        reason="planned" if rate == cfg.reply_rate else f"planned({rate:.2f})",
    )
