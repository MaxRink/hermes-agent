"""The desktop's welcome trial (``hermes_cli.welcome_trial``): who may claim it, what a claim leaves
behind, which guests keep the free model, and that a connector guest never asks for inference."""

import pytest

from hermes_cli import anon_auth, install_state, welcome_trial
from tests.hermes_cli.anon_portal import as_local_desktop_backend, claimed_trial, install_portal


@pytest.fixture
def portal(monkeypatch, tmp_path):
    return install_portal(monkeypatch, tmp_path)


def test_a_first_install_on_the_local_desktop_backend_claims_the_trial(portal, monkeypatch):
    state = claimed_trial(monkeypatch)

    assert state[anon_auth.WELCOME_CLAIM_FLAG] is True
    assert anon_auth.free_inference_active()
    assert install_state.existing_install()  # the machine is marked: never claimed twice here
    assert welcome_trial.claim_blocker() is not None
    assert portal.creates() == 1


BLOCKED = {
    "the CLI": lambda mp, home: None,
    "an SSH backend": lambda mp, home: (mp.setenv("HERMES_DESKTOP", "1"), mp.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)),
    "guests switched off": lambda mp, home: (as_local_desktop_backend(mp),
                                              (home / "config.yaml").write_text("nous:\n  guest: false\n", encoding="utf-8")),
    "a provider key": lambda mp, home: (as_local_desktop_backend(mp), mp.setenv("OPENROUTER_API_KEY", "sk-or-test")),
    "a used machine": lambda mp, home: (as_local_desktop_backend(mp), install_state.mark_machine_used()),
}


@pytest.mark.parametrize("case", list(BLOCKED))
def test_the_trial_is_never_claimed_outside_a_first_desktop_install(portal, monkeypatch, case):
    from hermes_constants import get_hermes_home

    BLOCKED[case](monkeypatch, get_hermes_home())

    assert welcome_trial.claim_desktop_free_tier() is None
    assert portal.creates() == 0
    assert not anon_auth.free_inference_active()


def test_a_connector_guest_neither_claims_nor_carries_inference(portal, monkeypatch):
    as_local_desktop_backend(monkeypatch)
    anon_auth.ensure_portal_identity(explicit=True)  # the first hosted connector action

    assert welcome_trial.claim_desktop_free_tier() is None
    assert not anon_auth.free_inference_active()
    from hermes_cli.auth import resolve_nous_access_token
    resolve_nous_access_token()
    assert [r["body"]["purpose"] for r in portal.token_requests] == ["connectors"]


def test_a_claimed_guest_carries_inference_only_on_the_local_desktop_backend(portal, monkeypatch):
    state = claimed_trial(monkeypatch)
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN")  # the same home, read by a CLI process

    assert not anon_auth.free_inference_active()
    assert anon_auth.guest_without_free_tier(state)


def _chat(db_path, model: str) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path)
    db.create_session("s1", "desktop", model=model)
    db.append_message("s1", "user", "hi")
    db.close()


@pytest.mark.parametrize(("model", "kept"), [(anon_auth.GUEST_MODEL, True), ("anthropic/claude", False)])
def test_a_guest_that_chatted_on_the_free_model_keeps_it(portal, monkeypatch, model, kept):
    from hermes_constants import get_hermes_home

    as_local_desktop_backend(monkeypatch)
    anon_auth.ensure_portal_identity(explicit=True)
    _chat(get_hermes_home() / "state.db", model)

    assert welcome_trial.grandfather_existing_guest() is kept
    assert anon_auth.free_inference_active() is kept


def test_a_lost_claim_is_not_replaced(portal, monkeypatch):
    claimed_trial(monkeypatch)
    anon_auth.clear_dead_guest("anon_credential_dead")

    assert welcome_trial.claim_desktop_free_tier() is None
    assert portal.creates() == 1
