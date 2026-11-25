"""项目三全局配置管理（自动复用系统 DEEPSEEK_API_KEY 与项目一 Langfuse 配置）。"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    vm_host: str = "192.168.150.130"
    postgres_dsn: str = "postgresql://rag_user:rag_password_change_me@192.168.150.130:5432/ecom_rag"
    redis_url: str = "redis://192.168.150.130:6379/1"
    selection_schema: str = "ecom_selection"

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    langfuse_enabled: bool = True
    langfuse_host: str = "http://192.168.150.130:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    max_concurrent_tasks: int = 3
    min_comparable_competitors: int = 3
    min_independent_reviews: int = 4
    max_audit_rewrites: int = 1

    @model_validator(mode="after")
    def _resolve_keys(self) -> "Settings":
        if not self.llm_api_key or "please-replace" in self.llm_api_key:
            self.llm_api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        if not self.langfuse_public_key or not self.langfuse_secret_key:
            sibling_env = Path(__file__).resolve().parents[3] / "ecom-agentic-rag" / ".env"
            if sibling_env.is_file():
                vals = dotenv_values(sibling_env)
                if not self.langfuse_public_key and vals.get("LANGFUSE_PUBLIC_KEY"):
                    self.langfuse_public_key = str(vals["LANGFUSE_PUBLIC_KEY"]).strip()
                if not self.langfuse_secret_key and vals.get("LANGFUSE_SECRET_KEY"):
                    self.langfuse_secret_key = str(vals["LANGFUSE_SECRET_KEY"]).strip()
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
