from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """re:action backend runtime settings.

    환경변수 키는 .env.example 참고.
    민감 값(LLM/OAuth/암호화 키)은 후속 이슈에서 채운다.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 앱 ──
    app_env: Literal["local", "dev", "staging", "prod"] = "local"
    app_name: str = "reaction-backend"
    app_version: str = "0.1.0"

    host: str = "0.0.0.0"
    port: int = 8000

    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # Vercel preview URL 등 패턴 매칭이 필요할 때 사용 (CORSMiddleware allow_origin_regex).
    # 예: ^https://reaction-frontend-.*\.vercel\.app$
    cors_allow_origin_regex: str | None = None

    # ── DB ──
    # 형식: postgresql://user:pass@host:port/db
    # SQLAlchemy 사용 시 코드가 자동으로 postgresql+asyncpg:// 로 변환.
    # 비어있으면 앱은 기동되지만 DB 호출 시점에 명확한 에러로 surface.
    database_url: str = ""
    db_echo: bool = False

    # ── LLM (Issue #5 / ADR-0003) ──
    # Gemini API key. 비어있으면 Tool Executor 가 항상 fallback 으로 분기.
    gemini_api_key: str = ""
    # 호출 모델. Flash-Lite 기본 (구 gemini-2.0-flash-exp 는 retired → 404).
    llm_model: str = "gemini-flash-lite-latest"
    # 단일 호출 timeout (초). ADR-0003 §1 동결값.
    llm_timeout_seconds: float = 8.0
    # 재시도 횟수 (지수 backoff). Tool Executor §1.
    llm_max_retries: int = 3
    # 일일 토큰 예산 (in + out 합산, 사용자당). 0 이면 무제한.
    llm_daily_token_budget: int = 200_000
    # 1K 입력/출력 토큰당 USD ¢. Flash 무료 티어 기본 0, 유료 환산용.
    llm_cost_per_1k_input_cents: float = 0.0
    llm_cost_per_1k_output_cents: float = 0.0

    # ── 보안 (Issue #5 §3) ──
    # 32-byte AES-GCM 키 (urlsafe base64 인코딩). 비어있으면 암호화 함수가 명시 에러.
    column_encryption_key: str = ""

    # ── Auth (Issue #16) ──
    # Google OAuth client (Google Cloud Console 발급). FE/BE가 같은 client_id를 공유.
    # 비어있으면 /auth/google 호출 시 명확한 503 에러로 surface (auth_stub_mode=False 일 때).
    google_oauth_client_id: str = ""
    # SPA + id_token 흐름에서는 BE가 사용하지 않음. server-side code flow 대비 자리만 둠.
    google_oauth_client_secret: str = ""

    # JWT — HS256. JWT_SECRET 은 32+ bytes 권장 (python -c "import secrets; print(secrets.token_hex(32))").
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_minutes: int = 60
    jwt_refresh_token_ttl_days: int = 14

    # Local 개발에서 Google id_token 검증을 우회하고 고정 demo user 를 발급한다.
    # staging/prod 는 반드시 False. True 일 때 GOOGLE_OAUTH_CLIENT_ID 가 비어도 부팅 가능.
    auth_stub_mode: bool = False

    # ── Scheduler (#24) ──
    # True 면 앱 기동 시 in-process APScheduler 로 cron job 을 등록한다.
    # 기본 False — 테스트/로컬은 안 돈다(데모는 시드로 커버). ⚠️ in-process 라
    # 다중 인스턴스 배포 시 중복 실행(모든 job idempotent → 안전하나 단일 인스턴스 권장).
    scheduler_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
