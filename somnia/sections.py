"""把人设文件切成块 —— 面板按块编辑，写回时保持框架不变。

人设文件是"给人看的框架 + 给模型看的内容"混在一起的：
`>` 开头的行是说明，其余是内容。这里负责把两者分开，
让面板能一块一块地编辑内容，而不破坏说明和顺序。
"""

import re

# 顺序即面板里的顺序；id 用于前端，title 必须和文件里的 `## 标题` 对得上
SECTIONS = [
    ("identity", "身份", "ta 是谁：年龄、眉眼里的一笔、做着什么、性格三两个词"),
    ("relation", "关系", "此刻走到哪一步 —— 初见，还是已经熟稔"),
    ("partner", "对方", "对面这个人是谁，ta 又如何看他"),
    ("life", "ta 的日常", "工作、同事、家人、一只猫、住在哪 —— 总得有点可说的事，否则只剩「在忙」"),
    ("voice", "说话方式", "句子的长短、标点、口头禅、走神的频率 —— 最容易走样的一块"),
    ("habits", "习惯与小动作", "语气词、惯用的标点、何时会连着说两句、何时会发图"),
    ("state", "状态", "忙、倦、心里不痛快的时候，ta 变成什么样"),
    ("mood", "情绪开关", "什么让 ta 欢喜、什么让 ta 不快、什么让 ta 吃味"),
    ("closeness", "黏人与边界", "主动到什么分寸；被冷落时，ta 如何自处"),
    ("taboo", "不做什么", "不能说出口的话，越多越好"),
    ("unknown", "ta 尚不知晓的事", "此刻 ta 还不该知道的事 —— 免得 ta 全知全能，说出没有来由的话"),
    ("examples", "示例", "好与坏的对照 —— 例子的分量，比规矩重"),
    ("rhythm", "作息", "一行一段时间，例如 08:00-09:00 挤地铁上班"),
]


BY_TITLE = {title: (sid, hint) for sid, title, hint in SECTIONS}
ORDER = [sid for sid, _, _ in SECTIONS]


def parse(text: str):
    """切块。返回 (头部行, [{title, hint:[行], body:[行]}, ...])。"""
    head, blocks = [], []
    cur = None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("## "):
            cur = {"title": s[3:].strip(), "hint": [], "body": []}
            blocks.append(cur)
            continue
        if s.startswith(">"):
            (cur["hint"] if cur else head).append(line)
            continue
        (cur["body"] if cur else head).append(line)
    return head, blocks


def to_json(text: str):
    """给面板的结构：[{id, title, hint, flaw, body}]。

    文件里没有的块也会列出来（body 空），这样面板始终是同一套框架。
    flaw 只是文案，面板显示用，保存时不会写回文件。
    """
    head, blocks = parse(text)
    found = {}
    extra = []
    for b in blocks:
        sid = BY_TITLE.get(b["title"], (None, None))[0]
        body = "\n".join(b["body"]).strip("\n")
        hint = "\n".join(b["hint"])
        if sid and sid not in found:
            found[sid] = {"id": sid, "title": b["title"], "hint": hint, "body": body}
        else:
            extra.append({"id": "x" + str(len(extra)), "title": b["title"],
                          "hint": hint, "body": body})

    out = []
    for sid, title, default_hint in SECTIONS:
        item = found.get(sid) or {"id": sid, "title": title,
                                  "hint": "> " + default_hint, "body": ""}
        out.append(item)
    out.extend(extra)
    out.append({"id": "_head", "title": "开头说明",
                "hint": "", "body": "\n".join(head).strip()})
    return out


def render(items) -> str:
    """按面板提交的内容重写文件。空块整段丢掉（不留空标题）。"""
    head = ""
    parts = []
    for it in items:
        if it.get("id") == "_head":
            head = (it.get("body") or "").strip()
            continue
        body = (it.get("body") or "").strip()
        if not body:
            continue                     # 没写的块不落地
        hint = (it.get("hint") or "").strip()
        block = f"## {it['title']}"
        if hint:
            block += "\n" + hint
        block += "\n\n" + body
        parts.append(block)

    out = []
    if head:
        out.append(head)
    out.extend(parts)
    return "\n\n".join(out).rstrip() + "\n"


_HEADING = re.compile(r"^##\s")
_NOTE = re.compile(r"^>")


def strip_empty_sections(body: str) -> str:
    """去掉没有内容的小节 —— 空标题只会让模型发懵。"""
    lines = body.splitlines()
    out, buf, keep = [], [], True
    def flush():
        if keep:
            out.extend(buf)
    for line in lines:
        if _HEADING.match(line):
            flush()
            buf, keep = [line], False      # 先假设是空块
        else:
            buf.append(line)
            if line.strip() and not _NOTE.match(line.strip()):
                keep = True
    flush()
    return "\n".join(out).strip()
