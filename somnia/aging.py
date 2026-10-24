"""记忆分层：三天内留细节，一周内留印象，一个月后只剩档案。

    ≤3 天   细节    库里原文，直接进 prompt
    3–7 天  印象    原文搬进档案文件，库里只留每天两三句印象
    7–30 天 印象    只读最近一周；更早的读不到，但档案里还在
    >30 天  档案    库里连印象都删掉，只剩文件

档案不占 prompt。她"想起来"的时候才去翻（见 archive.search）。
"""

import time

from . import archive


IMPRESSION_PROMPT = """把下面这段聊天压成两三句"印象"，留给之后的自己看。
只留：那天聊了什么、他的状态和情绪、有过的约定。
不留：具体几点、原话、寒暄、"嗯""在吗"这类。
写成"那天…"的口气，不要评价，不要总结成"关系很好"。

{lines}"""


def _lines(seg) -> str:
    return "\n".join(f"{'他' if r == 'user' else '她'}：{t}" for r, t, _ in seg)


def impression(seg, llm=None) -> str:
    """一段对话 → 一句印象。模型不在就用首尾拼。"""
    text = ""
    if llm:
        try:
            text = (llm(IMPRESSION_PROMPT.format(lines=_lines(seg))) or "").strip()
        except Exception:
            text = ""
    if not text:
        head = seg[0][1][:24]
        tail = seg[-1][1][:24]
        text = f"那天从「{head}」聊到「{tail}」。"
    return text


def roll(store, conn, cfg, llm=None, now=None, base=None) -> dict:
    """把三天前的原文搬进档案，并留下印象。返回搬了多少。"""
    now = time.time() if now is None else now
    hot_days = getattr(cfg, "hot_days", 3)
    keep_days = getattr(cfg, "summary_keep_days", 30)
    cutoff = now - hot_days * 86400

    rows = store.messages_before(conn, cutoff)
    out = {"archived": 0, "segments": 0, "summaries": 0}
    if rows:
        plain = [(r, t, ts) for r, t, ts, _id in rows]
        segs = archive.split_segments(plain)
        sums = [impression(seg, llm) for seg in segs]
        out["archived"] = archive.append_segments(segs, sums, base=base)
        out["segments"] = len(segs)

        # 每段印象落到它开始的那一天
        for seg, sm in zip(segs, sums):
            day = time.strftime("%Y-%m-%d", time.localtime(seg[0][2]))
            old = dict(store.summaries_since(conn, 400)).get(day, "")
            store.add_summary(conn, day, (old + " " + sm).strip() if old else sm)
            out["summaries"] += 1

        store.delete_messages_upto(conn, cutoff)

    out["pruned"] = store.summaries_prune(conn, keep_days, now=now)
    return out
