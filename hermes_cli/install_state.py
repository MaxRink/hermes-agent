"""Has this OS user already used Hermes on this computer?

The desktop's welcome trial goes only to a first install, once per OS user, so ``onboarding.start``
asks this before it claims one. The answer is about the OS user, not the active profile: it looks at
every Hermes root the machine names (default roots, channel and package roots, the roots gateway
services and the ``hermes`` launcher point at) and at a marker file no Hermes setting can move.

This is product gating, not security: the inference API's limits stop abuse. So it reads only small
known files, never scans the disk, never writes into a Hermes root, and treats doubt as "used".
"""

from __future__ import annotations

import contextlib
import enum
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import cache, cached_property
from glob import glob
from pathlib import Path
from typing import Any

_MAX_FILE_BYTES = 1 << 20


class Evidence(enum.Enum):
    """Why the machine counts as used. For logs and tests; never shown to users."""

    MACHINE_MARKER = "machine-marker"      # an earlier probe or trial claim on this machine
    ONBOARDING_DONE = "onboarding-done"    # a root's config has ``onboarding.run: false``
    NAMED_PROFILE = "named-profile"        # a live profile someone created
    PROVIDER_CONFIG = "provider-config"    # config.yaml picks a provider other than auto
    PROVIDER_KEY = "provider-key"          # .env holds a provider, tool, messaging or skill key
    PROVIDER_LOGIN = "provider-login"      # auth.json / nous_auth.json holds a login or a guest
    MESSAGES = "messages"                  # state.db holds a message
    UNREADABLE = "unreadable"              # a root we could not, or must not, read


@dataclass(frozen=True, slots=True)
class Finding:
    evidence: Evidence
    where: Path


@dataclass(frozen=True, slots=True)
class InstallState:
    findings: tuple[Finding, ...]

    @property
    def existing(self) -> bool:
        return bool(self.findings)

    @property
    def fresh(self) -> bool:
        return not self.findings


Source = Callable[[], Iterable[Path]]


@dataclass(frozen=True, slots=True)
class Places:
    """Where to look. ``markers[0]`` is the marker this process writes; every entry is read."""

    markers: tuple[Path, ...]
    sources: tuple[Source, ...]
    protected: tuple[Path, ...] = ()

    @classmethod
    def of_this_machine(cls) -> Places:
        from hermes_platform.resolver import known_dirs

        return cls(
            markers=tuple(_expand_all(known_dirs.machine_marker_files())),
            sources=(_active_root, _home_globs, _shared_auth_dir, _registry_home, _service_homes, _launcher_root),
            protected=_protected_dirs(),
        )


def machine_places() -> Places:
    """The seam tests replace; production always looks at the real machine."""
    return Places.of_this_machine()


@cache
def probe() -> InstallState:
    """One answer per process (the first finding wins). :func:`mark_machine_used` clears it."""
    places = machine_places()
    if marker := next((m for m in places.markers if m.is_file()), None):
        return InstallState((Finding(Evidence.MACHINE_MARKER, marker),))
    for root in candidate_roots(places):
        if finding := root_evidence(root, places):
            if finding.evidence is not Evidence.UNREADABLE:  # doubt blocks the trial, never marks
                mark_machine_used()
            return InstallState((finding,))
    return InstallState(())


def existing_install() -> bool:
    return probe().existing


def mark_machine_used() -> None:
    """Record that this machine has a used install, or just claimed the trial. Atomic, idempotent."""
    places = machine_places()
    if places.markers:
        from utils import atomic_json_write

        marker = places.markers[0]
        marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(marker, {})  # unique temp name: two backends may mark at once
    probe.cache_clear()


# ── where to look ─────────────────────────────────────────────────────────────────────────────


def candidate_roots(places: Places) -> Iterator[Path]:
    """Each named root once, existing directories only. A root inside a protected folder is yielded
    unresolved: :func:`root_evidence` reports it without touching it."""
    seen: set[Path] = set()
    for source in places.sources:
        for raw in source():
            path = Path(os.path.abspath(os.path.expanduser(str(raw))))
            if not _is_protected(path, places):
                try:
                    path = path.resolve()
                except OSError:
                    continue
                if not path.is_dir():
                    continue
            if path not in seen:
                seen.add(path)
                yield path


def _is_protected(path: Path, places: Places) -> bool:
    return _inside(path, places.protected)


def _inside(path: Path, dirs: Iterable[Path]) -> bool:
    return any(path == d or d in path.parents for d in dirs)


def _protected_dirs() -> tuple[Path, ...]:
    from hermes_platform.resolver import known_dirs

    return tuple(_expand_all(known_dirs.mac_privacy_protected_dirs()))


def _expand_all(patterns: Iterable[str]) -> Iterator[Path]:
    """Expand ``~`` and environment variables (``$X`` on POSIX, ``%X%`` on Windows), then globs. An
    entry naming an unset variable is dropped."""
    for pattern in patterns:
        expanded = os.path.expanduser(os.path.expandvars(pattern))
        if "$" in expanded or "%" in expanded:
            continue
        if any(ch in expanded for ch in "*?["):
            yield from (Path(hit) for hit in sorted(glob(expanded)))
        else:
            yield Path(expanded)


def _active_root() -> Iterable[Path]:
    from hermes_constants import _get_platform_default_hermes_home, get_default_hermes_root

    return (get_default_hermes_root(), _get_platform_default_hermes_home())


def _home_globs() -> Iterable[Path]:
    from hermes_platform.resolver import known_dirs

    return _expand_all(known_dirs.hermes_home_globs())


def _shared_auth_dir() -> Iterable[Path]:
    raw = os.environ.get("HERMES_SHARED_AUTH_DIR", "").strip()
    return (Path(raw),) if raw else ()


def _registry_home() -> Iterable[Path]:
    """The ``HERMES_HOME`` older Windows installers saved for the user (the desktop honours it)."""
    if sys.platform != "win32":
        return ()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, "HERMES_HOME")
    except OSError:
        return ()
    return (Path(os.path.expandvars(str(value))),) if str(value).strip() else ()


def _service_homes() -> Iterable[Path]:
    from hermes_cli.gateway_service_owner import service_definition_homes
    from hermes_platform.resolver import known_dirs

    for definition in _expand_all(known_dirs.gateway_service_definition_globs()):
        with contextlib.suppress(OSError, UnicodeError, ValueError):
            yield from (Path(home.strip().strip('"')) for home in service_definition_homes(_read_text(definition)))


def _launcher_root() -> Iterable[Path]:
    """The root that owns the ``hermes`` on PATH: its launcher execs that root's ``tools/`` Python."""
    from hermes_cli._launchers import _launcher_python
    from hermes_platform.resolver import locate_command

    found = locate_command("hermes").command
    if not found:
        return ()
    protected = _protected_dirs()
    launcher = Path(os.path.abspath(found[0]))
    if not _inside(launcher, protected):
        launcher = launcher.resolve()
    if _inside(launcher, protected):
        return (launcher.parent,)  # reading it would raise a macOS permission prompt; reported as unreadable
    python = _launcher_python(launcher)
    owners = [p.parent for p in (python.parents if python else ()) if p.name == "tools"]
    if not owners and launcher.parent.name == "bin" and launcher.parent.parent.name == ".hermes":
        owners = [launcher.parent.parent.parent.parent]  # <root>/<checkout>/.hermes/bin/hermes
    return owners[:1]


# ── what counts as used, per root ─────────────────────────────────────────────────────────────


def root_evidence(root: Path, places: Places) -> Finding | None:
    if _is_protected(root, places):
        return Finding(Evidence.UNREADABLE, root)
    home = _Root(root)
    for evidence, check in CHECKS:
        try:
            if check(home):
                return Finding(evidence, root)
        except (OSError, ValueError, UnicodeError, sqlite3.Error):
            return Finding(Evidence.UNREADABLE, root)
    return None


class _Root:
    """One root's files, each read at most once. No ``load_config``: it writes SOUL.md and the
    directory skeleton into the root it reads."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @cached_property
    def config(self) -> dict[str, Any]:
        return _read_mapping(self.path / "config.yaml", _parse_yaml)

    @cached_property
    def auth_stores(self) -> tuple[dict[str, Any], ...]:
        # The shared-auth override names the directory itself, so its file sits at the top.
        names = ("auth.json", "shared/nous_auth.json", "nous_auth.json")
        return tuple(_read_mapping(self.path / name, json.loads) for name in names)


def _onboarding_done(home: _Root) -> bool:
    section = home.config.get("onboarding")
    return isinstance(section, dict) and section.get("run") is False


def _named_profile(home: _Root) -> bool:
    from hermes_constants import PROFILE_ID_RE, named_profile_has_identity, named_profile_is_deleted

    profiles = home.path / "profiles"
    return profiles.is_dir() and any(
        entry.is_dir() and entry.name != "default" and PROFILE_ID_RE.match(entry.name)
        and named_profile_has_identity(entry) and not named_profile_is_deleted(entry)
        for entry in profiles.iterdir())


def _provider_config(home: _Root) -> bool:
    model = home.config.get("model")
    provider = model.get("provider") if isinstance(model, dict) else None
    return isinstance(provider, str) and provider.strip() not in ("", "auto")


# A deployment secret, not something a user entered.
_IGNORED_ENV_KEYS = frozenset({"HERMES_ANON_API_SECRET"})
_USER_ENV_CATEGORIES = frozenset({"provider", "tool", "messaging", "skill"})


def _provider_key(home: _Root) -> bool:
    from hermes_cli.config_defaults import OPTIONAL_ENV_VARS

    from agent.secret_scope import load_env_file

    path = home.path / ".env"
    if not path.is_file():
        return False
    _read_text(path)  # too large or unreadable raises, so doubt counts as used; the tokenizer swallows both
    return any(value and key not in _IGNORED_ENV_KEYS
               and (OPTIONAL_ENV_VARS.get(key) or {}).get("category") in _USER_ENV_CATEGORIES
               for key, value in load_env_file(path).items())


def _provider_login(home: _Root) -> bool:
    """A login, an active provider or a guest (only a connector action or a trial claim makes one),
    or a credential the user added by hand. Pool rows Hermes seeds on its own (shell env keys, other
    tools' CLI logins) prove nothing."""
    for store in home.auth_stores:
        providers = store.get("providers")
        if store.get("active_provider") or (isinstance(providers, dict) and any(providers.values())):
            return True
        if store.get("auth_method") or store.get("anon_token") or store.get("refresh_token"):
            return True  # nous_auth.json holds one provider state at its top level
        pool = store.get("credential_pool")
        rows = (row for rows in (pool.values() if isinstance(pool, dict) else ()) if isinstance(rows, list)
                for row in rows if isinstance(row, dict))
        if any(str(row.get("source", "")).split(":", 1)[0] == "manual" for row in rows):
            return True
    return False


_DB_BUSY_SECONDS = 1.0
_DB_BUSY_ERRORS = frozenset({"SQLITE_BUSY", "SQLITE_LOCKED"})


def _messages(home: _Root) -> bool:
    db = home.path / "state.db"
    if not db.is_file():
        return False
    # Read-only URI, never the state registry: that opens a writer and migrates a schema we do not own.
    try:
        with contextlib.closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=_DB_BUSY_SECONDS)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone() is None:
                return False
            return conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError as exc:
        # Still locked after sqlite's own retries: a first boot is creating or healing this database
        # right now. A lock is not a message, and reading it as "unreadable" would cost a new user the trial.
        if getattr(exc, "sqlite_errorname", "") in _DB_BUSY_ERRORS:
            return False
        raise


CHECKS: tuple[tuple[Evidence, Callable[[_Root], bool]], ...] = (
    (Evidence.ONBOARDING_DONE, _onboarding_done),
    (Evidence.NAMED_PROFILE, _named_profile),
    (Evidence.PROVIDER_CONFIG, _provider_config),
    (Evidence.PROVIDER_KEY, _provider_key),
    (Evidence.PROVIDER_LOGIN, _provider_login),
    (Evidence.MESSAGES, _messages),  # last: the only check that opens a database
)


# ── small readers ─────────────────────────────────────────────────────────────────────────────


def _read_text(path: Path) -> str:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"{path} is larger than {_MAX_FILE_BYTES} bytes")
    return path.read_text(encoding="utf-8-sig")


def _read_mapping(path: Path, parse: Callable[[str], Any]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = parse(_read_text(path))
    return data if isinstance(data, dict) else {}


def _parse_yaml(text: str) -> Any:
    import hermes_yaml

    try:
        return hermes_yaml.safe_load(text)
    except hermes_yaml.YAMLError as exc:
        raise ValueError(str(exc)) from exc
