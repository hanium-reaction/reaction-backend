"""GET /policy-snapshot/current (#83 §14) — 현재 활성 정책 스냅샷."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.models.policy_snapshot import PolicySnapshot
from reaction_backend.schemas.common import now_kst
from tests.conftest import DEMO_USER_UUID, FakePolicySnapshotRepo, FakeReviewRepo


def _seed_snapshot(repo: FakePolicySnapshotRepo, *, version: int = 3) -> PolicySnapshot:
    s = PolicySnapshot()
    s.user_id = DEMO_USER_UUID
    s.version = version
    s.is_active = True
    s.behavioral_profile = {"attention_span": 25, "energy_cycle": "morning"}
    s.execution_constraints = {"daily_max_load": 180, "buffer_ratio": 0.2}
    s.interaction_style = {"recovery_tone": "gentle", "suggestion_style": "soft"}
    s.recovery_policy = {"min_recovery_step_minutes": 10}
    s.source = "llm"
    s.reason_for_update = "주간 KPI 반영"
    s.valid_from = now_kst()
    s.valid_to = None
    repo.seed(s)
    return s


def test_current_404_when_no_active_snapshot(client: TestClient) -> None:
    """활성 스냅샷이 없으면 404 — FE 는 카운트-only 폴백."""
    resp = client.get("/policy-snapshot/current")
    assert resp.status_code == 404
    assert resp.json()["code"] == "POLICY_NOT_FOUND"


def test_current_returns_active_snapshot(
    client: TestClient,
    fake_policy_snapshot_repo: FakePolicySnapshotRepo,
) -> None:
    """활성 스냅샷의 4 영역을 그대로 노출한다."""
    _seed_snapshot(fake_policy_snapshot_repo, version=3)

    resp = client.get("/policy-snapshot/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 3
    assert body["source"] == "llm"
    # JSONB 값 내부 키는 그대로(모델 필드명만 camel 화 — behavioral_profile → behavioralProfile)
    assert body["behavioralProfile"]["attention_span"] == 25
    assert body["executionConstraints"]["daily_max_load"] == 180
    assert body["interactionStyle"]["recovery_tone"] == "gentle"
    assert body["recoveryPolicy"]["min_recovery_step_minutes"] == 10
    assert body["reasonForUpdate"] == "주간 KPI 반영"
    assert body["validFrom"].endswith("+09:00")  # KST


def test_current_picks_latest_active_version(
    client: TestClient,
    fake_policy_snapshot_repo: FakePolicySnapshotRepo,
) -> None:
    """활성 스냅샷이 여러 개면 최신 버전을 반환한다."""
    _seed_snapshot(fake_policy_snapshot_repo, version=1)
    _seed_snapshot(fake_policy_snapshot_repo, version=5)
    body = client.get("/policy-snapshot/current").json()
    assert body["version"] == 5


# ── #168 생산 경로 (history / preview-update / apply / rollback) ─────────────
#
# 이전에는 라우트가 `current` 하나뿐이었고 `policy_snapshots` 에 행을 넣는 코드가 레포
# 전체에 0곳이라 그 endpoint 가 **항상 404** 였다. api-contract §14 는 5개를 계약으로
# 적어둔 상태였다(계약-구현 불일치).


def test_history_is_empty_not_404_when_no_snapshots(client: TestClient) -> None:
    """이력은 '아직 없음' 이 정상 상태 — `current` 와 달리 404 를 내지 않는다."""
    resp = client.get("/policy-snapshot/history")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_history_lists_newest_first_including_inactive(
    client: TestClient, fake_policy_snapshot_repo: FakePolicySnapshotRepo
) -> None:
    old = _seed_snapshot(fake_policy_snapshot_repo, version=1)
    old.is_active = False
    old.valid_to = now_kst()
    _seed_snapshot(fake_policy_snapshot_repo, version=2)

    items = client.get("/policy-snapshot/history").json()["items"]
    assert [i["version"] for i in items] == [2, 1]
    assert [i["isActive"] for i in items] == [True, False]
    assert items[1]["validTo"] is not None


def test_preview_builds_v1_from_profile_when_no_snapshot_exists(client: TestClient) -> None:
    """스냅샷 0개(=지금 모든 사용자)여도 후보가 나와야 한다 — 이게 첫 진입 경로다."""
    resp = client.post("/policy-snapshot/preview-update")
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseVersion"] is None
    assert body["nextVersion"] == 1
    assert body["isDraft"] is True, "HITL — 승인 전이다"
    assert body["aiSource"] == "rule", "LLM 이 아니라 룰 산출물"
    assert body["executionConstraints"]["daily_max_load"] == 180


def test_preview_reflects_weekly_kpi(
    client: TestClient,
    fake_policy_snapshot_repo: FakePolicySnapshotRepo,
    fake_review_repo: FakeReviewRepo,
) -> None:
    """주간 KPI 가 실제로 후보에 반영되고, 근거가 숫자로 실린다."""
    _seed_snapshot(fake_policy_snapshot_repo, version=2)
    summary = PeriodSummary()
    summary.user_id = DEMO_USER_UUID
    summary.period_type = "weekly"
    summary.start_date = date(2026, 8, 17)
    summary.end_date = date(2026, 8, 23)
    summary.adherence_rate = Decimal("0.40")
    fake_review_repo.seed_summary(summary)

    body = client.post("/policy-snapshot/preview-update").json()
    assert body["baseVersion"] == 2
    assert body["nextVersion"] == 3
    change = next(c for c in body["changes"] if c["field"] == "daily_max_load")
    assert change["before"] == 180
    assert change["after"] == 144
    assert "40%" in change["why"]


def test_preview_does_not_persist_anything(
    client: TestClient, fake_policy_snapshot_repo: FakePolicySnapshotRepo
) -> None:
    """AGENTS §1 자동 적용 금지 — 미리보기는 읽기 전용이다."""
    client.post("/policy-snapshot/preview-update")
    assert fake_policy_snapshot_repo.all_of(DEMO_USER_UUID) == []


def _apply_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "behavioralProfile": {"attention_span": 30},
        "executionConstraints": {"daily_max_load": 144},
        "interactionStyle": {"recovery_tone": "gentle"},
        "recoveryPolicy": {"min_recovery_step_minutes": 10},
        "source": "rule",
        "reasonForUpdate": "주간 KPI 반영",
    }
    body.update(overrides)
    return body


def test_apply_creates_the_first_version(
    client: TestClient, fake_policy_snapshot_repo: FakePolicySnapshotRepo
) -> None:
    resp = client.post("/policy-snapshot/apply", json=_apply_body())
    assert resp.status_code == 201
    body = resp.json()
    assert body["version"] == 1
    assert body["source"] == "rule"
    assert body["executionConstraints"]["daily_max_load"] == 144

    stored = fake_policy_snapshot_repo.all_of(DEMO_USER_UUID)
    assert len(stored) == 1
    assert stored[0].is_active is True


def test_apply_closes_the_previous_active_version(
    client: TestClient, fake_policy_snapshot_repo: FakePolicySnapshotRepo
) -> None:
    """append-only — 이전 행은 지우지 않고 is_active=false + valid_to 로 닫는다."""
    previous = _seed_snapshot(fake_policy_snapshot_repo, version=1)

    resp = client.post("/policy-snapshot/apply", json=_apply_body())
    assert resp.status_code == 201
    assert resp.json()["version"] == 2

    assert previous.is_active is False
    assert previous.valid_to is not None
    assert len(fake_policy_snapshot_repo.all_of(DEMO_USER_UUID)) == 2, "이전 행이 지워졌다"

    active = client.get("/policy-snapshot/current").json()
    assert active["version"] == 2


def test_apply_records_user_edits_as_user_manual(
    client: TestClient, fake_policy_snapshot_repo: FakePolicySnapshotRepo
) -> None:
    """사용자가 값을 고쳤는지는 화면이 안다 — `/recovery/decisions` 의 accepted/edited 관례."""
    resp = client.post("/policy-snapshot/apply", json=_apply_body(source="user_manual"))
    assert resp.json()["source"] == "user_manual"


def test_apply_rejects_unknown_source(client: TestClient) -> None:
    resp = client.post("/policy-snapshot/apply", json=_apply_body(source="llm"))
    assert resp.status_code == 422


def test_rollback_copies_old_values_into_a_new_version(
    client: TestClient, fake_policy_snapshot_repo: FakePolicySnapshotRepo
) -> None:
    """옛 행의 is_active 를 다시 켜지 않는다 — 그러면 '언제 롤백했나' 가 이력에서 사라진다."""
    v1 = _seed_snapshot(fake_policy_snapshot_repo, version=1)
    v1.execution_constraints = {"daily_max_load": 240}
    v1.is_active = False
    _seed_snapshot(fake_policy_snapshot_repo, version=2)

    resp = client.post("/policy-snapshot/rollback/1")
    assert resp.status_code == 201
    body = resp.json()
    assert body["version"] == 3, "롤백도 새 버전이다 (append-only)"
    assert body["executionConstraints"]["daily_max_load"] == 240, "v1 의 값이 복원돼야 한다"
    assert body["source"] == "user_manual"
    assert "v1" in (body["reasonForUpdate"] or "")

    versions = [s.version for s in fake_policy_snapshot_repo.all_of(DEMO_USER_UUID)]
    assert sorted(versions) == [1, 2, 3], "옛 버전이 사라지면 안 된다"


def test_rollback_to_missing_version_is_404(client: TestClient) -> None:
    resp = client.post("/policy-snapshot/rollback/9")
    assert resp.status_code == 404
    assert resp.json()["code"] == "POLICY_NOT_FOUND"


def test_rollback_to_the_active_version_is_409(
    client: TestClient, fake_policy_snapshot_repo: FakePolicySnapshotRepo
) -> None:
    """같은 값을 한 번 더 쌓을 이유가 없다."""
    _seed_snapshot(fake_policy_snapshot_repo, version=2)
    resp = client.post("/policy-snapshot/rollback/2")
    assert resp.status_code == 409
    assert resp.json()["code"] == "POLICY_ALREADY_ACTIVE"


def test_all_policy_routes_require_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.get("/policy-snapshot/history").status_code == 401
    assert unauthed_client.post("/policy-snapshot/preview-update").status_code == 401
    assert unauthed_client.post("/policy-snapshot/apply", json=_apply_body()).status_code == 401
    assert unauthed_client.post("/policy-snapshot/rollback/1").status_code == 401
