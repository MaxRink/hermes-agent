"""Memory status rollup for ``/api/status``.

Read side for signals the gateway already persists: the 30s ``state/gateway.heartbeat``
(RSS + MemAvailable/MemTotal + swap) and the lifecycle sentinel's ``suspected_oom``
flag — two small file reads, no IPC.  ``/api/status`` is unauthenticated, so only
coarse numbers (MB), enums and booleans.  A missing/corrupt file degrades to
``pressure="unknown"`` rather than raising into the status endpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, Optional

# Thresholds on system MemAvailable.  ``critical`` doubles as the lifecycle
# ledger's OOM-suspicion heuristic: a level that makes a later unclean death
# "suspected OOM" already warns while the process is alive.
_CRITICAL_AVAILABLE_KIB = 64 * 1024  # < 64 MiB available
_CRITICAL_AVAILABLE_FRACTION = 0.05  # < 5% of MemTotal
_ELEVATED_AVAILABLE_KIB = 128 * 1024  # < 128 MiB available
_ELEVATED_AVAILABLE_FRACTION = 0.15  # < 15% of MemTotal
# A container limit must not hide an absolute host emergency.  Keep this
# separate from ``classify_pressure`` so a host with a large total can still
# force a warning before the cgroup signal is selected.
_HOST_ABSOLUTE_DANGER_KIB = 128 * 1024  # < 128 MiB available
_PRESSURE_TIERS = (  # order-sensitive: worst first
    ("critical", _CRITICAL_AVAILABLE_KIB, _CRITICAL_AVAILABLE_FRACTION),
    ("elevated", _ELEVATED_AVAILABLE_KIB, _ELEVATED_AVAILABLE_FRACTION),
)

# Writer cadence is 30s; 150s tolerates a briefly stalled loop without letting
# a long-dead gateway's last sample pose as current.
_HEARTBEAT_FRESH_TTL_S = 150.0


def _nonneg_int(value: Any) -> Optional[int]:
    """Return *value* if it is a non-negative int (bools rejected), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _mb(kib: Any) -> Optional[int]:
    return None if _nonneg_int(kib) is None else kib // 1024


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed is not None and parsed.tzinfo is None else parsed


def classify_pressure(available_kib: Any, total_kib: Any) -> str:
    """``ok``/``elevated``/``critical`` from MemAvailable/MemTotal; ``unknown`` when the
    sample is missing/malformed — "could not read it" must never read as "fine"."""
    available, total = _nonneg_int(available_kib), _nonneg_int(total_kib)
    if available is None:
        return "unknown"
    fraction = available / total if total else None
    for level, kib_floor, frac_floor in _PRESSURE_TIERS:
        if available < kib_floor or (fraction is not None and fraction < frac_floor):
            return level
    return "ok"


def _host_absolute_danger(available_kib: Any) -> bool:
    """Whether host available memory is low enough to override cgroup pressure."""
    available = _nonneg_int(available_kib)
    return available is not None and available < _HOST_ABSOLUTE_DANGER_KIB


def _cgroup_from_heartbeat(mem: dict[str, Any]) -> Optional[dict[str, int]]:
    """Recover the gateway cgroup sample written by its heartbeat producer.

    The dashboard may run in a different container, so reading its own cgroup
    would measure the dashboard rather than the gateway. Older heartbeats lack
    these fields and intentionally use the host-pressure fallback.
    """
    total = _nonneg_int(mem.get("cgroup_total_bytes"))
    available = _nonneg_int(mem.get("cgroup_available_bytes"))
    if total is None or available is None or total <= 0 or available > total:
        return None
    return {"total_bytes": total, "available_bytes": available}


def _read_state_files(home: Optional[Path]) -> tuple:
    """``(heartbeat, sentinel)`` dicts, each ``None`` when unreadable."""
    try:
        from gateway.lifecycle_ledger import _read_json, get_lifecycle_sentinel_path
        from gateway.shutdown_watchdog import get_loop_heartbeat_path

        return _read_json(get_loop_heartbeat_path(home)), _read_json(get_lifecycle_sentinel_path(home))
    except Exception:
        return None, None


def collect_memory_status(
    home: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """``memory`` block for ``/api/status``; ``home`` scopes to a profile (``None`` =
    active), ``now`` is injectable.  Never raises — a down gateway or corrupt files
    yield ``pressure="unknown"`` plus whatever fields could be recovered."""
    moment = now or datetime.now(UTC)
    status: dict[str, Any] = {
        "pressure": "unknown", "host_pressure": "unknown", "container_pressure": "unknown",
        "gateway_rss_mb": None, "system_total_mb": None, "system_available_mb": None,
        "container_total_mb": None, "container_available_mb": None,
        "swap_used_mb": None, "sampled_at": None, "last_boot_unclean": False, "last_boot_suspected_oom": False,
        # Identity of the CURRENT life (sentinel started_at): the dashboard keys
        # banner dismissal on it so acknowledging one OOM restart does not mute the NEXT.
        "boot_id": None, "host_absolute_danger": False,
    }

    heartbeat, sentinel = _read_state_files(home)
    if heartbeat:
        sampled_at, mem = _parse_iso(heartbeat.get("updated_at")), heartbeat.get("mem")
        if isinstance(mem, dict):
            for dst, src in (("gateway_rss_mb", "rss_kib"), ("system_total_mb", "mem_total_kib"),
                             ("system_available_mb", "mem_available_kib"), ("swap_used_mb", "swap_used_kib")):
                status[dst] = _mb(mem.get(src))
            if sampled_at is not None:
                status["sampled_at"] = sampled_at.isoformat()
                # Stale sample: numbers still reported (sampled_at says when) but
                # pressure stays "unknown" so a dead gateway's final gasp cannot
                # render a live "critical" banner forever.
                if 0 <= (moment - sampled_at).total_seconds() <= _HEARTBEAT_FRESH_TTL_S:
                    status["host_pressure"] = classify_pressure(mem.get("mem_available_kib"), mem.get("mem_total_kib"))
                    status["host_absolute_danger"] = _host_absolute_danger(mem.get("mem_available_kib"))
                    cgroup = _cgroup_from_heartbeat(mem)
                    if cgroup is None:
                        # Native/non-container installs retain the original host
                        # signal. Containers use their effective limit instead,
                        # so ample host RAM does not create a misleading banner.
                        status["pressure"] = status["host_pressure"]
                    else:
                        total_kib = cgroup["total_bytes"] // 1024
                        available_kib = cgroup["available_bytes"] // 1024
                        status["container_total_mb"] = total_kib // 1024
                        status["container_available_mb"] = available_kib // 1024
                        status["container_pressure"] = classify_pressure(available_kib, total_kib)
                        # A healthy cgroup cannot make an absolute host
                        # emergency safe; retain the host danger signal while
                        # still ignoring its normal fraction warning.
                        status["pressure"] = (
                            "critical" if status["host_absolute_danger"] else status["container_pressure"]
                        )

    if sentinel:
        status["last_boot_unclean"] = bool(sentinel.get("prior_unclean_exit"))
        status["last_boot_suspected_oom"] = bool(sentinel.get("prior_suspected_oom"))
        started_at = sentinel.get("started_at")
        status["boot_id"] = started_at if isinstance(started_at, str) and started_at else None

    return status
