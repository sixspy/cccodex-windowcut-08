"""窗口锁与争用登记（公共并发面，题面争用句应对得上）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import begin_immediate, connect, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _log_contention(conn, lock_key: str, holder: str, owner: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={owner}", now),
    )


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
        except sqlite3.IntegrityError:
            # 并发：两方都通过 SELECT 后，后到者唯一键撞车 → 按争用处理而非 500
            owner = conn.execute(
                "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            _log_contention(conn, lock_key, holder, owner["holder"] if owner else "未知")
            return False
        return True
    if row["holder"] == holder:
        return True
    _log_contention(conn, lock_key, holder, row["holder"])
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    try:
        acquired = try_acquire(conn, lock_key, holder)
    except sqlite3.OperationalError as exc:
        # BEGIN IMMEDIATE 撞上其他写事务：按争用语义处理（后到只记争用，不写业务）
        if "locked" in str(exc).lower():
            raise HTTPException(409, "争用：数据库写锁被占，后到只记争用") from exc
        raise
    if not acquired:
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        begin_immediate(conn)
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：后到只记争用")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        begin_immediate(conn)
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}
