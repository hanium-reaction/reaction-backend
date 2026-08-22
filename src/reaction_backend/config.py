from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 모델별 공개 단가 — (입력 USD/1M, 출력 USD/1M). 2026-07-30 기준.
#
# 왜 표가 필요한가: 요율을 하나만 두면 틀린다. 지금 쓰는 두 모델의 출력 단가가 **3.6배**
# 차이나기 때문이다(lite $2.50 vs flash $9.00). 하나로 뭉개면 어느 모듈이 돈을 쓰는지
# 영영 알 수 없다 — 실제로 그래서 "비용이 이상한데" 를 쿼리로 답하지 못했다.
#
# **사고(thinking) 토큰은 출력 단가로 과금된다.** `tokens_out` 이 이미 사고분을 포함하므로
# (provider `_extract_usage`) 여기서 따로 더하지 않는다.
#
# 새 모델을 도입하면 이 표에 넣어야 한다 — 빠뜨리면 `test_every_configured_model_has_pricing`
# 이 CI 에서 잡는다. 단가는 공개 가격표 기준이라 실제 청구서(할인·배치·캐시)와는 다를 수 있다.
LLM_PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-2.5-flash": (0.15, 1.25),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}


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
    # 호출 모델(base). 인터뷰/인박스/brief 등 지연 민감·경량 태스크가 이 모델을 쓴다.
    #
    # ⚠️ **`-latest` alias 를 쓰지 말 것.** 예전엔 퇴역(404) 회피용으로 alias 를 썼는데,
    # alias 는 Google 이 가리키는 대상을 말없이 바꾼다 — 실제로 `gemini-flash-latest` 가
    # `gemini-3.6-flash` 로 올라가 있었고, 코드 변경 0건으로 모델·단가·기본 thinking 정책이
    # 전부 바뀌었다. 자체 기록에는 요청한 alias 이름만 남아 청구서와 대조도 안 됐다.
    # 모델 교체는 **의도한 변경**이어야 하므로 고정 버전만 쓴다. 퇴역은 배포 전에 알아채는
    # 문제(호출 실패)지만, 조용한 alias 이동은 알아챌 방법이 없다.
    llm_model: str = "gemini-3.5-flash-lite"
    # task 별 모델 오버라이드. 빈 문자열이면 llm_model 로 폴백 (매핑은 `model_for_module`).
    #
    # planning 이 lite 인 이유(실측 2026-08-02, 동일 goal_decompose 프롬프트 3회씩):
    #
    #     모델                      세션수  세션길이  중복  출력   지연    회당
    #     gemini-3.5-flash          16 ✅   120 ✅    0   2,896  9.9s  $0.0290
    #     gemini-3.5-flash-lite     16 ✅   120 ✅    0   2,698  7.4s  $0.0073
    #
    # lite 가 4배 싸면서 **더 빠르고**, 분해 계약(총 세션 수·사용자가 말한 세션 길이)을 똑같이
    # 지킨다. 내용도 비교했고 `plan_quality` v2 검토가 양쪽 다 3/3 승인했다. 지연이 줄어
    # 타임아웃(45초) 여유도 커진다.
    #
    # recovery 도 lite 로 내려 **전 모듈을 한 모델로 통일**했다 (2026-08-03).
    #
    # ⚠️ 이 값은 원래 상위 flash 였고, 그 근거는 "if-then 코핑 문장 자체가 산출물이라 품질
    # 민감도가 다르다 — 평가 없이 내리지 않는다" 였다. **그 평가는 아직 하지 않았다.**
    # 통일은 운영 결정으로 내려온 것이지 실측으로 뒷받침된 게 아니다. 회복 카드 문구 품질이
    # 눈에 띄게 나빠지면 이 줄만 `gemini-3.5-flash` 로 되돌리면 된다(다른 변경 불필요).
    #
    # 비용 측면에서는 이 줄이 이번 통일의 실질 이득이다 — 출력 단가가 $9.00 → $2.50/1M 로
    # 3.6배 내려간다. interview/inbox/brief/planning 은 이미 lite 라 변화가 없다.
    #
    # 통일 대상으로 `gemini-2.5-flash`(단가가 정확히 절반)가 아니라 lite 를 고른 이유:
    # 2.5-flash 는 이 레포의 작업으로 품질을 **한 번도 측정한 적이 없다**. 반면 lite 는
    # 계획 분해 계약을 지키는 것이 실측으로 확인됐다(위 표). 절약액은 호출당 0.03센트 수준
    # (인터뷰 1,095회 ≈ $0.3)인데, 분해가 계약을 어기면 계획 전 구간이 룰 폴백 자리표시자로
    # 떨어진다. 그 리스크를 살 만한 금액이 아니다.
    #
    # 빈 문자열로 두지 않고 **명시한다.** 빈 문자열이면 base 를 따라가므로, 나중에 누가 base 를
    # 상위 모델로 올리면 계획·회복도 같이 올라가 비용이 조용히 는다.
    llm_model_planning: str = "gemini-3.5-flash-lite"
    llm_model_recovery: str = "gemini-3.5-flash-lite"
    # 검색 그라운딩 전용 모델 (#259 §4.2 ⑧). 다른 모듈과 **따로** 고정한다.
    #
    # 여기서 모델을 가르는 기준은 품질이 아니라 **지연**이다. 실측(#259 §2): lite 는 중앙
    # 8.5s(6.8~9.2) 에 출처 6~11 개, flash 는 23~80s. 그라운딩은 계획 생성 앞단의 별도
    # 단계라(⑦) 사용자가 화면 앞에서 기다린다 — flash 의 80s 는 그냥 못 쓴다.
    #
    # `model_for_module("planning")` 을 재사용하지 않는 이유: 지금은 우연히 둘 다 lite 지만,
    # 분해 품질 때문에 planning 을 상위 모델로 올리는 판단은 언제든 나올 수 있고 그때
    # 그라운딩까지 따라 올라가면 **이 단계가 조용히 못 쓰게 된다.** 두 결정은 근거가 다르므로
    # 값도 분리해 둔다.
    llm_model_grounding: str = "gemini-3.5-flash-lite"
    # 단일 호출 timeout (초). ADR-0003 §1 동결값.
    llm_timeout_seconds: float = 8.0
    # 그라운딩 호출 timeout (초). 동결된 8.0 은 **중앙값(8.5s)보다 짧아** 절반이 타임아웃난다.
    # 상한(9.2s)에 여유를 얹어 20s. 계획 분해(45s, #179) 안에 인라인하지 않는 전제(⑦)라
    # 이 지연은 이 단계 안에서만 산다.
    llm_grounding_timeout_seconds: float = 20.0
    # 재시도 횟수 (지수 backoff). Tool Executor §1.
    llm_max_retries: int = 3
    # 계획(First Plan) 분해·검토 전용 — 인터뷰 턴은 지연 민감(#76)이라 thinking 0 을 유지하되,
    # 분해(goal_decompose)·검토(plan_quality)는 진짜 추론이 필요해 modest thinking 을 허용한다.
    # gemini-2.5-flash 기준 thinking_budget(토큰). 0 이면 비활성(인터뷰와 동일). 로컬 튜닝용 env.
    llm_planning_thinking_budget: int = 2048
    # thinking 이 붙어 단일 호출이 길어지므로 계획 호출 timeout 은 별도로 상향(초).
    #
    # 45초인 이유(실측): 분해 프롬프트가 마감까지의 세션을 **한 번에** 만들도록 바뀌면서
    # (#coverage) 호출당 지연이 평균 8.7초에서 20~30초대로 올라갔다. 20초로 두면 성공 사례가
    # 17~19.7초에 밀집해 벽에 붙고, Gemini 가 조금만 느리면 재시도 3회가 전부 타임아웃해
    # **룰 폴백**으로 떨어진다 — 그러면 계획 전 구간이 "N회차" 자리표시자가 된다.
    # 45초에서 같은 입력 3회 모두 LLM 분해 성공(25.4 / 30.6 / 35.1초).
    #
    # 트레이드오프: 진짜 실패 시 llm_max_retries(3) × 45초 = 최대 135초를 기다린 뒤 폴백한다.
    # 계획 생성은 온보딩의 일회성 동작이고, 그 대기의 대안이 '자리표시자로 채워진 계획'이라
    # 기다리는 쪽을 택했다.
    #
    # 만다라 Stage A/B 도 이 timeout 을 공유한다(궁극목표 만다라트, #6-B) — 배포 전 실측
    # 필수 항목이었다(전략 문서 §11 항목 3). `scripts/measure_mandala_stage_b_latency.py`
    # 로 같은 입력 3회 실측(2026-08-21):
    #
    #     단계                         출력토큰(3회)          지연(3회)
    #     Stage A(mandala_subgoals)    245 / 1,754 / 1,518   3.0 / 5.7 / 5.6초
    #     Stage B(mandala_cells)       2,549 / 2,740 / 1,391 7.6 / 8.3 / 5.1초
    #
    # Stage B(64칸, 가장 큰 출력)도 최악 8.3초로 45초 상한에 크게 못 미친다 — goal_decompose
    # 가 겪은 "20초 벽" 패턴이 재현되지 않았다. A′(축 4개씩 2콜로 쪼개기) + 202 폴링 전환은
    # 불필요 — §11 항목 3 은 이 실측으로 닫혔다.
    llm_planning_timeout_seconds: float = 45.0
    # 일일 토큰 예산 (in + out 합산, 사용자당). 0 이면 무제한.
    llm_daily_token_budget: int = 200_000
    # 사용자별 **일일 검색 그라운딩 요청** 상한 (#259 §3). 0 이면 무제한(토큰 예산과 동일 관례).
    #
    # 5 인 이유: 그라운딩은 사용자가 버튼을 눌러야 도는 명시적 행위이고(#259 §4.1 ③ 결정),
    # 목표 하나에 한 번이면 충분하다. 재시도·다른 목표까지 감안해 5 로 둔다. 이 값은
    # **폭주 차단선**이지 정상 사용의 상한이 아니다 — 정상 사용자가 여기 닿으면 값이 아니라
    # 트리거 설계를 다시 봐야 한다.
    llm_daily_grounding_budget: int = 5
    # 1K 입력/출력 토큰당 USD ¢. Flash 무료 티어 기본 0, 유료 환산용.
    # 모델을 모르는 호출의 폴백 요율(1K 토큰당 센트). 보통은 `LLM_PRICING_USD_PER_1M` 이
    # 이긴다 — 이 둘은 표에 없는 모델을 만났을 때만 쓰인다.
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

    # ── Web Push VAPID (#16/#20) ──
    # 비어있으면 발송이 unconfigured 로 조용히 skip (GEMINI_API_KEY 부재와 같은 degrade).
    # 라이브 키는 provision-vapid.yml 워크플로가 EC2 .env 에 생성 — private key 는 레포·
    # 로그 어디에도 남기지 않는다. public key 는 FE pushManager.subscribe 에 필요(공개값).
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    # VAPID 스펙상 연락처 URI (mailto: 또는 https:). 푸시 서비스가 문제 시 연락하는 주소.
    vapid_subject: str = "mailto:j07801@hanyang.ac.kr"

    def model_for_module(self, module: str) -> str:
        """llm_runs.module → 사용할 Gemini 모델.

        계획(planning)·회복(recovery)만 전용 오버라이드(`llm_model_planning`/
        `llm_model_recovery`)를 가지고, interview/inbox/brief 는 base(`llm_model`)를 쓴다.
        오버라이드가 빈 문자열이면 base 로 폴백한다.

        2026-08-03 현재 세 값이 모두 `gemini-3.5-flash-lite` 라 **결과적으로 전 모듈이 같은
        모델**이다. 그래도 분기를 남겨 두는 이유: 회복은 실측 없이 내린 값이라 되돌릴 수 있어야
        하고(그때 이 분기가 유일한 손잡이다), 계획은 base 가 올라가도 딸려 올라가지 않아야
        한다. 값이 같다고 오버라이드를 지우면 그 두 성질이 사라진다.
        """
        override = {
            "planning": self.llm_model_planning,
            "recovery": self.llm_model_recovery,
        }.get(module, "")
        return override or self.llm_model

    @model_validator(mode="after")
    def _forbid_auth_stub_in_deployed_envs(self) -> Self:
        """staging/prod 에서 AUTH_STUB_MODE=true 를 부팅 시점에 차단한다 (Issue #94).

        stub 모드는 Google id_token 서명 검증을 통째로 건너뛰고 고정 demo 클레임
        (`demo@reaction.local`)을 반환한다 — 즉 **인증 우회**. 배포 환경(staging/prod)에
        이 값이 켜지면 서로 다른 Google 계정이 전부 한 유저로 붕괴한다 (#94 증상).
        설정 실수를 조용히 통과시키지 않고 앱 기동을 실패시켜(loud fail) 잘못된 배포가
        헬스체크를 통과하지 못하게 한다. local/dev 는 기존대로 stub 허용.
        """
        if self.auth_stub_mode and self.app_env in ("staging", "prod"):
            raise ValueError(
                f"AUTH_STUB_MODE=true is forbidden when APP_ENV='{self.app_env}'. "
                "Stub mode bypasses Google id_token verification and collapses every "
                "login into the demo user. Set AUTH_STUB_MODE=false and configure "
                "GOOGLE_OAUTH_CLIENT_ID for staging/prod."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
