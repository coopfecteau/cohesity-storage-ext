"""The ExtensionImpl lifecycle as the SDK actually drives it.

Everything else in the suite tests the client and the metric mapping in isolation. This file
exists because that left the class wiring them to the SDK untested, and it broke on a real
ActiveGate: the SDK runs fastcheck() WITHOUT ever calling initialize(), so any state created
only in initialize() does not exist yet when fastcheck() probes the cluster.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dynatrace_extension import Extension, StatusValue

from cohesity_storage.__main__ import EXTENSION_NAME, ExtensionImpl

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FakeActivationConfig:
    def __init__(self, endpoints):
        self._endpoints = endpoints

    def get(self, key, default=None):
        return {"endpoints": self._endpoints}.get(key, default)


@pytest.fixture
def fresh_extension():
    # Extension is a process-wide singleton, so an instance left over from another test would
    # already carry initialize()'s state and hide exactly the bug this file guards against.
    Extension._instance = None
    extension = ExtensionImpl(name=EXTENSION_NAME)
    for attribute in ("_clients", "_last_run"):
        if hasattr(extension, attribute):
            delattr(extension, attribute)
    yield extension
    Extension._instance = None


def replay_endpoint() -> dict:
    return {
        "name": "replay",
        "host": "10.20.30.40",
        "apiKey": "replay-key",
        "fixtureDir": str(FIXTURES),
    }


def test_fastcheck_runs_before_initialize(fresh_extension):
    assert not hasattr(fresh_extension, "_clients")
    fresh_extension.activation_config = FakeActivationConfig([replay_endpoint()])

    status = fresh_extension.fastcheck()

    assert status.status == StatusValue.OK, status.message


def test_fastcheck_leaves_state_that_query_can_reuse(fresh_extension):
    fresh_extension.activation_config = FakeActivationConfig([replay_endpoint()])

    fresh_extension.fastcheck()

    assert len(fresh_extension._clients) == 1
