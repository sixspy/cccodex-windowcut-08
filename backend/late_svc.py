"""迟到桶只读与处置。

甲路径（late_attribution_registry 锁定 A）：
- 桶行归属以 late_bucket.window_id 存活外键为准，窗名字段仅作展示冗余；
- 明细、汇总、窗口条带、对账、审计五处同态，全部按 window_id 同源聚合；
- 改挂桶行归属必须占桶锁 late-bucket:{id}，后到只记争用不写业务；
- 任何偏离归属册的写路径拒写并记 late_attribution_drift。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from attribution_svc import require_path_a
from db import begin_immediate, connect, finish, write_audit
from locks import release, require_lock

router = APIRouter(tags=["late"])


class LatePromoteIn(BaseModel):
    late_id: int
    target_window_name: str


class LateReassignIn(BaseModel):
    late_id: int
    target_window_id: int


def _orphan_count(conn) -> int:
    return conn.execute(
        """SELECT COUNT(*) c FROM late_bucket lb
           LEFT JOIN windows w ON w.id=lb.window_id WHERE w.id IS NULL"""
    ).fetchone()["c"]


@router.get("/api/late-bucket")
def list_late(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        # 明细：JOIN 存活窗取现窗名；window_id 为归属主键
        rows = conn.execute(
            """SELECT lb.*, w.name AS window_name_live
               FROM late_bucket lb
               JOIN windows w ON w.id=lb.window_id
               ORDER BY lb.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/api/late-bucket/summary")
def late_summary(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM late_bucket"
        ).fetchone()
        # 汇总卡按 window_id 分组（与明细同源），不按窗名字符串分组
        by_window = conn.execute(
            """SELECT lb.window_id, w.name AS window_name,
                      COUNT(*) c, COALESCE(SUM(lb.amount),0) s
               FROM late_bucket lb
               JOIN windows w ON w.id=lb.window_id
               GROUP BY lb.window_id ORDER BY lb.window_id"""
        ).fetchall()
        orphan = _orphan_count(conn)
        quarantine = conn.execute(
            "SELECT COUNT(*) c FROM late_bucket_quarantine"
        ).fetchone()["c"]
        return {
            "count": row["c"],
            "amount": row["s"],
            "by_window": [dict(r) for r in by_window],
            "orphan_buckets": orphan,
            "quarantined_buckets": quarantine,
        }


@router.post("/api/late-bucket/reassign")
def reassign_bucket(body: LateReassignIn, authorization: AuthHeader = None):
    """正式写路径：把桶行归属改挂到另一存活窗（甲路径外键改挂）。

    占桶锁 late-bucket:{late_id}；后到者 409 且只记争用，不写业务。
    """
    user = require_user(authorization)
    lock_key = f"late-bucket:{body.late_id}"
    with connect() as conn:
        begin_immediate(conn)
        try:
            require_lock(conn, lock_key, user)
            require_path_a(conn, user, str(body.late_id), {"write": "late_reassign"})
            late = conn.execute(
                "SELECT * FROM late_bucket WHERE id=?", (body.late_id,)
            ).fetchone()
            if not late:
                raise HTTPException(404, "迟到记录不存在")
            target = conn.execute(
                "SELECT * FROM windows WHERE id=?", (body.target_window_id,)
            ).fetchone()
            if not target:
                raise HTTPException(404, "目标窗口不存在")
            if target["id"] == late["window_id"]:
                conn.commit()
                return {"ok": True, "noop": True, "window_id": target["id"]}
            conn.execute(
                "UPDATE late_bucket SET window_id=?, window_name=? WHERE id=?",
                (target["id"], target["name"], body.late_id),
            )
            write_audit(
                conn,
                user,
                "late_reassign",
                str(body.late_id),
                {
                    "from_window_id": late["window_id"],
                    "to_window_id": target["id"],
                    "biz_key": late["biz_key"],
                },
            )
            conn.commit()
            return {"ok": True, "late_id": body.late_id, "window_id": target["id"]}
        finally:
            release(conn, lock_key, user)
            finish(conn)


@router.post("/api/late-bucket/promote-stub")
def promote_stub(body: LatePromoteIn, authorization: AuthHeader = None):
    """空桩：补入目标窗。母仓仅登记意图，不改桶行归属（归属以 reassign 正式写路径为准）。"""
    user = require_user(authorization)
    lock_key = f"late-promote:{body.late_id}"
    with connect() as conn:
        begin_immediate(conn)
        try:
            require_lock(conn, lock_key, user)
            late = conn.execute(
                "SELECT * FROM late_bucket WHERE id=?", (body.late_id,)
            ).fetchone()
            if not late:
                raise HTTPException(404, "迟到记录不存在")
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.target_window_name,)
            ).fetchone()
            if not w:
                raise HTTPException(404, "目标窗口不存在")
            write_audit(
                conn,
                user,
                "late_promote_stub",
                str(body.late_id),
                {
                    "target": body.target_window_name,
                    "note": "空桩：未改正式窗内事件，待题面写路径落地",
                },
            )
            conn.commit()
            return {
                "ok": True,
                "stub": True,
                "message": "迟到补入空桩已登记，正式窗内事件未改",
            }
        finally:
            release(conn, lock_key, user)
            finish(conn)


@router.post("/api/late-bucket/{late_id}/discard-stub")
def discard_stub(late_id: int, authorization: AuthHeader = None):
    """空桩：作废迟到项（不删物理行，只审计）。"""
    user = require_user(authorization)
    with connect() as conn:
        late = conn.execute(
            "SELECT * FROM late_bucket WHERE id=?", (late_id,)
        ).fetchone()
        if not late:
            raise HTTPException(404, "迟到记录不存在")
        write_audit(
            conn,
            user,
            "late_discard_stub",
            str(late_id),
            {"biz_key": late["biz_key"], "at": datetime.now().isoformat(timespec="seconds")},
        )
        conn.commit()
    return {"ok": True, "stub": True}


@router.get("/api/late-bucket/quarantine")
def list_quarantine(authorization: AuthHeader = None):
    """检疫桶：迁移期无法回填 window_id 的旧桶行，可核证、不进任何窗汇总。"""
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM late_bucket_quarantine ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows], "count": len(rows)}
