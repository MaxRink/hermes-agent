"""The one-shot release of the agentic onboarding's setup profiles: a marked profile becomes a plain one
(marker and setup tool limits gone; chats, credentials and memories kept), the old guide's outcome maps
onto ``onboarding.run``, and the retired onboarding skills are uninstalled."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import onboarding_migrations, profiles
from hermes_cli.config import atomic_config_write, read_user_config_raw
from hermes_state import SessionDB

MARKER = ".setup-profile.json"
SETUP_DEFER = list(onboarding_migrations._SETUP_DEFERRED_TOOLS)


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared-auth"))
    return home


def _setup_profile(name: str, marker: dict) -> Path:
    """A profile the agentic build made: its marker, its tool limits on top of a user-disabled toolset,
    a task chat, a memory and a guest identity."""
    path = profiles.create_profile(name, no_alias=True)
    atomic_config_write(path / "config.yaml", {
        "agent": {"disabled_toolsets": ["browser", "project", "catalog"], "reasoning_effort": "low"},
        "platform_toolsets": {"cli": ["setup", "start_chat", "connections", "no_mcp"]},
        "tools": {"tool_search": {"defer": SETUP_DEFER}},
    })
    (path / MARKER).write_text(json.dumps({"setup_disabled_toolsets": ["project", "catalog"], **marker}))
    db = SessionDB(path / "state.db")
    try:
        db.create_session("task-chat", "desktop")
    finally:
        db.close()
    (path / "memories").mkdir(exist_ok=True)
    (path / "memories" / "USER.md").write_text("Likes short answers.\n")
    (path / "auth.json").write_text(json.dumps({"version": 1, "providers": {"nous": {"anon_token": "anon_guest"}}}))
    return path


def _sessions(path: Path) -> int:
    db = SessionDB(path / "state.db")
    try:
        return len(db._read_all("SELECT id FROM sessions"))
    finally:
        db.close()


def test_a_finished_guide_releases_its_profile_once_and_keeps_its_chats(root):
    setup = _setup_profile("hermes-setup", {"intro": "seen", "completed_at": "2026-10-07T10:00:00+00:00"})
    work = profiles.create_profile("work", no_alias=True)
    atomic_config_write(work / "config.yaml", {"platform_toolsets": {"cli": ["terminal"]}})

    onboarding_migrations.release_setup_profiles()

    assert not (setup / MARKER).exists()
    config = read_user_config_raw(setup / "config.yaml")
    assert config["agent"] == {"disabled_toolsets": ["browser"], "reasoning_effort": "low"}
    assert "platform_toolsets" not in config and "tools" not in config
    assert _sessions(setup) == 1
    assert (setup / "memories" / "USER.md").read_text() == "Likes short answers.\n"
    assert json.loads((setup / "auth.json").read_text())["providers"]["nous"]["anon_token"] == "anon_guest"
    assert read_user_config_raw(work / "config.yaml") == {"platform_toolsets": {"cli": ["terminal"]}}

    root_config = read_user_config_raw(root / "config.yaml")
    assert root_config["onboarding"]["run"] is False
    assert root_config["onboarding"]["seen"]["setup_profile_released"] is True

    from hermes_cli.auth_nous import _read_shared_nous_state
    assert (_read_shared_nous_state() or {}).get("anon_token") == "anon_guest"

    # Latched: a marker that reappears (an old build ran again) is left for that build.
    (setup / MARKER).write_text("{}")
    onboarding_migrations.release_setup_profiles()
    assert (setup / MARKER).exists()


@pytest.mark.parametrize("cli", [["safe"], []])
def test_a_cli_toolset_list_the_user_changed_outlives_the_release(root, cli):
    setup = _setup_profile("hermes-setup", {"intro": "seen"})
    config = read_user_config_raw(setup / "config.yaml")
    config["platform_toolsets"] = {"cli": cli}
    atomic_config_write(setup / "config.yaml", config)

    onboarding_migrations.release_setup_profiles()

    assert read_user_config_raw(setup / "config.yaml")["platform_toolsets"] == {"cli": cli}
