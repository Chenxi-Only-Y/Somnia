"""表情包：图片 + 备注。

文件放 `stickers/`，备注放 `data/stickers.json`。
备注的用处是"知道哪张是什么"（开心 / 无语 / 晚安），
以后想按语境选图时要用得上。
"""

import json
import re
from pathlib import Path

SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_BYTES = 5 * 1024 * 1024

# 照片的"时间归属"：靠备注里的词判断。半夜发"今天的外卖"、
# 中午发"凌晨的窗外"——都是没标时间的后果。
TIME_WORDS = {
    "凌晨": "deep_night", "深夜": "deep_night", "半夜": "deep_night", "睡不着": "deep_night",
    "早上": "morning", "早晨": "morning", "清晨": "morning", "早餐": "morning", "上午": "morning",
    "中午": "noon", "午饭": "noon", "午餐": "noon", "午休": "noon",
    "下午": "afternoon", "午后": "afternoon",
    "傍晚": "evening", "日落": "evening", "下班": "evening", "晚饭": "evening",
    "夜市": "evening", "黄昏": "evening",
    "晚上": "night", "夜里": "night", "今晚": "night",
}

SLOT_HOURS = (
    (0, 5, "deep_night"),
    (5, 11, "morning"),
    (11, 14, "noon"),
    (14, 17, "afternoon"),
    (17, 20, "evening"),
    (20, 24, "night"),
)

NEIGHBORS = {
    "deep_night": {"deep_night", "night"},
    "morning": {"morning", "noon"},
    "noon": {"noon", "morning", "afternoon"},
    "afternoon": {"afternoon", "noon", "evening"},
    "evening": {"evening", "afternoon", "night"},
    "night": {"night", "evening", "deep_night"},
}


def slot_of_hour(hour: int) -> str:
    for lo, hi, slot in SLOT_HOURS:
        if lo <= hour < hi:
            return slot
    return "night"


def time_tag(text: str):
    for word, slot in TIME_WORDS.items():
        if word in (text or ""):
            return slot
    return None

_SAFE = re.compile(r"[^\w\u4e00-\u9fff.\-]+")


def safe_name(name: str) -> str:
    """只留安全字符，保留后缀。防目录穿越。"""
    base = Path(str(name or "sticker")).name
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, "png"
    stem = _SAFE.sub("_", stem) or "sticker"
    suffix = _SAFE.sub("", suffix).lower() or "png"
    return f"{stem}.{suffix}"[:80]


def _bigrams(text: str):
    t = "".join(ch for ch in (text or "") if ch.strip() and ch.isprintable())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


POINTING = ("给你看", "你看", "看这个", "这张", "拍给", "发你", "给你", "拍张")

# 虚词不参与"搭不搭"的判断，否则"的/了/在"会让所有句子都相关
STOP_CHARS = set("的了在是我你他她它这那就都也还又很吧啊呢吗嘛哦呀个一不没有说着过和与给把被让"
                 "上下里外时候点儿什么怎样")


def content_chars(text: str):
    return {ch for ch in (text or "")
            if "\u4e00" <= ch <= "\u9fff" and ch not in STOP_CHARS}


def matches_text(note: str, text: str, threshold: float = 0.15) -> bool:
    """这张图和她刚说的话有没有关系。

    她挑图只看备注，不看自己说了什么 —— 结果是"今天累死了"配一张猫。
    这里做个确定性的兜底：

      1. 说了"给你看"这类指代 → 放行
      2. 字面二元组有重叠 → 放行
      3. 共用内容字（雨/猫/外卖）→ 放行
         "飘起雨了" 和 "被雨打湿的梧桐叶" 只共用一个"雨"，
         只靠二元组会误杀，所以补这一条
    """
    note = (note or "").strip()
    text = (text or "").strip()
    if not note or not text:
        return True
    if any(p in text for p in POINTING):
        return True

    a, b = _bigrams(text), _bigrams(note)
    if a and b and len(a & b) / min(len(a), len(b)) >= threshold:
        return True

    shared = content_chars(text) & content_chars(note)
    return bool(shared)


class StickerBook:
    def __init__(self, directory, meta_path=None, rng=None):
        self.dir = Path(directory)
        self.meta_path = (Path(meta_path) if meta_path
                          else self.dir.parent / "data" / f"{self.dir.name}.json")
        import random
        self.rng = rng or random
        self._recent = []

    # ── 元数据 ────────────────────────────────────────────

    def _defaults(self) -> dict:
        """随包发的备注：stickers/notes.json。用户改了也不动它。"""
        try:
            raw = json.loads((self.dir / "notes.json").read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _user_meta(self) -> dict:
        try:
            raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _meta(self) -> dict:
        """默认备注 + 用户改过的（用户优先）。

        为什么不直接读 data/：备注写在 data/ 里的话，随包发的表情包到了
        别人机器上就没有备注了 —— 没备注的图不进清单，等于白装。
        """
        out = self._defaults()
        out.update(self._user_meta())
        return out

    def _save_meta(self, data: dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    # ── 查询 ──────────────────────────────────────────────

    def files(self):
        try:
            return sorted(p for p in self.dir.iterdir()
                          if p.is_file() and p.suffix.lower() in SUFFIXES)
        except OSError:
            return []

    def items(self):
        meta = self._meta()
        out = []
        for p in self.files():
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "note": meta.get(p.name, ""),
                "bytes": st.st_size,
                "mtime": int(st.st_mtime),
            })
        return out

    def __len__(self):
        return len(self.files())

    # ── 增删改 ────────────────────────────────────────────

    def add(self, name: str, data: bytes, note: str = "") -> str:
        if len(data) > MAX_BYTES:
            raise ValueError("文件超过 5MB")
        fname = safe_name(name)
        if Path(fname).suffix.lower() not in SUFFIXES:
            raise ValueError(f"不支持的格式：{fname}")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / fname).write_bytes(data)
        if note:
            self.set_note(fname, note)
        return fname

    def set_note(self, name: str, note: str) -> bool:
        fname = safe_name(name)
        if not (self.dir / fname).exists():
            return False
        meta = self._user_meta()
        meta[fname] = (note or "").strip()[:40]
        self._save_meta(meta)
        return True

    def delete(self, name: str) -> bool:
        fname = safe_name(name)
        p = self.dir / fname
        if not p.exists():
            return False
        try:
            p.unlink()
        except OSError:
            return False
        meta = self._user_meta()
        meta.pop(fname, None)
        self._save_meta(meta)
        return True

    # ── 选图（发送时用）───────────────────────────────────

    def labels(self) -> dict:
        """{含义: [文件...]}。没写备注的不进清单——她自己都不知道那是什么意思。"""
        meta = self._meta()
        out = {}
        for p in self.files():
            note = str(meta.get(p.name, "")).strip()
            if note:
                out.setdefault(note, []).append(p)
        return out

    def menu_text(self, limit: int = 16, core: int = 8, slot: str = None) -> str:
        """给模型看的清单：只列含义，不列文件名。

        单纯按数量取前 N 个的话，新加进来的、含义唯一的图永远排不进去。
        所以是「高频前 core 个」+「其余里随机补齐」——
        常用的稳定出现，冷门的也有机会被看到。

        slot 是当前时段。给了就只留时间对得上的照片，
        备注里没写时间的（通用图）一律保留。
        """
        groups = self.labels()
        if not groups:
            return ""

        if slot:
            allowed = NEIGHBORS.get(slot, {slot})
            groups = {k: v for k, v in groups.items()
                      if time_tag(k) is None or time_tag(k) in allowed}
            if not groups:
                return ""

        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        head = ordered[:min(core, limit)]
        rest = ordered[len(head):]
        if len(head) < limit and rest:
            extra = min(limit - len(head), len(rest))
            head += self.rng.sample(rest, extra)

        return "\n".join(
            f"- {label}" + (f"（{len(files)} 张）" if len(files) > 1 else "")
            for label, files in head
        )

    def find(self, want: str):
        """按含义找一张；同一含义有多张就随机挑一张。"""
        if not want:
            return None
        want = want.strip()
        groups = self.labels()

        for label, files in groups.items():
            if label == want:
                return self.rng.choice(files)
        for label, files in groups.items():
            if want in label or label in want:
                return self.rng.choice(files)
        return None

    def note_of(self, path) -> str:
        try:
            return str(self._meta().get(Path(path).name, ""))
        except Exception:
            return ""

    def pick(self, recent_keep: int = 3):
        pool = self.files()
        if not pool:
            return None
        fresh = [p for p in pool if p.name not in self._recent]
        chosen = self.rng.choice(fresh or pool)
        self._recent.append(chosen.name)
        del self._recent[:-recent_keep]
        return chosen
