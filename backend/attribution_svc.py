"""迟到归属册（甲路径：late_bucket.window_id 存活外键）。

口径纪律（题面）：
- 正确性口径只走甲、乙中的一种，两边不得混用；本仓选甲，登记在册，当天禁止改口；
- 任何偏离归属册的写路径请求一律拒写，并在审计记 late_attribution_drift（归属偏了）；
- 乙路径（window_name_snapshot 强制复检）在本仓不实现、不接受切换。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit

router = APIRouter(tags=["late-attribution"])

PATH_A = "A"
PATH_B = "B"
PATH_NAMES = {
    PATH_A: "window_id_fk",
    PATH_B: "window_name_snapshot",
}


class ChoosePathIn(BaseModel):
    path: str


def get_registry(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM late_attribution_registry WHERE id=1"
    ).fetchone()
    if row is None:  # 兜底：正常由 init_db 预置
        today = datetime.now().date().isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO late_attribution_registry(
                 id,active_path,path_name,chosen_by,chosen_date,chosen_at,drift_rejections)
               VALUES (1,'A','window_id_fk','system',?,?,0)""",
            (today, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM late_attribution_registry WHERE id=1"
        ).fetchone()
    return dict(row)


def require_path_a(conn, actor: str, target: str, payload: dict | None = None) -> None:
    """桶行写路径守卫：仅允许甲路径；偏离拒写并记归属偏了。"""
    reg = get_registry(conn)
    if reg["active_path"] != PATH_A:
        conn.execute(
            "UPDATE late_attribution_registry SET drift_rejections=drift_rejections+1 WHERE id=1"
        )
        write_audit(
            conn, actor, "late_attribution_drift", target,
            {"rejected_path": reg["active_path"], "required_path": PATH_A,
             **(payload or {})},
        )
        raise HTTPException(409, "归属偏了：迟到归属册锁定甲路径，偏离写路径拒写")


@router.get("/api/late-attribution/registry")
def read_registry(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        return get_registry(conn)


@router.post("/api/late-attribution/choose")
def choose_path(body: ChoosePathIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    path = body.path.strip().upper()
    if path not in PATH_NAMES:
        raise HTTPException(400, "path 须为 A（window_id 外键）或 B（窗名快照复检）")
    with connect() as conn:
        reg = get_registry(conn)
        today = datetime.now().date().isoformat()
        if path != reg["active_path"] and reg["chosen_date"] == today:
            # 当天改口：拒写并记归属偏了
            conn.execute(
                "UPDATE late_attribution_registry SET drift_rejections=drift_rejections+1 WHERE id=1"
            )
            write_audit(
                conn, user, "late_attribution_drift", "registry",
                {"rejected_path": path, "active_path": reg["active_path"],
                 "reason": "当天禁止改口"},
            )
            conn.commit()
            raise HTTPException(
                409,
                f"迟到归属册当天已锁定 {reg['active_path']} 路径，当天禁止改口",
            )
        if path == reg["active_path"]:
            conn.commit()
            return reg
        # 跨日才允许改口；本仓业务上不启用乙路径写实现，仅保留登记纪律
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """UPDATE late_attribution_registry
               SET active_path=?, path_name=?, chosen_by=?, chosen_date=?, chosen_at=?
               WHERE id=1""",
            (path, PATH_NAMES[path], user, today, now),
        )
        write_audit(
            conn, user, "late_attribution_choose", "registry",
            {"path": path, "path_name": PATH_NAMES[path]},
        )
        conn.commit()
        return get_registry(conn)
