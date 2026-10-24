"""记忆策略：什么值得记、什么时候取。

L0 压缩概述  —— 窗口满了，最老的 200 条压成一段，依次往前滚
L1 最近窗口  —— store.recent_days，按天取
L2 日摘要    —— 每天压成一两句，跨天靠它
L3 事实库    —— 只记具体的东西，不记形容词
"""

import time

from .config import Config
from . import archive

# 值得记的信号：具体的人、事、时间、偏好
MARKERS = (
    "我喜欢", "我讨厌", "我不喜欢", "我是", "我住", "我在",
    "明天", "后天", "下周", "周末", "面试", "生日", "考试",
    "答应", "说好", "约", "讨厌",
)


QUESTION_MARKS = ("?", "？", "吗", "呢", "啥", "怎么", "为什么", "是不是", "要不要")


def worth_remembering(text: str) -> bool:
    if not (4 < len(text) < 60):
        return False
    # 问句不是事实。以前把"你啥时候生日啊"当事实存进去了，纯噪音。
    if any(q in text for q in QUESTION_MARKS):
        return False
    return any(m in text for m in MARKERS)


def importance_of(text: str) -> float:
    """重要度决定它会不会被时间冲掉。

    偏好 > 承诺 > 事件 > 其它。
    """
    if any(p in text for p in ("我喜欢", "我讨厌", "我不喜欢")):
        return 0.85
    if any(p in text for p in ("答应", "说好", "约")):
        return 0.75
    if any(p in text for p in ("面试", "考试", "生日", "下周", "明天")):
        return 0.70
    return 0.50


# 带时间的记忆会过期。"他今天要去看牙"过了就别再提了。
TTL_DAYS = (
    ("今天", 1.5),
    ("今晚", 1.5),
    ("明天", 2.5),
    ("后天", 3.5),
    ("这周", 5),
    ("周末", 5),
    ("下周", 8),
)
DEFAULT_TTL_DAYS = 120.0


def ttl_days(text: str) -> float:
    for kw, days in TTL_DAYS:
        if kw in text:
            return days
    return DEFAULT_TTL_DAYS


def expires_at(text: str, now: float = None) -> float:
    import time as _t
    now = _t.time() if now is None else now
    return now + ttl_days(text) * 86400


def maybe_remember(store, conn, text: str) -> bool:
    if not worth_remembering(text):
        return False
    store.add_fact(conn, text, importance_of(text), expires=expires_at(text))
    return True


# ── L3 补强：让模型判断"什么值得记" ──────────────────────

EXTRACT_PROMPT = """从下面这句话里，提取关于"他"的**具体**事实。

要：身份、住哪、做什么工作、家人宠物、喜好讨厌、最近在忙的事、
    接下来要做或要发生的事（哪怕只是今天/明天）、约好的事。
短期的事也要，只要是真发生的、具体的。

不要：临时的情绪（"我好烦"）、客气话、玩笑、疑问句、你的推测。

每行一条，一行一个事实，第三人称"他"开头，不超过 15 个字。
没有值得记的就只输出：无

他说：{text}
"""


def parse_facts(raw: str, limit: int = 4):
    """把模型输出解析成事实列表。"""
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        s = line.strip().lstrip("-•·0123456789.、 ").strip()
        s = s.strip("「」\"'。. ")
        if not s or s in ("无", "没有", "暂无"):
            continue
        if len(s) > 30:
            s = s[:30]
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def extract_facts(text: str, llm=None, limit: int = 4):
    """用模型抽事实。没有模型就返回空（不要用启发式瞎猜）。"""
    if not llm:
        return []
    try:
        raw = llm(EXTRACT_PROMPT.format(text=text)) or ""
    except Exception:
        return []
    return parse_facts(raw, limit)


def remember_facts(store, conn, text: str, llm=None) -> list:
    """抽出来就直接入库（add_fact 自己会去重/替换冲突）。

    模型偶尔会回"无"——这时用规则兜底，别让"下周面试"这种事漏掉。
    """
    facts = extract_facts(text, llm)
    if not facts and worth_remembering(text):
        facts = [text.strip()[:40]]
    for f in facts:
        store.add_fact(conn, f, 0.8, expires=expires_at(f))
    return facts


# ── L2：日摘要 ────────────────────────────────────────────

SUMMARY_PROMPT = """下面是你和他某一天的全部对话。
把它压成一两句，只保留具体的事：他说了什么、发生了什么、答应过什么。
不要评论，不要总结情绪，不要写"他们聊了…"。
超过 40 个字就算失败。

{dialogue}
"""


def day_key(ts: float = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


def _fallback(msgs) -> str:
    """没有模型时的降级摘要。确定性，可测。"""
    said = [t for r, t in msgs if r == "user" and len(t) >= 6][:4]
    if not said:
        said = [t for r, t in msgs if r == "user"][:2]
    if not said:
        return ""
    return "他说：" + "；".join(said)


def summarize_day(store, conn, cfg: Config, day: str, llm=None) -> str:
    msgs = store.messages_on(conn, day)
    if not msgs:
        return ""

    text = ""
    if llm:
        dialogue = "\n".join(f"{'你' if r == 'assistant' else '他'}: {t}" for r, t in msgs)
        try:
            text = (llm(SUMMARY_PROMPT.format(dialogue=dialogue)) or "").strip()
        except Exception:
            text = ""
        if len(text) > 60:
            text = text[:60]

    if not text:
        text = _fallback(msgs)

    if text:
        store.add_summary(conn, day, text)
    return text


def catch_up(store, conn, cfg: Config, llm=None, limit: int = 7):
    """补做过去几天的摘要（今天不做，今天还没过完）。"""
    today = day_key()
    pending = [d for d in store.days_with_messages(conn) if d < today]
    done = []
    for d in pending[-limit:]:
        if summarize_day(store, conn, cfg, d, llm):
            done.append(d)
    return done


# ── 取用 ──────────────────────────────────────────────────

def _bigrams(text: str):
    t = "".join(ch for ch in (text or "") if ch.strip())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _overlap(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / min(len(x), len(y))


# 虚词不参与判断，否则任何两句话都"相关"
STOP_CHARS = set("的了在是我你他她它这那就都也还又很吧啊呢吗嘛哦呀个一不没有说着过和与给把被让"
                 "上下里外时候点儿什么怎样多少")


def content_chars(text: str):
    return {ch for ch in (text or "")
            if "\u4e00" <= ch <= "\u9fff" and ch not in STOP_CHARS}


def trim_facts(facts, history=None, user_text: str = "", keep: int = 12):
    """挑事实进 prompt：别把十几条全塞进去，也别反复提同一件小事。

    两条规则：
      1. 她刚说过的（和最近发言共两个以上内容字）→ 不要再提一遍
      2. 和当前这句话有关的优先，其余按原顺序补到 keep 条
    """
    if not facts:
        return []
    said = [t for r, t in ((i[0], i[1]) for i in (history or []))
            if r == "assistant" and not str(t).startswith(("[表情]", "[照片]"))][-8:]
    said_chars = set()
    for s in said:
        said_chars |= content_chars(s)

    fresh = [f for f in facts if len(content_chars(f) & said_chars) < 2]

    mine = content_chars(user_text)
    relevant = [f for f in fresh if content_chars(f) & mine] if mine else []
    rest = [f for f in fresh if f not in relevant]
    return (relevant + rest)[:keep]


ASKING = ("记得", "还记得", "之前", "上次", "那天", "以前", "聊过",
          "你说过", "想起来", "那次", "月初", "上周")


def recall_from_archive(cfg: Config, user_text: str, base=None) -> str:
    """去档案里翻。平时不翻，只有说到旧事、或者他追问时才翻。"""
    if not getattr(cfg, "recall_enabled", True):
        return ""
    q = user_text or ""
    asking = any(w in q for w in ASKING) or "还记得" in q
    hits = archive.search(q, base=base, limit=getattr(cfg, "recall_limit", 3),
                          min_hit=2 if asking else 3)
    if not hits:
        return ""
    return "\n".join(archive.render(seg, lines=4) for seg in hits)


def context(store, conn, cfg: Config, history=None, user_text: str = "", base=None):
    """返回 (近期细节, 事实, 印象, 档案里想起来的事)。

    - 细节：hot_days 天内的原文
    - 印象：最近 impression_days 天的每日印象
    - 档案：不常驻，只在说到旧事/追问时检索，命中才给
    """
    recent = store.recent_days(conn, getattr(cfg, "hot_days", 3),
                               limit=cfg.recent_n)
    facts = store.recall(conn, cfg.recall_k, half_life_days=cfg.fact_half_life_days)
    impressions = store.summaries_since(conn, getattr(cfg, "impression_days", 7))
    return (
        recent,
        trim_facts(facts, history=history or recent, user_text=user_text),
        impressions,
        recall_from_archive(cfg, user_text, base=base),
    )


COMPRESS_PROMPT = """把下面这段聊天压成一段概述，留给之后的自己看。
保留：聊了什么、他的近况和情绪、约定过的事、没说完的话题。
丢掉：寒暄、"在吗"、"嗯"、重复的话。
写成 2-4 句，用第三人称说他，不要点评，不要"感情很好"这种空话。

{lines}"""


def compress(store, conn, llm, keep: int = 400, batch: int = 200) -> int:
    """窗口满了就把最老的 batch 条压成一段概述，依次往前滚。返回压了几条。"""
    ids = store.window_overflow(conn, keep=keep, batch=batch)
    if not ids:
        return 0
    block = store.fetch_messages(conn, ids)
    if not block:
        return 0
    lines = "\n".join(f"{who}：{t}" for who, t in block["lines"])
    text = ""
    if llm:
        try:
            text = (llm(COMPRESS_PROMPT.format(lines=lines)) or "").strip()
        except Exception:
            text = ""
    if not text:
        text = f"（这段时间聊了 {block['count']} 条，细节记不清了）"
    store.add_chronicle(conn, block["ts_start"], block["ts_end"],
                        block["label"], text)
    store.archive_messages(conn, ids)
    return block["count"]
