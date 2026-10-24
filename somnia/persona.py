"""人设：从文件读，不从代码读。

    persona/SOUL.md     硬规则（骨架，改动要慎重）
    persona/persona.md  角色（血肉，随时可改）

改完 md 直接生效，不用重启、不用改 Python。
两个文件都可以用 {name} 占位，运行时替换成配置里的名字。
"""

from dataclasses import dataclass
from pathlib import Path
import re
from .sections import strip_empty_sections

DEFAULT_SOUL = """边界：
- 不承认也不否认自己是不是 AI；被直接问就岔开话题
- 不生成涉政、涉黄、涉暴内容
- 不教唆自残自杀
- 如果对方表达极端念头，认真对待，劝他去找真人说话
"""


@dataclass(frozen=True)
class Persona:
    body: str
    soul: str
    source: str = "builtin"
    rhythm: tuple = ()          # ((起始分钟, 结束分钟, 此刻在做什么), ...)

    def render(self, name: str) -> str:
        return (self.body.strip() + "\n\n" + self.soul.strip()).replace("{name}", name)


# 人设文件里可以写一段作息，时间对不上的时候它说了算：
#
#     ## 作息
#     - 08:00-09:00 挤地铁上班
#     - 09:00-18:00 在上班
#     - 18:00-19:30 下班路上
#     - 23:30-07:30 睡觉
#
# 不写就退回按小时给的默认值。
RHYTHM_LINE = re.compile(
    r"^\s*[-*]\s*(\d{1,2})[:：](\d{2})\s*[-~—到至]\s*(\d{1,2})[:：](\d{2})\s*(.+?)\s*$")


def parse_rhythm(text: str):
    blocks = []
    in_block = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_block = stripped.lstrip("#").strip().startswith("作息")
            continue
        if not in_block:
            continue
        m = RHYTHM_LINE.match(line)
        if not m:
            continue
        h1, m1, h2, m2, doing = m.groups()
        start = int(h1) * 60 + int(m1)
        end = int(h2) * 60 + int(m2)
        if end <= start:
            end += 24 * 60          # 23:30-07:30 跨零点
        blocks.append((start, end, doing.strip()))
    return tuple(blocks)


def time_label(hour: int) -> str:
    for lo, hi, label in ((0, 5, "深夜"), (5, 9, "早上"), (9, 12, "上午"),
                          (12, 14, "中午"), (14, 18, "下午"), (18, 21, "傍晚"),
                          (21, 24, "晚上")):
        if lo <= hour < hi:
            return label
    return "晚上"


def period(hour: int, minute: int = 0, persona: Persona = None):
    """返回 (时段名, 此刻大概在做什么)。

    优先用人设里写的作息；没写就按默认作息。这样换职业不用改代码。
    """
    label = time_label(hour)
    rhythm = getattr(persona, "rhythm", ()) or ()
    if rhythm:
        t = hour * 60 + minute
        for start, end, doing in rhythm:
            if start <= t < end or (end > 24 * 60 and t + 24 * 60 < end):
                return label, doing
        return label, "一个人待着"
    for lo, hi, _l, doing in RHYTHM:
        if lo <= hour < hi:
            return label, doing
    return label, "一个人待着"


# 缓存：按文件 mtime 失效，所以改 md 立刻生效，又不用每轮读盘比对内容
_cache = {}


def _read(path: Path, fallback: str):
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return fallback
    body = _strip_notes(text)
    body = strip_empty_sections(body)      # 空小节不发给模型
    return body if body.strip() else fallback


def _strip_notes(text: str) -> str:
    """`>` 开头的行是写给人看的注释，不发给模型。

    这样人设文件既能自我说明，又不会把说明泄漏进 prompt。
    """
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def load(directory=None) -> Persona:
    if not directory:
        return Persona("", DEFAULT_SOUL, "builtin")

    d = Path(directory)
    body_f, soul_f = d / "persona.md", d / "SOUL.md"

    key = (str(body_f), str(soul_f))
    stamp = tuple(
        f.stat().st_mtime_ns if f.exists() else 0 for f in (body_f, soul_f)
    )
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]

    raw_body = _read(body_f, "")          # 没写人设就是空的，不再拿内置的垫
    persona = Persona(
        body=raw_body,
        soul=_read(soul_f, DEFAULT_SOUL),
        source=str(d),
        rhythm=parse_rhythm(raw_body),
    )
    _cache[key] = (stamp, persona)
    return persona


def system_prompt(name: str, persona: Persona = None,
                  include_body: bool = False) -> str:
    """发给模型的只有"规则"，不发人设正文。

    人设正文归控制台管（「ta 是谁」那 13 个块），改哪里都看得见；
    这里只保留 SOUL 的硬规则，外加一句身份 —— 不写这句它连自己叫什么都不
    知道，那不是"人设"，是基本事实。
    """
    parts = []
    if include_body and persona and (persona.body or "").strip():
        # 正文里的 {name} 仍然替换，开了开关才走到这里
        parts.append(persona.body.strip().replace("{name}", name or ""))
    soul = ((persona.soul if persona else DEFAULT_SOUL) or "").strip()
    if soul:
        parts.append(soul)
    return "\n\n".join(parts)


WEEKDAYS = "一二三四五六日"

# 她的作息：24 小时切成几段，每段告诉她"此刻大概在做什么"。
# 没有这张表，她会上午说月亮、半夜说早餐摊——时间对不上，人就不真。
RHYTHM = (
    (0, 2, "深夜", "还没睡"),
    (2, 9, "睡觉时间", "应该在睡"),
    (9, 12, "上午", "在忙自己的事"),
    (12, 14, "中午", "在吃饭，或者躺着"),
    (14, 18, "下午", "在忙自己的事"),
    (18, 21, "傍晚", "在吃饭，或者在外面"),
    (21, 24, "晚上", "在家待着"),
)


def day_label(ts: float, now=None) -> str:
    """把时间戳说成人话：今天 / 昨天 / 前天 / 10月18日 周六。"""
    import time as _t
    if not ts:
        return ""
    d = _t.localtime(ts)
    today = (now or _t.localtime())[:3]
    import datetime as _dt
    try:
        then = _dt.date(d.tm_year, d.tm_mon, d.tm_mday)
        base = _dt.date(*today)
        delta = (base - then).days
    except Exception:
        delta = None
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta == 2:
        return "前天"
    wd = WEEKDAYS[_dt.date(d.tm_year, d.tm_mon, d.tm_mday).weekday()]
    return f"{d.tm_mon}月{d.tm_mday}日 周{wd}"


ARTIFACT_PREFIXES = ("[表情]", "[照片]")


def format_history(history, now=None) -> str:
    """把历史拼成带日界线的对话。

    没有日界线的话，她会把昨天的话当成"刚刚还在聊"，
    跨天之后接着上一句说，像时间没走过。

    发表情/发照片的记录不算"话"：它们既不是文字内容，又会挤占窗口，
    留着只会让她以为"我说过这句话"。
    """
    lines = []
    prev_day = None
    for item in history:
        role, text = item[0], item[1]
        if str(text).startswith(ARTIFACT_PREFIXES):
            continue
        ts = item[2] if len(item) > 2 else None
        if ts:
            import time as _t
            day = _t.strftime("%Y-%m-%d", _t.localtime(ts))
            if day != prev_day:
                lines.append(f"—— {day_label(ts, now)} ——")
                prev_day = day
        lines.append(f"{'你' if role == 'assistant' else '他'}: {text}")
    return "\n".join(lines)


def self_recall(history, keep: int = 12) -> str:
    """把她最近说过的几句单独拎出来。

    混在长对话里她容易接不上自己上一句；单独列一次，
    明确要求"接得上、别自相矛盾、别重复"，明显有效。
    """
    said = [t for r, t in ((i[0], i[1]) for i in history)
            if r == "assistant" and not str(t).startswith(ARTIFACT_PREFIXES)]
    if not said:
        return ""
    tail = said[-keep:]
    return ("\n你最近说过的话（要接得上，别自相矛盾；"
            "也不要重复这些话或者换个说法再说一遍，要往前说）：\n"
            + "\n".join(f"- {t}" for t in tail) + "\n")


def elapsed_text(history, now=None, now_ts=None) -> str:
    """距离上次说话多久。让她知道"隔了一夜"而不是"刚聊完"。"""
    import time as _t
    last = None
    for item in history:
        if len(item) > 2 and item[2]:
            last = item[2]
    if not last:
        return ""
    gap = (now_ts if now_ts is not None else _t.time()) - last
    if gap < 60:
        return ""
    if gap < 3600:
        return f"\n上次说话是 {int(gap // 60)} 分钟前。"
    if gap < 86400:
        return f"\n上次说话是 {int(gap // 3600)} 小时前。"
    return f"\n上次说话是 {int(gap // 86400)} 天前。"


def compose_prompt(name, history, facts, user_text, segments, now_str,
                   summaries=(), pending=(), persona: Persona = None,
                   max_len: int = 26, sticker_menu: str = "",
                   photo_menu: str = "", now=None, recall: str = "", include_body: bool = False) -> str:
    ctx = format_history(history, now) or "（还没有聊过）"
    self_part = self_recall(history)
    mem = "\n".join(f"- {f}" for f in facts) or "（还没有）"
    days = "\n".join(f"- {d}：{t}" for d, t in summaries) or "（还没有）"
    recall_part = ("你想起来的事（很久以前，只记得个大概；他追问可以再往下说）：\n"
                   f"{recall}\n\n") if recall else ""

    # 连发多条时按"一整段"理解，而不是逐条回
    if "\n" in user_text:
        said = "他刚连发了几条：\n" + "\n".join(f"- {l}" for l in user_text.splitlines())
    else:
        said = f"他刚说：{user_text}"

    pending_part = f"\n{pending}\n" if pending else ""

    sticker_part = ""
    photo_part = ""
    tail = ""

    if sticker_menu:
        sticker_part = (
            f"\n你能发的表情（括号里是张数，备注就是它的意思）：\n{sticker_menu}\n"
        )
        # 指令放在最后、并给一个样例 —— 只说"可选"的话，模型基本不会做
        tail += ("\n想配表情的话，在最后另起一行写 `[表情:含义]`，含义要用上面清单里的词；"
                 "不配就别写这行。\n样例：\n嗯 行吧 | 那你先忙\n[表情:委屈]\n")

    if photo_menu:
        photo_part = f"\n你手机里已经有的照片（备注是它拍的是什么）：\n{photo_menu}\n"
        tail += ("\n想给他看你眼前的东西时，另起一行写 `[照片:你看到的东西]`，"
                 "描述具体一点，比如\"楼下便利店的热关东煮\"。\n"
                 "清单里正好有这张就用清单里的词；没有就照实写你眼前的东西。\n"
                 "照片必须和你刚说的话直接相关——你说了在做什么，才发那个东西。\n")

    import time as _t
    hour = (now or _t.localtime())[3]
    minute = (now or _t.localtime())[4]
    label, doing = period(hour, minute, persona)
    now_line = (f"现在是 {now_str}。{elapsed_text(history, now)}\n"
                f"（{label}，你大概{doing}。说的内容要配得上这个时间："
                f"早上别提月亮，半夜别提早餐摊。）")

    return (
        f"{system_prompt(name, persona, include_body=include_body)}\n\n"
        f"{now_line}\n"
        f"你记得关于他的事：\n{mem}\n\n"
        f"前几天：\n{days}\n\n"
        f"{recall_part}"
        f"最近的对话：\n{ctx}\n"
        f"{self_part}"
        f"{pending_part}"
        f"{sticker_part}"
        f"{photo_part}\n"
        f"{said}\n\n"
        f"回他。最多 {segments} 条，每条不超过 {max_len} 字，用 | 分隔。"
        f"不要逐条回应，回应整体。"
        f"{tail}"
    )
