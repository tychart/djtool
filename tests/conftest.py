"""Test isolation: redirect the project state dir (config + cache) to a
per-test temp directory so tests never read or clobber the real
djtool.toml / .djtool-cache.json in the project folder.
"""

import pytest

from djtool import core as dt


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(dt, "project_dir", lambda: state)
    return state
