"""调度器：她有自己的时间感。

两种唤醒：

  1. 沉默触发 —— 他 48 小时没说话
  2. 漂移触发 —— 每小时小概率"突然想起他"，避免变成机械定时

共同约束：静默时段不唤醒；冷却期内不重复。

产出的是**念头**，不是消息。先落 Outbox，等下一轮对话自然带出。
这样即使通道不能主动下发，主动性也不会消失——只是变成了"她憋着"。
"""

import random

from .config import Config

SILENCE_HOURS = 6.0     # 默认值；实际取 cfg.silence_hours

# 没有模型时的念头池。中性、不索取、不指责。
THOUGHT_POOL = (
    "不知道他今天忙不忙",
    "刚才看到一只很胖的猫",
    "想问问他那个事后来怎么样了",
    "有点想吃上次说的那家",
)

THOUGHT_PROMPT = """你是{name}。现在是{now}（{label}），你大概{doing}。
{memory}{chat}
你想主动给他发条消息。

挑一件具体的事说：
- 你此刻正在做的事（画画、吃东西、睡不着、在阳台站着）
- 他之前提过的事，你想起来问问后来怎样了
- 你刚看到的一件小事

说的事必须和这个时间对得上：早上别提月亮，半夜别提早餐摊，
中午别刚起床，晚上别说天刚亮。
不要问"你怎么不理我"，不要连发好几句。
可以直接丢一句"在吗""干嘛呢"，也可以说"我想你了"——但别每句都这样。
不要复述你记得的他的事，要找一件新的事说。
不要说"我拍给你看""我给你打电话"这类做不到的承诺——这条消息只会是一句话。
一两句，不超过 20 字。只输出消息本身。
"""

# 短问候语。它们本身不到 4 个字，按长度打分会被误杀。
GREETINGS = ("在吗", "在么", "在不在", "干嘛呢", "在干啥", "睡了吗", "醒了吗")


def _memory_block(store, conn, cfg) -> str:
    """只挑几条，而且要打乱。

    全给的话她会一直绕着同一个话题转——实测只给了"喜欢吃辣"一条，
    三次主动开口全是吃辣的。
    """
    try:
        facts = store.recall(conn, 0)
    except Exception:
        facts = []
    if not facts:
        return ""
    picked = random.sample(facts, min(3, len(facts)))
    return "你记得关于他的事：\n" + "\n".join(f"- {f}" for f in picked) + "\n"


def _chat_block(store, conn) -> str:
    try:
        msgs = store.recent(conn, 6)
    except Exception:
        msgs = []
    msgs = [(r, t) for r, t in msgs if not str(t).startswith(("[表情]", "[照片]"))]
    if not msgs:
        return ""
    lines = "\n".join(f"{'你' if r == 'assistant' else '他'}: {t}" for r, t in msgs)
    return f"你们最近聊到：\n{lines}\n\n"


def in_quiet(cfg: Config, now) -> bool:
    lo, hi = cfg.quiet_hours
    return lo <= now.hour < hi


def should_wake(cfg: Config, now, last_user_ts, last_wake_ts, rng=random,
                wakes_today: int = 0, talked_today: bool = True,
                evening_done: bool = False):
    """返回 (是否唤醒, 原因)。"""
    if in_quiet(cfg, now):
        return False, "quiet"

    cap = getattr(cfg, "proactive_max_per_day", 0)
    if cap and wakes_today >= cap:
        return False, "daily_cap"

    now_ts = now.timestamp()
    if last_wake_ts and (now_ts - last_wake_ts) < cfg.wake_cooldown_h * 3600:
        return False, "cooldown"

    # 晚间问候：一整天没联系，到点她主动来问一句（这是"有事"，不走概率）
    if getattr(cfg, "evening_checkin", False) and not talked_today and not evening_done:
        lo, hi = getattr(cfg, "evening_window", (20, 22))
        if lo <= now.hour < hi:
            return True, "evening"

    silence_h = (now_ts - last_user_ts) / 3600 if last_user_ts else 1e9
    if silence_h >= getattr(cfg, "silence_hours", SILENCE_HOURS):
        # 沉默越久越可能开口：从 silence_hours 的小概率，
        # 线性升到 guarantee_hours 的必发（100%）
        start = cfg.silence_hours
        end = max(start + 0.1, getattr(cfg, "proactive_guarantee_hours", start + 4))
        base = getattr(cfg, "proactive_base_chance", 0.02)
        frac = min(1.0, (silence_h - start) / (end - start))
        chance = base + (1.0 - base) * frac
        if rng.random() < chance:
            return True, "silence"
        return False, "silence_wait"

    if rng.random() < 0.02:
        return True, "drift"

    return False, "idle"


def grade(text: str) -> float:
    """给念头打分。这是占位实现——等有标注样本再校准。

    刻意简单：先让流程跑通，别假装这个数字有意义。
    """
    if not text:
        return 0.0
    t = text.strip()
    if t in GREETINGS or t.rstrip("?？") in GREETINGS:
        return 0.65          # 问候是正常的开场，别因为字数短被否
    s = 0.45
    if 4 <= len(t) <= 40:      # 上限放到 40：一句带具体细节的话比短句更好
        s += 0.25
    if any(k in t for k in ("他", "你")):
        s += 0.1
    if text.rstrip().endswith(("?", "？")):
        s -= 0.05          # 问句略扣分：主动开口最好是有话想说，不是索取回应
    return round(min(1.0, max(0.0, s)), 2)


def make_thought(cfg: Config, now, rng=random, llm=None,
                 memory: str = "", chat: str = "", persona=None,
                 angle: str = "") -> str:
    from .persona import period
    label, doing = period(now.hour, now.minute, persona)
    extra = f"\n{angle}\n" if angle else ""
    if llm:
        try:
            text = (llm(THOUGHT_PROMPT.format(
                name=cfg.name,
                now=now.strftime("%H:%M"),
                label=label,
                doing=doing,
                memory=memory,
                chat=chat) + extra) or "").strip()
            if text:
                return text.splitlines()[0][:40]
        except Exception:
            pass
    return rng.choice(THOUGHT_POOL)


def tick(store, conn, cfg: Config, now, llm=None, rng=random, persona=None):
    """一次唤醒检查。返回 (念头, 分数, 原因, outbox_id) 或 None。"""
    # 答应过的事优先：到点必须兑现，不受概率和别人有没有说话影响
    promise = store.due_promise(conn, now.timestamp())
    if promise and not in_quiet(cfg, now):
        # 九成按时，一成拖到更晚才想起来 —— 真人也会忘
        if rng.random() >= getattr(cfg, "promise_keep_rate", 0.9):
            delay = getattr(cfg, "promise_delay_h", 2.5) * 3600
            store.postpone_promise(conn, promise["id"], now.timestamp() + delay)
        else:
            if promise.get("late"):
                angle = (f"你答应过要找他，但刚才一直被事情绊着，现在才腾出手。"
                         f"原话是「{promise['text']}」。"
                         "别说自己忘了，也别郑重道歉，一句话带过去就接着说正事。")
            else:
                angle = ("你之前说过要找他，现在到点了。"
                         f"原话是「{promise['text']}」，兑现它。"
                         "别道歉，也别解释为什么现在才来。")
            thought = make_thought(cfg, now, rng, llm,
                                   memory=_memory_block(store, conn, cfg),
                                   chat=_chat_block(store, conn),
                                   persona=persona, angle=angle)
            if thought:
                store.close_promise(conn, promise["id"])
                score = grade(thought)
                oid = store.outbox_add(conn, thought, score)
                return thought, score, "promise", oid

    wake, reason = should_wake(
        cfg, now,
        store.last_user_ts(conn),
        store.last_wake_ts(conn),
        rng,
        wakes_today=store.wakes_today(conn, now.timestamp()),
        talked_today=store.has_message_today(conn, now.timestamp()),
        evening_done=store.decisions_with_reason_today(conn, "evening") > 0,
    )
    if not wake:
        return None

    angle = ""
    if reason == "evening":
        angle = ("一整天没联系了，你到晚上主动来找他。"
                 "可以带一点点不满（比如\"你怎么不找我\"），"
                 "但别质问、别说\"是不是不想理我\"，说完就转到关心上。")

    thought = make_thought(cfg, now, rng, llm,
                           memory=_memory_block(store, conn, cfg),
                           chat=_chat_block(store, conn),
                           persona=persona, angle=angle)
    if not thought:
        return None

    score = grade(thought)
    oid = store.outbox_add(conn, thought, score)
    return thought, score, reason, oid


def pending_block(thoughts) -> str:
    """把憋着的念头变成 prompt 里的一段。

    关键：明确禁止内疚式表达。这是产品红线，不是文风偏好。
    """
    if not thoughts:
        return ""
    items = "\n".join(f"- {t}" for t in thoughts)
    return (
        "你之前想跟他说、但一直没说出口的事：\n"
        f"{items}\n"
        "如果合适，可以自然地带一句。可以有一点点小抱怨，但一天最多一次；"
        "绝对不要威胁，也不要让他觉得欠你什么。"
    )
