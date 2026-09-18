"""Review — Weekly Review (S21). Issue #21-A.

MVP 룰 기반 (LLM 한 줄 평 P2 — 이슈 #21). cron 이 `period_summaries` 를 precompute 하고,
GET 은 그 주의 **확정본**(회고 창이 닫힌 뒤 집계한 행)만 저장값으로 믿는다. 확정 전이면 —
진행 중인 주, 또는 늦은 회고가 아직 들어올 수 있는 지난주 — 매번 즉석 계산한다(쓰기 X).
cron 미실행 환경(데모)에서도 빈 화면이 안 나오는 것도 같은 경로다.

집계/영속화 로직은 `scheduler/weekly_review_precompute.py` 단일 소스를 재사용한다.

endpoint:
- GET  /reviews/weekly?weekStart=YYYY-MM-DD       — 주간 리뷰 (확정본 우선, 아니면 즉석 계산)
- POST /reviews/weekly/generate                   — 수동 재생성 + 영속화 (디버그)
- GET  /reviews/habit-penalty                     — 3주 미달 빈도 재설계 후보 (S22, #21-C)
- POST /reviews/habit-penalty/{habitId}/accept    — 빈도 다운 수락 (Idempotency-Key, #21-C)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import cycle_proposal, mandala_adapter
from reaction_backend.orchestrator.habit_penalty import PenaltyEval, evaluate_penalty
from reaction_backend.orchestrator.weekly_review import (
    ExecutionStat,
    WeeklyKpi,
    compute_effort_minutes,
    compute_weekly_kpis,
)
from reaction_backend.repositories.goal_repo import GoalRepo, get_goal_repo
from reaction_backend.repositories.habit_instance_repo import (
    HabitInstanceRepo,
    get_habit_instance_repo,
)
from reaction_backend.repositories.habit_repo import (
    HabitRepo,
    current_week_start_kst,
    get_habit_repo,
)
from reaction_backend.repositories.review_repo import ReviewRepo, get_review_repo
from reaction_backend.scheduler.weekly_review_precompute import (
    is_final_summary,
    persist_weekly_review,
    week_start_of,
    week_window,
)
from reaction_backend.schemas.common import now_kst, to_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.reviews import (
    EffortMinutes,
    GoalCompletionProposal,
    HabitPenaltyAcceptResponse,
    HabitPenaltyCandidate,
    HabitPenaltyListResponse,
    HabitWeekStat,
    MandalaHabitWeekStat,
    MandalaWeeklySummary,
    NextCycleProposal,
    StaleAxisProposal,
    TopFailureContext,
    WeeklyGenerateRequest,
    WeeklyReviewResponse,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])

ReviewRepoDep = Annotated[ReviewRepo, Depends(get_review_repo)]
GoalRepoDep = Annotated[GoalRepo, Depends(get_goal_repo)]
HabitRepoDep = Annotated[HabitRepo, Depends(get_habit_repo)]
HabitInstRepoDep = Annotated[HabitInstanceRepo, Depends(get_habit_instance_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

_HABIT_PREFIX = "habit_"


def _parse_week_start(raw: str | None) -> date:
    """weekStart 파싱 → 해당 주 월요일. None 이면 이번 주. 형식 오류 422."""
    if raw is None:
        return week_start_of(now_kst().date())
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as e:
        raise ApiError(
            ErrorCode.REVIEW_INVALID_WEEK,
            "weekStart 는 YYYY-MM-DD 형식이어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="weekStart",
        ) from e
    return week_start_of(parsed)


@dataclass(frozen=True, slots=True)
class _ReadTimeSections:
    """응답 중 **매 요청 파생**하는 절 — `period_summaries` 에 저장하지 않는다.

    KPI(저장본이든 즉석 계산이든) 옆에 붙는 나머지 전부다. 한곳에 모아 응답 조립을 하나로
    둔다 — 예전엔 저장본용·즉석 계산용 조립 함수가 두 벌이라 새 필드를 한쪽에만 배선하면
    다른 경로에서 조용히 기본값이 나갔다.
    """

    effort: EffortMinutes
    mandala: MandalaWeeklySummary | None
    next_cycle_proposals: list[NextCycleProposal]
    goal_completion_proposals: list[GoalCompletionProposal]
    stale_axis_proposals: list[StaleAxisProposal]
    top_failure_contexts: list[TopFailureContext]
    unstarted_blocks: int = 0


def _kpi_from_summary(summary: PeriodSummary) -> WeeklyKpi:
    """저장본(PeriodSummary, Numeric=Decimal) → KPI(float) — 응답 조립을 하나로 모으는 다리."""
    return WeeklyKpi(
        adherence_rate=_f(summary.adherence_rate),
        consistency_days=summary.consistency_days,
        resilience_rate=_f(summary.resilience_rate),
        avg_delay_minutes=_f(summary.avg_delay_minutes),
        restart_success_rate=_f(summary.restart_success_rate),
        repeated_failure_count=summary.repeated_failure_count,
        average_recovery_minutes=_f(summary.average_recovery_minutes),
        category_success_rate={k: float(v) for k, v in summary.category_success_rate.items()},
        drain_point_window=summary.drain_point_window,
        peak_point_window=summary.peak_point_window,
        one_liner=summary.llm_one_liner,
        policy_update_candidates=summary.policy_update_candidates,
    )


def _to_response(
    week_start: date,
    kpi: WeeklyKpi,
    *,
    generated_at: datetime,
    sections: _ReadTimeSections,
) -> WeeklyReviewResponse:
    """KPI + 매 요청 파생 절 → 응답. 저장본 경로와 즉석 계산 경로가 **같은 함수**를 탄다."""
    return WeeklyReviewResponse(
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        adherence_rate=kpi.adherence_rate,
        consistency_days=kpi.consistency_days,
        resilience_rate=kpi.resilience_rate,
        avg_delay_minutes=kpi.avg_delay_minutes,
        restart_success_rate=kpi.restart_success_rate,
        repeated_failure_count=kpi.repeated_failure_count,
        average_recovery_minutes=kpi.average_recovery_minutes,
        effort=sections.effort,
        unstarted_blocks=sections.unstarted_blocks,
        category_success_rate=kpi.category_success_rate,
        peak_window=kpi.peak_point_window,
        drain_window=kpi.drain_point_window,
        one_liner=kpi.one_liner,
        policy_update_candidates=kpi.policy_update_candidates,
        top_failure_contexts=sections.top_failure_contexts,
        mandala=sections.mandala,
        next_cycle_proposals=sections.next_cycle_proposals,
        goal_completion_proposals=sections.goal_completion_proposals,
        stale_axis_proposals=sections.stale_axis_proposals,
        generated_at=generated_at,
    )


def _f(value: object | None) -> float | None:
    """Numeric(Decimal) → float. None 보존."""
    return None if value is None else float(value)  # type: ignore[arg-type]


@dataclass(slots=True)
class _MandalaTree:
    """궁극목표의 만다라 트리 + 반복형 칸 습관 — 요청당 **한 번만** 읽는다.

    '이번 주 만다라트' 절과 '손 못 댄 축' 제안이 같은 트리·습관을 본다. 예전엔 둘이 각자
    궁극목표·노드·습관을 다시 읽고 이번 주 인스턴스도 두 번 읽었다. 주별 인스턴스는
    `instances_by_week` 에 캐시해 같은 주를 두 번 읽지 않는다.
    """

    nodes: list[GoalNode]
    habits_by_node: dict[UUID, Habit]
    instances_by_week: dict[date, dict[UUID, HabitInstance]] = field(default_factory=dict)

    async def instances_for(
        self, week_start: date, *, session: AsyncSession
    ) -> dict[UUID, HabitInstance]:
        cached = self.instances_by_week.get(week_start)
        if cached is None:
            habit_ids = [h.id for h in self.habits_by_node.values()]
            cached = await mandala_adapter.fetch_habit_instances_for_week(
                session, habit_ids, week_start
            )
            self.instances_by_week[week_start] = cached
        return cached


async def _load_mandala_tree(
    user_id: UUID, *, goal_repo: GoalRepo, session: AsyncSession
) -> _MandalaTree | None:
    """궁극목표/승인된 트리 없으면 None — 두 절 모두 이때는 생략(ADR-0008 §8 "E")."""
    ultimate = await goal_repo.get_ultimate(user_id)
    if ultimate is None:
        return None
    nodes = await goal_repo.list_nodes(ultimate.id, tree_kind="mandala")
    if not nodes:
        return None
    leaf_ids = [n.id for n in nodes if n.depth == 2]
    habits_by_node = await mandala_adapter.fetch_habits_for_nodes(session, leaf_ids)
    return _MandalaTree(nodes=list(nodes), habits_by_node=habits_by_node)


async def _mandala_weekly_summary(
    tree: _MandalaTree | None, week_start: date, *, session: AsyncSession
) -> MandalaWeeklySummary | None:
    """GET /reviews/weekly 의 '이번 주 만다라트' 절 — 궁극목표/승인된 트리 없으면 None(생략, ADR-0008 §8 "E").

    `period_summaries` 에 저장하지 않고 매 호출 시 파생한다(`mandala_adapter.compute_progress`
    가 `goal_nodes.progress` 컬럼을 안 두는 것과 같은 이유) — GET/POST 두 응답 경로가 이
    함수 하나를 공유해 단일 소스를 유지한다.
    """
    if tree is None:
        return None
    stat = mandala_adapter.compute_weekly_stat(
        tree.nodes,
        week_start=week_start,
        habits_by_node=tree.habits_by_node,
        instances_by_habit=await tree.instances_for(week_start, session=session),
    )
    return MandalaWeeklySummary(
        completed_this_week=stat.completed_this_week,
        completed_total=stat.completed_total,
        total_leaves=stat.total_leaves,
        touched_this_week=stat.touched_this_week,
        untouched_axis_titles=stat.untouched_axis_titles,
        habits=[
            MandalaHabitWeekStat(
                axis_title=h.axis_title,
                cell_title=h.cell_title,
                done_count=h.done_count,
                target_count=h.target_count,
            )
            for h in stat.habits
        ],
    )


async def _top_failure_contexts(
    user_id: UUID, week_end: date, *, repo: ReviewRepo
) -> list[TopFailureContext]:
    """최근 28일(해당 주 일요일 기준 역산) 실패 사유 상위 3개 — #301.

    `mandala`/`next_cycle_proposals` 와 같은 이유로 `period_summaries` 에 저장하지 않고
    조회 시점에 파생한다 — 이번 주 실패 하나가 지난주 저장된 스냅샷에 안 잡히는 지연을
    피한다. `d0=d1=week_end` 로 넘겨 [week_end-27, week_end] 28일 창을 만든다.
    """
    rows = await repo.get_top_failure_contexts(user_id, week_end, week_end)
    return [
        TopFailureContext(tag_code=r.tag_code, label_ko=r.label_ko, count=r.count, share=r.share)
        for r in rows
    ]


async def _cycle_proposals(
    user_id: UUID, *, goal_repo: GoalRepo, session: AsyncSession
) -> tuple[list[NextCycleProposal], list[GoalCompletionProposal]]:
    """다음 주기 열기 제안 + **목표 완료 확인** 제안 (ADR-0008 §8 "G" + ADR-0007 §5 PR-4) — `week_start` 와 무관하게
    **현재** 상태만 본다.

    조회 대상 주가 과거든 이번 주든, 이 카드는 항상 "지금 열어도 되는가"를 말한다(달력
    스냅샷이 아니라 실시간 콜투액션이라서). 두 스코프를 합쳐서 낸다:

    ① **만다라 2주(G)** — 승격된 만다라 축 목표 중 실행 중(`status='active'`)인 것 전부
       (`fetch_promoted_active_goals_for_user`). 마일스톤 층이 없을 수 있어 가드 없이 판정.
    ② **일반형(PR-4)** — 마일스톤이 있는 임의 목표(`fetch_goals_with_milestones`) 중 ①과
       겹치지 않는 것. 열린 마일스톤이 남아 있어야만 제안한다(`has_open_milestone`) — 그래야
       "다음 주기"와 "목표 완료 확인"이 갈린다(§5 세 번째 가드).

    ①·②를 교집합 제거 없이 각자 돌리면 같은 목표가 두 번 뜰 수 있다 — 만다라 승격 목표도
    Stage A 를 거쳐 마일스톤을 가질 수 있기 때문(이 점은 확인되지 않았다, 방어적으로 겹치지
    않게 뺀다). 각 목표의 **현재 활성** 계획 트리 leaf 에 매달린 action_item 만 보고 판정한다
    (과거 주기 종결 카드가 섞이면 첫 주기 이후 판정이 항상 참이 돼버린다 —
    `cycle_proposal.should_propose_next_cycle` 참고).

    `today` 는 `week_start` 가 아니라 **실제 오늘**(KST)이다 — 이 카드가 "지금 열어도 되는가"를
    말하는 것과 같은 이유로, 밀린 카드 판정도 조회 대상 주가 아니라 현재 시각 기준이어야 한다.
    """
    today = now_kst().date()
    proposals: list[NextCycleProposal] = []
    completions: list[GoalCompletionProposal] = []

    mandala_goals = await mandala_adapter.fetch_promoted_active_goals_for_user(session, user_id)
    mandala_goal_ids = {g.id for g in mandala_goals}
    axis_titles = await mandala_adapter.fetch_promoted_axis_titles(session, list(mandala_goal_ids))
    for goal in mandala_goals:
        nodes = await goal_repo.list_nodes(goal.id, tree_kind="plan")
        leaf_ids = [n.id for n in nodes if n.node_type == "leaf"]
        action_items = await cycle_proposal.fetch_action_items_for_leaf_nodes(session, leaf_ids)
        if cycle_proposal.should_propose_next_cycle(action_items, today=today):
            proposals.append(
                NextCycleProposal(
                    goal_id=goal.id, goal_title=goal.title, axis_title=axis_titles.get(goal.id)
                )
            )

    milestones_by_goal = await cycle_proposal.fetch_goals_with_milestones(session, user_id)
    for goal_id, milestones in milestones_by_goal.items():
        if goal_id in mandala_goal_ids:
            continue  # 방어적 — ① 에서 이미 판정됨(위 docstring 참고)
        target_goal = await goal_repo.get_by_id(user_id, goal_id)
        if target_goal is None:
            continue
        nodes = await goal_repo.list_nodes(goal_id, tree_kind="plan")
        leaf_ids = [n.id for n in nodes if n.node_type == "leaf"]
        action_items = await cycle_proposal.fetch_action_items_for_leaf_nodes(session, leaf_ids)
        has_open = cycle_proposal.has_open_milestone(milestones)
        if not has_open:
            completions.append(
                GoalCompletionProposal(goal_id=target_goal.id, goal_title=target_goal.title)
            )
            # 배타성은 이 `continue` 가 아니라 가드가 보장한다 —
            # `should_propose_next_cycle` 이 `has_open_milestone=False` 면 곧바로 False 다.
            # 여기서 끊는 건 의도를 눈에 보이게 두는 것뿐이라 지워도 동작은 같다
            # (뮤테이션 확인). 조회를 아끼지도 않는다 — action_item 조회는 위에서 이미 끝났다.
            continue
        if cycle_proposal.should_propose_next_cycle(
            action_items, today=today, has_open_milestone=has_open
        ):
            proposals.append(
                NextCycleProposal(
                    goal_id=target_goal.id, goal_title=target_goal.title, axis_title=None
                )
            )

    return proposals, completions


_STALE_AXIS_WEEKS = 3  # ADR-0008 §6 — "3주 연속 손 못 댄 축"


async def _stale_axis_proposals(
    tree: _MandalaTree | None, *, session: AsyncSession
) -> list[StaleAxisProposal]:
    """3주 연속 손 못 댄 축 제안 목록 (ADR-0008 §8 "H") — `week_start` 와 무관하게 **현재** 상태만 본다.

    `_mandala_weekly_summary` 와 같은 전제(궁극목표·승인된 트리 없으면 빈 목록). 최근
    `_STALE_AXIS_WEEKS`(이번 주부터 과거로) 각각 `compute_weekly_stat` 을 계산해 전부에서
    빠짐없이 "손 못 댐"으로 잡힌 축만 제안한다(`mandala_adapter.compute_stale_axes`).
    """
    if tree is None:
        return []
    this_week = week_start_of(now_kst().date())
    week_starts = [this_week - timedelta(weeks=i) for i in range(_STALE_AXIS_WEEKS)]
    untouched_id_sets: list[set[UUID]] = []
    for ws in week_starts:
        stat = mandala_adapter.compute_weekly_stat(
            tree.nodes,
            week_start=ws,
            habits_by_node=tree.habits_by_node,
            instances_by_habit=await tree.instances_for(ws, session=session),
        )
        untouched_id_sets.append(set(stat.untouched_axis_ids))

    stale_axes = mandala_adapter.compute_stale_axes(
        tree.nodes, untouched_id_sets, earliest_week_start=week_starts[-1]
    )
    return [StaleAxisProposal(axis_id=axis.id, axis_title=axis.title) for axis in stale_axes]


def _effort_minutes(executions: list[ExecutionStat]) -> EffortMinutes:
    """그 주를 **분**으로 다시 센 요약 (ADR-0009 D5).

    `period_summaries` 에 저장하지 않고 매 호출 시 파생한다 — `_mandala_weekly_summary` 와
    같은 이유이자 같은 방식이다. 컬럼을 늘리면 마이그레이션이 필요하고, 이 값은 그 주의
    `execution_events` 만 있으면 언제든 다시 셀 수 있어 저장할 이유가 없다.

    KPI 와 **같은 실행 표본**(호출자가 한 번 모은 것)을 받는다 — 두 지표가 다른 시점의
    표본을 보면 나란히 놓는 의미가 없다.
    """
    totals = compute_effort_minutes(executions)
    return EffortMinutes(
        planned_minutes=totals.planned_minutes,
        completed_minutes=totals.completed_minutes,
        actual_minutes=totals.actual_minutes,
        adherence_rate=totals.adherence_rate,
    )


async def _read_time_sections(
    user_id: UUID,
    monday: date,
    executions: list[ExecutionStat],
    *,
    repo: ReviewRepo,
    goal_repo: GoalRepo,
    session: AsyncSession,
) -> _ReadTimeSections:
    """GET·POST generate 공통 — 매 요청 파생 절을 한 번씩만 읽어 모은다."""
    tree = await _load_mandala_tree(user_id, goal_repo=goal_repo, session=session)
    proposals, completions = await _cycle_proposals(user_id, goal_repo=goal_repo, session=session)
    start_dt, end_dt = week_window(monday)
    return _ReadTimeSections(
        effort=_effort_minutes(executions),
        mandala=await _mandala_weekly_summary(tree, monday, session=session),
        next_cycle_proposals=proposals,
        goal_completion_proposals=completions,
        stale_axis_proposals=await _stale_axis_proposals(tree, session=session),
        top_failure_contexts=await _top_failure_contexts(
            user_id, monday + timedelta(days=6), repo=repo
        ),
        # 시작도 안 하고 지나간 블록 — 준수율(시작한 카드만 셈) 옆에 둔다. 조회 시점 파생이라
        # 확정 저장본 경로에서도 같은 값이 나간다.
        unstarted_blocks=await repo.count_unstarted_blocks(
            user_id, start_dt, end_dt, now=now_kst()
        ),
    )


async def _live_kpi(
    user_id: UUID, monday: date, executions: list[ExecutionStat], *, repo: ReviewRepo
) -> WeeklyKpi:
    """이미 모은 실행 표본으로 KPI 즉석 계산 — 회복 표본만 더 읽는다."""
    start_dt, end_dt = week_window(monday)
    recoveries = await repo.collect_recovery_stats(user_id, start_dt, end_dt)
    return compute_weekly_kpis(executions, recoveries, monday)


@router.get("/weekly")
async def get_weekly_review(
    user: CurrentUser,
    repo: ReviewRepoDep,
    goal_repo: GoalRepoDep,
    session: SessionDep,
    week_start: Annotated[str | None, Query(alias="weekStart")] = None,
) -> WeeklyReviewResponse:
    """이번 주(또는 지정 주차) 리뷰. 확정본이 있으면 그것, 아니면 즉석 계산(쓰기 없음).

    확정본 = 그 주의 회고 창이 닫힌 뒤 집계한 행(`is_final_summary`). 예전엔 저장된 행이면
    무조건 믿었는데, 일요일 18:00 폴이 만든 행이 그 주 내내 잠겨 21:00 회고 알림을 받고 체크인한
    결과가 점수·한 줄 평에 안 들어갔다 — 같은 응답의 `effort`·`mandala` 는 매번 새로 세므로
    한 화면 안에서 두 시점의 숫자가 섞였다. 확정 전에는 저장본을 건너뛰고 즉석 계산한다.

    그 주 실행 표본은 **한 번만** 읽어 `effort` 와 KPI 가 같이 쓴다.
    """
    monday = _parse_week_start(week_start)
    start_dt, end_dt = week_window(monday)
    executions = await repo.collect_execution_stats(user.id, start_dt, end_dt)
    sections = await _read_time_sections(
        user.id, monday, executions, repo=repo, goal_repo=goal_repo, session=session
    )
    existing = await repo.get_weekly(user.id, monday)
    if existing is not None and is_final_summary(existing, monday):
        return _to_response(
            monday,
            _kpi_from_summary(existing),
            generated_at=existing.generated_at,
            sections=sections,
        )
    kpi = await _live_kpi(user.id, monday, executions, repo=repo)
    return _to_response(monday, kpi, generated_at=now_kst(), sections=sections)


@router.post("/weekly/generate")
async def generate_weekly_review(
    body: WeeklyGenerateRequest,
    user: CurrentUser,
    repo: ReviewRepoDep,
    goal_repo: GoalRepoDep,
    session: SessionDep,
) -> WeeklyReviewResponse:
    """주간 리뷰 강제 재생성 + 영속화 (디버그/관리자). 같은 주 덮어쓰기."""
    monday = _parse_week_start(body.week_start)
    start_dt, end_dt = week_window(monday)
    executions = await repo.collect_execution_stats(user.id, start_dt, end_dt)
    sections = await _read_time_sections(
        user.id, monday, executions, repo=repo, goal_repo=goal_repo, session=session
    )
    kpi = await _live_kpi(user.id, monday, executions, repo=repo)
    summary = await persist_weekly_review(user.id, monday, kpi, now_kst(), repo=repo)
    await session.commit()
    # 저장된 값(Numeric 자릿수 반영) 그대로 돌려준다 — 이후 GET 이 읽을 값과 같게.
    return _to_response(
        monday,
        _kpi_from_summary(summary),
        generated_at=summary.generated_at,
        sections=sections,
    )


# ───────────────────────── S22 Habit Penalty (#21-C) ─────────────────────────


def _parse_habit_id(raw: str) -> UUID:
    if not raw.startswith(_HABIT_PREFIX):
        raise _habit_not_found()
    try:
        return UUID(raw[len(_HABIT_PREFIX) :])
    except ValueError as e:
        raise _habit_not_found() from e


def _habit_not_found() -> ApiError:
    return ApiError(
        ErrorCode.HABIT_NOT_FOUND,
        "해당 습관을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _last_completed_monday() -> date:
    """직전에 끝난 주의 월요일 — 진행 중인 이번 주는 제외하고 감지."""
    return current_week_start_kst() - timedelta(days=7)


def _already_decided(habit: Habit, reference_week: date) -> bool:
    """이번 사이클(직전 완료 주)에 이미 페널티를 평가했으면 재제안하지 않는다."""
    decided_at = habit.last_penalty_evaluated_at
    return decided_at is not None and to_kst(decided_at).date() >= reference_week


def _candidate_message(target: int, avg_done: float, suggested: int) -> str:
    """비난 없는 재설계 톤 (베이스라인 §1.4)."""
    return (
        f"지난 3주 동안 주 {target}회 목표 중 평균 {avg_done:g}회를 했어요. "
        f"무리하지 않게 주 {suggested}회로 맞춰볼까요?"
    )


def _to_candidate(habit: Habit, ev: PenaltyEval) -> HabitPenaltyCandidate:
    target = ev.recent[-1][1] if ev.recent else habit.target_count
    return HabitPenaltyCandidate(
        habit_id=f"{_HABIT_PREFIX}{habit.id}",
        title=habit.title,
        current_frequency=habit.frequency_per_week,
        suggested_frequency=ev.suggested_frequency,
        recent_weeks=[HabitWeekStat(done_count=d, target_count=t) for d, t in ev.recent],
        message=_candidate_message(target, ev.avg_done, ev.suggested_frequency),
    )


@router.get("/habit-penalty")
async def list_habit_penalty(
    user: CurrentUser,
    habit_repo: HabitRepoDep,
    habit_inst_repo: HabitInstRepoDep,
) -> HabitPenaltyListResponse:
    """3주 연속 미달(50% 미만) habit 의 빈도 재설계 제안 후보 (S22)."""
    reference = _last_completed_monday()
    candidates: list[HabitPenaltyCandidate] = []
    for habit in await habit_repo.list_active(user.id):
        if _already_decided(habit, reference):
            continue
        instances = await habit_inst_repo.list_recent_for_habit(habit.id, reference, 3)
        ev = evaluate_penalty(instances, habit.frequency_per_week)
        if ev is not None:
            candidates.append(_to_candidate(habit, ev))
    return HabitPenaltyListResponse(candidates=candidates)


@router.post("/habit-penalty/{habit_id}/accept")
async def accept_habit_penalty(
    habit_id: str,
    user: CurrentUser,
    habit_repo: HabitRepoDep,
    habit_inst_repo: HabitInstRepoDep,
    session: SessionDep,
) -> HabitPenaltyAcceptResponse:
    """빈도 재설계 수락 → frequency 다운 (Idempotency-Key 필수 — §1.7 미들웨어)."""
    habit = await habit_repo.get_by_id(user.id, _parse_habit_id(habit_id))
    if habit is None:
        raise _habit_not_found()

    reference = _last_completed_monday()
    instances = await habit_inst_repo.list_recent_for_habit(habit.id, reference, 3)
    ev = evaluate_penalty(instances, habit.frequency_per_week)
    if ev is None or _already_decided(habit, reference):
        raise ApiError(
            ErrorCode.HABIT_PENALTY_NOT_ELIGIBLE,
            "지금은 빈도 재설계를 제안할 조건이 아니에요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    previous = habit.frequency_per_week
    await habit_repo.apply_penalty(
        habit, new_frequency=ev.suggested_frequency, decided_at=now_kst()
    )
    await session.commit()

    return HabitPenaltyAcceptResponse(
        habit_id=f"{_HABIT_PREFIX}{habit.id}",
        previous_frequency=previous,
        new_frequency=ev.suggested_frequency,
        message=f"주 {previous}회에서 {ev.suggested_frequency}회로 조정했어요. 이 리듬으로 가봐요.",
    )
