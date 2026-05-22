"""Onboarding 도메인 스키마 (api-contract §3) — S01–S08."""

from __future__ import annotations

from reaction_backend.schemas.common import CamelModel


class OnboardingStatus(CamelModel):
    """GET /onboarding/status 응답 — 현재 상태 + 다음 화면 hint."""

    current_state: str
    suggested_next_screen: str
