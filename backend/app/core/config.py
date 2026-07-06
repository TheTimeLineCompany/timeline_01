"""Runtime settings for Timeline."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment and optional `.env`."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Timeline API"
    environment: str = "local"

    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "timeline"
    pg_password: str = ""
    pg_database: str = "timeline"
    pg_schema: str = "timeline_v4"

    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "timeline"

    llm_base_url: str = "http://127.0.0.1:8101/v1"
    llm_api_key: str = ""
    llm_model: str = "your-vllm-model"
    llm_timeout_seconds: float = 60.0
    llm_hard_timeout_seconds: float = 180.0
    llm_keepalive: bool = False
    llm_guided_json: bool = True
    timeline_v4_llm_lane_enabled: bool = True
    llm_focus_concurrency: int = 3
    llm_sweep_concurrency: int = 2
    llm_max_inflight: int = 4
    llm_temporal_budget_per_article: int = 10
    llm_related_budget_per_article: int = 12
    llm_sections_per_temporal_call: int = 5
    llm_focus_topk_routes: int = 5
    llm_guided_json_enabled: bool = True
    worker_concurrency: int = 2
    job_backoff_base_seconds: int = 5

    parser_version: str = "v4-parser-0.2-media-blocks"
    model_version: str = "seed-only-0.1"
    embedding_dimensions: int = 384
    embedding_batch_size: int = 64
    embedding_torch_threads: int = 4
    embedding_torch_interop_threads: int = 1

    gliner2_enabled: bool = False
    gliner2_model: str = "fastino/gliner2-base-v1"
    gliner2_threshold: float = 0.45
    gliner2_max_chars: int = 2500
    gliner_decoder_enabled: bool = False
    gliner_decoder_model: str = "knowledgator/gliner-decoder-large-v1.0"
    gliner_decoder_threshold: float = 0.30
    gliner_decoder_max_chars: int = 2500
    gliner_decoder_section_limit: int = 3
    cpu_entity_torch_threads: int = 6
    cpu_entity_torch_interop_threads: int = 1

    graph_frontier_l1_limit: int = 0
    graph_frontier_l1_cache_limit: int = 0
    graph_frontier_l2_links_per_l1: int = 0
    graph_frontier_l2_source_scope: str = "intro_l1"
    graph_frontier_article_load_timeout_seconds: float = 30.0

    related_l1_limit: int = 12
    related_l2_per_l1_limit: int = 6
    related_rank_candidate_limit: int = 24
    related_return_limit: int = 8

    @property
    def pg_dsn(self) -> str:
        """PostgreSQL async SQLAlchemy DSN."""

        user = quote_plus(self.pg_user)
        password = quote_plus(self.pg_password)
        auth = user if not password else f"{user}:{password}"
        return f"postgresql+asyncpg://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"


@lru_cache
def get_settings() -> Settings:
    """Return cached settings."""

    return Settings()
