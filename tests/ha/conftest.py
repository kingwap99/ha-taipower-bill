"""Fixtures for the real-Home-Assistant suite.

Two things happen here, and both are load-bearing.

**1. The integration under test is this repo's own copy.**

``pytest-homeassistant-custom-component`` resolves custom integrations from
``<config_dir>/custom_components``, and its default ``config_dir`` is the
plugin's own ``testing_config`` -- which does not contain ``taipower_bill``.
The usual workaround is a symlink from site-packages into the repo, which is
invisible to CI and easy to leave pointing at a stale checkout.  Instead we
override ``hass_config_dir`` to a per-test temporary directory seeded with the
repo's ``custom_components/taipower_bill``, so a run always exercises the code
that is in the working tree.

**2. The recorder's database is prepared before ``hass`` is set up.**

``recorder_db_url`` asserts ``hass_fixture_setup`` is still empty, because the
recorder caches the time zone and the database at setup time.  ``hass`` in turn
depends on ``hass_config_dir``.  So an autouse fixture that asks for
``enable_custom_integrations`` (which depends on ``hass``) without first
pulling in ``recorder_db_url`` sets ``hass`` up first and every
recorder-backed test errors out.  Chaining the two fixtures makes the order
explicit rather than a matter of declaration order.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INTEGRATION = REPO_ROOT / "custom_components" / "taipower_bill"


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """A throwaway HA config dir carrying a fresh copy of this repo's integration."""
    dest = pathlib.Path(hass_tmp_config_dir) / "custom_components" / INTEGRATION.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ``hass_tmp_config_dir`` copies the plugin's ``testing_config`` with
    # ``symlinks=True``, so if somebody left a symlink there we must unlink it
    # rather than rmtree it (rmtree refuses symlinks).
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        INTEGRATION, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def _prepare_recorder_database_first(recorder_db_url):
    """Force the recorder's test database to be prepared before ``hass``."""
    return recorder_db_url


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(
    _prepare_recorder_database_first, enable_custom_integrations
):
    """Every test in this suite loads ``custom_components/`` from the test dir."""
    yield
