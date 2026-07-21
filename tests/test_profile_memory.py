"""프로필 메모리 (#A-1·A-2) — 인터뷰 지속형 선호 → Policy Snapshot 레이어 영속 + 설정 편집.

3층: ① 매핑 순수 함수(한국어 칩→enum/버킷) ② GET/PATCH /settings/profile 라우트.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient

from reaction_backend.orchestrator import profile_memory as pm


def test_seed_slots_from_profile_reverses_editable_fields() -> None:
    """설정에서 수정 가능한 프로필 필드 → 재인터뷰 시드 슬롯값으로 역매핑(#reduce-reask)."""
    beh = cast(Any, SimpleNamespace(energy_cycle="evening", attention_span=45))
    inter = cast(Any, SimpleNamespace(recovery_tone="gentle"))
    seed = pm.seed_slots_from_profile(
        behavioral=beh,
        interaction=inter,
        focus_mode_prefs={"downscope_unit_min": 15, "rest_ok": False},
    )
    assert seed["time.peak_window"] == {"type": "chip", "values": ["저녁"]}
    assert seed["energy.focus_duration"] == {"type": "chip", "values": ["45분"]}
    assert seed["recovery.tone"] == {"type": "chip", "values": ["따뜻"]}
    assert seed["recovery.downscope_unit"] == {"type": "chip", "values": ["15분"]}
    assert seed["recovery.rest_ok"] == {"type": "chip", "values": ["아니오"]}
    # 활동창(preferred_*)은 설정 편집 대상이 아니라 프로필로 만들지 않는다 → 호출자가 원답 사용.
    assert "time.activity_window" not in seed


def test_seed_slots_from_profile_empty_when_absent() -> None:
    """프로필·focus_mode 가 없으면 빈 시드 → 오버레이가 지난 인터뷰 원답을 덮지 않는다."""
    assert pm.seed_slots_from_profile(behavioral=None, interaction=None, focus_mode_prefs={}) == {}


# ───────────────────────── 매핑 순수 함수 ─────────────────────────


def test_energy_cycle_from_peak() -> None:
    assert pm.energy_cycle_from_peak(["오전"]) == "morning"
    assert pm.energy_cycle_from_peak(["저녁", "오전"]) == "evening"  # 첫 값 기준
    assert pm.energy_cycle_from_peak(["변동"]) == "varies"
    assert pm.energy_cycle_from_peak([]) == "varies"
    assert pm.energy_cycle_from_peak(["없는칩"]) == "varies"  # 미지원 → 안전 폴백


def test_chunk_bucket() -> None:
    assert pm.chunk_bucket(None) == "30"
    assert pm.chunk_bucket(50) == "60"
    assert pm.chunk_bucket(90) == "90"
    assert pm.chunk_bucket(120) == "90"


def test_recovery_tone_enum() -> None:
    assert pm.recovery_tone_enum("따뜻") == "gentle"
    assert pm.recovery_tone_enum("담백") == "normal"
    assert pm.recovery_tone_enum("유머") == "encouraging"
    assert pm.recovery_tone_enum("모르는값") == "normal"  # 폴백


def test_recovery_speed_from_prefs() -> None:
    """회복 최소 단위 + 휴식 수용 → fast/medium/slow 파생."""
    assert pm.recovery_speed_from_prefs(10, True) == "fast"  # 작은 단위 + 휴식 OK
    assert pm.recovery_speed_from_prefs(5, True) == "fast"
    assert pm.recovery_speed_from_prefs(30, True) == "slow"  # 큰 단위만 가능
    assert pm.recovery_speed_from_prefs(45, False) == "slow"
    assert pm.recovery_speed_from_prefs(15, True) == "medium"
    assert pm.recovery_speed_from_prefs(10, False) == "medium"  # 휴식 거부 → fast 아님
    assert pm.recovery_speed_from_prefs(None, True) == "medium"


# ───────────────────────── GET/PATCH /settings/profile ─────────────────────────


def test_get_profile_empty_when_not_set(client: TestClient) -> None:
    """인터뷰가 아직 안 채웠으면 각 항목 null (행 미생성)."""
    resp = client.get("/settings/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["behavioral"] is None
    assert body["interaction"] is None


def test_patch_profile_creates_and_persists(client: TestClient) -> None:
    resp = client.patch(
        "/settings/profile",
        json={
            "energyCycle": "morning",
            "attentionSpan": 50,
            "timeChunkPreference": "60",
            "recoveryTone": "gentle",
            "reminderFrequency": "minimal",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["behavioral"]["energyCycle"] == "morning"
    assert body["behavioral"]["attentionSpan"] == 50
    assert body["behavioral"]["timeChunkPreference"] == "60"
    assert body["interaction"]["recoveryTone"] == "gentle"
    assert body["interaction"]["reminderFrequency"] == "minimal"

    # 재조회 시 유지 (영속)
    got = client.get("/settings/profile").json()
    assert got["behavioral"]["energyCycle"] == "morning"
    assert got["interaction"]["recoveryTone"] == "gentle"


def test_patch_profile_partial_keeps_others(client: TestClient) -> None:
    """지정 필드만 갱신 — 나머지는 유지."""
    client.patch("/settings/profile", json={"attentionSpan": 40, "recoveryTone": "encouraging"})
    resp = client.patch("/settings/profile", json={"energyCycle": "evening"})
    body = resp.json()
    assert body["behavioral"]["energyCycle"] == "evening"
    assert body["behavioral"]["attentionSpan"] == 40  # 유지
    assert body["interaction"]["recoveryTone"] == "encouraging"  # 유지


def test_patch_recovery_prefs_round_trip(client: TestClient) -> None:
    """회복 선호(downscopeUnitMin·restOk) → focus_mode_preferences 저장/조회."""
    resp = client.patch("/settings/profile", json={"downscopeUnitMin": 15, "restOk": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["downscopeUnitMin"] == 15
    assert body["restOk"] is False

    got = client.get("/settings/profile").json()
    assert got["downscopeUnitMin"] == 15
    assert got["restOk"] is False


def test_patch_profile_invalid_enum(client: TestClient) -> None:
    resp = client.patch("/settings/profile", json={"energyCycle": "bogus"})
    assert resp.status_code == 422


def test_profile_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.get("/settings/profile").status_code == 401


def test_patch_activity_window_round_trip(client: TestClient) -> None:
    """활동 시간대(계획 배치 창) 편집 → focus_mode_preferences 저장/조회 (#editable-activity-window)."""
    resp = client.patch(
        "/settings/profile", json={"activityStart": "06:00", "activityEnd": "24:00"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["activityStart"] == "06:00"
    assert body["activityEnd"] == "24:00"
    got = client.get("/settings/profile").json()
    assert got["activityStart"] == "06:00"
    assert got["activityEnd"] == "24:00"


def test_patch_activity_window_invalid(client: TestClient) -> None:
    assert client.patch("/settings/profile", json={"activityStart": "25:00"}).status_code == 422
