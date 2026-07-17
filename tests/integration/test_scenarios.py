"""엔드투엔드 시나리오 테스트 — 최신 코드가 도메인 전 구간을 오류 없이 도는지 검증.

단위/라우터 테스트(각 도메인 happy+실패 1건)와 달리, 여기서는 **여러 도메인을 가로지르는
실제 사용자 여정**을 HTTP 레벨로 이어 붙여 "기능이 통째로 동작하는지"를 본다.

- 시나리오 A: 신규 유저 딥 인터뷰 여정 (start → 다중 턴 답 → finish → outcome → 재조회)
- 시나리오 B: 하루 실행 회복 루프 (inbox 수집 → 오늘 카드 → 시작 → 실패 → 회복 → 재배치)
- 시나리오 C: 수집·목표·습관 관리 (inbox→목표화, 티어 한도, 습관, 재분류/보류/삭제)

LLM(GEMINI_API_KEY 빈 상태)은 자동 룰 fallback 을 타므로 별도 stub 없이 실동작을 검증한다.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from fastapi.testclient import TestClient

# ─────────────────────────────────────────────────────────────────────────────
# 시나리오 A — 신규 유저 딥 인터뷰 여정
# ─────────────────────────────────────────────────────────────────────────────


def test_scenario_deep_interview_journey(client: TestClient) -> None:
    """세션 시작 → 정체성·목표 슬롯 다중 턴 답변 → [충분해요] 마감 → outcome/summary 확정.

    라우터→인터뷰 엔진→슬롯 영속(재조립) 전 구간을 실 fallback 로 돌린다.
    마감 후 같은 세션 재조회가 동일 outcome 을 결정적으로 재빌드하는지까지 확인한다.
    """
    # 1) 세션 시작 — FSM 첫 필수 슬롯(identity.role) 질문
    start = client.post("/interview/sessions")
    assert start.status_code == 201, start.text
    session = start.json()
    sid = session["sessionId"]
    assert session["currentQuestion"]["slotKey"] == "identity.role"
    assert session["endReason"] is None

    # 2) 여러 턴에 걸쳐 답 제출 (chip 2개 + 자유서술 1개) — 매 턴 200 + 진행
    def answer(slot: str, value: Any, turn: int) -> dict[str, Any]:
        res = client.post(
            f"/interview/sessions/{sid}/answers",
            json={"slotKey": slot, "value": value, "clientTurn": turn},
        )
        assert res.status_code == 200, res.text
        return cast(dict[str, Any], res.json())

    after_role = answer("identity.role", ["3학년"], 1)
    assert after_role["currentQuestion"]["slotKey"] == "identity.season"

    after_season = answer("identity.season", ["방학"], 2)
    # 정체성 다 채우면 목표 슬롯으로 진행
    assert after_season["currentQuestion"]["slotKey"] == "goals.list"

    after_goals = answer("goals.list", "캡스톤 프로젝트 마무리하고 토익 900점 준비", 3)
    assert after_goals["endReason"] is None  # 아직 진행 중

    # 3) [충분해요] 조기 마감 — 남은 필수 슬롯은 안전 default, outcome/summary 확정
    finish = client.post(f"/interview/sessions/{sid}/finish")
    assert finish.status_code == 200, finish.text
    done = finish.json()
    assert done["endReason"] in {"early_user", "completed"}
    assert done["currentQuestion"] is None
    assert done["summary"]["confirmQuestion"]  # S03 확인 카드
    outcome = done["outcome"]
    assert outcome is not None
    assert outcome["sessionId"] == sid
    assert outcome["coreGoals"], "인터뷰에서 추출한 목표가 최소 1개는 있어야 한다"
    # 답한 슬롯은 미해결 목록에 없어야
    assert "identity.role" not in outcome["unresolvedSlots"]

    # 4) 종료 세션 재조회 — 결정적 재빌드(LLM 0회)로 동일 outcome
    refetch = client.get(f"/interview/sessions/{sid}")
    assert refetch.status_code == 200, refetch.text
    again = refetch.json()
    assert again["endReason"] == done["endReason"]
    assert again["outcome"]["sessionId"] == sid

    # 5) 새 세션 시작이 다시 허용되는지(이전 세션 종료됨 → 충돌 없음)
    restart = client.post("/interview/sessions")
    assert restart.status_code == 201


# ─────────────────────────────────────────────────────────────────────────────
# 시나리오 B — 하루 실행 회복 루프 (중간발표 데모 루프)
# ─────────────────────────────────────────────────────────────────────────────


def test_scenario_daily_execution_and_recovery_loop(
    client: TestClient,
    fake_action_item_repo: Any,
    fake_scheduled_block_repo: Any,
) -> None:
    """inbox 수집 → 오늘 카드 승격 → 시작 → 실패 → 사유 태깅 → 회복 카드 수락 → 재배치.

    today / reflection / recovery / replan 을 하나의 여정으로 이어, 원본 카드 status 불변
    (Resilience 지표 전제, AGENTS §2)까지 지키는지 확인한다.
    """
    # 1) inbox 로 할 일 수집 → 오늘 카드로 승격
    captured = client.post("/inbox", json={"rawText": "GROUP BY 실습 정리"})
    assert captured.status_code == 201, captured.text
    inbox_id = captured.json()["inboxId"]

    promoted = client.post(f"/inbox/{inbox_id}/convert-to-action")
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "promoted"

    # 2) 오늘 아젠다에 카드가 뜨는지 → actionId 확보
    agenda = client.get("/today/agenda")
    assert agenda.status_code == 200, agenda.text
    cards = agenda.json()["cards"]
    assert len(cards) == 1
    action_id = cards[0]["actionId"]
    assert action_id.startswith("action_")

    # 3) [▶ 시작] → in_progress 실행 생성
    started = client.post(f"/today/actions/{action_id}/start")
    assert started.status_code == 201, started.text
    exec_id = started.json()["executionId"]
    assert started.json()["completionStatus"] == "in_progress"

    # 4) [못함] 체크인 → 실패 사유 태깅 필요
    check = client.post(
        "/today/check-ins",
        json={"executionId": exec_id, "completionStatus": "failed"},
    )
    assert check.status_code == 200, check.text
    assert check.json()["needsFailureTags"] is True

    # 5) 실패 사유: 막막해서 시작 못 함 (AMBIGUITY)
    tag = client.post(
        f"/reflection/failure-tags/{exec_id}",
        json={"tagCodes": ["AMBIGUITY"], "memo": "어디서 시작할지 몰랐다"},
    )
    assert tag.status_code == 201, tag.text

    # 6) 회복 카드 생성 — AMBIGUITY → NANO_STEP 선두, Draft Layer 강제
    proposals = client.post("/recovery/proposals/generate", json={"executionId": exec_id})
    assert proposals.status_code == 201, proposals.text
    pbody = proposals.json()
    assert pbody["isDraft"] is True
    assert pbody["aiSource"] == "rule"  # LLM 키 없음 → 룰 fallback
    top = pbody["cards"][0]
    assert top["strategyType"] == "NANO_STEP"
    assert "GROUP BY 실습" in top["suggestedActionText"]  # 원본 제목 치환

    # 7) [수락] → 새 5분 다운스코프 카드 생성
    decision = client.post(
        "/recovery/decisions",
        json={
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": top["attemptId"],
        },
        headers={"Idempotency-Key": f"scenario-b-{uuid4()}"},
    )
    assert decision.status_code == 200, decision.text
    dbody = decision.json()
    assert dbody["isDraft"] is False
    recovery_action_id = dbody["resultingActionItemId"]
    assert recovery_action_id is not None

    # 8) 재배치 diff → 승인으로 회복 블록 생성
    diff = client.get(f"/replan/{exec_id}")
    assert diff.status_code == 200, diff.text
    assert diff.json()["isDraft"] is True
    assert diff.json()["before"]["actionItemId"] != diff.json()["after"]["actionItemId"]

    approve = client.post(
        f"/replan/{exec_id}/approve",
        headers={"Idempotency-Key": f"scenario-b-approve-{uuid4()}"},
    )
    assert approve.status_code == 200, approve.text
    abody = approve.json()
    assert abody["isDraft"] is False
    assert abody["scheduledBlockId"].startswith("block_")
    assert abody["startAt"].endswith("+09:00")  # KST 직렬화

    # 9) 불변식 — 원본 카드 status 는 failed 그대로, 회복 카드는 별도 계보로 생성
    original = next(a for a in fake_action_item_repo._items.values() if a.source == "inbox")
    assert original.status == "failed"
    recovered = [
        a for a in fake_action_item_repo._items.values() if a.source == "recovery_downscope"
    ]
    assert len(recovered) == 1
    assert recovered[0].parent_action_item_id == original.id
    # 회복 scheduled_block 1건
    rec_blocks = [b for b in fake_scheduled_block_repo._blocks.values() if b.source == "recovery"]
    assert len(rec_blocks) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 시나리오 C — 수집·목표·습관 관리 (티어 한도 불변식 포함)
# ─────────────────────────────────────────────────────────────────────────────


def test_scenario_goal_and_habit_management(client: TestClient) -> None:
    """inbox→목표 승격, Focus 한도(3), 습관 생성/아젠다 노출, 재분류·보류·삭제 여정.

    goals / habits / inbox / today 를 가로질러 잠금 제품 결정(Focus≤3, DevBaseline §1.4)이
    실제로 강제되는지 확인한다.
    """
    # 0) 초기 목표 목록은 빈 3-티어 구조
    assert client.get("/goals").json() == {"focus": [], "maintain": [], "parked": []}

    # 1) inbox 로 떠오른 목표 수집 → 목표(maintain)로 승격
    cap = client.post("/inbox", json={"rawText": "캡스톤 마무리"})
    assert cap.status_code == 201, cap.text
    conv = client.post(f"/inbox/{cap.json()['inboxId']}/convert-to-goal")
    assert conv.status_code == 200, conv.text
    assert conv.json()["promotedGoalId"].startswith("goal_")

    goals = client.get("/goals").json()
    assert len(goals["maintain"]) == 1
    assert goals["maintain"][0]["title"] == "캡스톤 마무리"

    # 2) Focus 목표 3개 생성 → 4번째는 한도 초과로 거절
    focus_ids: list[str] = []
    for i in range(3):
        res = client.post(
            "/goals",
            json={
                "title": f"집중 목표 {i}",
                "category": "project",
                "goalTier": "focus",
                "priorityLevel": 1,
            },
        )
        assert res.status_code == 201, res.text
        focus_ids.append(res.json()["goalId"])

    over = client.post(
        "/goals",
        json={
            "title": "4번째 집중",
            "category": "project",
            "goalTier": "focus",
            "priorityLevel": 1,
        },
    )
    assert over.status_code == 422
    assert over.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"

    # 3) 습관 생성 → 오늘 아젠다에 이번 주 인스턴스로 노출
    habit = client.post(
        "/habits",
        json={
            "title": "아침 운동",
            "category": "health",
            "frequencyPerWeek": 3,
            "minutesPerSession": 30,
            "timePreference": "morning",
            "priorityLevel": 2,
        },
    )
    assert habit.status_code == 201, habit.text
    assert len(client.get("/habits").json()) == 1

    agenda_habits = client.get("/today/agenda").json()["habits"]
    assert len(agenda_habits) == 1
    assert agenda_habits[0]["targetCount"] == 3
    assert agenda_habits[0]["doneCount"] == 0

    # 4) 재분류 — Focus 하나를 Parked 로 내리면 Focus 한 칸이 빈다
    reclass = client.patch(f"/goals/{focus_ids[0]}", json={"goalTier": "parked"})
    assert reclass.status_code == 200, reclass.text
    assert reclass.json()["goalTier"] == "parked"

    # 이제 Focus 는 2개 → 새 Focus 생성이 다시 허용
    again = client.post(
        "/goals",
        json={
            "title": "다시 집중",
            "category": "project",
            "goalTier": "focus",
            "priorityLevel": 1,
        },
    )
    assert again.status_code == 201, again.text

    # 5) 보류(park) + 소프트 삭제
    park = client.post(f"/goals/{focus_ids[1]}/park")
    assert park.status_code == 200
    assert park.json()["goalTier"] == "parked"

    delete = client.delete(f"/goals/{focus_ids[2]}")
    assert delete.status_code == 204

    # 6) 최종 상태 — 3-티어 구조 유지, 삭제분 제외
    final = client.get("/goals").json()
    titles = {g["title"] for tier in final.values() for g in tier}
    assert "집중 목표 2" not in titles  # 삭제됨
    assert "캡스톤 마무리" in titles  # 승격분 유지
    assert len(final["parked"]) == 2  # reclass + park
