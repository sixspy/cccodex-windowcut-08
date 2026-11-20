"""SQLite 连接、建表、种子。业务窗关账母仓公共库层。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FIXTURES = ROOT / "fixtures"
FRONTEND = ROOT / "frontend"

PRODUCT_META = {
    "modules": [
        "windows",
        "ingest",
        "late_bucket",
        "materialize",
        "reconcile",
        "locks",
        "audit",
        "hold_staging",
        "reopen",
    ],
    "stubs": ["hold_release", "late_promote", "partition_mat", "reopen"],
}


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    # autocommit（isolation_level=None）：事务边界由服务显式 BEGIN IMMEDIATE 控制，
    # 避免 Python sqlite3 默认 deferred 事务把写锁拖到首次 DML 才取、并发互斥失效。
    # timeout=0：写锁被占时后到者立即收到 SQLITE_BUSY，由锁层翻译为 409 争用——
    # 双人同改一桶只许一方成功，后到只记争用、不排队、不串行化“看似成功”。
    conn = sqlite3.connect(DB_PATH, timeout=0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def finish(conn: sqlite3.Connection) -> None:
    """安全收尾：事务仍开着才 commit；已 commit/rollback 则不动作。

    autocommit（isolation_level=None）连接上，对“无事务”状态再调 commit() 会隐式
    BEGIN 一个空事务，导致 finally 里的锁释放无法落库；这里用 in_transaction 守门。
    """
    if getattr(conn, "in_transaction", False):
        conn.commit()


def begin_immediate(conn: sqlite3.Connection) -> None:
    """开启立即写事务：进入即取 RESERVED 锁，并发写者在事务入口就撞锁而非中途。"""
    conn.execute("BEGIN IMMEDIATE")


def sha_password(password: str) -> str:
    import hashlib

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  password_sha TEXT NOT NULL,
  role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS windows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  open_at TEXT NOT NULL,
  close_at TEXT NOT NULL,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  seal_token TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  disposition TEXT NOT NULL,
  hold_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS late_bucket(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL,
  /* 甲路径：桶行必须挂存活 window_id 外键，窗删同事务拒删或级联迁移，杜绝孤儿/串名 */
  window_id INTEGER NOT NULL REFERENCES windows(id) ON DELETE RESTRICT,
  window_name TEXT,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
/* 迁移检疫：旧库回填 window_id 失败的桶行隔离于此，可核证、不静默丢弃、不串名 */
CREATE TABLE IF NOT EXISTS late_bucket_quarantine(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER,
  legacy_window_name TEXT,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  quarantined_at TEXT NOT NULL
);
/* 迟到归属册：选定甲/乙路径后当天禁改口；偏离拒写并记 late_attribution_drift */
CREATE TABLE IF NOT EXISTS late_attribution_registry(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  active_path TEXT NOT NULL,
  path_name TEXT NOT NULL,
  chosen_by TEXT NOT NULL,
  chosen_date TEXT NOT NULL,
  chosen_at TEXT NOT NULL,
  drift_rejections INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS materializations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  total_amount REAL NOT NULL,
  partition_key TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hold_staging(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS window_locks(
  lock_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contention_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lock_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_trail(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reopen_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  from_generation INTEGER NOT NULL,
  to_generation INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reject_slips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_name TEXT,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def ensure_users(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("admin", sha_password("123456"), "管理员"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("viewer", sha_password("view123"), "只读"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("closer", sha_password("close123"), "关账员"),
        )


def _load_fixture(name: str) -> list | dict | None:
    path = FIXTURES / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def seed_if_empty(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] > 0:
        return
    windows = _load_fixture("windows.json") or [
        {
            "name": "W-1101",
            "open_at": "2026-11-01T00:00:00",
            "close_at": "2026-11-01T23:59:59",
            "status": "已关闭",
            "generation": 1,
        },
        {
            "name": "W-1102",
            "open_at": "2026-11-02T00:00:00",
            "close_at": "2026-11-02T23:59:59",
            "status": "开启",
            "generation": 1,
        },
        {
            "name": "W-1103",
            "open_at": "2026-11-03T00:00:00",
            "close_at": "2026-11-03T23:59:59",
            "status": "开启",
            "generation": 1,
        },
    ]
    for w in windows:
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,?)",
            (
                w["name"],
                w["open_at"],
                w["close_at"],
                w["status"],
                int(w.get("generation", 1)),
            ),
        )
    events = _load_fixture("events.json") or [
        {
            "window_id": 1,
            "biz_key": "ORD-1",
            "event_ts": "2026-11-01T10:00:00",
            "amount": 12.5,
            "disposition": "窗内",
            "created_at": "2026-11-01T10:00:01",
        },
        {
            "window_id": None,
            "biz_key": "ORD-late",
            "event_ts": "2026-11-01T23:50:00",
            "amount": 3.0,
            "disposition": "迟到桶",
            "created_at": "2026-11-02T00:01:00",
        },
    ]
    for e in events:
        conn.execute(
            "INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at) VALUES (?,?,?,?,?,?)",
            (
                e.get("window_id"),
                e["biz_key"],
                e["event_ts"],
                e["amount"],
                e["disposition"],
                e.get("created_at", e["event_ts"]),
            ),
        )
        if e["disposition"] == "迟到桶":
            seed_w = conn.execute(
                "SELECT id FROM windows WHERE name='W-1101'"
            ).fetchone()
            conn.execute(
                """INSERT INTO late_bucket(event_id,window_id,window_name,biz_key,event_ts,amount,reason,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                    seed_w["id"],
                    "W-1101",
                    e["biz_key"],
                    e["event_ts"],
                    e["amount"],
                    "关窗后到达",
                    e.get("created_at", e["event_ts"]),
                ),
            )
    conn.execute(
        "INSERT INTO materializations(window_id,status,row_count,total_amount,partition_key,created_at) VALUES (?,?,?,?,?,?)",
        (1, "成功", 1, 12.5, "W-1101-g1", "2026-11-02T00:10:00"),
    )
    conn.execute(
        "INSERT INTO audit_trail(actor,action,target,payload,created_at) VALUES (?,?,?,?,?)",
        ("system", "seed", "windows", json.dumps({"count": len(windows)}), "2026-11-01T00:00:00"),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_late_bucket_fk(conn: sqlite3.Connection) -> None:
    """挂卷迁移：旧 late_bucket（仅 window_name）升级为甲路径外键表。

    - 能按窗名匹配到存活窗的行回填 window_id（同名窗取现存唯一行）；
    - 匹配不上的行整笔转入 late_bucket_quarantine 检疫，可核证、不丢、不串名；
    - 表整体重建以挂上 NOT NULL + FOREIGN KEY，迁移在单事务内完成。
    """
    if "window_id" in _table_columns(conn, "late_bucket"):
        return  # 新库或已迁移
    conn.execute(
        """CREATE TABLE late_bucket_new(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             event_id INTEGER NOT NULL,
             window_id INTEGER NOT NULL REFERENCES windows(id) ON DELETE RESTRICT,
             window_name TEXT,
             biz_key TEXT NOT NULL,
             event_ts TEXT NOT NULL,
             amount REAL NOT NULL,
             reason TEXT NOT NULL,
             created_at TEXT NOT NULL
           )"""
    )
    from datetime import datetime

    now = datetime.now().isoformat(timespec="seconds")
    legacy_rows = conn.execute("SELECT * FROM late_bucket").fetchall()
    for r in legacy_rows:
        w = conn.execute(
            "SELECT id FROM windows WHERE name=?", (r["window_name"],)
        ).fetchone()
        if w is None:
            conn.execute(
                """INSERT INTO late_bucket_quarantine(
                     event_id,legacy_window_name,biz_key,event_ts,amount,reason,
                     created_at,quarantined_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    r["event_id"], r["window_name"], r["biz_key"], r["event_ts"],
                    r["amount"], r["reason"], r["created_at"], now,
                ),
            )
            continue
        conn.execute(
            """INSERT INTO late_bucket_new(
                 id,event_id,window_id,window_name,biz_key,event_ts,amount,reason,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                r["id"], r["event_id"], w["id"], r["window_name"], r["biz_key"],
                r["event_ts"], r["amount"], r["reason"], r["created_at"],
            ),
        )
    conn.execute("DROP TABLE late_bucket")
    conn.execute("ALTER TABLE late_bucket_new RENAME TO late_bucket")


def _ensure_attribution_registry(conn: sqlite3.Connection) -> None:
    """归属册初始化：本仓业务窗关账选甲路径（window_id 外键），当天锁定。"""
    from datetime import datetime

    row = conn.execute("SELECT id FROM late_attribution_registry WHERE id=1").fetchone()
    if row is not None:
        return
    today = datetime.now().date().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO late_attribution_registry(
             id,active_path,path_name,chosen_by,chosen_date,chosen_at,drift_rejections)
           VALUES (1,'A','window_id_fk','system',?,?,0)""",
        (today, now),
    )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_late_bucket_fk(conn)
        _ensure_attribution_registry(conn)
        ensure_users(conn)
        seed_if_empty(conn)
        conn.commit()


def write_audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    target: str | None = None,
    payload: dict | None = None,
    created_at: str | None = None,
) -> None:
    from datetime import datetime

    now = created_at or datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO audit_trail(actor,action,target,payload,created_at) VALUES (?,?,?,?,?)",
        (actor, action, target, json.dumps(payload or {}, ensure_ascii=False), now),
    )
