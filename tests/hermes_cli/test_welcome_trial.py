"""The desktop's welcome trial (``hermes_cli.welcome_trial``): who may claim it, what a claim leaves
behind, which guests keep the free model, and that a connector guest never asks for inference."""

import contextlib

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


def test_the_claim_is_stamped_by_the_mint_write_itself(portal, monkeypatch):
    def lost_second_write(_flag):
        raise OSError("disk full")
    monkeypatch.setattr(anon_auth, "set_guest_flag", lost_second_write)
    as_local_desktop_backend(monkeypatch)

    with contextlib.suppress(OSError):
        welcome_trial.claim_desktop_free_tier()

    assert anon_auth.free_inference_active()


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


def _legacy_guest() -> None:
    """A guest minted before the claim existed: no welcome_claim key at all, in either store."""
    from hermes_cli.auth import _load_auth_store, _load_provider_state, _save_auth_store, _store_provider_state
    from hermes_cli.auth_nous import _write_shared_nous_state

    anon_auth.ensure_portal_identity(explicit=True)
    store = _load_auth_store()
    state = dict(_load_provider_state(store, "nous"))
    state.pop(anon_auth.WELCOME_CLAIM_FLAG, None)
    _store_provider_state(store, "nous", state, set_active=False)
    _save_auth_store(store)
    _write_shared_nous_state(state)


@pytest.mark.parametrize(("model", "kept"), [(anon_auth.GUEST_MODEL, True), ("anthropic/claude", False)])
def test_a_guest_that_chatted_on_the_free_model_keeps_it(portal, monkeypatch, model, kept):
    from hermes_constants import get_hermes_home

    as_local_desktop_backend(monkeypatch)
    _legacy_guest()  # grandfathering is only for guests minted before the key existed
    _chat(get_hermes_home() / "state.db", model)

    assert welcome_trial.grandfather_existing_guest() is kept
    assert anon_auth.free_inference_active() is kept


def test_a_lost_claim_is_not_replaced(portal, monkeypatch):
    claimed_trial(monkeypatch)
    anon_auth.clear_dead_guest("anon_credential_dead")

    assert welcome_trial.claim_desktop_free_tier() is None
    assert portal.creates() == 1


def test_a_reaped_claimant_replaced_by_a_connector_guest_is_not_grandfathered(portal, monkeypatch):
    from hermes_constants import get_hermes_home

    claimed_trial(monkeypatch)
    _chat(get_hermes_home() / "state.db", anon_auth.GUEST_MODEL)
    anon_auth.clear_dead_guest("anon_credential_dead")
    anon_auth.ensure_portal_identity(explicit=True)  # a connector action mints the replacement

    assert welcome_trial.grandfather_existing_guest() is False
    assert not anon_auth.free_inference_active()


def test_a_connector_guest_adopted_from_the_shared_store_is_not_grandfathered(portal, monkeypatch):
    from hermes_cli.auth import _load_auth_store, _save_auth_store
    from hermes_constants import get_hermes_home

    as_local_desktop_backend(monkeypatch)
    anon_auth.ensure_portal_identity(explicit=True)  # connector mint: welcome_claim False, mirrored
    store = _load_auth_store()
    store["providers"].pop("nous")  # a profile whose own identity differs adopts the shared one
    _save_auth_store(store)
    _chat(get_hermes_home() / "state.db", anon_auth.GUEST_MODEL)

    adopted = anon_auth.ensure_portal_identity(explicit=True)

    assert adopted[anon_auth.WELCOME_CLAIM_FLAG] is False
    assert welcome_trial.grandfather_existing_guest() is False
    assert portal.creates() == 1


LOSSES = {
    "reaped": (lambda: anon_auth.clear_dead_guest("anon_credential_dead"), 1),
    "replaced_by_connector": (lambda: (anon_auth.clear_dead_guest("anon_credential_dead"),
                                       anon_auth.ensure_portal_identity(explicit=True)), 2),
}


@pytest.mark.parametrize("loss", list(LOSSES))
def test_a_lost_trial_is_never_claimed_again_after_a_relaunch(portal, monkeypatch, loss):
    from hermes_cli import free_tier_bootstrap
    from hermes_cli.auth import resolve_provider
    from hermes_cli.auth_constants import AuthError
    from hermes_constants import get_hermes_home

    lose, creates = LOSSES[loss]
    claimed_trial(monkeypatch)
    _chat(get_hermes_home() / "state.db", anon_auth.GUEST_MODEL)
    lose()
    install_state.probe.cache_clear()  # the relaunch
    free_tier_bootstrap.reset_for_tests()
    free_tier_bootstrap.run_bootstrap(announce=False)

    assert welcome_trial.claim_desktop_free_tier() is None
    assert portal.creates() == creates
    assert welcome_trial.grandfather_existing_guest() is False
    assert not anon_auth.free_inference_active()
    with pytest.raises(AuthError):
        resolve_provider("auto")
    record = free_tier_bootstrap.current_record()
    assert (record.provider_configured, record.free_tier_account) == (False, False)



def test_a_leftover_guest_pool_row_is_not_reported_as_a_nous_login(portal, monkeypatch):
    from hermes_cli.auth import _load_auth_store, _save_auth_store, resolve_nous_runtime_credentials
    from hermes_cli.auth_nous import get_nous_auth_status_local

    claimed_trial(monkeypatch)
    resolve_nous_runtime_credentials()  # the trial ran, so the pool holds the guest's exchanged token
    store = _load_auth_store()
    store["providers"].pop("nous")  # the claim is lost; only the pool row is left
    _save_auth_store(store)

    # Provider setup would otherwise tag Nous Portal "Connected" for a credential that runs nothing.
    assert get_nous_auth_status_local()["logged_in"] is False
