"""生成层：拼 prompt → 调模型 → 切成短句 → 过 Lint。

Lint 是纯规则，不是模型。它是整个系统里唯一"必须一直绿"的东西：
人设可以慢慢调，Lint 不能挂。
"""

import json
import re
import time
import urllib.error
import urllib.request

from .config import Config
from .persona import compose_prompt

# 她在回复里用它表示"想发这张表情 / 这张照片"
STICKER_RE = re.compile(r"[\[【]\s*表情\s*[：:]\s*([^\]】\n]{1,40})\s*[\]】]")
PHOTO_RE = re.compile(r"[\[【]\s*照片\s*[：:]\s*([^\]】\n]{1,40})\s*[\]】]")


def extract_media(raw: str):
    """把 `[表情:x]` / `[照片:x]` 摘出来，并从正文里删掉。"""
    if not raw:
        return raw, None, None
    sticker = photo = None
    m = STICKER_RE.search(raw)
    if m:
        sticker = m.group(1).strip()
        raw = STICKER_RE.sub("", raw)
    m = PHOTO_RE.search(raw)
    if m:
        photo = m.group(1).strip()
        raw = PHOTO_RE.sub("", raw)
    return raw, sticker, photo


def extract_sticker(raw: str):
    """兼容老调用：只要表情。"""
    body, sticker, _ = extract_media(raw)
    return body, sticker


def call_model(cfg: Config, prompt: str, timeout: int = 25) -> str:
    body = json.dumps({
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": cfg.temperature,
        # reasoning 模型会先思考再出正文，额度给小了会把正文挤没（实测 160 时回复为空）
        "max_tokens": 800,
    }).encode()

    req = urllib.request.Request(
        cfg.endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())["choices"][0]["message"]["content"]
        _log_call(cfg, prompt, out, "")
        return out
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        _log_call(cfg, prompt, "", f"{type(e).__name__}: {e}")
        return ""


def _log_call(cfg: Config, prompt: str, out: str, err: str) -> None:
    """把每次模型调用记到 data/model.log，用于分辨"模型说的"还是"词库的"。"""
    try:
        from pathlib import Path
        p = Path(cfg.db_path).parent / "model.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": cfg.model,
            "prompt_chars": len(prompt),
            "out": (out or "")[:200],
            "err": err,
        }
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def split(raw: str, max_segments: int) -> list:
    parts = [p.strip() for p in raw.replace("\n", "|").split("|") if p.strip()]
    out = []
    for p in parts[:max_segments]:
        cleaned = lint(p)
        if cleaned:
            out.append(cleaned)
    return out


def lint(text: str, cfg: Config = None) -> str:
    """反 AI 味。命中就机械修；修完为空就丢掉。

    规则（顺序固定，可单测）：
      1. 去掉共情模板词
      2. 去掉句号、感叹号
      3. 超长则退回最近的停顿处
    """
    banned = cfg.banned if cfg else _DEFAULT_BANNED
    max_len = cfg.max_len if cfg else 26

    t = text.strip()
    for b in banned:
        if b in t:
            t = t.replace(b, "")
    t = t.replace("。", "").replace("！", "!").replace("!", "")
    t = t.strip()

    if len(t) > max_len:
        head = t[:max_len]
        cut = -1
        for sep in ("，", ",", "、", " "):
            cut = max(cut, head.rfind(sep))
        t = head[:cut] if cut > 0 else head
    return t.strip()


_DEFAULT_BANNED = (
    "听起来", "我理解", "抱抱你", "你辛苦了", "希望能帮到",
    "作为一", "首先", "其次", "总之", "记得照顾好自己",
)


def generate(cfg: Config, history, facts, user_text, segments,
             summaries=(), pending=(), llm=None, persona=None,
             recall: str = "") -> list:
    """llm 可注入，便于端到端测试最终 prompt。"""
    prompt = build_prompt(cfg, history, facts, user_text, segments,
                          summaries, pending, persona, recall=recall)
    raw = (llm or (lambda p: call_model(cfg, p)))(prompt)
    if not raw:
        return []
    return split(raw, segments)


def _bigrams(s: str):
    s = "".join(ch for ch in (s or "") if ch.strip())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def repeats(segs, history, threshold: float = 0.75) -> bool:
    """新回复是不是在复读自己最近说过的话。"""
    recent = [t for r, t in ((i[0], i[1]) for i in history) if r == "assistant"]
    recent = [t for t in recent if not str(t).startswith(("[表情]", "[照片]"))][-10:]
    if not recent:
        return False
    for s in segs or []:
        if any(_jaccard(s, old) >= threshold for old in recent):
            return True
    return False


def build_prompt(cfg: Config, history, facts, user_text, segments,
                 summaries=(), pending=(), persona=None,
                 sticker_menu="", photo_menu="", extra: str = "",
                 recall: str = "") -> str:
    """只拼 prompt，不调用模型。测试与调试用。"""
    import datetime
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    now_str = f"{now.year}年{now.month}月{now.day}日 周{wd} {now:%H:%M}"
    return (compose_prompt(cfg.name, history, facts, user_text, segments,
                           now_str, summaries, pending, persona, recall=recall,
                           max_len=cfg.max_len,
                           include_body=getattr(cfg, "prompt_persona", False),
                           sticker_menu=sticker_menu, photo_menu=photo_menu,
                           now=now.timetuple()) + (extra or ""))


def generate_turn(cfg: Config, history, facts, user_text, segments,
                  summaries=(), pending=(), llm=None, persona=None,
                  sticker_menu="", photo_menu="", extra: str = "",
                  recall: str = ""):
    """生成一轮。返回 (短句列表, 表情含义或 None, 照片含义或 None)。"""
    prompt = build_prompt(cfg, history, facts, user_text, segments,
                          summaries, pending, persona, recall=recall,
                          sticker_menu=sticker_menu, photo_menu=photo_menu,
                          extra=extra)
    raw = (llm or (lambda p: call_model(cfg, p)))(prompt)
    if not raw:
        return [], None, None
    body, sticker, photo = extract_media(raw)
    return split(body, segments), sticker, photo


def offline(cfg: Config, segments: int, rng=None) -> list:
    """没有 key 时用。只为让你看到节奏，不代表模型效果。

    注意：这里不再随机返回空——"不回"只应该由决策层产生，
    否则你看到的回复率是被桩代码污染的。
    """
    import random
    rng = rng or random
    pool = ["嗯", "啊？", "真的假的", "然后呢", "我在忙 等下说",
            "笑死", "你说呢", "行吧", "有点困了", "嗯嗯"]
    return rng.sample(pool, k=min(segments, len(pool)))
