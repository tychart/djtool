"""Test isolation: redirect the project state dir (config + cache + decisions)
to a per-test temp directory so tests never read or clobber the real
djtool.toml / .djtool-cache.json / .djtool-decisions.json in the project folder.
"""

import importlib
import pkgutil

import pytest

import djtool


def _all_modules():
    """Yield every djtool submodule (importing them as needed)."""
    for m in pkgutil.walk_packages(djtool.__path__, prefix="djtool."):
        yield importlib.import_module(m.name)


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    # Patch every module that references project_dir (state defines it, others
    # import the name) so cache/config/decisions all redirect to tmp_path.
    for mod in _all_modules():
        if hasattr(mod, "project_dir"):
            monkeypatch.setattr(mod, "project_dir", lambda: state)
    return state
