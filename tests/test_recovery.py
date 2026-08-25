"""Recovery — #20-A 수직 슬라이스 (api-contract §12).

`GEMINI_API_KEY` 가 빈 상태이므로 `aiClient.run` 은 자동으로 룰 fallback 분기
→ 카드 문구는 카탈로그 템플릿, `aiSource="rule"`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.orchestrator.recovery import (
    RECOVERY_NIGHT_CUTOFF_HOUR,
    re_engagement_anchor_at,
    recovery_target_date,
    recovery_unit_minutes,
    render_template,
    select_strategies,
    shift_to_recovery_day,
)
from reaction_backend.schemas.common import KST
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeRecoveryRepo,
    default_recovery_strategies,
)


def _seed_failed_execution(
    recovery_repo: FakeRecoveryRepo,
    action_repo: FakeActionItemRepo,
    *,
    completion_status: str = "failed",
    failure_tags: list[str] | None = None,
    title: str = "GROUP BY 실습",
    target_date: date = date(2026, 6, 5),
    plan_start_at: datetime | None = None,
) -> str:
    """실패한 실행 1건 시드 → `exec_<uuid>` ID 반환.

    `target_date`/`plan_start_at` 기본값은 서로 수십 일 떨어져 있어 day_delta 가 커진다
    (= 시프트 결과가 미래). #174 의 과거 슬롯을 재현하려면 **둘을 같은 날로** 넘길 것.
    """
    action = _seed_action(action_repo, title=title, target_date=target_date)
    execution = recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status=completion_status,
        failure_tags=failure_tags or ["AMBIGUITY"],
        plan_start_at=plan_start_at,
    )
    return f"exec_{execution.id}"


def _seed_action(
    action_repo: FakeActionItemRepo, *, title: str, target_date: date = date(2026, 6, 5)
) -> Any:
    from reaction_backend.db.models.action_item import ActionItem

    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = title
    a.target_date = target_date
    a.category = "study"
    a.source = "manual"
    a.status = "failed"
    a.priority = 3
    a.estimated_minutes = 60
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.archived_at = None
    action_repo.seed(a)
    return a


def _generate(client: TestClient, execution_id: str) -> Any:
    return client.post(
        "/recovery/proposals/generate",
        json={"executionId": execution_id},
    )


def _decide(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(
        "/recovery/decisions",
        json=body,
        headers={"Idempotency-Key": f"test-{uuid4()}"},
    )


# ───────────────────────── proposals/generate ─────────────────────────


def test_generate_returns_2_to_4_cards(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "CONFLICT"]
    )
    resp = _generate(client, exec_id)
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["executionId"] == exec_id
    assert 2 <= len(body["cards"]) <= 4
    # Draft Layer 강제 (ADR-0005 §7.2)
    assert body["isDraft"] is True
    # LLM 키 없음 → 룰 fallback
    assert body["aiSource"] == "rule"


def test_generate_max_one_card_per_group(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    # FATIGUE 는 DOWNSCOPE_DEFAULT 와 ACTIVE_RECOVERY 둘 다 트리거 — 그룹별 1장 보장 확인
    exec_id = _seed_failed_execution(
        fake_recovery_repo,
        fake_action_item_repo,
        failure_tags=["FATIGUE", "PLAN_TOO_BIG", "LOW_ENERGY"],
    )
    body = _generate(client, exec_id).json()
    groups = [c["optionGroup"] for c in body["cards"]]
    assert len(groups) == len(set(groups)), f"같은 그룹 중복 노출: {groups}"


def test_generate_ambiguity_maps_to_nano_step(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    body = _generate(client, exec_id).json()
    top = body["cards"][0]
    assert top["strategyType"] == "NANO_STEP"
    assert top["optionGroup"] == "DOWNSCOPE"
    assert top["triggerTag"] == "AMBIGUITY"
    # 템플릿 변수 {first_step} 치환 — 원본 카드 제목이 문구에 포함
    assert "GROUP BY 실습" in top["suggestedActionText"]


def test_generate_applies_llm_personalized_text_to_leading_card(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    monkeypatch: Any,
) -> None:
    """LLM 성공 시 선두 카드 문구가 personalize 된다.

    회귀: 과거엔 `LLM.strategy_code ∈ 선택 전략키`일 때만 적용했는데, LLM 은 generic 코드
    ("downscope")를 반환하고 선택키는 strategy_type("NANO_STEP")이라 항상 불일치 →
    Gemini 문구가 통째로 폐기되고 카탈로그 템플릿만 노출됐다. 이제 선두 카드에 직접 적용한다."""
    from reaction_backend.llm import RunResult, aiClient
    from reaction_backend.schemas.recovery import RecoveryProposalLLM

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        return RunResult(
            value=RecoveryProposalLLM(
                strategy_code="downscope",  # generic — 선택키 NANO_STEP 과 불일치해도 적용돼야
                if_clause="오늘 GROUP BY 5문제가 버겁게 느껴지면",
                then_clause="핵심 2문제만 골라 풀고 나머지는 내일 이어가요",
                rationale="부담을 낮춰 시작을 쉽게",
                estimated_workload_change_minutes=-15,
            ),
            fell_back=False,
            reason=None,
            prompt_id="recovery/if_then_proposal",
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    body = _generate(client, exec_id).json()
    top = body["cards"][0]
    assert top["strategyType"] == "NANO_STEP"  # 룰이 고른 선두 전략
    assert (
        top["suggestedActionText"]
        == "오늘 GROUP BY 5문제가 버겁게 느껴지면 핵심 2문제만 골라 풀고 나머지는 내일 이어가요"
    )
    assert body["aiSource"] == "llm"


def test_generate_avoidance_tag_routes_to_v3_and_fills_coping_plan_on_leading_card_only(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    monkeypatch: Any,
) -> None:
    """AVOIDANCE 태그 → v3(코핑 플랜 + acknowledgment) 라우팅 (acknowledgment/v3 승격, AVOIDANCE 전용).

    v3 는 v2 와 입력 변수 계약이 같으므로 갈리는 건 `prompt_id`/`schema` 뿐 — 그 둘을
    실제로 호출부가 넘겼는지 캡처해서 확인한다. 코핑 플랜은 실제로 personalize 된
    선두 카드에만 실려야 한다 — 형제 카드는 카탈로그 템플릿 그대로라 붙이면 내용이
    어긋난다(`db/models/recovery_attempt.py` 모듈 주석의 근거).
    """
    from reaction_backend.llm import RunResult, aiClient
    from reaction_backend.schemas.recovery import RecoveryProposalLLMv3

    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        captured["prompt_id"] = kwargs["prompt_id"]
        captured["schema"] = kwargs["schema"]
        return RunResult(
            value=RecoveryProposalLLMv3(
                strategy_code="downscope",
                if_clause="책상 앞에 앉으면",
                then_clause="오늘 배울 표현 하나만 소리 내어 3번 읽어봐요",
                rationale="시작하는 마음 자체가 무거웠던 것 같아요.",
                obstacle="소리 내어 읽는 게 괜히 부담스러울 수 있어요",
                coping_clause="그마저 부담스러우면 오늘은 표현을 눈으로만 한 번 읽어봐요",
                acknowledgment="누구나 시작이 막막할 때가 있어요",
                estimated_workload_change_minutes=-20,
            ),
            fell_back=False,
            reason=None,
            prompt_id="recovery/if_then_proposal",
            prompt_version="v3",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    exec_id = _seed_failed_execution(
        fake_recovery_repo,
        fake_action_item_repo,
        failure_tags=["AVOIDANCE", "HARD_TO_START"],
    )
    body = _generate(client, exec_id).json()

    assert captured["prompt_id"] == "recovery/if_then_proposal@v3"
    assert captured["schema"] is RecoveryProposalLLMv3

    top = body["cards"][0]
    assert top["obstacle"] == "소리 내어 읽는 게 괜히 부담스러울 수 있어요"
    assert top["copingClause"] == "그마저 부담스러우면 오늘은 표현을 눈으로만 한 번 읽어봐요"
    assert top["acknowledgment"] == "누구나 시작이 막막할 때가 있어요"
    assert body["aiSource"] == "llm"

    assert len(body["cards"]) >= 2, "최소 2장 보장 — 형제 카드가 있어야 이 단언이 의미 있다"
    for sibling in body["cards"][1:]:
        assert sibling["obstacle"] is None
        assert sibling["copingClause"] is None
        assert sibling["acknowledgment"] is None


def test_generate_non_avoidance_tag_stays_on_v2_without_coping_plan(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    monkeypatch: Any,
) -> None:
    """AVOIDANCE 가 없으면 v3 로 새지 않는다 — 여전히 v2, 코핑 플랜 필드는 전부 null."""
    from reaction_backend.llm import RunResult, aiClient
    from reaction_backend.schemas.recovery import RecoveryProposalLLM

    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        captured["prompt_id"] = kwargs["prompt_id"]
        captured["schema"] = kwargs["schema"]
        return RunResult(
            value=RecoveryProposalLLM(
                strategy_code="downscope",
                if_clause="책상에 앉으면",
                then_clause="핵심 2문제만 풀어봐요",
                rationale="",
            ),
            fell_back=False,
            reason=None,
            prompt_id="recovery/if_then_proposal",
            prompt_version="v2",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["HARD_TO_START"]
    )
    body = _generate(client, exec_id).json()

    assert captured["prompt_id"] == "recovery/if_then_proposal@v2"
    assert captured["schema"] is RecoveryProposalLLM

    for card in body["cards"]:
        assert card["obstacle"] is None
        assert card["copingClause"] is None
        assert card["acknowledgment"] is None


def test_generate_forces_environment_shift_lead_and_skips_llm_at_l2(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    monkeypatch: Any,
) -> None:
    """근거 대장 §5.2 L2 — 동일 (계보, tag_code) 3회 연속 실패 → ENVIRONMENT_SHIFT 선두
    강제 + "문구 다듬기 중단"(LLM personalize 호출 자체를 건너뜀).

    같은 action_item 에 DISTRACTION 태그로 3회 연속 실패(가장 최근 건은 HARD_TO_START 도
    같이 달림) 이력을 만든다. 태그 없이 보면 HARD_TO_START → NANO_STEP(display_priority
    10)이 DISTRACTION → ENVIRONMENT_SHIFT(30)보다 같은 DOWNSCOPE 그룹에서 이긴다 — 그래서
    이 케이스는 "L2 가 정상 매칭 1등을 실제로 갈아치우는지"를 검증한다. LLM 은 스텁으로
    성공 응답을 주도록 해 두고, 그 스텁이 **한 번도 호출되지 않아야** "다듬기 중단"이
    실제로 호출을 건너뛴 것이지 응답을 받고 버린 게 아님을 증명한다.
    """
    from reaction_backend.llm import aiClient

    call_count = 0

    async def stub_run(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        raise AssertionError("L2 에서는 aiClient.run 이 호출되면 안 된다")

    monkeypatch.setattr(aiClient, "run", stub_run)

    action = _seed_action(fake_action_item_repo, title="집중 안 되는 작업")
    for i in range(2):
        fake_recovery_repo.register_execution(
            user_id=DEMO_USER_UUID,
            action_item_id=action.id,
            completion_status="failed",
            failure_tags=["DISTRACTION"],
            plan_start_at=datetime(2026, 6, 1, tzinfo=KST) + timedelta(days=i),
        )
    current = fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status="failed",
        failure_tags=["HARD_TO_START", "DISTRACTION"],
        plan_start_at=datetime(2026, 6, 1, tzinfo=KST) + timedelta(days=2),
    )

    body = _generate(client, f"exec_{current.id}").json()

    assert call_count == 0, "LLM 스텁이 호출됐다 — '문구 다듬기 중단'이 지켜지지 않았다"
    top = body["cards"][0]
    assert top["strategyType"] == "ENVIRONMENT_SHIFT"
    assert "NANO_STEP" not in {c["strategyType"] for c in body["cards"]}
    assert body["aiSource"] == "rule"


def test_generate_does_not_escalate_one_below_l2_threshold(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """경계 — 동일 태그 2회 연속(3회 미만)은 아직 L2 가 아니다. 정상 매칭(NANO_STEP)이 이긴다."""
    action = _seed_action(fake_action_item_repo, title="집중 안 되는 작업")
    fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status="failed",
        failure_tags=["DISTRACTION"],
        plan_start_at=datetime(2026, 6, 1, tzinfo=KST),
    )
    current = fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status="failed",
        failure_tags=["HARD_TO_START", "DISTRACTION"],
        plan_start_at=datetime(2026, 6, 2, tzinfo=KST),
    )

    body = _generate(client, f"exec_{current.id}").json()

    assert body["cards"][0]["strategyType"] == "NANO_STEP"


def test_generate_excludes_downscope_default_at_l1_but_still_calls_llm(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    monkeypatch: Any,
) -> None:
    """근거 대장 §5.2 L1 — 동일 카드 2회 연속 실패 → DOWNSCOPE_DEFAULT(축소 스타일) 배제,
    패딩이 분해 스타일(NANO_STEP)로 채운다. L2 와 달리 "문구 다듬기 중단"이 아니므로
    personalize 호출은 그대로 일어난다 — 그 차이를 직접 확인한다.
    """
    from reaction_backend.llm import RunResult, aiClient
    from reaction_backend.schemas.recovery import RecoveryProposalLLM

    call_count = 0

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        nonlocal call_count
        call_count += 1
        return RunResult(
            value=RecoveryProposalLLM(
                strategy_code="downscope",
                if_clause="많이 지쳤으면",
                then_clause="가벼운 산책 후 정리만 해볼까요",
                rationale="",
            ),
            fell_back=False,
            reason=None,
            prompt_id="recovery/if_then_proposal",
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    action = _seed_action(fake_action_item_repo, title="보고서 작성")
    fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status="failed",
        failure_tags=["FATIGUE"],
        plan_start_at=datetime(2026, 6, 1, tzinfo=KST),
    )
    current = fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status="failed",
        failure_tags=["FATIGUE"],
        plan_start_at=datetime(2026, 6, 2, tzinfo=KST),
    )

    body = _generate(client, f"exec_{current.id}").json()

    assert call_count == 1, "L1 은 personalize 호출을 건너뛰면 안 된다(L2 와 다름)"
    types = {c["strategyType"] for c in body["cards"]}
    assert "DOWNSCOPE_DEFAULT" not in types
    assert "NANO_STEP" in types
    assert body["aiSource"] == "llm"


def test_generate_does_not_escalate_one_below_l1_threshold(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """경계 — 동일 카드 1회 실패(2회 미만)는 아직 L1 이 아니다. DOWNSCOPE_DEFAULT 가 그대로 뜬다."""
    action = _seed_action(fake_action_item_repo, title="보고서 작성")
    current = fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status="failed",
        failure_tags=["FATIGUE"],
        plan_start_at=datetime(2026, 6, 1, tzinfo=KST),
    )

    body = _generate(client, f"exec_{current.id}").json()

    assert "DOWNSCOPE_DEFAULT" in {c["strategyType"] for c in body["cards"]}


def test_generate_no_tags_still_pads_to_min_cards(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """태그가 없어도 항상 최소 2장 — '빈 화면' 금지."""
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo, failure_tags=[])
    body = _generate(client, exec_id).json()
    assert len(body["cards"]) >= 2


def test_generate_stamps_prompt_version_on_created_attempts(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """P4 — 생성 배치가 쓴 프롬프트 버전이 attempt 에 남는다.

    `GEMINI_API_KEY` 가 없어 룰 fallback 으로 빠지지만, 프롬프트 렌더는 provider 호출보다
    먼저 일어난다(tool_executor.py) — AMBIGUITY 는 AVOIDANCE 가 아니라 `_PROMPT_ID_V2`
    ("recovery/if_then_proposal@v2")로 라우팅되고, fallback 경로에서도 정상 해석돼
    버전 "2" 가 채워져야 한다. 카드 여러 장이 한 배치에서 나오므로(선두 카드만 실제
    personalize) `llm_fallback_used` 와 같은 범위로 전부 동일.
    """
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    body = _generate(client, exec_id).json()
    assert body["aiSource"] == "rule"  # 전제 확인 — 이 테스트가 실제로 fallback 경로를 탐

    attempts = list(fake_recovery_repo._attempts.values())
    assert attempts, "생성된 attempt 가 없다"
    assert all(a.prompt_version == "2" for a in attempts), (
        f"prompt_version 이 배치 전체에 동일하게 안 채워짐: {[a.prompt_version for a in attempts]}"
    )


def test_generate_stamps_first_viewed_at_once_and_idempotent_recall_does_not_overwrite(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """P6 — 카드가 응답으로 나가는 시점에 first_viewed_at 이 채워지고, 재호출로 안 바뀐다.

    "노출"의 근사치일 뿐이라 이름이 first — 같은 pending 카드가 멱등 재호출(새로고침 등)로
    다시 나가도 최초 1회 시각을 유지해야 ITT 분모가 재호출마다 밀리지 않는다.
    """
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _generate(client, exec_id)

    attempts = list(fake_recovery_repo._attempts.values())
    assert attempts
    assert all(a.first_viewed_at is not None for a in attempts)
    first_stamp = {a.id: a.first_viewed_at for a in attempts}

    _generate(client, exec_id)  # 같은 execution 재호출 — 멱등 경로(이미 pending 카드 반환)
    attempts_after = list(fake_recovery_repo._attempts.values())
    assert {a.id: a.first_viewed_at for a in attempts_after} == first_stamp, (
        "재호출이 first_viewed_at 을 덮어썼다 — 'first' 의미가 깨진다"
    )


def test_generate_is_idempotent_while_pending(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    first = _generate(client, exec_id).json()
    second = _generate(client, exec_id).json()
    assert [c["attemptId"] for c in first["cards"]] == [c["attemptId"] for c in second["cards"]]


def test_generate_409_after_decision_is_final(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """결정이 끝난 실행에 generate 를 다시 부르면 409 — 두 번째 카드 세트를 만들지 않는다.

    회귀(실제 있었던 버그): 멱등 가드가 `pending` 카드만 봐서, 결정 후에는 pending 이 0건이라
    가드를 그냥 통과해 새 세트를 INSERT 했다. 그 결과 (a) `/recovery/decisions` 의 409 가
    무력화돼 같은 실패에 회복 ActionItem 이 2개 생기고, (b) `_accepted_replan_attempt` 는
    created_at 오름차순의 **첫** 채택 카드를 잡으므로 replan 이 옛 결정에 고정돼 사용자가
    다시 고른 최신 회복은 화면에도 안 뜨고 블록도 영영 안 생겼다.
    """
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    first_cards = _generate(client, exec_id).json()["cards"]
    _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": first_cards[0]["attemptId"],
        },
    )

    resp = _generate(client, exec_id)
    assert resp.status_code == 409, resp.json()
    assert resp.json()["code"] == "RECOVERY_ALREADY_DECIDED"
    # 카드가 늘지 않았다 — 두 번째 세트가 INSERT 되지 않았다는 뜻.
    assert len(fake_recovery_repo._attempts) == len(first_cards)
    # 회복 ActionItem 도 1개뿐.
    recovery_actions = [
        a for a in fake_action_item_repo._items.values() if a.source.startswith("recovery_")
    ]
    assert len(recovery_actions) == 1


def test_generate_409_after_skip_too(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """'오늘은 쉬기'(skipped) 로 닫은 실행도 재생성 대상이 아니다 — 같은 상태 머신."""
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _generate(client, exec_id)
    _decide(client, {"executionId": exec_id, "decision": "skipped"})

    resp = _generate(client, exec_id)
    assert resp.status_code == 409
    assert resp.json()["code"] == "RECOVERY_ALREADY_DECIDED"


def test_generate_404_unknown_execution(client: TestClient) -> None:
    resp = _generate(client, f"exec_{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "RECOVERY_EXECUTION_NOT_FOUND"


def test_generate_422_not_eligible(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, completion_status="done"
    )
    resp = _generate(client, exec_id)
    assert resp.status_code == 422
    assert resp.json()["code"] == "RECOVERY_NOT_ELIGIBLE"


# ───────────────────────── decisions ─────────────────────────


def test_decisions_requires_idempotency_key(client: TestClient) -> None:
    resp = client.post(
        "/recovery/decisions",
        json={"executionId": f"exec_{uuid4()}", "decision": "skipped"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_decision_accept_downscope_creates_action_and_rejects_siblings(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
        },
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["isDraft"] is False
    assert body["acceptedAttemptId"] == accepted["attemptId"]
    assert len(body["rejectedAttemptIds"]) == len(cards) - 1
    # DOWNSCOPE 수락 → 새 ActionItem(source=recovery_downscope) 생성
    assert body["resultingActionItemId"] is not None
    new_actions = [
        a for a in fake_action_item_repo._items.values() if a.source == "recovery_downscope"
    ]
    assert len(new_actions) == 1
    # 원본 카드 status 불변 (AGENTS.md §2 — Resilience 지표 전제)
    original = next(a for a in fake_action_item_repo._items.values() if a.source == "manual")
    assert original.status == "failed"
    # 혈통 기록
    assert new_actions[0].parent_action_item_id == original.id


def test_decision_accept_park_stamps_re_engagement_anchor(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """PARK 수락 — 새 카드는 없어도(§3 S8) `re_engagement_anchor_at` 은 반드시 찍힌다."""
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AVOIDANCE"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "PARK")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
        },
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["resultingActionItemId"] is None  # PARK 은 새 카드를 안 만든다

    attempt_id = UUID(accepted["attemptId"].removeprefix("rec_"))
    stored = fake_recovery_repo._attempts[attempt_id]
    assert stored.re_engagement_anchor_at is not None
    assert stored.re_engagement_anchor_at.weekday() == 0  # 다음 주 월요일


def test_decision_response_echoes_default_re_engagement_anchor(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """서버 기본값으로 채워진 앵커가 응답(`reEngagementAnchorAt`)에도 그대로 실린다 (#327).

    회귀: `_adopt()` 가 DB 에는 앵커를 찍지만, 라우트가 응답 스키마에 실어 보내지 않으면
    FE 는 그 값을 확인할 방법이 없다(요청/응답 계약이 없던 원래 구현의 공백).
    """
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AVOIDANCE"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "PARK")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
        },
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["reEngagementAnchorAt"] is not None


def test_decision_accepts_explicit_re_engagement_anchor_override(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """FE 가 명시 앵커를 보내면 서버 기본값 대신 그 값이 저장·반환된다 (#327, FE #221)."""
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "PRIORITY_SHIFT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "CARRY_OVER")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
            "reEngagementAnchorAt": "2026-08-01T08:30:00+09:00",
        },
    )

    assert resp.status_code == 200, resp.json()
    assert resp.json()["reEngagementAnchorAt"] == "2026-08-01T08:30:00+09:00"
    attempt_id = UUID(accepted["attemptId"].removeprefix("rec_"))
    stored = fake_recovery_repo._attempts[attempt_id]
    assert stored.re_engagement_anchor_at == datetime(2026, 8, 1, 8, 30, tzinfo=KST)


def test_decision_downscope_rejects_re_engagement_anchor(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """DOWNSCOPE/RESCHEDULE 은 앵커 개념이 없다 — 값을 보내면 조용히 버리지 않고 422."""
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "PRIORITY_SHIFT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
            "reEngagementAnchorAt": "2026-08-01T08:30:00+09:00",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_decision_skipped_rejects_re_engagement_anchor(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "PRIORITY_SHIFT"]
    )
    _generate(client, exec_id)  # pending 카드가 있어야 skipped 도 유효한 결정이 된다.
    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "skipped",
            "reEngagementAnchorAt": "2026-08-01T08:30:00+09:00",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_decision_rejects_naive_re_engagement_anchor(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """시간대 없는 값 — reEngagementAnchorAt 는 +09:00 등 offset 이 있어야 한다."""
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "PRIORITY_SHIFT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "CARRY_OVER")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
            "reEngagementAnchorAt": "2026-08-01T08:30:00",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_decision_accept_downscope_leaves_re_engagement_anchor_none(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")

    _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
        },
    )

    attempt_id = UUID(accepted["attemptId"].removeprefix("rec_"))
    stored = fake_recovery_repo._attempts[attempt_id]
    assert stored.re_engagement_anchor_at is None


def test_decision_accept_reschedule_creates_no_action(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    reschedule = next(c for c in cards if c["optionGroup"] == "RESCHEDULE")
    body = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": reschedule["attemptId"],
        },
    ).json()
    # RESCHEDULE 은 새 ActionItem 없음 (§5.16 — replan S20 에서 scheduled_blocks 처리)
    assert body["resultingActionItemId"] is None


def test_decision_skip_all(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    cards = _generate(client, exec_id).json()["cards"]
    body = _decide(
        client,
        {"executionId": exec_id, "decision": "skipped", "decisionReason": "오늘은 쉬기"},
    ).json()
    assert body["acceptedAttemptId"] is None
    assert len(body["skippedAttemptIds"]) == len(cards)


def test_decision_conflict_when_already_decided(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _generate(client, exec_id)
    _decide(client, {"executionId": exec_id, "decision": "skipped"})
    resp = _decide(client, {"executionId": exec_id, "decision": "skipped"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "RECOVERY_ALREADY_DECIDED"


def test_decision_accept_requires_attempt_id(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _generate(client, exec_id)
    resp = _decide(client, {"executionId": exec_id, "decision": "accepted"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


# ───────────────────────── replan (S20, #20-B) ─────────────────────────


def _accept_group(client: TestClient, exec_id: str, option_group: str) -> dict[str, Any]:
    """제안 생성 → 지정 그룹 카드 수락. 수락 응답(dict) 반환."""
    cards = _generate(client, exec_id).json()["cards"]
    target = next(c for c in cards if c["optionGroup"] == option_group)
    return _decide(  # type: ignore[no-any-return]
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": target["attemptId"],
        },
    ).json()


def _approve_replan(client: TestClient, exec_id: str, key: str | None = None) -> Any:
    return client.post(
        f"/replan/{exec_id}/approve",
        headers={"Idempotency-Key": key or f"test-{uuid4()}"},
    )


def test_replan_diff_returns_before_after(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    _accept_group(client, exec_id, "DOWNSCOPE")

    resp = client.get(f"/replan/{exec_id}")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["executionId"] == exec_id
    assert body["optionGroup"] == "DOWNSCOPE"
    # Draft Layer 프리뷰 — 아직 미승인
    assert body["isDraft"] is True
    assert body["alreadyApproved"] is False
    # before = 원본 카드, after = 회복 카드 (서로 다른 ActionItem)
    assert body["before"]["actionItemId"] != body["after"]["actionItemId"]
    assert "GROUP BY 실습" in body["before"]["title"]
    # 시각은 KST(+09:00) 직렬화
    assert body["after"]["startAt"].endswith("+09:00")


def test_replan_approve_creates_recovery_block(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: Any,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    decision = _accept_group(client, exec_id, "DOWNSCOPE")
    recovery_action_id = decision["resultingActionItemId"]

    resp = _approve_replan(client, exec_id)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["isDraft"] is False
    assert body["scheduledBlockId"].startswith("block_")
    assert body["actionItemId"] == recovery_action_id
    assert body["startAt"].endswith("+09:00")

    # scheduled_block(source='recovery') 1건 생성
    blocks = [b for b in fake_scheduled_block_repo._blocks.values() if b.source == "recovery"]
    assert len(blocks) == 1
    # 원본 카드 status 불변 (AGENTS.md §2 — Resilience 지표 전제)
    original = next(a for a in fake_action_item_repo._items.values() if a.source == "manual")
    assert original.status == "failed"


def test_replan_approve_is_idempotent(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: Any,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _accept_group(client, exec_id, "DOWNSCOPE")

    # 서로 다른 Idempotency-Key 로 두 번 — 미들웨어 캐시가 아니라 DB 가드로 멱등 보장
    first = _approve_replan(client, exec_id, key="k1").json()
    second = _approve_replan(client, exec_id, key="k2").json()
    assert first["scheduledBlockId"] == second["scheduledBlockId"]
    blocks = [b for b in fake_scheduled_block_repo._blocks.values() if b.source == "recovery"]
    assert len(blocks) == 1


def test_replan_approve_respects_blocks_from_other_sources(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: Any,
) -> None:
    """이미 다른 경로가 배치한 블록이 있으면 approve 는 그걸 반환하고 새로 만들지 않는다.

    회귀(실제 있었던 버그): 멱등 판정이 `source == 'recovery'` 인 블록만 봤다. S15 블록
    이동(`PATCH /plans/{planId}/blocks/{blockId}`)이 `source='user_edit'` 로 덮어쓰거나
    주간 forward 재계획이 `source='ai_plan'` 으로 만들면 필터에 안 걸려, GET 은
    `alreadyApproved=false` 로 '배치하기' CTA 를 다시 띄우고 approve 는 블록을 하나 더
    INSERT 했다 (`create_block` 은 겹침 검사도 안 한다).
    """
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    decision = _accept_group(client, exec_id, "DOWNSCOPE")
    recovery_action_id = decision["resultingActionItemId"]

    # 1) 정상 승인 → source='recovery' 블록 1건
    first = _approve_replan(client, exec_id, key="k1").json()
    # 2) 사용자가 주간 편집기에서 그 블록을 옮김 → source 가 'user_edit' 로 바뀐다
    moved = fake_scheduled_block_repo._blocks[UUID(first["scheduledBlockId"][len("block_") :])]
    moved.source = "user_edit"

    # 3) 다시 승인해도 새 블록을 만들지 않고 옮겨진 그 블록을 돌려준다
    second = _approve_replan(client, exec_id, key="k2").json()
    assert second["scheduledBlockId"] == first["scheduledBlockId"]
    assert second["actionItemId"] == recovery_action_id
    blocks = [
        b
        for b in fake_scheduled_block_repo._blocks.values()
        if str(b.action_item_id) == recovery_action_id[len("action_") :]
    ]
    assert len(blocks) == 1, f"소스가 바뀐 블록을 못 보고 중복 배치했다: {blocks}"

    # 4) GET 도 같은 판정 — 소스가 바뀌었다고 '미승인'으로 되돌아가지 않는다
    assert client.get(f"/replan/{exec_id}").json()["alreadyApproved"] is True


def test_replan_approve_never_places_past_block(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: Any,
    monkeypatch: Any,
) -> None:
    """21시 회고에서 DOWNSCOPE 를 수락해도 블록이 과거에 생기지 않는다 (#174 라우트 회귀).

    이전에는 `day_delta=0` 이라 원본 실패 슬롯(14:00) 시각이 그대로 쓰여, 결정 시점(21시)
    기준 7시간 전 블록이 INSERT 됐다. 그 블록은 pre_card 스윕 창을 영영 만나지 못한다.

    시계를 고정하는 이유: 밤 컷오프(23시) 때문에 실제 실행 시각에 따라 결과가 달라지면
    CI 가 새벽에만 깨진다.
    """
    from reaction_backend.api.routes import recovery as recovery_routes

    fixed_now = datetime(2026, 7, 29, 21, 3, tzinfo=KST)
    monkeypatch.setattr(recovery_routes, "now_kst", lambda: fixed_now)

    exec_id = _seed_failed_execution(
        fake_recovery_repo,
        fake_action_item_repo,
        target_date=date(2026, 7, 29),
        plan_start_at=datetime(2026, 7, 29, 14, 0, tzinfo=KST),
    )
    _accept_group(client, exec_id, "DOWNSCOPE")

    body = _approve_replan(client, exec_id).json()
    assert body["startAt"] == "2026-07-29T21:15:00+09:00", body
    block = next(b for b in fake_scheduled_block_repo._blocks.values() if b.source == "recovery")
    assert block.start_at > fixed_now, "과거 시각에 블록을 만들었다"


def test_replan_diff_returns_placed_block_after_approve(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    monkeypatch: Any,
) -> None:
    """승인 후 프리뷰는 **실제 배치된 블록** 시각을 돌려준다 — 재계산하면 화면과 DB 가 갈린다.

    보정이 `now` 의존이라, 승인 뒤에 다시 조회할 때마다 재계산하면 사용자가 보는 시각과
    실제 블록이 계속 어긋난다.
    """
    from reaction_backend.api.routes import recovery as recovery_routes

    monkeypatch.setattr(
        recovery_routes, "now_kst", lambda: datetime(2026, 7, 29, 21, 3, tzinfo=KST)
    )
    exec_id = _seed_failed_execution(
        fake_recovery_repo,
        fake_action_item_repo,
        target_date=date(2026, 7, 29),
        plan_start_at=datetime(2026, 7, 29, 14, 0, tzinfo=KST),
    )
    _accept_group(client, exec_id, "DOWNSCOPE")
    approved = _approve_replan(client, exec_id).json()

    # 40분 뒤 재조회 — 재계산이면 21:55 로 밀린다.
    monkeypatch.setattr(
        recovery_routes, "now_kst", lambda: datetime(2026, 7, 29, 21, 43, tzinfo=KST)
    )
    diff = client.get(f"/replan/{exec_id}").json()
    assert diff["alreadyApproved"] is True
    assert diff["after"]["startAt"] == approved["startAt"], "프리뷰가 실제 블록과 어긋난다"


def test_replan_diff_already_approved_after_approve(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _accept_group(client, exec_id, "DOWNSCOPE")
    _approve_replan(client, exec_id)

    body = client.get(f"/replan/{exec_id}").json()
    assert body["alreadyApproved"] is True


def test_replan_approve_requires_idempotency_key(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _accept_group(client, exec_id, "DOWNSCOPE")
    resp = client.post(f"/replan/{exec_id}/approve")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_replan_422_when_skipped(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _generate(client, exec_id)
    _decide(client, {"executionId": exec_id, "decision": "skipped"})

    diff = client.get(f"/replan/{exec_id}")
    assert diff.status_code == 422
    assert diff.json()["code"] == "RECOVERY_NO_REPLAN"
    approve = _approve_replan(client, exec_id)
    assert approve.status_code == 422
    assert approve.json()["code"] == "RECOVERY_NO_REPLAN"


def test_replan_422_for_reschedule_group(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    # RESCHEDULE 수락은 새 ActionItem 을 만들지 않음 → 재배치 대상 없음
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["CONFLICT"]
    )
    decision = _accept_group(client, exec_id, "RESCHEDULE")
    assert decision["resultingActionItemId"] is None
    assert client.get(f"/replan/{exec_id}").status_code == 422


def test_replan_404_unknown_execution(client: TestClient) -> None:
    assert client.get(f"/replan/exec_{uuid4()}").status_code == 404
    assert _approve_replan(client, f"exec_{uuid4()}").status_code == 404


# ───────────────────────── 룰 엔진 단위 테스트 ─────────────────────────


def test_select_strategies_caps_at_max() -> None:
    strategies = default_recovery_strategies()
    # 4그룹 모두 트리거 + 패딩 → 최대 4장
    tags = ["AMBIGUITY", "CONFLICT", "PRIORITY_SHIFT", "DISTRACTION", "EMERGENCY"]
    cards = select_strategies(tags, strategies)
    assert len(cards) <= 4
    groups = [c.option_group for c in cards]
    assert len(groups) == len(set(groups))


def test_select_strategies_score_beats_priority() -> None:
    strategies = default_recovery_strategies()
    # FATIGUE+LOW_ENERGY → ACTIVE_RECOVERY(2점) 가 RESCHEDULE_DEFAULT(0점) 대신 선택
    cards = select_strategies(["FATIGUE", "LOW_ENERGY"], strategies)
    reschedule_cards = [c for c in cards if c.option_group == "RESCHEDULE"]
    assert reschedule_cards and reschedule_cards[0].strategy_type == "ACTIVE_RECOVERY"


def test_render_template_missing_var_is_safe() -> None:
    assert render_template("딱 5분만 {first_step}", {}) == "딱 5분만"
    assert (
        render_template("{suspended_step} 부터 다시", {"suspended_step": "ERD 검토"})
        == "ERD 검토 부터 다시"
    )


def test_recovery_target_date_only_carry_over_moves_to_tomorrow() -> None:
    """'내일로 이어가기'는 CARRY_OVER 뿐 — 나머지 그룹은 결정한 날 그대로."""
    decided_on = date(2026, 7, 29)
    assert recovery_target_date(decided_on, "CARRY_OVER") == date(2026, 7, 30)
    for group in ("DOWNSCOPE", "RESCHEDULE", "PARK"):
        assert recovery_target_date(decided_on, group) == decided_on


# ── re_engagement_anchor_at (근거 대장 §3 S8) ──────────────────────────────


def test_re_engagement_anchor_none_for_downscope_and_reschedule() -> None:
    """오늘 안에 끝나거나(DOWNSCOPE) 이미 재배치된(RESCHEDULE) 그룹은 새 접점이 불필요."""
    decided_at = datetime(2026, 7, 29, 21, 3, tzinfo=KST)  # 수요일
    for group in ("DOWNSCOPE", "RESCHEDULE"):
        assert re_engagement_anchor_at(group, decided_at) is None


def test_re_engagement_anchor_carry_over_is_tomorrow_at_anchor_hour() -> None:
    decided_at = datetime(2026, 7, 29, 21, 3, tzinfo=KST)  # 수요일 21:03
    anchor = re_engagement_anchor_at("CARRY_OVER", decided_at)
    assert anchor == datetime(2026, 7, 30, 9, 0, tzinfo=KST)


def test_re_engagement_anchor_park_is_next_week_monday() -> None:
    """수요일에 결정하면 이번 주가 아니라 **다음 주** 월요일 (다음 주 리뷰 약속과 일치)."""
    decided_at = datetime(2026, 7, 29, 21, 3, tzinfo=KST)  # 수요일 (2026-07-29)
    anchor = re_engagement_anchor_at("PARK", decided_at)
    assert anchor == datetime(2026, 8, 3, 9, 0, tzinfo=KST)  # 다음 주 월요일
    assert anchor.weekday() == 0


def test_re_engagement_anchor_park_on_monday_skips_to_next_week_not_today() -> None:
    """오늘이 이미 월요일이어도 '오늘'이 아니라 **다음** 월요일 — '다음 주'라는 약속 그대로."""
    monday = datetime(2026, 7, 27, 8, 0, tzinfo=KST)  # 2026-07-27 은 월요일
    assert monday.weekday() == 0
    anchor = re_engagement_anchor_at("PARK", monday)
    assert anchor == datetime(2026, 8, 3, 9, 0, tzinfo=KST)


def test_re_engagement_anchor_time_of_day_is_fixed_regardless_of_decision_time() -> None:
    """결정이 몇 시였든 앵커의 시각은 항상 고정 시간대(아침) — 늦은 밤 결정도 예외 없다."""
    late_night = datetime(2026, 7, 29, 23, 55, tzinfo=KST)
    anchor = re_engagement_anchor_at("CARRY_OVER", late_night)
    assert anchor is not None
    assert (anchor.hour, anchor.minute) == (9, 0)


def test_recovery_unit_minutes_floors_at_default() -> None:
    """전략이 없거나(비활성/삭제) 최소 단위가 더 짧으면 기본 5분."""
    assert recovery_unit_minutes(None) == 5
    assert recovery_unit_minutes(3) == 5
    assert recovery_unit_minutes(5) == 5
    assert recovery_unit_minutes(30) == 30


def test_shift_to_recovery_day_preserves_wall_clock_across_days() -> None:
    """일 단위 시프트라 KST 벽시계 시각이 보존된다 (KST 는 DST 가 없다).

    회복 날짜가 내일이면 보정 대상이 아니다 — 보정은 **같은 날 안에서만** 한다.
    """
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    start_at, end_at = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 30),  # CARRY_OVER
        estimated_minutes=30,
        now=datetime(2026, 7, 29, 21, 3, tzinfo=KST),
    )
    assert start_at == datetime(2026, 7, 30, 14, 0, tzinfo=KST)
    assert end_at == datetime(2026, 7, 30, 14, 30, tzinfo=KST)


def test_shift_to_recovery_day_floors_past_slot_to_next_quarter() -> None:
    """DOWNSCOPE(day_delta=0)의 과거 슬롯을 `지금+10분` 15분 격자로 당긴다 (#174 회귀 핀).

    예전엔 원본 슬롯(14:00)을 그대로 써서, 21시 회고에서 수락하면 **7시간 전 과거**에
    블록이 생겼다. 그 블록은 pre_card 스윕 창을 영영 만나지 못한다.
    """
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    start_at, end_at = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 29),
        estimated_minutes=5,
        now=datetime(2026, 7, 29, 21, 3, tzinfo=KST),
    )
    assert start_at == datetime(2026, 7, 29, 21, 15, tzinfo=KST)  # 21:13 → 격자 올림
    assert end_at == datetime(2026, 7, 29, 21, 20, tzinfo=KST)


def test_shift_to_recovery_day_leaves_future_same_day_slot_untouched() -> None:
    """같은 날이어도 **아직 오지 않은** 슬롯은 건드리지 않는다.

    부등호를 뒤집는 뮤턴트(`earliest < start_at`)는 날짜 가드를 통과하므로 이 케이스만 잡는다.
    """
    plan_start = datetime(2026, 7, 29, 22, 0, tzinfo=KST)
    start_at, end_at = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 29),
        estimated_minutes=5,
        now=datetime(2026, 7, 29, 21, 3, tzinfo=KST),
    )
    assert start_at == plan_start
    assert end_at == datetime(2026, 7, 29, 22, 5, tzinfo=KST)


def test_shift_to_recovery_day_still_corrects_when_queried_the_next_day() -> None:
    """어제 결정한 회복을 오늘 조회해도 **여전히 보정된다** (#258 도그푸딩 결함 수정).

    예전엔 여기서 `same_day` 가드가 걸려 보정을 포기했다(블록이 어제 14:00 과거에 그대로
    남음) — 그 결과가 실제로 도그푸딩에서 나타났다(수락 2건 중 완주 0건). 이제는 "오늘
    (조회 시점 기준) 안에 자리가 있는가"만 본다 — 09:00 조회면 밤 컷오프 전에 충분히
    끝나므로 정상 보정된다.
    """
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    start_at, end_at = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 29),
        estimated_minutes=5,
        now=datetime(2026, 7, 30, 9, 0, tzinfo=KST),  # 다음 날 조회
    )
    assert start_at == datetime(2026, 7, 30, 9, 15, tzinfo=KST)
    assert end_at == datetime(2026, 7, 30, 9, 20, tzinfo=KST)


def test_shift_to_recovery_day_rolls_to_next_morning_at_night() -> None:
    """밤(23시 이후로 넘어가면) 그 날은 포기하고 **다음날 아침(07시)** 로 넘긴다.

    예전엔 여기서 아예 보정을 포기해 블록이 과거(14:00)에 그대로 남았다(#258 도그푸딩
    실측 — 21시 이후 결정이 전부 이 경로였다). `create_block` 이 정책 검사를 안 해도
    "그 날 밤 안에 욱여넣기"는 여전히 안 하지만, 이제 다음날로는 넘긴다.
    """
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    start_at, end_at = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 29),
        estimated_minutes=30,
        now=datetime(2026, 7, 29, 22, 52, tzinfo=KST),  # earliest 23:05 → 오늘은 컷오프 밖
    )
    assert start_at == datetime(2026, 7, 30, 7, 0, tzinfo=KST)
    assert end_at == datetime(2026, 7, 30, 7, 30, tzinfo=KST)


def test_shift_to_recovery_day_pulls_forward_when_lead_crosses_midnight_into_quiet_tail() -> None:
    """`+RECOVERY_MIN_LEAD_MINUTES` 가 자정을 넘겨 quiet hours 꼬리(00~07시)에 떨어지면,
    다음날까지 안 밀고 **같은 날** 07시로 당긴다 — 하루를 통째로 더 미룰 이유가 없다.
    """
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    now = datetime(2026, 7, 29, 23, 50, tzinfo=KST)
    start_at, _ = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 29),
        estimated_minutes=5,
        now=now,
    )
    # earliest = 23:50 + 10분 = 00:00(7/30) — quiet hours 꼬리. 07시로 당기되 날짜는 그대로.
    assert start_at == datetime(2026, 7, 30, 7, 0, tzinfo=KST)
    assert start_at - now >= timedelta(minutes=10)
    assert start_at.minute % 15 == 0


def test_shift_to_recovery_day_corrects_even_after_a_multi_day_gap() -> None:
    """며칠 뒤에 뒤늦게 승인해도(1일보다 더 벌어져도) 여전히 '조회 시점 오늘'로 보정된다."""
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    start_at, _ = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 29),
        estimated_minutes=5,
        now=datetime(2026, 8, 3, 10, 0, tzinfo=KST),  # 5일 뒤 조회
    )
    assert start_at == datetime(2026, 8, 3, 10, 15, tzinfo=KST)


def test_shift_to_recovery_day_allows_block_ending_exactly_at_cutoff() -> None:
    """컷오프에 **정확히** 닿는 블록은 허용 — 경계 부등호(`<=`)를 좁히는 뮤턴트를 잡는다."""
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    start_at, end_at = shift_to_recovery_day(
        plan_start,
        original_target_date=date(2026, 7, 29),
        recovery_target_date=date(2026, 7, 29),
        estimated_minutes=15,
        now=datetime(2026, 7, 29, 22, 35, tzinfo=KST),  # earliest 22:45, 종료 23:00 = 컷오프
    )
    assert start_at == datetime(2026, 7, 29, 22, 45, tzinfo=KST)
    assert end_at == datetime(2026, 7, 29, 23, 0, tzinfo=KST)


def test_shift_to_recovery_day_lead_always_covers_pre_card_window() -> None:
    """보정된 블록은 항상 `지금+10분` 이후 · 15분 격자 — pre_card 스윕이 최소 1회 본다.

    스윕은 5분 폴로 `[now+2m, now+7m)` 만 보므로 리드가 7분 아래로 내려가면 알림이 샌다.
    21:00~21:59 매 분을 돌려 lead 축소·라운딩 제거 뮤턴트를 잡는다.
    """
    plan_start = datetime(2026, 7, 29, 14, 0, tzinfo=KST)
    for minute in range(60):
        now = datetime(2026, 7, 29, 21, minute, tzinfo=KST)
        start_at, _ = shift_to_recovery_day(
            plan_start,
            original_target_date=date(2026, 7, 29),
            recovery_target_date=date(2026, 7, 29),
            estimated_minutes=5,
            now=now,
        )
        assert start_at - now >= timedelta(minutes=10), f"{now:%H:%M} 리드 부족: {start_at:%H:%M}"
        assert start_at.minute % 15 == 0, f"{now:%H:%M} 격자 이탈: {start_at:%H:%M}"


def test_night_cutoff_matches_push_gate_quiet_hours() -> None:
    """회복 밤 컷오프 = 알림 quiet hours 시작. 두 상수가 갈라지면 여기서 잡는다.

    orchestrator 는 순수 유지를 위해 safety 를 import 하지 않으므로 **테스트가 유일한 연결**이다.
    """
    from reaction_backend.safety.push_gate import QUIET_START_HOUR

    assert RECOVERY_NIGHT_CUTOFF_HOUR == QUIET_START_HOUR


# ───────────────────────── decisions: edited (#20 DoD 7) ─────────────────


def test_decision_edited_uses_user_text_and_preserves_ai_original(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """'수정' 수락 — 새 카드 제목은 사용자 문구, **AI 원문은 보존**.

    보존이 핵심 계약이다: `suggested_action_text` 를 덮어쓰면 "AI 제안을 사용자가 얼마나
    고쳐 썼나"(= Draft Layer 잠금 결정의 효과)를 영영 못 잰다.
    """
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    target = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")
    ai_original = next(
        a.suggested_action_text
        for a in fake_recovery_repo._attempts.values()
        if f"rec_{a.id}" == target["attemptId"]
    )

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "edited",
            "acceptedAttemptId": target["attemptId"],
            "editedActionText": "GROUP BY 예제 1문제만 풀어볼까요",
        },
    )

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["acceptedAttemptId"] == target["attemptId"]
    assert len(body["rejectedAttemptIds"]) == len(cards) - 1  # 형제는 accepted 와 동일하게 rejected
    assert body["resultingActionItemId"] is not None

    new_action = next(
        a for a in fake_action_item_repo._items.values() if a.source == "recovery_downscope"
    )
    assert new_action.title == "GROUP BY 예제 1문제만 풀어볼까요"

    attempt = next(
        a for a in fake_recovery_repo._attempts.values() if f"rec_{a.id}" == target["attemptId"]
    )
    assert attempt.user_decision == "edited"
    assert attempt.suggested_action_text == ai_original, "AI 원문이 덮어써졌다"
    assert attempt.suggested_action_text != new_action.title

    # 원본 카드 status 불변 (AGENTS.md §2)
    original = next(a for a in fake_action_item_repo._items.values() if a.source == "manual")
    assert original.status == "failed"


def test_decision_edited_enables_replan(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """편집 수락도 S20 replan 이 열린다 — 다른 endpoint 의 관측 결과로 검증.

    회귀: `_accepted_replan_attempt` 가 'accepted' 만 찾으면 '수정'을 고른 사용자만 재배치가
    422 로 막히는 비대칭 버그가 된다. 내가 고친 함수가 아니라 GET /replan 응답으로 확인한다.
    """
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    target = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")
    _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "edited",
            "acceptedAttemptId": target["attemptId"],
            "editedActionText": "딱 5분만 열어볼까요",
        },
    )

    resp = client.get(f"/replan/{exec_id}")

    assert resp.status_code == 200, resp.json()
    assert resp.json()["after"]["title"] == "딱 5분만 열어볼까요"


def test_decision_edited_keeps_user_text_verbatim(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """사용자 편집문은 금지어 필터를 **거치지 않는다** — 톤 잠금은 AI 출력 대상이다.

    사전에 있는 '실패'가 포함돼도 글자 그대로 저장된다. 서버가 사용자 말을 몰래 고치면
    "Be on your side" 가 사용자를 검열하는 도구로 뒤집힌다. 누가 나중에 사용자 입력에
    enforce 를 붙이면 이 테스트가 즉시 실패한다.
    """
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    target = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")

    _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "edited",
            "acceptedAttemptId": target["attemptId"],
            "editedActionText": "실패해도 5분만 해보기",
        },
    )

    new_action = next(
        a for a in fake_action_item_repo._items.values() if a.source == "recovery_downscope"
    )
    assert new_action.title == "실패해도 5분만 해보기"


def test_decision_edited_rejects_group_without_new_card(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """RESCHEDULE/PARK 은 새 카드를 안 만들어 문구를 담을 곳이 없다 → 422."""
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    target = next(c for c in cards if c["optionGroup"] == "RESCHEDULE")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "edited",
            "acceptedAttemptId": target["attemptId"],
            "editedActionText": "다른 문구",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "RECOVERY_EDIT_NOT_SUPPORTED"


@pytest.mark.parametrize("text", ["", "   "])
def test_decision_edited_requires_non_empty_text(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    text: str,
) -> None:
    """decision='edited' 인데 문구가 비면 422 — 요청 자체가 모순이다."""
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    target = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "edited",
            "acceptedAttemptId": target["attemptId"],
            "editedActionText": text,
        },
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_edited_text_with_non_edited_decision_is_rejected(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """decision='accepted' 인데 편집문을 보내면 422 — 조용히 무시하면 유실을 숨긴다."""
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    target = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": target["attemptId"],
            "editedActionText": "무시되면 안 되는 문구",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"
