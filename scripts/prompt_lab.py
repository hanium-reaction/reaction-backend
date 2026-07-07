"""prompt_lab — 프롬프트 실측 튜닝 하네스 (AI 파트 / Issue #5 보조 도구).

실제 프로덕션 LLM 게이트(`aiClient.run`)를 **그대로** 통과시켜, 프롬프트를 실데이터
시나리오로 돌리고 출력·토큰·비용·금지어·fallback 여부를 한눈에 비교한다. 즉 여기서
보는 결과 = 사용자가 받는 결과(렌더→Gemini→금지어 필터→schema 검증→fallback).

DB/서버 불필요 — `session=None` 으로 호출하므로 budget check·llm_runs INSERT 는 건너뛴다.
`GEMINI_API_KEY` 가 없으면 전부 fallback 으로 표시되지만, **렌더된 프롬프트는 그대로**
보여주므로 키가 오기 전에도 프롬프트 문안을 다듬는 데 쓸 수 있다.

사용법 (repo 루트에서):

    # 사용 가능한 프롬프트·시나리오 목록
    uv run python scripts/prompt_lab.py --list

    # recovery 프롬프트를 모든 시나리오로 (키 있으면 실 Gemini, 없으면 fallback)
    uv run python scripts/prompt_lab.py recovery

    # 한 시나리오만 + 렌더된 프롬프트 원문·raw JSON 까지
    uv run python scripts/prompt_lab.py recovery --scenario ambiguity --raw

    # 같은 입력을 3번 — 출력 변동성(일관성) 점검. 프롬프트 튜닝의 핵심.
    uv run python scripts/prompt_lab.py recovery --repeat 3

    # 키 없이 프롬프트 문안만 보기 (오프라인)
    uv run python scripts/prompt_lab.py brief --show-prompt

    # 유료 환산 비용 미리보기 (cents per 1K tokens; 기본은 .env 설정값=0)
    uv run python scripts/prompt_lab.py recovery --price-in 0.0075 --price-out 0.03

키 주입은 평소처럼 `.env` 의 `GEMINI_API_KEY=...` 로 하거나, 일회성으로
`--api-key`/`--model` 플래그를 쓴다 (env 에 주입 후 settings 로딩).

⚠️ 이 스크립트는 프롬프트 파일(`prompts/<domain>/<name>.vN.md`)만 고쳐가며 반복 실행하는
용도다. 금지어 필터·schema 강제를 끄지 않는다 (AGENTS.md §2). 프롬프트를 수정한 뒤에는
자동으로 레지스트리 캐시를 무효화하고 다시 읽는다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Windows 콘솔(cp949)에서도 한글/기호가 깨지지 않게.
with contextlib.suppress(Exception):  # pragma: no cover - tty 환경 의존
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


# ─────────────────────────── 시나리오 정의 ───────────────────────────
@dataclass(slots=True)
class Scenario:
    """프롬프트 1회 호출 입력 + (선택) 소프트 기대값."""

    name: str
    variables: dict[str, str]
    note: str = ""
    # 소프트 체크 — 출력 필드가 이 후보들 중 하나로 시작하면 ✓ (실패해도 멈추지 않음).
    expect_field: str | None = None
    expect_starts_with: tuple[str, ...] = ()


@dataclass(slots=True)
class PromptSpec:
    """한 프롬프트(prompt_id)의 module/schema/fallback + 시나리오 묶음."""

    key: str  # CLI 에서 쓰는 짧은 이름
    prompt_id: str
    module: str
    schema_path: str  # "reaction_backend.schemas.recovery:RecoveryProposalLLM"
    fallback_factory: Callable[[type], Any]
    headline_field: str  # 요약 테이블에 보여줄 핵심 필드
    scenarios: list[Scenario] = field(default_factory=list)
    # 계획(분해·검토)처럼 thinking 이 필요한 프롬프트는 기본 예산을 싣는다 (프로덕션 배선과 동일).
    # None 이면 인터뷰와 같은 기본(flash=0). `--thinking N` 으로 언제든 오버라이드해 A/B.
    thinking_budget: int | None = None
    # thinking 이 붙으면 단일 호출이 길어지므로 기본 timeout 상향. None 이면 8.0.
    timeout: float | None = None


def _import_schema(path: str) -> type:
    module_path, _, attr = path.partition(":")
    import importlib

    mod = importlib.import_module(module_path)
    return getattr(mod, attr)


# fallback 인스턴스 — 키 없을 때/오류 시 무엇이 사용자에게 가는지 보여준다.
def _recovery_fallback(schema: type) -> Any:
    return schema(
        strategy_code="downscope_default",
        if_clause="",
        then_clause="(fallback) 오늘은 절반만, 가능한 만큼만 해볼까요?",
        rationale="",
        estimated_workload_change_minutes=0,
    )


def _inbox_fallback(schema: type) -> Any:
    return schema(
        ai_category_guess="other",
        confidence=0.0,
        suggested_title="(fallback)",
        needs_user_override=True,
    )


def _brief_fallback(schema: type) -> Any:
    return schema(
        headline_ko="(fallback) 오늘도 한 걸음씩 가봐요. 가장 작은 것부터 시작해요.",
        first_step="가장 작은 카드 하나만 5분",
        reason_why_now="",
        adjustment_hints=[],
    )


def _next_question_fallback(schema: type) -> Any:
    return schema(
        question="(fallback) 조금만 더 구체적으로 알려주실 수 있을까요?",
        empathy_one_liner="천천히 알려주셔도 괜찮아요.",
        suggested_answers=[],
    )


def _ambiguity_fallback(schema: type) -> Any:
    return schema(
        slot_key="goals.list",
        clarity_score=0.5,
        new_ambiguity=0.5,
        normalized_value=None,
    )


def _summary_fallback(schema: type) -> Any:
    return schema(
        headline="(fallback) 요약",
        goal_summary="핵심 목표를 정리했어요.",
        time_summary="가용 시간대를 확인했어요.",
        preference_summary="선호하는 방식을 확인했어요.",
        confirm_question="이대로 계획을 세워볼까요?",
    )


def _decompose_fallback(schema: type) -> Any:
    from reaction_backend.schemas.planning import GoalNodeDraft

    return schema(
        goal_nodes=[
            GoalNodeDraft(
                node_id="tmp-root",
                parent_id=None,
                title="캡스톤 프로젝트",
                node_type="root",
                order_index=0,
                is_leaf=True,
            )
        ],
        action_items=[],
        policy_violations=[],
    )


def _plan_review_fallback(schema: type) -> Any:
    return schema(approved=True, feedback=[])


def _slot_harvest_fallback(schema: type) -> Any:
    return schema(slots=[])


# 계획 프롬프트 기본 thinking 예산 — config 기본과 동일(프로덕션 배선 미러). `--thinking` 로 A/B.
_PLANNING_THINKING = 2048
_PLANNING_TIMEOUT = 20.0


SPECS: dict[str, PromptSpec] = {
    "recovery": PromptSpec(
        key="recovery",
        prompt_id="recovery/if_then_proposal",
        module="recovery",
        schema_path="reaction_backend.schemas.recovery:RecoveryProposalLLM",
        fallback_factory=_recovery_fallback,
        headline_field="then_clause",
        scenarios=[
            Scenario(
                name="ambiguity",
                note="막막함 — 어디서 시작할지 모름 → 작게 쪼개는 제안이어야",
                variables={
                    "failure_type": "AMBIGUITY",
                    "confidence": "0.82",
                    "interruption_summary": "없음",
                    "context_summary": "실행 카드: GROUP BY 실습 / 결과: 못 함, 어디서 시작할지 막막했음",
                },
                expect_field="strategy_code",
                expect_starts_with=("nano", "downscope", "context"),
            ),
            Scenario(
                name="fatigue",
                note="피곤/저에너지 → 휴식 후 가볍게 또는 재배치",
                variables={
                    "failure_type": "FATIGUE, LOW_ENERGY",
                    "confidence": "0.74",
                    "interruption_summary": "없음",
                    "context_summary": "실행 카드: 알고리즘 2문제 / 결과: 너무 피곤해서 시작 못 함",
                },
                expect_field="strategy_code",
                expect_starts_with=("active", "reschedule", "downscope"),
            ),
            Scenario(
                name="conflict",
                note="일정 충돌 → 재배치",
                variables={
                    "failure_type": "CONFLICT",
                    "confidence": "0.69",
                    "interruption_summary": "갑작스러운 가족 일정",
                    "context_summary": "실행 카드: 영어 단어 50개 / 결과: 갑자기 약속이 생겨 못 함",
                },
                expect_field="strategy_code",
                expect_starts_with=("reschedule", "carryover"),
            ),
            Scenario(
                name="plan_too_big",
                note="과대 과제 → 범위 축소",
                variables={
                    "failure_type": "PLAN_TOO_BIG",
                    "confidence": "0.88",
                    "interruption_summary": "없음",
                    "context_summary": "실행 카드: 캡스톤 보고서 전체 작성 / 결과: 너무 커서 손도 못 댐",
                },
                expect_field="strategy_code",
                expect_starts_with=("downscope", "nano"),
            ),
            Scenario(
                name="context_loss",
                note="맥락 상실 → 워밍업으로 다시 잡기",
                variables={
                    "failure_type": "CONTEXT_LOSS",
                    "confidence": "0.71",
                    "interruption_summary": "어제 중단 후 하루 경과",
                    "context_summary": "실행 카드: ERD 검토 (어제 중단) / 결과: 어디까지 했는지 기억 안 남",
                },
                expect_field="strategy_code",
                expect_starts_with=("context", "nano"),
            ),
            Scenario(
                name="avoidance",
                note="회피 — 톤이 비난조로 새지 않는지 특히 주의",
                variables={
                    "failure_type": "AVOIDANCE",
                    "confidence": "0.6",
                    "interruption_summary": "없음",
                    "context_summary": "실행 카드: 발표 연습 / 결과: 계속 미루고 싶었음",
                },
                expect_field="strategy_code",
                expect_starts_with=("nano", "downscope"),
            ),
        ],
    ),
    "inbox": PromptSpec(
        key="inbox",
        prompt_id="inbox/classify",
        module="inbox",
        schema_path="reaction_backend.schemas.inbox:InboxClassification",
        fallback_factory=_inbox_fallback,
        headline_field="ai_category_guess",
        scenarios=[
            Scenario(
                "study",
                {"raw_text": "토익 단어 매일 30개씩"},
                expect_field="ai_category_guess",
                expect_starts_with=("study",),
            ),
            Scenario(
                "project",
                {"raw_text": "캡스톤 ERD 다시 검토하기"},
                expect_field="ai_category_guess",
                expect_starts_with=("project",),
            ),
            Scenario(
                "schedule",
                {"raw_text": "내일 오후 3시 병원 예약"},
                expect_field="ai_category_guess",
                expect_starts_with=("schedule",),
            ),
            Scenario(
                "health",
                {"raw_text": "주 3회 헬스장 가기"},
                expect_field="ai_category_guess",
                expect_starts_with=("health", "routine"),
            ),
            Scenario(
                "vague",
                {"raw_text": "그냥 머릿속 정리 좀 하고 싶다"},
                note="모호 — confidence 낮고 needs_user_override=true 기대",
                expect_field="ai_category_guess",
                expect_starts_with=("other",),
            ),
        ],
    ),
    "brief": PromptSpec(
        key="brief",
        prompt_id="brief/morning_brief",
        module="brief",
        schema_path="reaction_backend.schemas.today:MorningBriefDraft",
        fallback_factory=_brief_fallback,
        headline_field="headline_ko",
        scenarios=[
            Scenario(
                name="typical",
                note="평범한 아침 — 따뜻하고 간결한 톤",
                variables={
                    "today_kst": "2026-06-05 (금)",
                    "yesterday_summary": "3개 중 2개 완료, 1개 미완(GROUP BY 실습)",
                    "today_focus_cards": "캡스톤 자료조사(60분), 토익 RC 1회(90분)",
                    "today_maintain_cards": "헬스 30분, 영어 단어 20개",
                    "behavioral_summary": "밤 10시 이후 큰 작업은 잘 안 됨. 오전 집중력 높음.",
                },
            ),
            Scenario(
                name="rough_day",
                note="전날 다 밀린 날 — '실패' 단어 없이 회복 톤 유지하는지 (금지어 체크 핵심)",
                variables={
                    "today_kst": "2026-06-05 (금)",
                    "yesterday_summary": "5개 중 0개 완료, 전부 밀림",
                    "today_focus_cards": "캡스톤 발표자료 초안(45분)",
                    "today_maintain_cards": "가벼운 산책 15분",
                    "behavioral_summary": "연속 미완 3일째. 작업 크기를 줄이는 게 좋아 보임.",
                },
            ),
        ],
    ),
    "interview_q": PromptSpec(
        key="interview_q",
        prompt_id="interview/next_question",
        module="interview",
        schema_path="reaction_backend.schemas.interview:NextQuestionSchema",
        fallback_factory=_next_question_fallback,
        headline_field="question",
        scenarios=[
            Scenario(
                name="goals_list",
                note="정체성 답한 뒤 목표 묻기 — 앞 답을 이어받고 자유서술 추천카드가 붙어야",
                variables={
                    "goal_title": "당신의 목표",
                    "answered_context": "학년/시기=3학년 / 학기=방학",
                    "ambiguous_slot": "goals.list",
                    "slot_label": "지금 머릿속에 있는 일들을 편하게 알려주세요",
                    "answer_type": "text",
                    "options": "(자유 입력)",
                    "last_answer": "방학",
                    "retry": "",
                },
            ),
            Scenario(
                name="heaviest",
                note="가장 무거운 목표 — 보기 하나를 지목하지 말고 '이 중에서'로 물어야",
                variables={
                    "goal_title": "캡스톤 프로젝트",
                    "answered_context": "학년/시기=3학년 / 학기=방학 / 목표=캡스톤, 토익",
                    "ambiguous_slot": "goals.heaviest",
                    "slot_label": "그중 가장 무겁게 느끼는 건 어떤 거예요?",
                    "answer_type": "select",
                    "options": "캡스톤, 토익",
                    "last_answer": "캡스톤 마무리하고 토익 준비",
                    "retry": "",
                },
            ),
            Scenario(
                name="reask_success_image",
                note="재질문 — 같은 질문 반복 말고 예시로 답하기 쉽게",
                variables={
                    "goal_title": "캡스톤 프로젝트",
                    "answered_context": "학년/시기=3학년 / 목표=캡스톤, 토익 / 가장 무거운 목표=캡스톤",
                    "ambiguous_slot": "goals.success_image",
                    "slot_label": "이번 주 끝에 어떤 모습이면 좋을까요?",
                    "answer_type": "text",
                    "options": "(자유 입력)",
                    "last_answer": "음 잘 모르겠어",
                    "retry": "재질문: 직전 답이 조금 모호했다. 같은 말 반복 말고 예시·보기를 들어 답하기 쉽게 물어라.",
                },
            ),
        ],
    ),
    "interview_score": PromptSpec(
        key="interview_score",
        prompt_id="interview/ambiguity_score",
        module="interview",
        schema_path="reaction_backend.schemas.interview:AmbiguityUpdate",
        fallback_factory=_ambiguity_fallback,
        headline_field="normalized_value",
        scenarios=[
            Scenario(
                name="chip_role",
                note="자유서술 속 학년을 보기로 매핑 — normalized_value='3학년', clarity 높게",
                variables={
                    "slot_key": "identity.role",
                    "answer": "나는 컴퓨터공학과 3학년이야",
                    "answer_type": "chip",
                    "options": "1학년, 2학년, 3학년, 4학년, 졸업유예, 대학원, 기타",
                    "today": "2026-07-06",
                },
                expect_field="normalized_value",
                expect_starts_with=("3",),
            ),
            Scenario(
                name="text_goals",
                note="자유서술 목표 여러 개 — 핵심값 배열 추출",
                variables={
                    "slot_key": "goals.list",
                    "answer": "캡스톤 마무리하고 토익 900점 준비",
                    "answer_type": "text",
                    "options": "(자유 입력)",
                    "today": "2026-07-06",
                },
            ),
            Scenario(
                name="date_relative",
                note="상대 표현 마감 → 오늘 기준 YYYY-MM-DD",
                variables={
                    "slot_key": "goals.deadlines",
                    "answer": "이번 학기 말까지",
                    "answer_type": "date_picker",
                    "options": "(자유 입력)",
                    "today": "2026-07-06",
                },
            ),
            Scenario(
                name="skip_fixed",
                note="'딱히 없어' → 유효한 스킵(빈 값), clarity 0.7+ (무한 재질문 금지)",
                variables={
                    "slot_key": "time.fixed_blocks",
                    "answer": "딱히 없어",
                    "answer_type": "text",
                    "options": "(자유 입력)",
                    "today": "2026-07-06",
                },
            ),
        ],
    ),
    "interview_summary": PromptSpec(
        key="interview_summary",
        prompt_id="interview/summary",
        module="interview",
        schema_path="reaction_backend.schemas.interview:InterviewSummary",
        fallback_factory=_summary_fallback,
        headline_field="headline",
        scenarios=[
            Scenario(
                name="rich",
                note="다 답한 경우 — 마감·성공 이미지·노터치·휴식·최소 단위까지 요약에 녹아야",
                variables={
                    "identity": "3학년 방학",
                    "goals": "캡스톤, 토익",
                    "heaviest": "캡스톤",
                    "deadlines": "2026-08-20",
                    "success_image": "데모가 매끄럽게 동작",
                    "time_window": "09:00~23:00",
                    "peak_window": "오전, 저녁",
                    "no_touch": "수면, 식사",
                    "tone": "담백",
                    "rest_ok": "네",
                    "downscope_unit": "10분",
                },
            ),
            Scenario(
                name="sparse",
                note="빈 항목은 지어내지 말고 '아직 정하지 않음'으로 — 환각 금지 체크",
                variables={
                    "identity": "3학년 방학",
                    "goals": "캡스톤",
                    "heaviest": "캡스톤",
                    "deadlines": "아직 정하지 않음",
                    "success_image": "아직 정하지 않음",
                    "time_window": "09:00~23:00",
                    "peak_window": "오전",
                    "no_touch": "아직 정하지 않음",
                    "tone": "담백",
                    "rest_ok": "아직 정하지 않음",
                    "downscope_unit": "아직 정하지 않음",
                },
            ),
        ],
    ),
    "interview_harvest": PromptSpec(
        key="interview_harvest",
        prompt_id="interview/slot_extraction",
        module="interview",
        schema_path="reaction_backend.schemas.interview:SlotHarvest",
        fallback_factory=_slot_harvest_fallback,
        headline_field="slots",
        scenarios=[
            Scenario(
                name="rich_multi",
                note="한 답에 학기·마감·집중시간이 섞임 → 3개 슬롯 미리 추출되어야 (재질문 감소)",
                variables={
                    "answered_slot": "identity.role",
                    "answer": "난 컴공 3학년인데 지금 방학이야. 캡스톤이 8월 20일 마감이고 보통 오전에 집중이 잘 돼.",
                    "today": "2026-07-07",
                    "open_slots": (
                        "- identity.season | 지금 학기 중이에요, 방학이에요? | chip | 학기 중, 방학, 계절학기\n"
                        "- goals.deadlines | 마감일이 정해진 게 있어요? | date_picker | (자유 입력)\n"
                        "- goals.success_image | 이번 주 끝에 어떤 모습이면 좋을까요? | text | (자유 입력)\n"
                        "- time.peak_window | 가장 잘 집중되는 시간대는요? | chip | 오전, 오후, 저녁, 심야, 변동\n"
                        "- recovery.tone | 못 한 날 어떤 톤이 좋아요? | chip | 담백, 따뜻, 유머, 코치처럼"
                    ),
                },
            ),
            Scenario(
                name="single_slot_only",
                note="답이 한 항목만 담으면 다른 슬롯은 추출하지 말 것 (빈 배열이 정상)",
                variables={
                    "answered_slot": "goals.list",
                    "answer": "캡스톤 프로젝트랑 토익 준비",
                    "today": "2026-07-07",
                    "open_slots": (
                        "- time.peak_window | 가장 잘 집중되는 시간대는요? | chip | 오전, 오후, 저녁, 심야, 변동\n"
                        "- recovery.tone | 못 한 날 어떤 톤이 좋아요? | chip | 담백, 따뜻, 유머, 코치처럼"
                    ),
                },
            ),
        ],
    ),
    "planning_decompose": PromptSpec(
        key="planning_decompose",
        prompt_id="planning/goal_decompose",
        module="planning",
        schema_path="reaction_backend.schemas.planning:GoalDecomposition",
        fallback_factory=_decompose_fallback,
        headline_field="goal_nodes",
        thinking_budget=_PLANNING_THINKING,
        timeout=_PLANNING_TIMEOUT,
        scenarios=[
            Scenario(
                name="first",
                note="첫 분해 — SMART leaf(≤60분), 시간 충돌은 추측하지 말 것",
                variables={
                    "goal_title": "캡스톤 프로젝트 마무리",
                    "why_now": "이번 학기 졸업요건",
                    "horizon": "2026-08-20",
                    "behavioral_summary": "회복 톤: 담백 / 휴식 수용: True / 집중 지속: 50분",
                    "time_policy_summary": "활동: 09:00~23:00 / 피크: 오전, 저녁",
                    "review_feedback": "(첫 분해 — 이전 피드백 없음)",
                },
            ),
            Scenario(
                name="replan",
                note="재분해 — 아래 피드백을 실제로 반영해 더 잘게/뒤로 미뤄야 (P0-2)",
                variables={
                    "goal_title": "캡스톤 프로젝트 마무리",
                    "why_now": "이번 학기 졸업요건",
                    "horizon": "2026-08-20",
                    "behavioral_summary": "회복 톤: 담백 / 휴식 수용: True / 집중 지속: 50분",
                    "time_policy_summary": "활동: 09:00~23:00 / 피크: 오전, 저녁",
                    "review_feedback": (
                        "- '캡스톤 설계' leaf 가 90분이라 60분 초과 — 더 잘게 쪼개기\n"
                        "- 첫 주에 5개는 많음 — 우선순위 낮은 2개는 다음 주로"
                    ),
                },
            ),
        ],
    ),
    "planning_review": PromptSpec(
        key="planning_review",
        prompt_id="planning/plan_quality",
        module="planning",
        schema_path="reaction_backend.schemas.planning:PlanReview",
        fallback_factory=_plan_review_fallback,
        headline_field="approved",
        thinking_budget=_PLANNING_THINKING,
        timeout=_PLANNING_TIMEOUT,
        scenarios=[
            Scenario(
                name="over_60min",
                note="60분 초과 leaf 가 있으면 approved=false + 사람이 읽을 다듬기 제안",
                variables={
                    "goal_nodes_json": (
                        '[{"node_id":"r","parent_id":null,"title":"캡스톤",'
                        '"node_type":"root","order_index":0,"is_leaf":false},'
                        '{"node_id":"l1","parent_id":"r","title":"설계 문서",'
                        '"node_type":"leaf","order_index":0,"is_leaf":true}]'
                    ),
                    "action_items_json": (
                        '[{"node_id":"l1","title":"ERD 전체 작성","estimated_minutes":90,'
                        '"category":"project","first_step":"엔티티 목록부터"}]'
                    ),
                    "time_policy_summary": "활동: 09:00~23:00 / 피크: 오전",
                    "conflict_report": "충돌 없음",
                },
            ),
        ],
    ),
}


# ─────────────────────────── 실행 ───────────────────────────
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _c(text: str, color: str, *, on: bool) -> str:
    return f"{color}{text}{RESET}" if on else text


@dataclass(slots=True)
class CallOutcome:
    source: str  # "LLM" | "FALLBACK"
    reason: str | None
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost_cents: int
    banned_hits: tuple[str, ...]
    value: Any
    prompt_version: str
    thinking_budget: int | None = None


async def _call(
    spec: PromptSpec,
    schema: type,
    variables: Mapping[str, str],
    *,
    timeout: float,
    thinking_budget: int | None,
    price_in: float | None,
    price_out: float | None,
) -> CallOutcome:
    from reaction_backend.llm import aiClient
    from reaction_backend.prompts import registry as prompt_registry
    from reaction_backend.safety.llm_budget import estimate_cost_cents

    # 프롬프트 파일을 방금 고쳤을 수 있으니 캐시 무효화 후 최신본으로.
    prompt_registry.reload()

    result = await aiClient.run(
        module=spec.module,
        schema=schema,
        prompt_id=spec.prompt_id,
        fallback=lambda: spec.fallback_factory(schema),
        timeout=timeout,
        variables=dict(variables),
        session=None,  # DB/budget/llm_runs 우회 — 순수 프롬프트 실측
        thinking_budget=thinking_budget,
    )

    if price_in is not None or price_out is not None:
        pi = price_in or 0.0
        po = price_out or 0.0
        cents = int(round((result.tokens_in / 1000.0) * pi + (result.tokens_out / 1000.0) * po))
    else:
        cents = estimate_cost_cents(result.tokens_in, result.tokens_out)

    return CallOutcome(
        source="FALLBACK" if result.fell_back else "LLM",
        reason=result.reason,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        latency_ms=result.latency_ms,
        cost_cents=cents,
        banned_hits=result.banned_hits,
        value=result.value,
        prompt_version=result.prompt_version,
        thinking_budget=thinking_budget,
    )


def _render_prompt(spec: PromptSpec, variables: Mapping[str, str]) -> tuple[str, str]:
    from reaction_backend.prompts import registry as prompt_registry

    prompt_registry.reload()
    text, tmpl = prompt_registry.render(spec.prompt_id, dict(variables))
    return text, tmpl.full_id


def _field(value: Any, name: str) -> str:
    data = value.model_dump() if hasattr(value, "model_dump") else dict(value)
    return str(data.get(name, ""))


def _print_outcome(
    spec: PromptSpec,
    scn: Scenario,
    outcome: CallOutcome,
    *,
    color: bool,
    raw: bool,
    rendered: str | None,
) -> None:
    src_color = GREEN if outcome.source == "LLM" else YELLOW
    tag = f"[{outcome.source}]"
    if outcome.reason:
        tag += f"({outcome.reason})"
    print()
    print(
        _c(f"┌─ {scn.name} ", BOLD, on=color)
        + _c(tag, src_color, on=color)
        + _c(f"  v{outcome.prompt_version}", DIM, on=color)
    )
    if scn.note:
        print(_c(f"│  기대: {scn.note}", DIM, on=color))

    cost = "무료(0)" if outcome.cost_cents == 0 else f"{outcome.cost_cents}¢"
    think = "기본(flash=0)" if outcome.thinking_budget is None else f"{outcome.thinking_budget} tok"
    print(
        f"│  토큰 in/out: {outcome.tokens_in}/{outcome.tokens_out}"
        f"  ·  {outcome.latency_ms}ms  ·  thinking {think}  ·  비용 {cost}"
    )

    if outcome.banned_hits:
        print(
            "│  "
            + _c(f"[!] 금지어 치환됨: {', '.join(outcome.banned_hits)}", RED, on=color)
            + _c("  (톤 가드가 출력을 수정함 — 프롬프트에서 사전 차단 권장)", DIM, on=color)
        )

    # 소프트 기대값 체크
    if scn.expect_field and scn.expect_starts_with and outcome.source == "LLM":
        got = _field(outcome.value, scn.expect_field).lower()
        ok = any(got.startswith(p) for p in scn.expect_starts_with)
        mark = _c("✓", GREEN, on=color) if ok else _c("✗", RED, on=color)
        exp = "|".join(scn.expect_starts_with)
        print(f"│  기대 {scn.expect_field} ~ ({exp}): {mark} 실제={got!r}")

    # 핵심 출력
    data = outcome.value.model_dump()
    print(_c("│  ── 출력 ──", CYAN, on=color))
    for k, v in data.items():
        print(f"│    {k}: {v}")

    if raw and rendered is not None:
        print(_c("│  ── 렌더된 프롬프트 ──", DIM, on=color))
        for line in rendered.splitlines():
            print(_c(f"│    {line}", DIM, on=color))
        print(_c("│  ── raw JSON ──", DIM, on=color))
        print(_c(f"│    {outcome.value.model_dump_json()}", DIM, on=color))
    print("└" + "─" * 40)


async def _run_spec(
    spec: PromptSpec,
    args: argparse.Namespace,
) -> list[tuple[Scenario, CallOutcome]]:
    try:
        schema = _import_schema(spec.schema_path)
    except (ImportError, AttributeError) as exc:
        # 예: RecoveryProposalLLM 은 #20-A(PR #53) 머지 후에야 존재. 그 전엔 친절히 안내.
        print(
            _c(
                f"[건너뜀] {spec.key}: schema '{spec.schema_path}' 없음 ({exc}).",
                YELLOW,
                on=args.color,
            )
        )
        print(
            _c(
                "         해당 PR 머지 후 사용 가능. inbox/brief 는 지금 바로 동작합니다.",
                DIM,
                on=args.color,
            )
        )
        return []
    scenarios = spec.scenarios
    if args.scenario:
        scenarios = [s for s in scenarios if s.name == args.scenario]
        if not scenarios:
            names = ", ".join(s.name for s in spec.scenarios)
            print(_c(f"시나리오 '{args.scenario}' 없음. 가능: {names}", RED, on=args.color))
            return []

    results: list[tuple[Scenario, CallOutcome]] = []
    for scn in scenarios:
        rendered: str | None = None
        if args.raw or args.show_prompt:
            try:
                rendered, _ = _render_prompt(spec, scn.variables)
            except Exception as exc:  # noqa: BLE001
                print(_c(f"[렌더 실패] {scn.name}: {exc}", RED, on=args.color))
                continue

        if args.show_prompt:
            print()
            print(_c(f"┌─ {scn.name} (렌더만) ", BOLD, on=args.color))
            for line in (rendered or "").splitlines():
                print(f"│  {line}")
            print("└" + "─" * 40)
            continue

        # thinking/timeout 유효값: CLI 오버라이드(--thinking) > 스펙 기본 > 전역 기본.
        eff_thinking = args.thinking if args.thinking is not None else spec.thinking_budget
        eff_timeout = args.timeout if args.timeout is not None else (spec.timeout or 8.0)

        # --repeat: 변동성 점검 (fallback 은 결정적이라 1회로 강제)
        n = max(1, args.repeat)
        first_outcome: CallOutcome | None = None
        for i in range(n):
            outcome = await _call(
                spec,
                schema,
                scn.variables,
                timeout=eff_timeout,
                thinking_budget=eff_thinking,
                price_in=args.price_in,
                price_out=args.price_out,
            )
            if first_outcome is None:
                first_outcome = outcome
            label = (
                scn
                if n == 1
                else Scenario(
                    name=f"{scn.name} #{i + 1}",
                    variables=scn.variables,
                    note=scn.note,
                    expect_field=scn.expect_field,
                    expect_starts_with=scn.expect_starts_with,
                )
            )
            _print_outcome(spec, label, outcome, color=args.color, raw=args.raw, rendered=rendered)
            if outcome.source == "FALLBACK" and n > 1:
                print(_c("   (fallback 은 결정적 — repeat 생략)", DIM, on=args.color))
                break
        if first_outcome is not None:
            results.append((scn, first_outcome))
    return results


def _print_summary(rows: list[tuple[str, Scenario, CallOutcome]], *, color: bool) -> None:
    if not rows:
        return
    print()
    print(_c("═══ 요약 ═══", BOLD, on=color))
    print(
        f"{'prompt':<9} {'scenario':<14} {'src':<9} {'tok(i/o)':<11} {'ms':>5} {'banned':>6}  key"
    )
    print("-" * 78)
    for key, scn, o in rows:
        src = _c(f"{o.source:<9}", GREEN if o.source == "LLM" else YELLOW, on=color)
        toks = f"{o.tokens_in}/{o.tokens_out}"
        banned = _c(f"{len(o.banned_hits):>6}", RED, on=color) if o.banned_hits else f"{0:>6}"
        head = _field(o.value, SPECS[key].headline_field)
        head = (head[:34] + "…") if len(head) > 35 else head
        print(f"{key:<9} {scn.name:<14} {src} {toks:<11} {o.latency_ms:>5} {banned}  {head}")
    n_fb = sum(1 for _, _, o in rows if o.source == "FALLBACK")
    if n_fb:
        print()
        print(
            _c(f"※ {n_fb}/{len(rows)} 건이 fallback. ", YELLOW, on=color)
            + "GEMINI_API_KEY 가 없거나 호출 실패 — 실 LLM 품질을 보려면 키를 넣으세요."
        )


def _detect_key() -> bool:
    from reaction_backend.config import get_settings

    return bool(get_settings().gemini_api_key)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prompt_lab",
        description="프롬프트 실측 튜닝 하네스 — aiClient.run 그대로 통과.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="list",
        help="recovery | inbox | brief | interview_q | interview_score | interview_harvest "
        "| interview_summary | planning_decompose | planning_review | all | list (기본: list)",
    )
    parser.add_argument("--list", action="store_true", help="프롬프트·시나리오 목록만 출력")
    parser.add_argument("--scenario", help="한 시나리오만 실행")
    parser.add_argument("--repeat", type=int, default=1, help="같은 입력 N회 (변동성 점검)")
    parser.add_argument("--raw", action="store_true", help="렌더된 프롬프트 + raw JSON 출력")
    parser.add_argument("--show-prompt", action="store_true", help="호출 없이 렌더된 프롬프트만")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="단일 호출 timeout(초). 미지정이면 스펙 기본(계획=상향) 또는 8.0",
    )
    parser.add_argument(
        "--thinking",
        type=int,
        default=None,
        help="Gemini thinking 예산(토큰) 오버라이드 — A/B 용. 0=끔, 예:2048=켬. "
        "미지정이면 스펙 기본(계획만 켜짐).",
    )
    parser.add_argument("--price-in", type=float, help="1K 입력토큰당 ¢ (유료 환산 미리보기)")
    parser.add_argument("--price-out", type=float, help="1K 출력토큰당 ¢")
    parser.add_argument("--api-key", help="일회성 GEMINI_API_KEY 주입 (.env 우선이 일반적)")
    parser.add_argument("--model", help="일회성 LLM_MODEL 오버라이드")
    parser.add_argument("--no-color", action="store_true", help="ANSI 색 끄기")
    args = parser.parse_args(argv)
    args.color = not args.no_color and sys.stdout.isatty()

    # tool_executor 의 fallback WARNING 로그는 하네스 출력에 노이즈 — 조용히.
    import logging

    logging.getLogger("reaction_backend").setLevel(logging.ERROR)

    # settings 로딩 전에 env 주입 (lru_cache 이므로 첫 get_settings 전에).
    if args.api_key:
        os.environ["GEMINI_API_KEY"] = args.api_key
    if args.model:
        os.environ["LLM_MODEL"] = args.model

    if args.list or args.target == "list":
        print(_c("사용 가능한 프롬프트:", BOLD, on=args.color))
        for key, spec in SPECS.items():
            print(f"  {_c(key, CYAN, on=args.color):<20} {spec.prompt_id}")
            for s in spec.scenarios:
                print(f"      - {s.name}")
        print()
        print("예: uv run python scripts/prompt_lab.py recovery --scenario ambiguity --raw")
        return 0

    targets = list(SPECS.values()) if args.target == "all" else None
    if targets is None:
        spec = SPECS.get(args.target)
        if spec is None:
            print(
                _c(f"알 수 없는 target: {args.target}", RED, on=args.color)
                + f"  (가능: {', '.join(SPECS)}, all, list)"
            )
            return 2
        targets = [spec]

    if not args.show_prompt:
        has_key = _detect_key()
        banner = (
            _c("● GEMINI_API_KEY 감지 — 실 Gemini 호출", GREEN, on=args.color)
            if has_key
            else _c(
                "○ GEMINI_API_KEY 없음 — 전부 fallback (렌더는 정상). 키 넣으면 실측됨.",
                YELLOW,
                on=args.color,
            )
        )
        model = os.environ.get("LLM_MODEL", "")
        print(banner + (_c(f"  model={model}", DIM, on=args.color) if model else ""))

    all_rows: list[tuple[str, Scenario, CallOutcome]] = []
    for spec in targets:
        print()
        print(_c(f"━━━ {spec.key}  ({spec.prompt_id}) ━━━", BOLD, on=args.color))
        results = asyncio.run(_run_spec(spec, args))
        all_rows.extend((spec.key, scn, o) for scn, o in results)

    if not args.show_prompt:
        _print_summary(all_rows, color=args.color)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
