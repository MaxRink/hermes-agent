"""The desktop's boot question, answered once: is onboarding due, and can something carry it.

``onboarding.state`` asks it on every launch, and a cold first boot answering it live competes
with every other boot thread for the same provider reads. The serve boot works it out on its own
thread (:func:`begin_boot`), and ``onboarding.state`` reads the cached answer, waiting a bounded
time for a boot computation still in flight. Every writer that can move the answer calls
:func:`invalidate`: the root ``onboarding.run`` (``onboarding_run.set_run``), the OS user's marker
(``install_state``) and the setup record (``free_tier_bootstrap``). The next reader then computes
live once and caches that. A *stamp* the caller passes (the files the answer reads) catches edits
no writer in this process saw, such as a key typed into ``.env`` by hand. The computation itself
lives with its caller (``tui_gateway``), which owns the default-profile scope it runs in.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable

# The desktop's first question waits this long for the boot computation before answering live.
BOOT_WAIT_SECONDS = 8.0

Due = dict[str, bool]
Stamp = Callable[[], Hashable]

_lock = threading.Lock()
_value: Due | None = None
_value_stamp: Hashable = None
_generation = 0
_boot_done: threading.Event | None = None


def begin_boot(stamp: Stamp = lambda: None) -> Callable[[Callable[[], Due]], None]:
    """Mark a boot computation as in flight (readers wait for it from now on) and return the call
    that finishes it: ``finish(compute)`` runs *compute* and caches its answer, unless a writer
    invalidated meanwhile."""
    global _boot_done
    done = threading.Event()
    with _lock:
        _boot_done = done

    def finish(compute: Callable[[], Due]) -> None:
        try:
            with _lock:
                generation = _generation
            value = compute()
            _store(value, generation, stamp())  # after: computing can itself write a config skeleton
        finally:
            done.set()

    return finish


def read(compute: Callable[[], Due], *, stamp: Stamp = lambda: None, wait: float = BOOT_WAIT_SECONDS) -> Due:
    """The cached answer; a boot computation in flight is waited for (bounded), and with nothing
    cached, or files moved since (*stamp*), *compute* answers live and is cached."""
    done = _boot_done
    if done is not None:
        done.wait(wait)
    seen = stamp()
    with _lock:
        if _value is not None and _value_stamp == seen:
            return dict(_value)
        generation = _generation
    value = compute()
    _store(value, generation, stamp())
    return dict(value)


def invalidate() -> None:
    global _value, _generation
    with _lock:
        _value = None
        _generation += 1


def reset_for_tests() -> None:
    global _boot_done
    invalidate()
    with _lock:
        _boot_done = None


def _store(value: Due, generation: int, seen: Hashable) -> None:
    global _value, _value_stamp
    with _lock:
        if generation == _generation:  # a writer that landed meanwhile wins over this computation
            _value, _value_stamp = dict(value), seen
