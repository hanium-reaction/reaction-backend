"""GET /policy-snapshot/current (#83 §14) — 현재 활성 정책 스냅샷."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reaction_backend.db.models.policy_snapshot import PolicySnapshot
from reaction_backend.schemas.common import now_kst
from tests.conftest import DEMO_USER_UUID, FakePolicySnapshotRepo


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
