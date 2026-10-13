"""``onboarding.state`` / ``onboarding.start`` and the desktop's welcome trial: a new run on a first
install claims it before the chat exists, a rerun never does, a failed claim writes nothing, and only
the desktop's local backend opens onboarding by itself."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent.onboarding_flow import Facts
from hermes_cli import anon_auth, install_state
from tests.hermes_cli.anon_portal import as_local_desktop_backend, install_portal
from tui_gateway import server


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "hermes-root"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", str(home))
    monkeypatch.setattr(server, "_db", None)
    monkeypatch.setattr("agent.onboarding_facts.read_facts", lambda **kw: Facts(os="macos", arch="arm64"))
    return home


@pytest.fixture
def portal(root, tmp_path, monkeypatch):
    fake = install_portal(monkeypatch, tmp_path)
    monkeypatch.setitem(server._methods, "session.resume",
                        lambda rid, params: {"result": {"session_id": f"rt-{params['session_id']}"}})
    monkeypatch.setitem(server._methods, "prompt.submit", lambda rid, params: {"result": {"status": "streaming"}})
    return fake


def _call(method: str, params: dict | None = None) -> dict:
    return server.handle_request({"jsonrpc": "2.0", "id": 9, "method": method, "params": params or {}})


def test_a_new_run_on_a_first_install_claims_the_trial_once(portal, monkeypatch):
    as_local_desktop_backend(monkeypatch)

    assert _call("onboarding.state")["result"]["run"] is True
    assert _call("onboarding.state")["result"]["eligible"] is True
    _call("onboarding.start")
    _call("onboarding.start")                    # a relaunch reopens the run: no second claim
    _call("onboarding.start", {"rerun": True})   # Settings: runs on the model it has

    assert portal.creates() == 1
    assert anon_auth.free_inference_active()


def test_a_rerun_on_a_first_install_never_claims(portal, monkeypatch):
    as_local_desktop_backend(monkeypatch)

    _call("onboarding.start", {"rerun": True})

    assert portal.creates() == 0


def test_a_failed_claim_opens_nothing_and_the_next_launch_tries_again(portal, monkeypatch):
    as_local_desktop_backend(monkeypatch)
    portal.create_response = httpx.Response(503, json={"error": "unavailable"})

    failed = _call("onboarding.start")

    assert failed["error"]["code"] == 5079
    assert _call("onboarding.state")["result"]["session_id"] is None
    assert install_state.probe().fresh
    portal.create_response = None
    anon_auth.reset_mint_memo_for_tests()
    assert "result" in _call("onboarding.start")
    assert anon_auth.free_inference_active()


def test_only_the_local_desktop_backend_opens_onboarding_by_itself(portal, monkeypatch):
    assert _call("onboarding.state")["result"]["eligible"] is False   # CLI / SSH / URL backend

    as_local_desktop_backend(monkeypatch)
    install_state.mark_machine_used()
    assert _call("onboarding.state")["result"] == {"run": False, "eligible": False, "session_id": None, "run_id": None}

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert _call("onboarding.state")["result"]["eligible"] is True


def test_the_dev_fresh_install_switch_treats_a_used_machine_as_new(portal, monkeypatch):
    as_local_desktop_backend(monkeypatch)
    install_state.mark_machine_used()

    assert _call("onboarding.state", {"assume_fresh": True})["result"]["run"] is True
    assert "result" in _call("onboarding.start", {"assume_fresh": True})
    assert portal.creates() == 1
