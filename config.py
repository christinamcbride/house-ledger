"""Settings, paths, and the model lock.

The project requirements are specific about where the key lives: the app must
look for `.env` **in the repo folder first, then one level above it**. The
teaching staff's `.env` sits in the parent directory and defines
`PORTKEY_API_KEY` and `OPENAI_API_KEY`, and those exact names are required.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = ROOT / "output"
PROMPTS = ROOT / "prompts"
STATIC = ROOT / "static"

HOUSE_AGREEMENT = DATA / "house_agreement.md"
PRICES = DATA / "prices.json"
LEDGER_DB = DATA / "house.db"

AUDIT_TRAIL = OUTPUT / "audit_trail.json"
USAGE_LOG = OUTPUT / "usage_log.json"
BLOCKED_ACTIONS = OUTPUT / "blocked_actions.json"

#: The course key allows this model only.
MODEL_NAME = "gpt-6-luna"


class AgentUnavailable(RuntimeError):
    """No API key, so no agent can run."""


class Settings(BaseSettings):
    # Repo folder first, then one level above — later entries win in
    # pydantic-settings, so the repo's own .env is listed last.
    model_config = SettingsConfigDict(
        env_file=(ROOT.parent / ".env", ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    portkey_api_key: str = ""
    openai_api_key: str = ""
    ai_base_url: str = "https://api.portkey.ai/v1"

    @property
    def configured(self) -> bool:
        return bool(self.portkey_api_key.strip() or self.openai_api_key.strip())

    @property
    def route(self) -> tuple[str, str]:
        """Which key to use, and where to send it.

        The course gateway comes first. A plain OpenAI key is accepted as a
        fallback so the project runs for someone who has one and not the
        other, but the model is the same either way — see MODEL_NAME.
        """
        if self.portkey_api_key.strip():
            return self.portkey_api_key.strip(), self.ai_base_url
        return self.openai_api_key.strip(), "https://api.openai.com/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def build_model(model_name: str = MODEL_NAME):
    if model_name != MODEL_NAME:
        raise ValueError(f"This project runs on {MODEL_NAME} only.")
    s = get_settings()
    if not s.configured:
        raise AgentUnavailable(
            "No API key. Set PORTKEY_API_KEY (or OPENAI_API_KEY) in .env in this "
            "folder, or one level above it. See .env.example."
        )
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
    from pydantic_ai.providers.openai import OpenAIProvider

    key, base_url = s.route
    return OpenAIChatModel(
        MODEL_NAME,
        provider=OpenAIProvider(base_url=base_url, api_key=key),
        # The gateway rejects function tools unless reasoning is off.
        settings=OpenAIChatModelSettings(openai_reasoning_effort="none"),
    )
