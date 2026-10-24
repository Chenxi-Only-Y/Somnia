"""并发测试：主循环写、唤醒线程写、读同时进行。

真实形态是「主循环 + 调度器线程」两个连接并发，
所以这里就用两个连接压，而不是单连接顺序跑。

跑法：python tests/test_concurrency.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store


def _db_path():
    return str(Path(tempfile.mkdtemp()) / "c.db")


def test_concurrent_writers_and_readers():
    db = _db_path()
    store.connect(db).close()          # 先建库，避免建表竞争

    stop = threading.Event()
    errors = []
    writes = {"n": 0}

    def writer(tag):
        c = store.connect(db)
        i = 0
        while not stop.is_set() and i < 200:
            try:
                store.add_message(c, "user", f"{tag}-{i}")
                if i % 10 == 0:
                    store.add_fact(c, f"{tag}说喜欢{i % 5}", importance=0.6)
                    store.log_decision(c, None, "reply", 1.0, 1, "busy")
                writes["n"] += 1
                i += 1
            except Exception as e:
                errors.append(f"{tag}: {type(e).__name__}: {e}")
                return

    def reader():
        c = store.connect(db)
        while not stop.is_set():
            try:
                store.recent(c, 10)
                store.recall(c, 5)
                store.outbox_pending_count(c)
            except Exception as e:
                errors.append(f"reader: {type(e).__name__}: {e}")
                return
            time.sleep(0.002)

    threads = [
        threading.Thread(target=writer, args=("a",), daemon=True),
        threading.Thread(target=writer, args=("b",), daemon=True),
        threading.Thread(target=reader, daemon=True),
    ]
    for t in threads:
        t.start()

    deadline = time.time() + 15
    while time.time() < deadline and writes["n"] < 400:
        time.sleep(0.02)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors[:3]
    assert writes["n"] == 400, writes["n"]


def test_busy_timeout_is_set():
    c = store.connect(_db_path())
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_scheduler_thread_connection_is_independent():
    """调度器用独立连接，不能和主连接共用对象。"""
    db = _db_path()
    main = store.connect(db)
    store.add_message(main, "user", "在吗")
    worker = store.connect(db)
    assert worker is not main
    assert store.recent(worker, 5) == [("user", "在吗")]


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
