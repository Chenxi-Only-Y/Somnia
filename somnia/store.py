"""SQLite 持久层。只管存取，不含策略。"""

import sqlite3
import time
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    id      INTEGER PRIMARY KEY,
    msg_id  TEXT UNIQUE,          -- 通道消息 ID，用于幂等去重
    role    TEXT NOT NULL,        -- user | assistant
    ts      REAL NOT NULL,
    text    TEXT NOT NULL,
    archived INTEGER DEFAULT 0    -- 1 = 已压缩进"更早的事"，不再进原文窗口
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

-- 压缩过的老对话：窗口满了就把最老的 200 条压成一段，依次往前滚
CREATE TABLE IF NOT EXISTS chronicle(
    id       INTEGER PRIMARY KEY,
    ts_start REAL NOT NULL,
    ts_end   REAL NOT NULL,
    label    TEXT NOT NULL,
    text     TEXT NOT NULL,
    ts       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS facts(
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    text       TEXT NOT NULL,
    norm       TEXT,
    importance REAL DEFAULT 0.5,
    last_seen  REAL,
    hits       INTEGER DEFAULT 0,
    retired    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS decisions(
    id        INTEGER PRIMARY KEY,
    msg_id    TEXT,
    ts        REAL NOT NULL,
    act       TEXT NOT NULL,      -- reply | skip | quiet
    delay     REAL DEFAULT 0,
    segments  INTEGER DEFAULT 1,
    reason    TEXT
);

CREATE TABLE IF NOT EXISTS outbox(
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    thought TEXT NOT NULL,
    score   REAL DEFAULT 0,
    status  TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS summaries(
    day   TEXT PRIMARY KEY,      -- YYYY-MM-DD，本地日期
    ts    REAL NOT NULL,
    text  TEXT NOT NULL
);

-- 小状态（水位线之类）
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY,
    v TEXT
);

-- 删过的消息记在这里。通道重启会重新读到微信里的老消息，
-- 没有这张表的话，删掉的东西会被重新写回来（表现为"重启就复活"）。
CREATE TABLE IF NOT EXISTS deleted_msgs(
    msg_id TEXT PRIMARY KEY,
    ts     REAL NOT NULL
);

-- 她说过的承诺："晚上找你""晚点跟你说"。到点必须兑现，
-- 否则她就是个说话不算数的人。
CREATE TABLE IF NOT EXISTS promises(
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    text   TEXT NOT NULL,
    due    REAL NOT NULL,
    status TEXT DEFAULT 'pending'
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")   # 主循环与唤醒线程并发写
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn) -> None:
    """老库补列。开发期会反复改结构，迁移比删库友好。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(facts)")}
    for name, ddl in (
        ("norm", "TEXT"),
        ("importance", "REAL DEFAULT 0.5"),
        ("last_seen", "REAL"),
        ("retired", "INTEGER DEFAULT 0"),
        ("expires", "REAL"),          # 到点自动忘掉（"他今天要去看牙"这类）
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE facts ADD COLUMN {name} {ddl}")

    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(promises)")}
    if "late" not in pcols:
        conn.execute("ALTER TABLE promises ADD COLUMN late INTEGER DEFAULT 0")

    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "archived" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN archived INTEGER DEFAULT 0")
        conn.execute("UPDATE messages SET archived = 0 WHERE archived IS NULL")

    conn.execute("UPDATE facts SET norm = text WHERE norm IS NULL")
    conn.execute("UPDATE facts SET last_seen = ts WHERE last_seen IS NULL")
    conn.execute("UPDATE facts SET retired = 0 WHERE retired IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_norm ON facts(norm)")


def add_message(conn, role: str, text: str, msg_id: Optional[str] = None) -> bool:
    """写入一条消息。msg_id 重复、或曾被删除时返回 False（幂等）。"""
    if msg_id and is_deleted(conn, msg_id):
        return False
    try:
        conn.execute(
            "INSERT INTO messages(msg_id, role, ts, text) VALUES(?,?,?,?)",
            (msg_id, role, time.time(), text),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def seen(conn, msg_id: str) -> bool:
    if not msg_id:
        return False
    row = conn.execute("SELECT 1 FROM messages WHERE msg_id = ? LIMIT 1", (msg_id,)).fetchone()
    return row is not None


def recent(conn, n: int):
    rows = conn.execute(
        "SELECT role, text FROM messages ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [(r["role"], r["text"]) for r in reversed(rows)]


def recent_with_ts(conn, n: int):
    """带时间戳的最近消息。她需要据此知道"这是昨天说的"。"""
    rows = conn.execute(
        "SELECT role, text, ts FROM messages WHERE archived = 0 "
        "ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [(r["role"], r["text"], r["ts"]) for r in reversed(rows)]


def _today_start(now=None) -> float:
    now = time.time() if now is None else now
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def recent_days(conn, days: int = 3, limit: int = 400, now=None):
    """按"天数"取原文，而不是按条数。

    固定条数的问题是：聊得密的那天能吃掉整个窗口，昨天的事反而被挤掉。
    按天取更符合"她记得最近这几天"的直觉。
    """
    start = _today_start(now) - max(0, days - 1) * 86400
    rows = conn.execute(
        "SELECT role, text, ts FROM messages WHERE ts >= ? AND archived = 0 "
        "ORDER BY id DESC LIMIT ?",
        (start, limit)).fetchall()
    return [(r["role"], r["text"], r["ts"]) for r in reversed(rows)]


def window_overflow(conn, keep: int = 400, batch: int = 200):
    """窗口满了没有。返回该压缩的那一批 (最老的 batch 条，且不碰最近 keep 条)。"""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE archived = 0").fetchone()["n"]
    if total <= keep:
        return []
    take = min(batch, total - keep)
    rows = conn.execute(
        "SELECT id FROM messages WHERE archived = 0 ORDER BY id LIMIT ?", (take,)
    ).fetchall()
    return [r["id"] for r in rows]


def fetch_messages(conn, ids):
    """按 id 取原文，用来写概述。"""
    if not ids:
        return None
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT role, text, ts FROM messages WHERE id IN ({marks}) ORDER BY id",
        list(ids)).fetchall()
    if not rows:
        return None
    lt = time.localtime
    return {
        "ts_start": rows[0]["ts"],
        "ts_end": rows[-1]["ts"],
        "label": f"{time.strftime('%m月%d日', lt(rows[0]['ts']))}"
                 f"-{time.strftime('%m月%d日', lt(rows[-1]['ts']))}",
        "lines": [("他" if r["role"] == "user" else "她", r["text"]) for r in rows],
        "count": len(rows),
    }


def archive_messages(conn, ids) -> None:
    """标成"已压缩"，之后不再进原文窗口。"""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE messages SET archived = 1 WHERE id IN ({marks})", list(ids))
    conn.commit()


def add_chronicle(conn, ts_start: float, ts_end: float, label: str, text: str) -> None:
    conn.execute(
        "INSERT INTO chronicle(ts_start, ts_end, label, text, ts) VALUES(?,?,?,?,?)",
        (ts_start, ts_end, label, text, time.time()))
    conn.commit()


def chronicle_list(conn, limit: int = 6):
    rows = conn.execute(
        "SELECT label, text FROM chronicle ORDER BY ts_end DESC LIMIT ?", (limit,)
    ).fetchall()
    return [(r["label"], r["text"]) for r in reversed(rows)]


def messages_by_day(conn, days: int = 7, per_day: int = 200, now=None):
    """面板用：最近几天，每天一组，从近到远。"""
    base = _today_start(now)
    out = []
    for i in range(days):
        day_start = base - i * 86400
        rows = conn.execute(
            "SELECT id, role, text, ts FROM messages WHERE ts >= ? AND ts < ? "
            "ORDER BY id DESC LIMIT ?",
            (day_start, day_start + 86400, per_day)).fetchall()
        if not rows:
            continue
        items = [{"id": r["id"], "role": r["role"], "text": r["text"],
                  "time": time.strftime("%H:%M", time.localtime(r["ts"]))}
                 for r in reversed(rows)]
        out.append({
            "day": time.strftime("%Y-%m-%d", time.localtime(day_start)),
            "offset": i,
            "count": len(items),
            "items": items,
        })
    return out


def add_fact(conn, text: str, importance: float = 0.5, now=None,
             expires: float = None) -> str:
    """写入事实。返回 'insert' | 'merge' | 'retire+insert'。

    三件事：
      1. 归一化后完全相同 → 只更新 last_seen / importance
      2. 高度相似（字符二元组 Jaccard）→ 合并
      3. 与已有事实语义相反（喜欢/讨厌）→ 把旧的退休

    expires 是"到点自动忘"的时间戳——"他今天要去看牙"过两天就不该再提。
    """
    now = time.time() if now is None else now
    norm = normalize(text)

    for row in conn.execute(
        "SELECT id, norm, importance, expires FROM facts WHERE retired = 0"
    ):
        if not row["norm"]:
            continue
        if row["norm"] == norm or _similar(row["norm"], norm):
            conn.execute(
                "UPDATE facts SET last_seen=?, importance=MAX(importance, ?), "
                "expires=COALESCE(?, expires) WHERE id=?",
                (now, importance, expires, row["id"]),
            )
            conn.commit()
            return "merge"

    retired = _retire_opposites(conn, text, norm)
    conn.execute(
        "INSERT INTO facts(ts, text, norm, importance, last_seen, hits, retired, expires) "
        "VALUES(?,?,?,?,?,0,0,?)",
        (now, text, norm, importance, now, expires),
    )
    conn.commit()
    return "retire+insert" if retired else "insert"


def purge_expired(conn, now=None) -> int:
    """把到期的事实退休。不硬删——留个痕迹比清空好查。"""
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE facts SET retired = 1 WHERE retired = 0 AND expires IS NOT NULL "
        "AND expires <= ?", (now,))
    conn.commit()
    return cur.rowcount


def normalize(text: str) -> str:
    t = "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return t.lower()


def _bigrams(s: str):
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def _similar(a: str, b: str, threshold: float = 0.75) -> bool:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return False
    inter = len(ba & bb)
    return inter / len(ba | bb) >= threshold


OPPOSITES = (("喜欢", "讨厌"), ("喜欢", "不喜欢"), ("想要", "不想要"))


def _object_after(text: str, marker: str):
    if marker not in text:
        return None
    return normalize(text.split(marker, 1)[1])


def _retire_opposites(conn, text: str, norm: str) -> bool:
    """他说"我讨厌香菜"时，把"我喜欢香菜"退休掉。"""
    changed = False
    for a, b in OPPOSITES:
        for marker, opposite in ((a, b), (b, a)):
            obj = _object_after(text, marker)
            if not obj:
                continue
            for row in conn.execute(
                "SELECT id, text, norm FROM facts WHERE retired = 0"
            ).fetchall():
                old_obj = _object_after(row["text"], opposite)
                if not old_obj:
                    continue
                if _similar(old_obj, obj, threshold=0.6) or old_obj == obj:
                    conn.execute("UPDATE facts SET retired = 1 WHERE id = ?", (row["id"],))
                    changed = True
    return changed


def recall(conn, k: int = 0, now=None, half_life_days: float = 30.0,
           hard_cap: int = 200):
    """按 重要度 × 时间衰减 × 命中 排序。

    衰减用半衰期：30 天前的同等重要度，权重减半。
    k<=0 表示**不限制条数**（长期用下来事实会越来越多，
    固定读 20 条等于把早先记住的事永久压在下面）。
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT id, text, ts, last_seen, importance, hits FROM facts "
        "WHERE retired = 0 AND (expires IS NULL OR expires > ?)", (now,)
    ).fetchall()

    scored = []
    for r in rows:
        seen = r["last_seen"] or r["ts"]
        age_days = max(0.0, (now - seen) / 86400.0)
        decay = 0.5 ** (age_days / half_life_days)
        imp = r["importance"] if r["importance"] is not None else 0.5
        boost = 1.0 + min(r["hits"] or 0, 20) * 0.05
        scored.append((imp * decay * boost, r["id"], r["text"]))

    scored.sort(reverse=True)
    top = scored if (k is None or k <= 0) else scored[:k]
    top = top[:hard_cap]

    for _, fid, _ in top:
        conn.execute("UPDATE facts SET hits = hits + 1 WHERE id = ?", (fid,))
    conn.commit()
    return [t for _, _, t in top]


def log_decision(conn, msg_id, act, delay, segments, reason) -> None:
    conn.execute(
        "INSERT INTO decisions(msg_id, ts, act, delay, segments, reason) VALUES(?,?,?,?,?,?)",
        (msg_id, time.time(), act, delay, segments, reason),
    )
    conn.commit()


def decision_stats(conn):
    """复盘用：各决策类型的次数。"""
    rows = conn.execute(
        "SELECT act, COUNT(*) AS n FROM decisions GROUP BY act ORDER BY n DESC"
    ).fetchall()
    return {r["act"]: r["n"] for r in rows}


# ── L2：日摘要 ────────────────────────────────────────────

def messages_on(conn, day: str):
    """按本地日期取当天消息。day 为 YYYY-MM-DD。"""
    rows = conn.execute(
        "SELECT role, text, ts FROM messages ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        if time.strftime("%Y-%m-%d", time.localtime(r["ts"])) == day:
            out.append((r["role"], r["text"]))
    return out


def add_summary(conn, day: str, text: str) -> None:
    conn.execute(
        "INSERT INTO summaries(day, ts, text) VALUES(?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET text=excluded.text, ts=excluded.ts",
        (day, time.time(), text),
    )
    conn.commit()


def summaries_since(conn, days: int = 3):
    rows = conn.execute(
        "SELECT day, text FROM summaries ORDER BY day DESC LIMIT ?", (days,)
    ).fetchall()
    return [(r["day"], r["text"]) for r in reversed(rows)]


def has_summary(conn, day: str) -> bool:
    return conn.execute("SELECT 1 FROM summaries WHERE day = ?", (day,)).fetchone() is not None


def days_with_messages(conn):
    """返回有消息但还没摘要的日期（升序）。"""
    seen = set()
    for (ts,) in conn.execute("SELECT ts FROM messages"):
        seen.add(time.strftime("%Y-%m-%d", time.localtime(ts)))
    done = {r["day"] for r in conn.execute("SELECT day FROM summaries")}
    return sorted(seen - done)


# ── Outbox：憋着的念头 ────────────────────────────────────

def last_message_ts(conn) -> float:
    """她这边最后一条消息的时间。通道用它判断"停机期间漏了什么"。"""
    row = conn.execute("SELECT MAX(ts) AS t FROM messages").fetchone()
    return float((row["t"] if row else 0) or 0.0)


def is_deleted(conn, msg_id: str) -> bool:
    return conn.execute("SELECT 1 FROM deleted_msgs WHERE msg_id=?",
                        (str(msg_id),)).fetchone() is not None


def delete_message(conn, msg_id_or_id) -> bool:
    """按行 id 删一条消息，并记下它的通道 msg_id —— 重启后不会再回来。"""
    row = conn.execute("SELECT id, msg_id FROM messages WHERE id=?",
                       (int(msg_id_or_id),)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
    if row["msg_id"]:
        conn.execute("INSERT OR REPLACE INTO deleted_msgs(msg_id, ts) VALUES(?,?)",
                     (row["msg_id"], time.time()))
    conn.commit()
    return True


def delete_messages(conn, ids) -> int:
    """批量删。返回值是真正删掉的条数。"""
    n = 0
    for i in ids or []:
        try:
            if delete_message(conn, i):
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def recent_with_ids(conn, limit: int = 12):
    rows = list(conn.execute(
        "SELECT id, role, text FROM messages ORDER BY id DESC LIMIT ?", (limit,)))
    return [{"id": i, "role": r, "text": t} for i, r, t in reversed(rows)]


def outbox_add(conn, thought: str, score: float) -> int:
    cur = conn.execute(
        "INSERT INTO outbox(ts, thought, score, status) VALUES(?,?,?,'pending')",
        (time.time(), thought, score),
    )
    conn.commit()
    return cur.lastrowid


def outbox_take(conn, threshold: float = 0.6, limit: int = 3):
    """取出够格的念头，但**不**标记已说。"""
    rows = conn.execute(
        "SELECT id, thought FROM outbox WHERE status='pending' AND score >= ? "
        "ORDER BY id LIMIT ?", (threshold, limit)
    ).fetchall()
    return [(r["id"], r["thought"]) for r in rows]


def outbox_deliver(conn, ids) -> None:
    """真正说出去之后再标记，避免生成失败时念头被吞掉。"""
    if not ids:
        return
    conn.executemany("UPDATE outbox SET status='delivered' WHERE id=?",
                     [(i,) for i in ids])
    conn.commit()


def outbox_pending_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]


def last_user_ts(conn):
    row = conn.execute(
        "SELECT MAX(ts) AS t FROM messages WHERE role='user'").fetchone()
    return row["t"] if row and row["t"] else None


def last_decision_act(conn):
    """上一次的判断结果。用来避免"连着两次不回"。"""
    row = conn.execute("SELECT act FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    return row["act"] if row else None


def last_assistant_ts(conn):
    row = conn.execute(
        "SELECT MAX(ts) AS t FROM messages WHERE role='assistant'").fetchone()
    return row["t"] if row and row["t"] else None


def last_user_message(conn):
    row = conn.execute(
        "SELECT text, ts FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return (row["text"], row["ts"]) if row else (None, None)


def last_ts_with_reason(conn, reason: str):
    """最近一次某种原因的决策时间。"""
    row = conn.execute(
        "SELECT MAX(ts) AS t FROM decisions WHERE reason = ?", (reason,)).fetchone()
    return row["t"] if row and row["t"] else None


def add_promise(conn, text: str, due: float, now=None) -> int:
    now = time.time() if now is None else now
    cur = conn.execute(
        "INSERT INTO promises(ts, text, due, status) VALUES(?,?,?,'pending')",
        (now, text, due))
    conn.commit()
    return cur.lastrowid


def due_promise(conn, now=None):
    """到点该兑现的承诺（最早的一条）。"""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT id, text, due, late FROM promises WHERE status='pending' AND due <= ? "
        "ORDER BY due LIMIT 1", (now,)).fetchone()
    return dict(row) if row else None


def postpone_promise(conn, pid: int, new_due: float) -> None:
    """这次没兑现，往后推，并标成"迟了"。"""
    conn.execute("UPDATE promises SET due=?, late=1 WHERE id=?", (new_due, pid))
    conn.commit()


def close_promise(conn, pid: int, status: str = "done") -> None:
    conn.execute("UPDATE promises SET status=? WHERE id=?", (status, pid))
    conn.commit()


def cancel_promises(conn) -> int:
    """他说话了 —— 她已经在跟他讲，之前的"晚上找你"就不用再补了。"""
    cur = conn.execute("UPDATE promises SET status='superseded' WHERE status='pending'")
    conn.commit()
    return cur.rowcount


def pending_promises(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, text, due FROM promises WHERE status='pending' ORDER BY due")]


def count_said_today(conn, phrases, now=None) -> int:
    """今天她说过几句带这些词的话。用来限制"你怎么不找我"的频率。"""
    now = time.time() if now is None else now
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    rows = conn.execute(
        "SELECT text FROM messages WHERE role='assistant' AND ts >= ?", (start,))
    return sum(1 for r in rows if any(p in (r["text"] or "") for p in phrases))


def has_message_today(conn, now=None) -> bool:
    """今天聊过没有。晚间问候用它判断"一整天没动静"。"""
    now = time.time() if now is None else now
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    row = conn.execute(
        "SELECT 1 FROM messages WHERE ts >= ? LIMIT 1", (start,)).fetchone()
    return row is not None


def backup(conn, path: str) -> str:
    """清空前先落一份完整备份（用 sqlite 自己的 backup，WAL 也安全）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(p))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return str(p)


TABLES = ("messages", "facts", "summaries", "chronicle",
          "promises", "decisions", "outbox", "deleted_msgs")


def clear_all(conn, backup_dir: str = None) -> dict:
    """清空所有记忆。返回每张表清掉了多少条。"""
    out = {}
    if backup_dir:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out["_backup"] = backup(conn, str(Path(backup_dir) / f"somnia_{stamp}.db"))
    for t in TABLES:
        try:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            conn.execute(f"DELETE FROM {t}")
            out[t] = int(n or 0)
        except sqlite3.Error:
            out[t] = 0
    conn.commit()
    set_watermark(conn, time.time())      # 清空后，只有之后的消息才算新的
    try:
        conn.execute("VACUUM")
    except sqlite3.Error:
        pass
    return out


def messages_before(conn, ts: float):
    """ts 之前的全部消息（按时间升序）。归档用。"""
    rows = conn.execute(
        "SELECT id, role, text, ts FROM messages WHERE ts < ? ORDER BY ts",
        (ts,)).fetchall()
    return [(r["role"], r["text"], r["ts"], r["id"]) for r in rows]


def delete_messages_upto(conn, ts: float) -> int:
    cur = conn.execute("DELETE FROM messages WHERE ts < ?", (ts,))
    conn.commit()
    return cur.rowcount


def summaries_prune(conn, keep_days: int, now=None) -> int:
    """只留最近 keep_days 天的印象，更早的已经进档案了。"""
    import datetime
    now = time.time() if now is None else now
    cut = (datetime.date.fromtimestamp(now)
           - datetime.timedelta(days=max(1, keep_days))).strftime("%Y-%m-%d")
    cur = conn.execute("DELETE FROM summaries WHERE day < ?", (cut,))
    conn.commit()
    return cur.rowcount


def set_meta(conn, k: str, v) -> None:
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    conn.commit()


def get_meta(conn, k: str, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_watermark(conn, ts: float) -> None:
    """记下"这一刻之前的消息都算处理过"。清空记忆时用它。"""
    set_meta(conn, "since_ts", float(ts))


def watermark(conn) -> float:
    try:
        return float(get_meta(conn, "since_ts", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def since_ts(conn) -> float:
    """通道从这里往后补消息：取"最后一条消息"和"水位线"里更晚的那个。"""
    return max(last_message_ts(conn), watermark(conn))


def reply_streak(conn, limit: int = 50) -> int:
    """连着回了多少次。用来让"聊得顺"的时候更顺。"""
    rows = conn.execute(
        "SELECT act FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    n = 0
    for r in rows:
        if r["act"] != "reply":
            break
        n += 1
    return n


def decisions_with_reason_since(conn, reason: str, since: float) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE reason = ? AND ts >= ?",
        (reason, since)).fetchone()
    return int(row["n"] or 0)


def decisions_with_reason_today(conn, reason: str, now=None) -> int:
    """今天某种原因的决策次数（比如"被吵醒"）。"""
    now = time.time() if now is None else now
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE reason = ? AND ts >= ?",
        (reason, start)).fetchone()
    return int(row["n"] or 0)


def last_wake_ts(conn):
    row = conn.execute("SELECT MAX(ts) AS t FROM outbox").fetchone()
    return row["t"] if row and row["t"] else None


def wakes_today(conn, now=None) -> int:
    """今天已经主动说过几次。用来兜住"别太频繁"。"""
    now = time.time() if now is None else now
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE status='delivered' AND ts >= ?",
        (start,)).fetchone()
    return int(row["n"] or 0)

