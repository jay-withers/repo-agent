"""`repoagent selfcheck`, which `make smoke` runs in every job after a deploy.

It has to fail on exactly what would break a real run, and do nothing else: a
false failure rolls back a good release, and a side effect would fire on every
deploy.
"""

from __future__ import annotations

import logging

import pytest

from repoagent import selfcheck, state
from repoagent.cli import main
from repoagent.github import auth


@pytest.fixture
def reachable(monkeypatch):
    monkeypatch.setattr(auth, "token", lambda: "ghs_value-of-secret")
    monkeypatch.setattr(state, "load", lambda: None)


def test_passes_when_the_secrets_github_and_storage_answer(reachable):
    assert selfcheck.run() == 0


def test_fails_on_a_missing_secret_and_names_it(reachable, monkeypatch, caplog):
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL secret GITHUB-APP-PRIVATE-KEY" in caplog.text


def test_fails_when_the_app_cannot_mint_a_token(reachable, monkeypatch, caplog):
    def refused():
        raise RuntimeError("401 from GitHub")

    monkeypatch.setattr(auth, "token", refused)
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL github app token" in caplog.text


def test_never_logs_a_secret_value(reachable, monkeypatch, caplog):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "value-of-secret")
    with caplog.at_level(logging.DEBUG):
        selfcheck.run()
    assert "value-of-secret" not in caplog.text


def test_is_dispatched_from_the_cli(monkeypatch):
    monkeypatch.setattr(selfcheck, "run", lambda: 0)
    assert main(["selfcheck"]) == 0
