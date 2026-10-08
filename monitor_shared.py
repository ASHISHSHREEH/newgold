"""
Shared in-process state between the strategy and MonitorActor.

The strategy writes close-reasons and current SL/TP here; the MonitorActor
reads them when processing position events.  Everything is module-level and
protected by a plain lock — both the strategy and the actor run on the same
asyncio event loop thread, so locking is defensive only.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()

# PositionId string → reason string ("TP", "SL", "EMA", "SHUTDOWN")
_close_reasons: dict[str, str] = {}

# PositionId string → {"sl": float|None, "tp": float|None}
_pos_sltp: dict[str, dict] = {}

# PositionId string → {"fast_ema","slow_ema","atr"} captured at entry (ML features)
_entry_features: dict[str, dict] = {}

# PositionId string → {"mae","mfe"}: running max adverse / favorable excursion
# in price points over the life of the trade (ML labels)
_excursions: dict[str, dict] = {}


def record_close_reason(pos_id: str, reason: str) -> None:
    with _lock:
        _close_reasons[pos_id] = reason


def pop_close_reason(pos_id: str, default: str = "CLOSED") -> str:
    with _lock:
        return _close_reasons.pop(pos_id, default)


def update_pos_sltp(pos_id: str, sl: float | None, tp: float | None) -> None:
    with _lock:
        _pos_sltp[pos_id] = {"sl": sl, "tp": tp}


def get_pos_sltp(pos_id: str) -> dict:
    with _lock:
        return dict(_pos_sltp.get(pos_id, {"sl": None, "tp": None}))


def remove_pos_sltp(pos_id: str) -> None:
    with _lock:
        _pos_sltp.pop(pos_id, None)


def record_entry_features(pos_id: str, features: dict) -> None:
    with _lock:
        _entry_features[pos_id] = dict(features)


def pop_entry_features(pos_id: str) -> dict:
    with _lock:
        return _entry_features.pop(pos_id, {})


def update_excursion(pos_id: str, mae: float, mfe: float) -> None:
    with _lock:
        _excursions[pos_id] = {"mae": mae, "mfe": mfe}


def pop_excursion(pos_id: str) -> dict:
    with _lock:
        return _excursions.pop(pos_id, {})
