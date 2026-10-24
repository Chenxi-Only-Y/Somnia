"""新版本检查和覆盖更新。

两件事：
1. 从 `version.json` 看有没有新版（面板那个「↓ 新版本」就靠它）
2. 下载 zip 并覆盖代码，同时保住用户数据

`version.json` 长这样（放在你能给出稳定网址的地方）：

    {
      "version": "0.2.1",
      "zip": "https://example.com/Somnia-0.2.1.zip",
      "notes": "修了什么什么"
    }

没配这个文件也没关系 —— 面板不会显示任何东西，用户照样能用 update.bat 手动更新。
"""

import json
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# 这些目录/文件属于「用户自己的」，更新时绝不覆盖
PROTECTED_DIRS = {"data", "memory", "photos"}
PROTECTED_FILES = {"persona/persona.md", ".env"}

# 表情包要「合并」而不是「跳过」：新版加的图应该能收到，用户自己加的不覆盖
MERGE_DIRS = {"stickers"}
# 这几个是随包内容，允许覆盖（用户改的备注在 data/stickers.json，不在这）
ALWAYS_OVERWRITE = {"stickers/notes.json"}
BACKUP_FILES = ("somnia.db", "somnia.db-wal", "somnia.db-shm", "settings.json")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UA = "Somnia-Updater/1.0"


# ── 版本号 ────────────────────────────────────────────────

def parse_version(s: str):
    """'0.2.1' → (0, 2, 1)。非数字部分当 0，烂字符串当全 0。"""
    out = []
    for part in str(s or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def current_version(root: Path = None) -> str:
    root = Path(root) if root else PROJECT_ROOT
    try:
        for line in (root / "somnia" / "__init__.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "0"


# ── 检查 ──────────────────────────────────────────────────

def fetch_manifest(url: str, timeout: float = 8.0) -> dict:
    """读 version.json。file:// 也支持（测试和本地发布用）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError("version.json 里没有 version 字段")
    return data


def split_urls(url: str):
    """update_url 可以写多个，用逗号或空白隔开，挨个试。

    国内访问 raw.githubusercontent.com 经常不通，留一个备用地址（比如 jsDelivr）
    能救命。
    """
    parts = []
    for chunk in str(url or "").replace(",", " ").split():
        chunk = chunk.strip()
        if chunk and chunk not in parts:
            parts.append(chunk)
    return parts


def check(url: str, root: Path = None) -> dict:
    """面板用：返回一个永远不抛异常的结果。"""
    cur = current_version(root)
    out = {"current": cur, "latest": "", "has_new": False,
           "notes": "", "zip": "", "error": ""}
    urls = split_urls(url)
    if not urls:
        out["error"] = "没有配置更新地址"
        return out

    data, fails = None, []
    for u in urls:
        try:
            data = fetch_manifest(u)
            break
        except Exception as e:
            fails.append(f"{type(e).__name__}: {e}")
    if data is None:
        out["error"] = "；".join(fails)[:300]
        return out
    out["latest"] = str(data.get("version", ""))
    out["notes"] = str(data.get("notes", ""))[:500]
    out["zip"] = str(data.get("zip", ""))
    out["has_new"] = is_newer(out["latest"], cur) and bool(out["zip"])
    return out


# ── 覆盖更新 ──────────────────────────────────────────────

def looks_like_project(root: Path) -> bool:
    return ((root / "somnia" / "__init__.py").exists()
            and (root / "somnia.toml").exists())


def pick_root(path: Path) -> Path:
    """zip 里常常多套一层文件夹，往下找一层真正的项目根。"""
    if looks_like_project(path):
        return path
    try:
        kids = [p for p in path.iterdir() if p.is_dir() and not p.name.startswith("__")]
    except OSError:
        raise ValueError(f"读不了这个路径：{path}")
    for kid in kids:
        if looks_like_project(kid):
            return kid
    raise ValueError(f"这不像本项目的目录：{path}（找不到 somnia/__init__.py 和 somnia.toml）")


def is_protected(rel: Path) -> bool:
    posix = rel.as_posix()
    if posix in PROTECTED_FILES:
        return True
    return posix.split("/")[0] in PROTECTED_DIRS


def should_merge(rel: Path, dst: Path) -> bool:
    """表情包：目标里没有就补上，已经有了就留着不动。"""
    posix = rel.as_posix()
    if posix in ALWAYS_OVERWRITE:
        return False
    if posix.split("/")[0] not in MERGE_DIRS:
        return False
    return (Path(dst) / rel).exists()


def plan(src: Path, dst: Path):
    """先算清楚要覆盖哪些、跳过哪些，别一边算一边改。"""
    copy, skip = [], 0
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if any(part == "__pycache__" for part in rel.parts):
            continue
        if is_protected(rel) or should_merge(rel, dst):
            skip += 1
            continue
        copy.append((item, rel))
    return copy, skip


def backup_data(dst: Path) -> Path:
    """把 db 和设置备份到 data/backup/pre_update_时间/。"""
    data = Path(dst) / "data"
    out = data / "backup" / f"pre_update_{time.strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    for name in BACKUP_FILES:
        src = data / name
        if src.exists():
            shutil.copy2(src, out / name)
    return out


def apply_update(src: Path, dst: Path) -> dict:
    items, skipped = plan(src, dst)
    copied = 0
    for item, rel in items:
        target = Path(dst) / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied += 1
    return {"copied": copied, "skipped": skipped}


def extract(zip_path: Path):
    """解压到临时目录，返回 TemporaryDirectory（调用方负责 cleanup）。"""
    tmp = tempfile.TemporaryDirectory(prefix="somnia_update_")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp.name)
    return tmp


def download(url: str, timeout: float = 60.0) -> Path:
    """把 zip 下到临时文件。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = Path(tempfile.mkdtemp(prefix="somnia_dl_")) / "update.zip"
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    return tmp


def update_from_zip(zip_path: Path, dst: Path = None) -> dict:
    """从本地 zip 更新。返回 {old, new, copied, skipped, backup}。"""
    dst = Path(dst) if dst else PROJECT_ROOT
    if not looks_like_project(dst):
        raise ValueError(f"不是项目根目录：{dst}")

    tmp = extract(zip_path)
    try:
        src = pick_root(Path(tmp.name))
        old, new = current_version(dst), current_version(src)
        items, skipped = plan(src, dst)
        bak = backup_data(dst)
        stats = apply_update(src, dst)
        return {"old": old, "new": new, "copied": stats["copied"],
                "skipped": skipped, "backup": str(bak)}
    finally:
        tmp.cleanup()


def update_from_url(url: str, dst: Path = None) -> dict:
    zip_path = download(url)
    try:
        return update_from_zip(zip_path, dst)
    finally:
        shutil.rmtree(zip_path.parent, ignore_errors=True)
