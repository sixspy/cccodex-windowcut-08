"""业务窗开/关/列表/删窗。删窗与迟到桶生命周期同事务绑定（甲路径）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from attribution_svc import require_path_a
from db import begin_immediate, connect, finish, write_audit
from locks import release, require_lock
from models import WindowCreateIn

router = APIRouter(tags=["windows"])


@router.get("/api/windows")
def list_windows(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT w.*,
                      (SELECT COUNT(*) FROM late_bucket lb WHERE lb.window_id=w.id)
                        AS late_bucket_count
               FROM windows w ORDER BY w.id"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/windows")
def create_window(body: WindowCreateIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        begin_immediate(conn)
        try:
            conn.execute(
                "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,1)",
                (body.name, body.open_at, body.close_at, "开启"),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, f"窗口创建失败: {exc}") from exc
        wid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        write_audit(conn, user, "window_create", body.name, {"id": wid})
        conn.commit()
        row = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        return dict(row)


@router.post("/api/windows/{wid}/close")
def close_window(wid: int, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"window:{wid}"
    with connect() as conn:
        begin_immediate(conn)
        try:
            require_lock(conn, lock_key, user)
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] == "已关闭":
                return {"ok": True, "already": True}
            now = datetime.now().isoformat(timespec="seconds")
            token = f"seal-{wid}-{w['generation']}-{now}"
            conn.execute(
                "UPDATE windows SET status='已关闭', seal_token=? WHERE id=?",
                (token, wid),
            )
            write_audit(conn, user, "window_close", str(wid), {"seal_token": token})
            conn.commit()
            return {"ok": True, "seal_token": token}
        finally:
            release(conn, lock_key, user)
            finish(conn)


@router.get("/api/windows/{wid}")
def get_window(wid: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "窗口不存在")
        ev = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM events WHERE window_id=? AND disposition='窗内'",
            (wid,),
        ).fetchone()
        late = conn.execute(
            "SELECT COUNT(*) c FROM late_bucket WHERE window_id=?", (wid,)
        ).fetchone()["c"]
        return {
            "window": dict(w),
            "in_window_count": ev["c"],
            "in_window_amount": ev["s"],
            "late_bucket_count": late,
        }


@router.delete("/api/windows/{wid}")
def delete_window(wid: int, migrate_to: int | None = None, authorization: AuthHeader = None):
    """删窗（甲路径，同事务）：

    - 该窗仍挂迟到桶行且未给 migrate_to：拒删（409），窗与桶行原样保留；
    - 给 migrate_to：校验目标存活后，同事务把桶行级联改挂目标窗，再删本窗；
      目标不存在/自迁等冲突整笔回滚；
    - 库层 late_bucket.window_id ON DELETE RESTRICT 外键是第二道硬约束。
    """
    user = require_user(authorization)
    lock_key = f"window:{wid}"
    with connect() as conn:
        begin_immediate(conn)
        try:
            require_lock(conn, lock_key, user)
            require_path_a(conn, user, str(wid), {"write": "window_delete"})
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            late_n = conn.execute(
                "SELECT COUNT(*) c FROM late_bucket WHERE window_id=?", (wid,)
            ).fetchone()["c"]
            if late_n > 0 and migrate_to is None:
                write_audit(
                    conn, user, "window_delete_rejected", str(wid),
                    {"reason": "存在迟到桶行，拒删防孤儿", "late_buckets": late_n},
                )
                conn.commit()
                raise HTTPException(
                    409, f"窗口仍挂 {late_n} 行迟到桶：拒删防孤儿；可指定 migrate_to 级联迁移"
                )
            if migrate_to is not None:
                if migrate_to == wid:
                    raise HTTPException(409, "迁移目标不能是本窗")
                target = conn.execute(
                    "SELECT id FROM windows WHERE id=?", (migrate_to,)
                ).fetchone()
                if not target:
                    raise HTTPException(404, "迁移目标窗口不存在")
                cur = conn.execute(
                    "UPDATE late_bucket SET window_id=? WHERE window_id=?",
                    (migrate_to, wid),
                )
                migrated = cur.rowcount
                write_audit(
                    conn, user, "late_bucket_migrate", str(wid),
                    {"from_window_id": wid, "to_window_id": migrate_to,
                     "migrated_rows": migrated},
                )
            # 其它引用表存在时同样拒删，避免静默孤儿（外键兜底）
            refs = {
                "events(窗内)": conn.execute(
                    "SELECT COUNT(*) c FROM events WHERE window_id=? AND disposition='窗内'",
                    (wid,),
                ).fetchone()["c"],
                "materializations": conn.execute(
                    "SELECT COUNT(*) c FROM materializations WHERE window_id=?", (wid,)
                ).fetchone()["c"],
                "hold_staging": conn.execute(
                    "SELECT COUNT(*) c FROM hold_staging WHERE window_id=?", (wid,)
                ).fetchone()["c"],
                "reopen_ledger": conn.execute(
                    "SELECT COUNT(*) c FROM reopen_ledger WHERE window_id=?", (wid,)
                ).fetchone()["c"],
            }
            blocked = {k: v for k, v in refs.items() if v > 0}
            if blocked:
                conn.rollback()
                raise HTTPException(409, f"窗口仍有关联记录，拒删：{blocked}")
            conn.execute("DELETE FROM windows WHERE id=?", (wid,))
            write_audit(
                conn, user, "window_delete", str(wid),
                {"migrate_to": migrate_to, "late_buckets": late_n},
            )
            conn.commit()
            return {"ok": True, "deleted_window_id": wid, "migrate_to": migrate_to}
        finally:
            release(conn, lock_key, user)
            finish(conn)
