"""前台版本信息及短期加载状态，不保存订单或浏览记录。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FRONTEND_FILE = Path(__file__).resolve().parents[1] / "static" / "index.html"
_ready: dict[str, dict[str, Any]] = {}


def frontend_version() -> dict[str, str]:
    return {
        "revision": hashlib.sha256(FRONTEND_FILE.read_bytes()).hexdigest()[:12],
        "updated_at": datetime.fromtimestamp(
            FRONTEND_FILE.stat().st_mtime, timezone(timedelta(hours=8))
        ).strftime("%m-%d %H:%M:%S"),
    }


def record_ready(page_id: str, revision: str, columns_ready: bool, shipment_form_visible: bool) -> None:
    if page_id not in _ready and len(_ready) >= 100:
        _ready.pop(next(iter(_ready)))
    _ready[page_id] = {"page_id": page_id, "revision": revision,
                       "columns_ready": columns_ready,
                       "shipment_form_visible": shipment_form_visible,
                       "seen_at": datetime.now(timezone(timedelta(hours=8))).isoformat()}


def page_ready(page_id: str) -> dict[str, Any]:
    state = _ready.get(page_id)
    current = frontend_version()
    return {"seen": state is not None, "latest": current,
            "is_latest": bool(state and state["revision"] == current["revision"]),
            "page": state}
