"""Application configuration — all values come from env vars (never hardcoded).

Rules:
- Every env var is documented in .env.example.
- Secrets (tokens, signing secrets, API keys) are never logged.
- LLM_PROVIDER switch needs no code change (rules.md §7 rule 3).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("heatguard.config")

# ---------------------------------------------------------------------------
# Default allow-list — only read tools; execute_operations is NEVER included.
# ---------------------------------------------------------------------------
_DEFAULT_ALLOWLIST = (
    "get_available_devices,"
    "list_parameters,"
    "get_telemetry_data,"
    "get_device_alarms,"
    "list_operations,"
    "get_sensor_history"
)

_WRITE_TOOL = "execute_operations"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- MCP Gateway -------------------------------------------------------
    mcp_server_url: str = Field(
        default="http://localhost:8001/mcp",
        description="Streamable-HTTP endpoint of the Device Gateway MCP server.",
    )
    mcp_auth_token: SecretStr = Field(
        default=SecretStr("dev-mcp-token-secret"),
        description="Bearer token for the MCP gateway (never logged).",
    )

    # ---- Approval / security -----------------------------------------------
    approval_signing_secret: SecretStr = Field(
        default=SecretStr("dev-approval-signing-secret-key-32chars"),
        description="HMAC-SHA256 signing secret shared with the MCP server.",
    )
    approval_ttl_seconds: int = Field(default=300, description="Plan approval TTL in seconds.")
    token_max_age_s: int = Field(default=600, description="Max age for individual approval tokens.")

    # ---- LLM provider ------------------------------------------------------
    llm_provider: Literal["ollama", "openai", "anthropic"] = Field(
        default="ollama",
        description="Active LLM provider. Switch without code changes.",
    )

    # Ollama
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.2")

    # OpenAI
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_model: str = Field(default="gpt-4o-mini")

    # Anthropic
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    anthropic_model: str = Field(default="claude-3-5-haiku-latest")

    # ---- Tool allow-list ---------------------------------------------------
    llm_tool_allowlist_str: str = Field(
        default=_DEFAULT_ALLOWLIST,
        alias="LLM_TOOL_ALLOWLIST",
        description=(
            "Comma-separated list of MCP tool names the LLM is allowed to call. "
            "execute_operations is forcibly excluded even if listed here."
        ),
    )

    # ---- Schema pinning ----------------------------------------------------
    mcp_schema_pins_file: str = Field(
        default="app/schema_pins.json",
        description="Path to the stored tool-schema hashes for pinning.",
    )
    mcp_schema_pin_strict: bool = Field(
        default=True,
        description="Refuse startup if pinned schemas differ from server. Set false only in dev.",
    )

    # ---- Database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/heatguard",
        description="Async PostgreSQL connection string for LangGraph checkpointer.",
    )

    # ---- Observability -----------------------------------------------------
    log_level: str = Field(default="INFO")

    # ---- Agent limits (rules.md §3) ----------------------------------------
    agent_max_tool_iterations: int = Field(default=6)
    agent_tool_timeout_s: float = Field(default=15.0)
    agent_request_timeout_s: float = Field(default=60.0)

    # ---- LLM temperature ---------------------------------------------------
    llm_temperature: float = Field(default=0.0, description="Temperature for router/parser calls.")

    # ---- Computed ----------------------------------------------------------
    def __init__(self, **data: Any) -> None:
        if "llm_tool_allowlist" in data and "llm_tool_allowlist_str" not in data:
            val = data.pop("llm_tool_allowlist")
            if isinstance(val, (list, tuple, set)):
                data["llm_tool_allowlist_str"] = ",".join(str(x) for x in val)
            else:
                data["llm_tool_allowlist_str"] = str(val)
        super().__init__(**data)

    @field_validator("llm_tool_allowlist_str", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> str:
        if isinstance(v, (list, tuple, set)):
            return ",".join(str(x).strip() for x in v)
        return str(v).strip()

    @property
    def llm_tool_allowlist(self) -> list[str]:
        """Parsed allow-list, with execute_operations forcibly removed."""
        raw = [t.strip() for t in self.llm_tool_allowlist_str.split(",") if t.strip()]
        # Hard safety rule: write tool is NEVER in the LLM allow-list.
        filtered = [t for t in raw if t != _WRITE_TOOL]
        if _WRITE_TOOL in raw:
            logger.error(
                "SAFETY: '%s' was found in LLM_TOOL_ALLOWLIST and has been removed. "
                "The write tool must never be exposed to the LLM.",
                _WRITE_TOOL,
            )
        return filtered


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings instance (cached)."""
    return Settings()
