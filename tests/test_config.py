import os
import sys
import pytest
from pydantic import ValidationError

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.infrastructure.config_loader import (
    load_config,
    get_chroma_db_path,
    is_local_only,
    is_connected,
    AppSettings,
    ConfigLoader,
)


def test_load_config():
    config = load_config()
    assert isinstance(config, dict)
    assert "database" in config
    assert "mode" in config["database"]
    assert "operating_mode" in config
    assert config["operating_mode"] in ("local_only", "connected")


def test_get_chroma_db_path():
    path = get_chroma_db_path()
    assert os.path.isabs(path)
    assert "chroma" in path.lower()


def test_operating_mode_defaults_and_helpers():
    settings = AppSettings()
    assert settings.operating_mode == "local_only"

    # An explicit canonical path deliberately bypasses operator-local overlay.
    loader = ConfigLoader(
        config_path=os.path.join(
            os.path.dirname(__file__), "..", "config", "settings.yaml"
        )
    )
    assert loader.is_local_only() is True
    assert loader.is_connected() is False
    assert is_local_only() is not is_connected()


def test_operating_mode_connected():
    settings = AppSettings(operating_mode="connected")
    assert settings.operating_mode == "connected"


def test_operating_mode_invalid_raises():
    with pytest.raises(ValidationError):
        AppSettings(operating_mode="unrestricted_cloud")


def test_operator_local_resource_configuration_overlays_canonical(tmp_path):
    canonical = tmp_path / "settings.yaml"
    canonical.write_text('operating_mode: "local_only"\n', encoding="utf-8")
    local = tmp_path / "config.toml"
    local.write_text(
        """
[ai_resources]
operating_mode = "connected"
[ai_resources.providers.codex]
enabled = true
[ai_resources.provider_policy]
allow_paid_api = false
""".strip(),
        encoding="utf-8",
    )

    loader = ConfigLoader(config_path=canonical, local_config_path=local)

    assert loader.settings.operating_mode == "connected"
    assert loader.settings.providers["codex"].enabled is True
    assert loader.settings.provider_policy.allow_paid_api is False


def test_duplicate_canonical_keys_fail_validation(tmp_path):
    canonical = tmp_path / "duplicate.yaml"
    canonical.write_text(
        "operating_mode: local_only\noperating_mode: connected\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate configuration key"):
        ConfigLoader(config_path=canonical)
