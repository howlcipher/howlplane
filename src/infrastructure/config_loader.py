#!/usr/bin/env python3
import os
from pathlib import Path
import tomllib
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.infrastructure.secret_manager import SecretManager


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate configuration keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate configuration key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _deep_merge(base: dict, override: dict) -> dict:
    """Returns a recursive mapping merge without mutating either input."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resource_local_config(local_data: dict) -> dict:
    """Extracts only supported application overrides from operator TOML."""
    source = local_data.get("ai_resources", local_data)
    supported = ("operating_mode", "providers", "provider_policy")
    return {key: source[key] for key in supported if key in source}


class DatabaseSettings(BaseModel):
    chroma_db_path: str = ".chroma"
    pgvector_dsn: str = ""
    mode: str = "sqlite"


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8000
    webhook_secret: str = ""


class AgentsSettings(BaseModel):
    default_language: str = "en_US"
    max_context_tokens: int = 8192


class BackupSettings(BaseModel):
    targets: list = ["documentation"]
    backup_dir: str = "infrastructure/backups"
    filename: str = "library_backup.tar.gz"


class TierModelsSettings(BaseModel):
    tier_1: str = ""
    tier_2: str = ""
    tier_3: str = ""


class TierTimeoutsSettings(BaseModel):
    tier_1: float = 0.0
    tier_2: float = 0.0
    tier_3: float = 0.0


class PayloadPipelineSettings(BaseModel):
    enabled: bool = False
    max_attempts: int = 3
    artifact_dir: str = "logs/payloads"
    preflight: bool = True
    preflight_timeout: float = 120.0
    timeout: float = 600.0
    transport_retries: int = 2
    transport_backoff: float = 2.0
    structured_outputs: bool = True
    tier_models: TierModelsSettings = TierModelsSettings()
    tier_timeouts: TierTimeoutsSettings = TierTimeoutsSettings()


class SkillRouterSettings(BaseModel):
    enabled: bool = True
    top_k: int = 3
    score_threshold: float = 0.0
    max_context_chars: int = 12000
    skills_dir: str = ".agents/skills"


class IndexingSettings(BaseModel):
    collection_name: str = "ai_library_knowledge"
    max_chunk_length: int = 1000
    batch_size: int = 100


class ProviderExecutionProfileSettings(BaseModel):
    """Operator overrides for one provider invocation's tool permissions."""

    extra_allowed_bash: List[str] = Field(default_factory=list)
    disallowed_tools: List[str] = Field(default_factory=list)
    permission_mode: Optional[Literal["acceptEdits", "plan", "manual", "dontAsk"]] = None

    model_config = {"extra": "forbid"}


class ProviderResourceSettings(BaseModel):
    """Operator permission and optional identity constraints for one resource."""

    enabled: bool = True
    interface_id: Optional[str] = None
    model_id: Optional[str] = None
    execution_profile: Optional[ProviderExecutionProfileSettings] = None

    model_config = {"extra": "forbid"}

    @field_validator("interface_id", "model_id")
    @classmethod
    def non_blank_optional_identity(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("resource identity overrides cannot be blank")
        return value


class ProviderPolicySettings(BaseModel):
    """Economic and deterministic preference policy below hard authority."""

    strategy: Literal["adaptive_capacity", "deterministic"] = "adaptive_capacity"
    subscription_first: bool = True
    prefer_existing_capacity: bool = True
    external_before_local: bool = True
    allow_paid_api: bool = False
    preserve_independent_review: bool = True
    preferred_external: List[str] = Field(default_factory=list)
    preferred_local: List[str] = Field(default_factory=list)
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    max_metered_invocations: Optional[int] = Field(default=None, ge=0)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_economic_budget(self):
        if (
            not self.allow_paid_api
            and self.max_metered_invocations not in (None, 0)
        ):
            raise ValueError(
                "max_metered_invocations cannot authorize spend when allow_paid_api is false"
            )
        return self


class AppSettings(BaseSettings):
    operating_mode: Literal["local_only", "connected"] = "local_only"
    llm_model: str = "ollama/qwen3:30b-instruct"
    llm_timeout: float = 600.0
    preflight: bool = True
    mcp_connect_timeout: float = 30.0
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    database: DatabaseSettings = DatabaseSettings()
    server: ServerSettings = ServerSettings()
    agents: AgentsSettings = AgentsSettings()
    backup: BackupSettings = BackupSettings()
    skill_router: SkillRouterSettings = SkillRouterSettings()
    indexing: IndexingSettings = IndexingSettings()
    payload_pipeline: PayloadPipelineSettings = PayloadPipelineSettings()
    providers: Dict[str, ProviderResourceSettings] = Field(default_factory=dict)
    provider_policy: ProviderPolicySettings = ProviderPolicySettings()
    active_mcps: list = []
    mcp_servers: dict = {}

    model_config = SettingsConfigDict(
        env_file=".env", env_nested_delimiter="__", extra="ignore"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            file_secret_settings,
        )


class ConfigLoader:
    """
    A class to load and provide access to configuration settings,
    as well as common directory paths to apply DRY principles.
    """

    def __init__(self, config_path=None, local_config_path=None):
        """
        Initializes the ConfigLoader and calculates common paths.
        """
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.repo_root = os.path.dirname(os.path.dirname(self.script_dir))

        if config_path is None:
            self.config_path = os.path.join(self.repo_root, "config", "settings.yaml")
        else:
            self.config_path = config_path

        yaml_data = {}
        if os.path.exists(self.config_path):
            with open(self.config_path, "r") as f:
                loader = _UniqueKeyLoader(f)
                try:
                    yaml_data = loader.get_single_data() or {}
                finally:
                    loader.dispose()

        local_candidate = local_config_path
        if local_candidate is None and config_path is None:
            local_candidate = (
                os.environ.get("HOWLPLANE_LOCAL_CONFIG")
                or Path.home() / ".config" / "howlplane" / "config.toml"
            )
        self.local_config_path = (
            Path(local_candidate).expanduser() if local_candidate else None
        )
        if self.local_config_path and self.local_config_path.is_file():
            with open(self.local_config_path, "rb") as local_file:
                local_data = tomllib.load(local_file)
            yaml_data = _deep_merge(yaml_data, _resource_local_config(local_data))

        # Load pydantic settings prioritizing .env over yaml_data
        self.settings = AppSettings(**yaml_data)

        # Override with Secret Manager if configured
        secret_mgr = SecretManager()
        if os.environ.get("USE_AWS_SECRETS_MANAGER") == "true":
            aws_api_key = secret_mgr.get_secret("GEMINI_API_KEY")
            if aws_api_key:
                self.settings.gemini_api_key = aws_api_key

            aws_anthropic_key = secret_mgr.get_secret("ANTHROPIC_API_KEY")
            if aws_anthropic_key:
                self.settings.anthropic_api_key = aws_anthropic_key

            aws_webhook = secret_mgr.get_secret("WEBHOOK_SECRET")
            if aws_webhook:
                self.settings.server.webhook_secret = aws_webhook

        self.config = self.settings.model_dump()

    def get(self, key, default=None):
        """
        Retrieves a value from the configuration using the provided key.
        """
        return self.config.get(key, default)

    def get_repo_root(self):
        """
        Returns the root directory of the repository.
        """
        return self.repo_root

    def is_local_only(self) -> bool:
        """
        Returns True if the operating mode is 'local_only'.
        """
        return self.get("operating_mode", "local_only") == "local_only"

    def is_connected(self) -> bool:
        """
        Returns True if the operating mode is 'connected'.
        """
        return self.get("operating_mode", "local_only") == "connected"


# Provide a default instance and config dictionary for backward compatibility
default_loader = ConfigLoader()
config = default_loader.config


def load_config():
    """
    Legacy function to load config directly.
    """
    return default_loader.config


def is_local_only() -> bool:
    """
    Convenience function to check if the current configuration enforces local_only mode.
    """
    return default_loader.is_local_only()


def is_connected() -> bool:
    """
    Convenience function to check if the current configuration permits connected mode.
    """
    return default_loader.is_connected()


def main():
    """
    Main entry point for testing the ConfigLoader.
    """
    print(f"Loaded config: {config}")


if __name__ == "__main__":
    main()


def get_chroma_db_path():
    """
    Returns the absolute path to the ChromaDB directory.
    """
    db_path = default_loader.get("database", {}).get("chroma_db_path", ".chromadb")
    return os.path.abspath(os.path.join(default_loader.get_repo_root(), db_path))


def resolve_utility_llm(cfg=None):
    """
    Picks a fast, cheap LiteLLM model for internal utility calls (query
    expansion, content verification) based on whichever provider API key
    is configured. Returns a (model, api_key) tuple, or (None, None) if
    no provider key is available.
    """
    cfg = cfg or default_loader.config
    gemini_key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        return "gemini/gemini-1.5-flash", gemini_key

    anthropic_key = cfg.get("anthropic_api_key") or os.environ.get(
        "ANTHROPIC_API_KEY"
    )
    if anthropic_key:
        return "anthropic/claude-haiku-4-5-20251001", anthropic_key

    return None, None
