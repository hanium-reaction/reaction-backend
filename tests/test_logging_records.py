"""로깅 `extra=` 가 LogRecord 예약 속성과 충돌하지 않는다 (운영 크래시 방지).

배경: `logging` 은 `extra` 의 키가 LogRecord 내장 속성과 겹치면 **KeyError 를 던지고, 그
예외는 로그 호출부로 전파된다**(핸들러 오류처럼 삼켜지지 않는다). 즉 `extra={"module": ...}`
한 줄이 그 코드 경로 전체를 죽인다.

이게 오래 숨어 있던 이유: 앱에 로깅 설정이 없어 root 가 WARNING 이었고, `_log.info()` 는
레코드를 만들기 **전에** 반환했다. 그래서 INFO 를 켜는 순간(= 운영 로그를 보려는 순간)
`llm_run_recorded` 가 터졌고, sweeps 의 per-user try/except 가 그걸 삼켜
"모닝 브리프가 전 사용자에게 조용히 실패" 하는 형태로 나타났다.

그래서 여기서는 **실제로 INFO 레코드를 만들어 본다** — 예약어를 쓰면 즉시 KeyError.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

# LogRecord 가 자체적으로 채우는 속성 — extra 로 덮으면 KeyError.
_RESERVED = ("module", "name", "msg", "args", "levelname", "filename", "lineno", "message")


def _emit(extra: dict[str, Any]) -> None:
    """INFO 가 켜진 로거로 실제 레코드를 만든다 — 예약어 충돌이면 여기서 KeyError."""
    logger = logging.getLogger("reaction_backend._logging_contract_probe")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.NullHandler())
    logger.info("probe", extra=extra)


@pytest.mark.parametrize("reserved", _RESERVED)
def test_reserved_extra_key_raises(reserved: str) -> None:
    """전제 확인 — 예약어를 쓰면 정말 터진다(테스트 자체가 공허하지 않음을 보장)."""
    with pytest.raises(KeyError):
        _emit({reserved: "x"})


async def test_record_llm_run_logging_survives_info_level() -> None:
    """`record` 의 로그가 INFO 에서 살아남는다 — 예약어 `module` 회귀 방지.

    실제 함수를 태운다. `extra={"module": ...}` 로 되돌리면 KeyError 로 즉시 실패한다.
    """
    from reaction_backend.safety.llm_budget import LlmRunRecord, record

    class _Session:
        def add(self, obj: object) -> None:
            return None

        async def flush(self) -> None:
            return None

    logging.getLogger("reaction_backend.safety.llm_budget").setLevel(logging.INFO)

    # 예외 없이 통과하면 계약 충족 (반환 id 는 관심사가 아니다).
    await record(
        _Session(),  # type: ignore[arg-type]
        LlmRunRecord(
            module="recovery",
            model="gemini-flash-latest",
            prompt_id="recovery/if_then_proposal",
            prompt_version="2",
            tokens_in=10,
            tokens_out=5,
            latency_ms=100,
            success=True,
            fell_back=False,
            cost_cents=0,
        ),
    )


def test_app_configures_logging_so_operational_info_is_visible() -> None:
    """`create_app()` 이 root 에 핸들러를 붙인다 — 없으면 운영 INFO 로그가 통째로 사라진다.

    "APScheduler started (N jobs)"(cron 기동 확인)와 "expire_unreflected: N cards"(만료
    건수, 사고 시 원복 범위)가 사후 진단의 유일한 수단인데, 로깅 설정이 없으면 둘 다
    lastResort(WARNING)에 걸려 안 남는다.
    """
    from reaction_backend.main import create_app

    create_app()

    assert logging.getLogger().handlers, "root 핸들러가 없다 — 앱 INFO 로그가 어디에도 안 남는다"
    assert logging.getLogger("reaction_backend").isEnabledFor(logging.INFO)
