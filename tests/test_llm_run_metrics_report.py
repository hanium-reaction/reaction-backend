"""LLM 시스템 지표 리포트(L1-4 예비 실행)의 순수 계산 고정.

`_percentile`/`_summarize` 는 DB 없이 도는 순수 함수라 여기서 직접 검증한다. `_fetch_runs`
(SQL) 는 대상이 아니다 — 이 파일의 목적은 "집계 로직이 맞는가"이지 "쿼리가 맞는가"가 아니다.

가드로 반드시 넣는 것: fallback 이 아닌 행이나 reason 이 없는(마이그레이션 이전) 행이
fallback 원인 집계에 섞이면 안 된다 — 섞이면 분모(fallback_n)와 분자(reasons 합)가
어긋나는데, 그 어긋남이 조용히 통과하면 이 리포트의 존재 이유가 없어진다.
"""

from __future__ import annotations

from scripts.report_llm_run_metrics import RunRow, _percentile, _summarize


def _row(
    *,
    module: str = "recovery",
    prompt_version: str = "2",
    model: str = "gemini-3.5-flash-lite",
    latency_ms: int = 1000,
    tokens_in: int = 100,
    tokens_out: int = 50,
    cost_micro_usd: int = 500,
    grounding_requests: int = 0,
    success: bool = True,
    fell_back: bool = False,
    reason: str | None = None,
) -> RunRow:
    return RunRow(
        module=module,
        prompt_version=prompt_version,
        model=model,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_micro_usd=cost_micro_usd,
        grounding_requests=grounding_requests,
        success=success,
        fell_back=fell_back,
        reason=reason,
    )


# ── _percentile ───────────────────────────────────────────────────────────


def test_percentile_empty_is_zero() -> None:
    assert _percentile([], 95) == 0.0


def test_percentile_single_value_returns_that_value() -> None:
    assert _percentile([42], 50) == 42.0
    assert _percentile([42], 95) == 42.0


def test_percentile_p50_of_ten_values() -> None:
    values = list(range(1, 11))  # 1..10
    p50 = _percentile(values, 50)
    assert p50 in values  # nearest-rank — 원본값 중 하나여야 한다
    assert 4 <= p50 <= 7  # 대략 중앙


def test_percentile_p95_is_never_below_p50() -> None:
    values = [10, 20, 30, 400, 500, 5000, 10, 20, 15, 3000]
    assert _percentile(values, 95) >= _percentile(values, 50)


def test_percentile_ignores_input_order() -> None:
    values = [500, 100, 300, 200, 400]
    assert _percentile(values, 50) == _percentile(sorted(values), 50)


# ── _summarize — 기본 집계 ────────────────────────────────────────────────


def test_summarize_empty_rows() -> None:
    s = _summarize([])
    assert s.count == 0
    assert s.fallback_rate == 0.0
    assert s.p50_latency_ms == 0.0
    assert s.fallback_reasons == {}


def test_summarize_counts_success_and_fallback_separately() -> None:
    rows = [
        _row(success=True, fell_back=False),
        _row(success=True, fell_back=False),
        _row(success=False, fell_back=True, reason="timeout"),
    ]
    s = _summarize(rows)
    assert s.count == 3
    assert s.success_n == 2
    assert s.fallback_n == 1
    assert s.fallback_rate == 1 / 3


def test_summarize_sums_tokens_and_cost() -> None:
    rows = [
        _row(tokens_in=100, tokens_out=50, cost_micro_usd=500),
        _row(tokens_in=200, tokens_out=80, cost_micro_usd=900),
    ]
    s = _summarize(rows)
    assert s.total_tokens_in == 300
    assert s.total_tokens_out == 130
    assert s.total_cost_micro_usd == 1400


# ── _summarize — fallback 원인 분해 (가드) ────────────────────────────────


def test_summarize_fallback_reasons_only_counts_fallback_rows() -> None:
    """success=True 행은 reason 이 있어도(정상적으로는 없지만) 원인 집계에 안 들어간다."""
    rows = [
        _row(success=False, fell_back=True, reason="timeout"),
        _row(success=False, fell_back=True, reason="validation"),
        _row(success=True, fell_back=False, reason=None),
    ]
    s = _summarize(rows)
    assert s.fallback_reasons == {"timeout": 1, "validation": 1}
    assert sum(s.fallback_reasons.values()) == s.fallback_n


def test_summarize_fallback_without_reason_is_excluded_not_counted_as_none() -> None:
    """마이그레이션 이전 행(reason 컬럼 없던 시절) — fallback 인데 reason=None.

    분모(fallback_n)에는 잡히되, 원인 분해 표에는 안 나가야 한다(None 이라는 '원인'은
    없다) — 이 가드가 없으면 나중에 reason 컬럼이 채워지기 시작했을 때만 원인별 %가
    자연스러워 보여서, 지금 당장 옛 데이터가 섞여도 아무도 눈치 못 챈다.
    """
    rows = [
        _row(success=False, fell_back=True, reason=None),
        _row(success=False, fell_back=True, reason="timeout"),
    ]
    s = _summarize(rows)
    assert s.fallback_n == 2
    assert s.fallback_reasons == {"timeout": 1}
    assert sum(s.fallback_reasons.values()) != s.fallback_n  # 이 어긋남 자체가 의도다


def test_summarize_multiple_reasons_are_tallied_independently() -> None:
    rows = [
        _row(fell_back=True, reason="timeout"),
        _row(fell_back=True, reason="timeout"),
        _row(fell_back=True, reason="banned"),
        _row(fell_back=True, reason="rate_limited"),
    ]
    s = _summarize(rows)
    assert s.fallback_reasons == {"timeout": 2, "banned": 1, "rate_limited": 1}
