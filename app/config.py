from functools import lru_cache
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]

# 占位符前缀（来自 .env.example 模板，未填真实值）：视为未配置，避免「假 ok」
_PLACEHOLDER_PREFIXES = ("your_", "xxx", "changeme", "placeholder", "<", "{", "「", "【")


def _normalize_secret(value: object) -> str:
    """API Key 归一化：去首尾空白；占位符/空白视为未配置（空字符串）。"""
    if value is None:
        return ""
    v = str(value).strip()
    if not v or v.lower().startswith(_PLACEHOLDER_PREFIXES):
        return ""
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", ROOT_DIR.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    zhihu_api_key: str = Field(default="", alias="ZHIHU_API_KEY")
    bocha_api_key: str = Field(default="", alias="BOCHA_API_KEY")
    # 默认搜索来源：auto=bocha 优先，未配置则回退 tavily/zhihu；tavily=仅 Tavily；bocha=仅博查；zhihu=仅知乎全网搜索
    search_provider: str = Field(default="auto", alias="SEARCH_PROVIDER")
    frontend_origin: str = Field(default="http://localhost:5173", alias="FRONTEND_ORIGIN")
    # 版本号：生产环境为 vX.Y.Z（与 Git 标签一致），测试/预发可带后缀 vX.Y.Z-beta.N
    app_version: str = Field(default="v1.0.0", alias="APP_VERSION")
    # 环境：显式指定 test/staging/prod；默认 auto 按版本号后缀推断（beta→test, rc→staging, 其余→prod）
    app_env: str = Field(default="auto", alias="APP_ENV")

    @field_validator("llm_api_key", "tavily_api_key", "zhihu_api_key", "bocha_api_key", mode="before")
    @classmethod
    def _clean_api_keys(cls, v: object) -> str:
        return _normalize_secret(v)

    @property
    def effective_env(self) -> str:
        """环境判定：APP_ENV 显式指定优先；否则按版本号后缀推断（beta→test, rc/staging 前缀→staging, 其余→prod）。"""
        if self.app_env in ("test", "staging", "prod"):
            return self.app_env
        v = self.app_version.lower()
        if "beta" in v:
            return "test"
        if "rc" in v or "pre" in v or "dev" in v or v.startswith("staging"):
            return "staging"
        return "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
