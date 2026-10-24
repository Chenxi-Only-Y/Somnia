"""永久档案：三天以前的对话，原文搬进文件，库里只留印象。

为什么要搬：
- 库里留原文 → 越攒越大，每次都要扫，而且她会忍不住把旧事拿出来重复讲
- 搬进文件 → 平时不说话、不占 prompt；真问起来，能翻出原话，连一整段事

文件按段落存，一段 = 一次连续对话（间隔超过 30 分钟就断开）。
段落里既有摘要（她"想起"的印象），也有原话（追问时能还原细节）。
"""

import json
import time
from pathlib import Path

GAP = 30 * 60            # 超过半小时算另一段
DEFAULT_DIR = Path("memory") / "archive"


def _dir(base=None) -> Path:
    p = Path(base) if base else DEFAULT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file(day: str, base=None) -> Path:
    """一天一个文件：memory/archive/2026-10-20.jsonl"""
    return _dir(base) / f"{day}.jsonl"


def split_segments(rows, gap: float = GAP):
    """把消息切成"一段事"。rows: [(role, text, ts)]，按时间升序。"""
    segs, cur = [], []
    for role, text, ts in rows:
        if cur and ts - cur[-1][2] > gap:
            segs.append(cur)
            cur = []
        cur.append((role, text, ts))
    if cur:
        segs.append(cur)
    return segs


def _seg_id(start_ts: float, i: int) -> str:
    return time.strftime("%Y%m%d-%H%M", time.localtime(start_ts)) + f"-{i}"


def append_segments(segments, summaries=None, base=None) -> int:
    """写进档案。segments 是 split_segments 的结果，summaries 是可选摘要列表。"""
    if not segments:
        return 0
    by_day = {}
    for i, seg in enumerate(segments):
        start = seg[0][2]
        day = time.strftime("%Y-%m-%d", time.localtime(start))
        record = {
            "seg": _seg_id(start, i),
            "day": day,
            "start": start,
            "end": seg[-1][2],
            "sum": (summaries[i] if summaries and i < len(summaries) else ""),
            "lines": [[r, t, ts] for r, t, ts in seg],
        }
        by_day.setdefault(day, []).append(record)

    n = 0
    for day, records in by_day.items():
        with open(_file(day, base), "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
    return n


# 档案是只追加的，文件一变缓存就失效。不缓存的话，每问一句旧事都要把
# 所有 jsonl 重新读一遍、重新算字符集 —— 聊上一年会明显变慢。
_CACHE = {"key": None, "segs": [], "chars": []}


def _sig(base=None):
    d = _dir(base)
    items = []
    for f in sorted(d.glob("*.jsonl")):
        try:
            st = f.stat()
            items.append((f.name, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return (str(d), tuple(items))


def _chars(text: str):
    return {ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff" and ch not in STOP}


def _index(base=None):
    """解析过的段落 + 每段的字符集，按文件签名缓存。"""
    key = _sig(base)
    if _CACHE["key"] == key:
        return _CACHE["segs"], _CACHE["chars"]

    segs, chars = [], []
    for f in sorted(_dir(base).glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                seg = json.loads(line)
                segs.append(seg)
                chars.append(_chars((seg.get("sum") or "") +
                                    "".join(t for _, t, _ in seg.get("lines", []))))
        except (OSError, ValueError):
            continue
    _CACHE.update(key=key, segs=segs, chars=chars)
    return segs, chars


def load(base=None, days: int = None):
    """读出档案里的所有段落（按天倒序，最多 days 天）。"""
    segs, _ = _index(base)
    if not days:
        return list(segs)
    keep = set(sorted({s.get("day", "") for s in segs}, reverse=True)[:days])
    return [s for s in segs if s.get("day", "") in keep]


STOP = set("的了在是我你他她它这那就都也还又很吧啊呢吗嘛哦呀个一不没有说着过和与给把被让"
           "上下里外时候点儿什么怎样多少")


def search(query: str, base=None, limit: int = 3, min_hit: int = 2):
    """按内容字的重合找段落。命中越多越靠前。"""
    mine = _chars(query)
    if len(mine) < 2:
        return []
    segs, chars = _index(base)
    scored = []
    for seg, cs in zip(segs, chars):
        hit = len(mine & cs)
        if hit >= min_hit:
            scored.append((hit, seg))
    scored.sort(key=lambda x: (-x[0], -x[1].get("end", 0)))
    return [seg for _, seg in scored[:limit]]


def find(seg_id: str, base=None):
    """按 id 取一整个段落 —— 追问时用它还原前后。"""
    for seg in load(base):
        if seg.get("seg") == seg_id:
            return seg
    return None


def render(seg, lines: int = 6) -> str:
    """把一段档案变成能塞进 prompt 的几行。"""
    if not seg:
        return ""
    day = seg.get("day", "")
    sumtxt = (seg.get("sum") or "").strip()
    body = []
    for role, text, _ts in seg.get("lines", [])[-lines:]:
        who = "你" if role == "user" else "ta"
        body.append(f"{who}：{text}")
    out = [f"- {day}" + (f"：{sumtxt}" if sumtxt else "")]
    out.extend("  " + b for b in body)
    return "\n".join(out)
