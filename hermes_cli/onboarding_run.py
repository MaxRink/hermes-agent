"""``onboarding.run``: whether the desktop first-run onboarding opens on the next launch.

The flag lives in the ROOT profile's ``config.yaml`` (``get_default_hermes_root()``), never in the
launch profile's: a pooled backend launched under ``-p work`` must read and write the same file as
one launched under ``default``, because onboarding always targets the default profile.
Unset means "only on a machine that never used Hermes"; ``true`` re-runs it once; ``false`` means done.
"""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_default_hermes_root


def _root_config() -> Path:
    return get_default_hermes_root() / "config.yaml"


def should_run(*, assume_fresh: bool = False) -> bool:
    """The root config's explicit ``onboarding.run`` (managed overlay included) when it is a bool,
    else whether this machine has never used Hermes (``install_state``; *assume_fresh* is the
    unpackaged desktop's ``--fresh-install`` switch)."""
    from hermes_cli.config_effective import load_user_config_effective

    # Not load_config(): it resolves the launch profile and fills the default None for an unset flag.
    section = load_user_config_effective(_root_config()).get("onboarding")
    value = section.get("run") if isinstance(section, dict) else None
    if isinstance(value, bool):
        return value
    from hermes_cli.install_state import probe

    return assume_fresh or probe().fresh


def set_run(value: bool, *, mark_profile_offered: bool = False) -> None:
    """Persist ``onboarding.run`` (and, with *mark_profile_offered*, ``onboarding.seen.profile_build_offered``)
    in the root ``config.yaml`` as one write, keeping every other key and comment. A failed write raises."""
    from agent.onboarding import PROFILE_BUILD_FLAG
    from hermes_cli import config as config_mod

    path = _root_config()
    with config_mod._CONFIG_LOCK:
        config = config_mod.read_user_config_raw(path)
        section = config_mod._ensure_dict(config, "onboarding")
        section["run"] = value
        if mark_profile_offered:
            config_mod._ensure_dict(section, "seen")[PROFILE_BUILD_FLAG] = True
        config_mod.atomic_config_write(path, config)
