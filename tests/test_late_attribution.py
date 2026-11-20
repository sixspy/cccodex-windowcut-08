"""迟到归属：跨窗串桶/孤儿桶复现 + 甲路径（window_id 外键）正确性 + 桶锁争用。

流程约束（题面）：
1. 先造跨窗串桶/孤儿桶夹具复现错乱，落归属错乱失败测（红）；
2. 再落双人同时改同一桶行争用失败测（红）；
3. 两条红测齐备后才准改 late_svc/windows_svc/reconcile_svc 写路径；
4. 全绿后迟到明细、汇总卡、窗口条带、对账迟到计数、审计五处同态、无孤儿。

本地夹具，SQLite，不触网。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db
import db


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "late.db")
    init_db()
    yield


def _auth(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _raw() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _seed_late(conn: sqlite3.Connection, window_name: str, biz_key: str, amount: float):
    """兼容新旧两版表结构的造数：新版带 window_id，旧版仅 window_name。

    用于在同一套夹具上复现“按窗名汇总”的错乱，并在甲路径落地后复跑转绿。
    """
    w = conn.execute("SELECT id FROM windows WHERE name=?", (window_name,)).fetchone()
    assert w is not None, f"夹具窗口不存在: {window_name}"
    cur = conn.execute(
        "INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (None, biz_key, "2026-11-01T23:50:00", amount, "迟到桶", "2026-11-02T00:01:00"),
    )
    cols = _cols(conn, "late_bucket")
    if "window_id" in cols:
        conn.execute(
            "INSERT INTO late_bucket(event_id,window_id,window_name,biz_key,event_ts,"
            "amount,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (cur.lastrowid, w["id"], window_name, biz_key, "2026-11-01T23:50:00",
             amount, "关窗后到达", "2026-11-02T00:01:00"),
        )
    else:
        conn.execute(
            "INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,"
            "reason,created_at) VALUES (?,?,?,?,?,?,?)",
            (cur.lastrowid, window_name, biz_key, "2026-11-01T23:50:00",
             amount, "关窗后到达", "2026-11-02T00:01:00"),
        )
    conn.commit()


def _reproduce_cross_window_mix() -> dict:
    """夹具复现错乱前提：造两窗三桶行，再演示甲路径如何阻断“删窗→孤儿→同名串桶”。

    旧实现（按 window_name 分组、窗生命周期不绑定）下：
      1. 删窗后桶行成孤儿，summary 仍按字符串窗名分组；
      2. 同名新窗建立后，旧桶行被串到新窗名下，明细/摘要口径漂移。
    甲路径下：裸删被外键硬拦；遗留孤儿行也不会被同名新窗吸收。
    """
    with _raw() as conn:
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-A','2026-11-01T00:00:00','2026-11-01T23:59:59','已关闭',1)"
        )
        old_a = conn.execute("SELECT id FROM windows WHERE name='W-A'").fetchone()["id"]
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-B','2026-11-02T00:00:00','2026-11-02T23:59:59','开启',1)"
        )
        wb = conn.execute("SELECT id FROM windows WHERE name='W-B'").fetchone()["id"]
        _seed_late(conn, "W-A", "LATE-A1", 5.0)
        _seed_late(conn, "W-A", "LATE-A2", 7.0)
        _seed_late(conn, "W-B", "LATE-B1", 2.0)
        conn.commit()
    return {"old_a": old_a, "wb": wb, "orphan_n": 2}


# ---------------------------------------------------------------------------
# 红测 1：归属错乱（跨窗串桶 / 孤儿桶）——甲路径落地前必红
# ---------------------------------------------------------------------------
def test_red_cross_window_mix_and_orphan_bucket_invariants():
    ctx = _reproduce_cross_window_mix()

    with _raw() as conn:
        # 不变量 0：桶行必须挂 window_id 外键列
        lb_cols = _cols(conn, "late_bucket")
        assert "window_id" in lb_cols, "late_bucket 缺少 window_id 外键列"

        fks = conn.execute("PRAGMA foreign_key_list(late_bucket)").fetchall()
        assert any(fk["table"] == "windows" and fk["from"] == "window_id" for fk in fks), (
            "late_bucket.window_id 未挂 windows 外键"
        )

        # 旧实现的错乱动作（裸 SQL 删窗）必须被外键硬拦，桶行不成孤儿
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM windows WHERE id=?", (ctx["old_a"],))
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE window_id=?", (ctx["old_a"],)
        ).fetchone()["c"] == 2

        # 模拟历史遗留孤儿（关 FK 制造旧脏数据）：同名新窗不得吸收旧桶行
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM windows WHERE id=?", (ctx["old_a"],))
        conn.commit()
    # 新连接重新打开 FK
    with _raw() as conn:
        legacy_orphans = conn.execute(
            "SELECT lb.id FROM late_bucket lb LEFT JOIN windows w ON w.id=lb.window_id"
            " WHERE w.id IS NULL"
        ).fetchall()
        assert len(legacy_orphans) == ctx["orphan_n"] == 2
        # 同名重建 W-A（新 id）
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-A','2026-11-05T00:00:00','2026-11-05T23:59:59','开启',1)"
        )
        new_a = conn.execute("SELECT id FROM windows WHERE name='W-A'").fetchone()["id"]
        conn.commit()

        # 不变量 2：同名新窗不得继承旧窗桶行（杜绝跨窗串桶）
        rows_new_a = conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE window_id=?", (new_a,)
        ).fetchone()["c"]
        assert rows_new_a == 0, "旧窗桶行被串到同名新窗"

    # 不变量 3：汇总卡与明细表同源——按 window_id JOIN 存活窗，孤儿不进任何窗分组
    c = TestClient(app)
    h = _auth(c)
    summary = c.get("/api/late-bucket/summary", headers=h).json()
    detail = c.get("/api/late-bucket", headers=h).json()["items"]
    assert summary["orphan_buckets"] == 2
    by_window = {b["window_id"]: b for b in summary["by_window"]}
    # W-B 仍为 1 笔；新 W-A 为 0 笔（不出现在分组里），孤儿 2 笔不被串名
    assert by_window[ctx["wb"]]["c"] == 1
    assert new_a not in by_window
    # 明细只含能 JOIN 上存活窗的行（W-B 一行；种子 W-1101 行若在也同源），与摘要逐窗可对账
    assert all(r["window_id"] in by_window for r in detail)
    assert sum(b["c"] for b in summary["by_window"]) == len(detail)
    assert by_window[ctx["wb"]]["c"] == 1
    # 孤儿两笔被隔离在所有窗分组之外（含 W-B 在内的分组总数不含孤儿）
    assert summary["count"] == len(detail) + summary["orphan_buckets"]


def test_red_window_delete_reject_or_cascade_in_same_tx():
    """窗删则同事务拒删；级联迁移冲突失败回滚。"""
    c = TestClient(app)
    h = _auth(c)
    with _raw() as conn:
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-D','2026-11-01T00:00:00','2026-11-01T23:59:59','已关闭',1)"
        )
        wd = conn.execute("SELECT id FROM windows WHERE name='W-D'").fetchone()["id"]
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-E','2026-11-02T00:00:00','2026-11-02T23:59:59','开启',1)"
        )
        we = conn.execute("SELECT id FROM windows WHERE name='W-E'").fetchone()["id"]
        conn.commit()
    # 走正式入账写路径生成桶行（带外键）
    r = c.post(
        "/api/events",
        headers=h,
        json={"window_name": "W-D", "biz_key": "D1",
              "event_ts": "2026-11-01T22:00:00", "amount": 4.0},
    )
    assert r.status_code == 200 and r.json()["disposition"] == "迟到桶"

    # 无迁移目标：同事务拒删，窗与桶行都原样保留
    r = c.delete(f"/api/windows/{wd}", headers=h)
    assert r.status_code == 409
    with _raw() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM windows WHERE id=?", (wd,)).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE window_id=?", (wd,)
        ).fetchone()["c"] == 1

    # 库层外键硬约束：绕过 API 直接删窗必须被 SQLite 拒绝
    with _raw() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM windows WHERE id=?", (wd,))

    # 级联迁移：同事务把桶行改挂存活窗，再删窗
    r = c.delete(f"/api/windows/{wd}?migrate_to={we}", headers=h)
    assert r.status_code == 200
    with _raw() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM windows WHERE id=?", (wd,)).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE window_id=?", (we,)
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE window_id=?", (wd,)
        ).fetchone()["c"] == 0

    # 迁移到不存在的窗：冲突失败整笔回滚
    r = c.delete(f"/api/windows/{we}?migrate_to=999999", headers=h)
    assert r.status_code in (404, 409)
    with _raw() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM windows WHERE id=?", (we,)).fetchone()["c"] == 1


def test_blocked_delete_rolls_back_and_releases_window_lock():
    """删窗被关联记录拦截回滚后，窗锁必须已释放（finish 不隐式重开空事务）。"""
    c = TestClient(app)
    h = _auth(c)
    # W-1101 种子即有窗内事件 + 物化，删除必被 refs 拦截
    r = c.delete("/api/windows/1", headers=h)
    assert r.status_code == 409
    # 窗仍在（回滚未半删）
    with _raw() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE id=1"
        ).fetchone()["c"] == 1
        # 窗锁已被 finally 释放，无残留持锁
        held = conn.execute(
            "SELECT holder FROM window_locks WHERE lock_key=?", ("window:1",)
        ).fetchall()
        assert held == []
    # 另一用户可立即取得同一把窗锁（证明未被卡死）
    h2 = _auth(c, "closer", "close123")
    assert c.post(
        "/api/locks/acquire", headers=h2, json={"lock_key": "window:1"}
    ).status_code == 200
    c.post("/api/locks/release", headers=h2, json={"lock_key": "window:1"})


# ---------------------------------------------------------------------------
# 红测 2：双人同时改同一桶行争用——桶锁，后到只记争用不写业务
# ---------------------------------------------------------------------------
def test_red_concurrent_reassign_same_bucket_lock_contention():
    c = TestClient(app)
    h1 = _auth(c)
    h2 = _auth(c, "closer", "close123")
    with _raw() as conn:
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-X','2026-11-01T00:00:00','2026-11-01T23:59:59','已关闭',1)"
        )
        wx = conn.execute("SELECT id FROM windows WHERE name='W-X'").fetchone()["id"]
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-Y','2026-11-02T00:00:00','2026-11-02T23:59:59','开启',1)"
        )
        wy = conn.execute("SELECT id FROM windows WHERE name='W-Y'").fetchone()["id"]
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation)"
            " VALUES ('W-Z','2026-11-03T00:00:00','2026-11-03T23:59:59','开启',1)"
        )
        wz = conn.execute("SELECT id FROM windows WHERE name='W-Z'").fetchone()["id"]
        conn.commit()
    _seed_late(_raw(), "W-X", "LX", 9.0)
    with _raw() as conn:
        late_id = conn.execute(
            "SELECT id FROM late_bucket WHERE biz_key='LX'"
        ).fetchone()["id"]

    # 确定性争用：先占桶锁，后到者必须 409、只记争用、桶行归属不变
    lock_key = f"late-bucket:{late_id}"
    assert c.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key}).status_code == 200
    r = c.post(
        "/api/late-bucket/reassign",
        headers=h2,
        json={"late_id": late_id, "target_window_id": wy},
    )
    assert r.status_code == 409
    with _raw() as conn:
        row = conn.execute("SELECT * FROM late_bucket WHERE id=?", (late_id,)).fetchone()
        assert row["window_id"] == wx
        cont = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?", (lock_key,)
        ).fetchone()["c"]
        assert cont >= 1
    c.post("/api/locks/release", headers=h1, json={"lock_key": lock_key})

    # 真实双线程并发：两个独立连接同时 BEGIN IMMEDIATE 抢同一桶行写锁，
    # 恰好一方成功，另一方收到 SQLITE_BUSY，经 require_lock 翻译为 409 争用、不写业务
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []

    def db_worker(holder: str, target: int):
        from locks import require_lock
        from locks import release as release_lock

        cc = db.connect()
        tx_open = False
        barrier.wait()
        try:
            db.begin_immediate(cc)  # 重叠事务入口抢 RESERVED 写锁
            tx_open = True
            require_lock(cc, lock_key, holder)
            tw = cc.execute("SELECT id FROM windows WHERE id=?", (target,)).fetchone()
            cc.execute(
                "UPDATE late_bucket SET window_id=? WHERE id=?", (tw["id"], late_id)
            )
            cc.execute(
                "INSERT INTO audit_trail(actor,action,target,payload,created_at)"
                " VALUES (?,?,?,?,?)",
                (holder, "late_reassign", str(late_id),
                 f'{{"to_window_id": {target}}}', "2026-11-20T00:00:00"),
            )
            cc.execute("COMMIT")
            tx_open = False
            outcomes.append({"status": 200, "target": target})
        except Exception as exc:  # noqa: BLE001
            if tx_open:
                cc.execute("ROLLBACK")
            outcomes.append({"status": 409, "target": target, "err": str(exc)})
        finally:
            try:
                release_lock(cc, lock_key, holder)
            except sqlite3.OperationalError:
                pass  # 未抢到锁的连接在赢家持锁期间清理：本就无锁可释
            cc.close()

    t1 = threading.Thread(target=db_worker, args=("admin", wy))
    t2 = threading.Thread(target=db_worker, args=("closer", wz))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(o["status"] for o in outcomes)
    assert statuses == [200, 409], outcomes
    with _raw() as conn:
        final_wid = conn.execute(
            "SELECT window_id FROM late_bucket WHERE id=?", (late_id,)
        ).fetchone()["window_id"]
        assert final_wid in (wy, wz)
        # 只可能落到一个窗，不得半写
        n = conn.execute(
            "SELECT COUNT(DISTINCT window_id) c FROM late_bucket WHERE id=?", (late_id,)
        ).fetchone()["c"]
        assert n == 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?", (lock_key,)
        ).fetchone()["c"] >= 1
    # 锁用完即释放，后续可正常取得
    assert c.post("/api/locks/acquire", headers=h1,
                  json={"lock_key": lock_key}).status_code == 200
    c.post("/api/locks/release", headers=h1, json={"lock_key": lock_key})


# ---------------------------------------------------------------------------
# 迟到归属册：甲路径登记、当天禁改口、偏离拒写并记归属偏了
# ---------------------------------------------------------------------------
def test_registry_locks_path_a_same_day_and_logs_drift():
    c = TestClient(app)
    h = _auth(c)
    reg = c.get("/api/late-attribution/registry", headers=h).json()
    assert reg["active_path"] == "A"
    assert reg["path_name"] == "window_id_fk"
    assert reg["drift_rejections"] == 0

    # 当天改口走乙路径：拒绝并记“归属偏了”
    r = c.post("/api/late-attribution/choose", headers=h, json={"path": "B"})
    assert r.status_code == 409
    reg2 = c.get("/api/late-attribution/registry", headers=h).json()
    assert reg2["active_path"] == "A"
    assert reg2["drift_rejections"] >= 1

    with _raw() as conn:
        acts = {
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_trail WHERE action='late_attribution_drift'"
            ).fetchall()
        }
        assert "late_attribution_drift" in acts

    # 重复确认甲路径幂等
    r = c.post("/api/late-attribution/choose", headers=h, json={"path": "A"})
    assert r.status_code == 200
    assert r.json()["active_path"] == "A"


# ---------------------------------------------------------------------------
# 五处同态：迟到明细 / 汇总卡 / 窗口条带 / 对账迟到计数 / 审计
# ---------------------------------------------------------------------------
def test_five_surfaces_homomorphic_and_no_orphan():
    c = TestClient(app)
    h = _auth(c)
    c.post("/api/windows/2/close", headers=h)
    r = c.post(
        "/api/events",
        headers=h,
        json={"window_name": "W-1102", "biz_key": "H-1",
              "event_ts": "2026-11-02T22:00:00", "amount": 4.0},
    )
    assert r.json()["disposition"] == "迟到桶"
    r = c.post(
        "/api/events",
        headers=h,
        json={"window_name": "W-1102", "biz_key": "H-2",
              "event_ts": "2026-11-02T22:30:00", "amount": 6.0},
    )
    assert r.json()["disposition"] == "迟到桶"

    # 1) 迟到明细
    detail = c.get("/api/late-bucket", headers=h).json()["items"]
    w1102 = [d for d in detail if d["window_id"] == 2]
    assert len(w1102) == 2
    assert all(d["window_name_live"] == "W-1102" for d in w1102)

    # 2) 汇总卡（与明细同源同数）
    summary = c.get("/api/late-bucket/summary", headers=h).json()
    card = next(b for b in summary["by_window"] if b["window_id"] == 2)
    assert card["c"] == 2 and abs(card["s"] - 10.0) < 1e-6
    assert summary["count"] == len(detail)
    assert abs(summary["amount"] - sum(d["amount"] for d in detail)) < 1e-6

    # 3) 窗口条带：每窗迟到计数
    windows = c.get("/api/windows", headers=h).json()["items"]
    stripe = next(w for w in windows if w["id"] == 2)
    assert stripe["late_bucket_count"] == 2

    # 4) 对账迟到计数（单窗 + 总览同口径）
    rec = c.get("/api/reconcile/2", headers=h).json()
    assert rec["late_bucket"] == 2
    rec_all = c.get("/api/reconcile", headers=h).json()["items"]
    assert next(x for x in rec_all if x["window_id"] == 2)["late_bucket"] == 2

    # 5) 审计：迟到入账动作留痕，且载荷带 window_id 可核证
    audits = c.get("/api/audit?limit=200", headers=h).json()["items"]
    assert any(a["action"] == "ingest_late" for a in audits)
    assert all('"window_id": 2' in a["payload"]
               for a in audits if a["action"] == "ingest_late")

    # 无孤儿：库内零孤儿，接口口径一致
    with _raw() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket lb LEFT JOIN windows w ON w.id=lb.window_id"
            " WHERE w.id IS NULL"
        ).fetchone()["c"] == 0
    assert summary["orphan_buckets"] == 0


def test_mounted_db_migration_preserves_existing_bucket(tmp_path, monkeypatch):
    """挂卷升级：对既有 data/app.db 副本做迁移，旧桶行回填 window_id，可核证不丢数。"""
    import shutil

    src = db.ROOT / "data" / "app.db"
    target = tmp_path / "up.db"
    shutil.copy(src, target)
    monkeypatch.setattr(db, "DB_PATH", target)
    init_db()  # 跑迁移
    with _raw() as conn:
        cols = _cols(conn, "late_bucket")
        assert "window_id" in cols
        fks = conn.execute("PRAGMA foreign_key_list(late_bucket)").fetchall()
        assert any(fk["table"] == "windows" for fk in fks)
        # 种子里 W-1101 的迟到行被回填到窗 1，无丢失、无孤儿
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE window_id=1"
        ).fetchone()["c"] >= 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket lb LEFT JOIN windows w ON w.id=lb.window_id"
            " WHERE w.id IS NULL"
        ).fetchone()["c"] == 0


def test_legacy_db_orphan_rows_quarantined_not_merged(tmp_path, monkeypatch):
    """旧版库（无 window_id）迁移：匹配不上存活窗的桶行进检疫，绝不按窗名串并。"""
    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.execute("PRAGMA foreign_keys=ON")
    raw.executescript(
        """
        CREATE TABLE windows(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
          open_at TEXT NOT NULL, close_at TEXT NOT NULL,
          status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1, seal_token TEXT);
        CREATE TABLE events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, window_id INTEGER, biz_key TEXT NOT NULL,
          event_ts TEXT NOT NULL, amount REAL NOT NULL, disposition TEXT NOT NULL,
          hold_id INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE late_bucket(
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
          window_name TEXT, biz_key TEXT NOT NULL, event_ts TEXT NOT NULL,
          amount REAL NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        """
    )
    raw.execute(
        "INSERT INTO windows(name,open_at,close_at,status,generation)"
        " VALUES ('W-KEEP','2026-11-01T00:00:00','2026-11-01T23:59:59','已关闭',1)"
    )
    keep_id = raw.execute("SELECT id FROM windows WHERE name='W-KEEP'").fetchone()[0]
    # 一行能按名回填；一行指向已删除窗 W-GONE（孤儿）
    for name, key in (("W-KEEP", "K1"), ("W-GONE", "G1")):
        cur = raw.execute(
            "INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)"
            " VALUES (NULL,?, '2026-11-01T23:50:00', 1.0, '迟到桶','2026-11-02T00:01:00')",
            (key,),
        )
        raw.execute(
            "INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,reason,created_at)"
            " VALUES (?,?,?, '2026-11-01T23:50:00', 1.0, '关窗后到达','2026-11-02T00:01:00')",
            (cur.lastrowid, name, key),
        )
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "DB_PATH", legacy)
    init_db()

    with _raw() as conn:
        cols = _cols(conn, "late_bucket")
        assert "window_id" in cols
        kept = conn.execute("SELECT * FROM late_bucket WHERE biz_key='K1'").fetchone()
        assert kept["window_id"] == keep_id
        # 孤儿行进检疫，不进 late_bucket、不被任何同名窗吸收
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE biz_key='G1'"
        ).fetchone()["c"] == 0
        q = conn.execute(
            "SELECT * FROM late_bucket_quarantine WHERE biz_key='G1'"
        ).fetchall()
        assert len(q) == 1 and q[0]["legacy_window_name"] == "W-GONE"
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_bucket lb LEFT JOIN windows w ON w.id=lb.window_id"
            " WHERE w.id IS NULL"
        ).fetchone()["c"] == 0
        # 行数守恒：1 回填 + 1 检疫 = 原 2 行
        assert conn.execute("SELECT COUNT(*) c FROM late_bucket").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM late_bucket_quarantine").fetchone()["c"] == 1
