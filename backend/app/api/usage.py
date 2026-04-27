"""GET /api/usage — live system-resource snapshot for the Usage page.

Returns CPU %, RAM %, disk % for the data volume, plus absolute
numbers so the UI can render "X.X GB of Y.Y GB" alongside the bars.

psutil's ``cpu_percent(interval=None)`` returns the delta since the
last call — so we warm it up once on first import and then every
poll gets a fresh reading without a blocking sleep.
"""

from __future__ import annotations

from pathlib import Path

import psutil
from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(prefix="/api", tags=["usage"])

# Prime the internal counter so the first real call returns a non-zero
# cpu_percent without blocking. 0.0 on the first read is fine.
psutil.cpu_percent(interval=None)


def _disk_for(path: Path) -> dict[str, float]:
    try:
        u = psutil.disk_usage(str(path))
        return {
            "total_bytes": u.total,
            "used_bytes": u.used,
            "free_bytes": u.free,
            "percent": u.percent,
        }
    except OSError:
        return {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "percent": 0.0}


@router.get("/usage")
async def usage() -> dict[str, object]:
    settings = get_settings()
    data_dir = settings.data_dir

    # CPU: interval=None uses the last snapshot, so no blocking sleep.
    cpu_pct = psutil.cpu_percent(interval=None)
    cpu_count = psutil.cpu_count(logical=True) or 1

    vm = psutil.virtual_memory()
    memory = {
        "total_bytes": vm.total,
        "used_bytes": vm.used,
        "available_bytes": vm.available,
        "percent": vm.percent,
    }

    # Swap is interesting on a 2 GB SBC — if swap is being hit, something
    # is wrong. Expose it but don't feature it prominently in the UI.
    sw = psutil.swap_memory()
    swap = {
        "total_bytes": sw.total,
        "used_bytes": sw.used,
        "percent": sw.percent,
    }

    storage = _disk_for(data_dir)

    # DB file details: operators want to see the aime.db size separately
    # from overall disk usage (the volume may be shared with backups,
    # logs, etc).
    db_path = data_dir / "aime.db"
    wal_path = data_dir / "aime.db-wal"
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0

    # Uptime of the backend process (seconds since boot of this PID).
    proc = psutil.Process()
    uptime_s = int(psutil.boot_time())  # fallback
    try:
        uptime_s = int(proc.create_time())
    except Exception:  # noqa: BLE001
        pass

    return {
        "cpu": {"percent": cpu_pct, "count": cpu_count},
        "memory": memory,
        "swap": swap,
        "storage": storage,
        "database": {
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "total_bytes": db_bytes + wal_bytes,
        },
        "process_started_epoch": uptime_s,
    }
