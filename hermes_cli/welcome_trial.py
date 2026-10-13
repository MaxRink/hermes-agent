"""The desktop's welcome trial: free inference on ``nous/welcome`` for a first install.

Only the backend the desktop spawned on this machine can claim it, only when this machine has never
used Hermes (``install_state``), and only with no provider and no Nous identity yet. The claim mints a
guest, stamps ``WELCOME_CLAIM_FLAG`` on it and marks the machine, so the trial is never claimed twice
here. A claim that is lost later (the guest is reaped, ``auth.json`` is deleted, the user signs in) is
never made again: that user lands on provider setup and sign-in like anyone else.

Guests that ran on the free model before the claim existed keep it (:func:`grandfather_existing_guest`).
Enforcement is client-side; the account service's own entitlement check follows separately.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from hermes_cli import anon_auth

logger = logging.getLogger("hermes_cli.auth")


def _has_nous_identity() -> bool:
    from hermes_cli.auth_nous import _read_shared_nous_state

    return anon_auth.current_nous_state() is not None or _read_shared_nous_state() is not None


def provider_configured() -> bool:
    """Something besides the welcome trial carries inference: a key, a config pin, a sign-in, a host
    credential. The resolver itself answers, with the free-tier rung hidden."""
    from hermes_cli.auth import resolve_provider

    try:
        provider = resolve_provider("auto", skip_free_tier=True)
    except Exception:  # no_provider_configured, or an unreadable store: nothing usable here
        return False
    return provider != "nous" or not anon_auth.has_guest()


def inference_available(*, assume_fresh: bool = False) -> bool:
    """Something can carry a first chat on this backend: the trial (claimed, or claimable now) or a
    configured provider."""
    return (anon_auth.free_inference_active() or provider_configured()
            or claim_blocker(assume_fresh=assume_fresh) is None)


def _machine_is_used(assume_fresh: bool) -> bool:
    from hermes_cli.install_state import probe

    return not assume_fresh and probe().existing


# Cheapest first; the last one reads the disk. Each is a reason the trial cannot be claimed.
_CLAIM_BLOCKERS: tuple[tuple[str, Callable[[bool], bool]], ...] = (
    ("not the desktop's local backend", lambda _fresh: not anon_auth.on_local_desktop_backend()),
    ("guests are switched off", lambda _fresh: not anon_auth.guest_allowed()),
    ("a Nous identity exists", lambda _fresh: _has_nous_identity()),
    ("a provider is configured", lambda _fresh: provider_configured()),
    ("Hermes was used on this machine", _machine_is_used),
)


def claim_blocker(*, assume_fresh: bool = False) -> Optional[str]:
    """Why the trial cannot be claimed here, or None when it can. *assume_fresh* is the unpackaged
    desktop's ``--fresh-install`` switch: it skips only the machine check."""
    return next((reason for reason, blocks in _CLAIM_BLOCKERS if blocks(assume_fresh)), None)


def claim_desktop_free_tier(*, assume_fresh: bool = False, force: bool = False) -> Optional[dict[str, Any]]:
    """Claim the trial when nothing blocks it: mint the guest, stamp the claim, mark the machine.
    Returns the claimed state, or None when the trial is not available here. A failed mint raises
    the coded ``AuthError`` from :func:`anon_auth.ensure_portal_identity` (memoised with its cooldown;
    *force* is the user's own retry)."""
    if reason := claim_blocker(assume_fresh=assume_fresh):
        logger.debug("welcome trial not claimed: %s", reason)
        return None
    state = anon_auth.ensure_portal_identity(explicit=True, force=force)
    if state is None:  # still inside the cooldown of the last failure
        failure = anon_auth.last_mint_failure() or {}
        raise anon_auth._anon_err(str(failure.get("error") or anon_auth.ANON_FAILURE_COPY[anon_auth.ANON_SERVER_ERROR]),
                                  str(failure.get("error_code") or anon_auth.ANON_SERVER_ERROR),
                                  retry_after=failure.get("retry_after"))
    claimed = _record_claim()
    logger.info("Nous free tier: welcome trial claimed on this machine")
    return claimed


def grandfather_existing_guest() -> bool:
    """A guest from before the claim existed keeps the free model when this home already chatted on
    it. Connector-only guests never qualify. Records the claim once; True when it did."""
    if not anon_auth.on_local_desktop_backend():
        return False
    state = anon_auth.current_nous_state()
    if not anon_auth.is_guest_state(state) or state.get(anon_auth.WELCOME_CLAIM_FLAG):
        return False
    from hermes_constants import get_default_hermes_root, get_hermes_home

    homes = dict.fromkeys((get_hermes_home(), get_default_hermes_root()))
    if not any(_chatted_on_welcome(home / "state.db") for home in homes):
        return False
    _record_claim()
    logger.info("Nous free tier: existing free-model guest kept its trial")
    return True


def _record_claim() -> Optional[dict[str, Any]]:
    from hermes_cli.auth_nous import _write_shared_nous_state
    from hermes_cli.install_state import mark_machine_used

    state = anon_auth.set_guest_flag(anon_auth.WELCOME_CLAIM_FLAG)
    if state is not None:
        _write_shared_nous_state(state)
    mark_machine_used()
    return state


def _chatted_on_welcome(db: Path) -> bool:
    """A session on the free model with at least one message. Read-only; a missing or unreadable
    database is no."""
    if not db.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=1)) as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions s WHERE s.model = ? "
                "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id) LIMIT 1",
                (anon_auth.GUEST_MODEL,)).fetchone()
    except sqlite3.Error:
        return False
    return row is not None
