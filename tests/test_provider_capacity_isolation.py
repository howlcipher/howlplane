"""The operator's provider capacity file is never read or written by tests.

Every test hides the real agent CLIs from PATH, so a pool that probed and saved
during a test recorded MISSING_EXECUTABLE for every worker in
~/.config/howlplane/provider_capacity.json, and `howlplane route` then refused
to select any of them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from howlplane.control_plane.synthesis import provider_pool
from howlplane.control_plane.synthesis.provider_pool import (
    ProviderCapacityStore,
    ProviderPoolManager,
    default_capacity_path,
)


def test_capacity_path_defaults_to_the_operator_config(monkeypatch, tmp_path):
    monkeypatch.delenv("HOWLPLANE_CAPACITY_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_capacity_path() == tmp_path / ".config" / "howlplane" / "provider_capacity.json"


def test_capacity_path_honours_override(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "capacity.json"
    monkeypatch.setenv("HOWLPLANE_CAPACITY_PATH", str(target))

    assert default_capacity_path() == target


def test_from_config_uses_the_resolved_capacity_path(monkeypatch, tmp_path):
    target = tmp_path / "capacity.json"
    monkeypatch.setenv("HOWLPLANE_CAPACITY_PATH", str(target))
    captured = {}

    def fake_from_settings(cls, settings, *, state_path=None, **kwargs):
        captured["state_path"] = state_path
        return None

    monkeypatch.setattr(ProviderPoolManager, "from_settings", classmethod(fake_from_settings))
    ProviderPoolManager.from_config(read_only=True, probe_on_start=False)

    assert Path(captured["state_path"]) == target


def test_suite_points_the_capacity_file_under_the_temporary_directory():
    resolved = default_capacity_path().resolve()

    assert resolved.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert provider_pool.default_capacity_path is default_capacity_path


def test_saving_capacity_outside_the_temporary_directory_is_refused(tmp_path):
    import pytest

    outside = Path.home() / ".config" / "howlplane" / "provider_capacity.json"
    before = outside.read_bytes() if outside.is_file() else None
    with pytest.raises(AssertionError, match="outside"):
        ProviderCapacityStore(outside).save({})
    after = outside.read_bytes() if outside.is_file() else None

    assert before == after
