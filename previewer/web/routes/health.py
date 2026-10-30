import json
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from ...config import get_settings
from ...db import SessionLocal
from ...models import Job

router = APIRouter()
HEARTBEAT_STALE_SECONDS = 60


def worker_status() -> dict:
    path = get_settings().data_dir / "worker.heartbeat"
    try:
        beat = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"ok": False, "reason": "no heartbeat"}
    age = time.time() - float(beat.get("ts", 0))
    return {
        "ok": age <= HEARTBEAT_STALE_SECONDS,
        "age_seconds": round(age, 1),
        "docker_ok": beat.get("docker_ok"),
        "running_jobs": beat.get("running_jobs"),
    }


@router.get("/health")
def health():
    out: dict = {"db": {"ok": False}}
    s = SessionLocal()
    try:
        s.execute(text("SELECT 1"))
        out["db"] = {"ok": True}
        rows = s.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        out["queue"] = {status: n for status, n in rows}
    except Exception as e:  # noqa: BLE001
        out["db"] = {"ok": False, "error": e.__class__.__name__}
    finally:
        s.close()
    out["worker"] = worker_status()
    out["ok"] = out["db"]["ok"]
    return JSONResponse(out, status_code=200 if out["ok"] else 503)
