"""M33 challenge stratum 16건 생성 — **입력 조건만으로 결정된다.**

`docs/experiments/m33-3arm-design.md` §3 의 격자를 기계로 만든다. 어떤 케이스가 실패했는지
**모르는 상태에서도 같은 16건**이 나온다 — 이 파일이 그 재현성이다.

## ⚠️ 초판 설계의 축 하나가 틀렸다 (2026-09-04 정정)

초판은 활동창 축을 **"≤ 6시간"** 이라는 **절대 시간**으로 적었다. 실측하니 **그 축은
변별력이 0** 이었다:

    활동창 09-23시(14h) → 20세션 전부 배치
    활동창 21-22시(1h)  → 20세션 전부 배치   ← 24분 세션이 하루 하나씩 들어간다

**활동창은 세션 길이보다 짧을 때만 물린다.**

    120분 세션 + 60분 창  → 20/20 미배치, M21 실패
    120분 세션 + 180분 창 → 20/20 배치,   M21 통과

→ 축을 **`활동창 < 세션 길이`** 라는 **상대 조건**으로 다시 정의하고, 그것이 성립하는
정확한 값을 아래에 고정한다. `>=`·`<=` 를 남기지 않는다.

## 축 네 개 — **양쪽 값이 전부 정확한 수**다

| 축 | 어려움 | 쉬움 | 왜 그 값인가 |
|---|---|---|---|
| **경계** | `session_length = 120` (집중용량과 **같다**) | `session_length = 90` | ③층 클램프가 여유 없이 걸린다(120분 사고의 조건) |
| **빈도** | `frequency_per_week = 7` | `frequency_per_week = 3` | 케이던스를 맞출 날이 많아 M20 이 빡빡 |
| **활동창** | `21:00-22:00` (**60분 < 세션 120분**) | `09:00-23:00` (840분) | 세션이 창에 안 들어가야 M21 이 발화한다 |
| **마일스톤** | 2개 | 0개 | M23·M24 가 **적용된다**(일반 34건은 6건·4건뿐) |

**`focus_duration_min = 120` 으로 16건 전부 고정한다.** 그래야 "경계" 가 `session_length`
하나로만 갈리고, 활동창이 세션 길이보다 짧다는 조건이 성립한다.

**`weekly_hours` 는 파생값**이다 — `frequency × session_length ÷ 60`. 그래야 세션 길이가
빈도와 무관하게 일정해져 **빈도 축이 홀로 갈린다**(안 그러면 빈도를 올릴 때 세션이 짧아져
활동창 축과 섞인다).

## 실행

    uv run python scripts/build_challenge_stratum.py          # 생성 + 저장
    uv run python scripts/build_challenge_stratum.py --check  # 저장본과 대조만

산출 `eval/golden_challenge_stratum.jsonl` — **결정적이라 커밋한다**(일반 골든셋과 같은 관행).
"""

from __future__ import annotations

import argparse
import json
import sys
from itertools import product
from pathlib import Path
from typing import Any, Final

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_PATH = _ROOT / "eval" / "golden_challenge_stratum.jsonl"

# ⚠️ 전 케이스 공통 — 여기가 흔들리면 축이 서로 섞인다.
FOCUS_DURATION_MIN: Final = 120
"""집중 용량. **경계 축이 `session_length` 하나로만 갈리게** 이 값을 고정한다."""

# 축별 (어려움, 쉬움) — **양쪽 다 정확한 값**이다.
SESSION_LENGTH: Final = {"hard": 120, "easy": 90}
FREQUENCY: Final = {"hard": 7, "easy": 3}
ACTIVITY_WINDOW: Final = {"hard": ("21:00", "22:00"), "easy": ("09:00", "23:00")}
MILESTONE_COUNT: Final = {"hard": 2, "easy": 0}

AXES: Final = ("boundary", "frequency", "window", "milestone")

_GOAL: Final = {
    "title": "정보처리기사 실기 합격",
    "category": "career",
    "success_image": "실기 시험에 합격해 자격증을 받는다",
    "current_level": "필기는 통과했고 실기는 처음이다",
    "deadline_offset_days": 28,
    "approach_note": "기출 위주로 회독을 늘린다",
}
_MILESTONES: Final = [
    {"title": "기출 3개년 1회독", "summary": "출제 범위를 훑는다"},
    {"title": "약한 단원 집중 보강", "summary": "1회독에서 틀린 곳만 다시"},
]


def _case(combo: dict[str, str]) -> dict[str, Any]:
    """한 조합 → 골든셋 케이스. **조합만으로 전부 결정된다.**"""
    sess = SESSION_LENGTH[combo["boundary"]]
    freq = FREQUENCY[combo["frequency"]]
    start, end = ACTIVITY_WINDOW[combo["window"]]
    n_ms = MILESTONE_COUNT[combo["milestone"]]
    # 세션 길이를 빈도와 무관하게 일정하게 만든다 — 안 그러면 빈도 축이 활동창 축과 섞인다.
    weekly_hours = round(freq * sess / 60)
    tag = "".join(combo[a][0] for a in AXES)  # 예: hhhh / hehe
    return {
        "case_id": f"chal-{tag}",
        "kind": "decompose",
        "block": "challenge_stratum",
        "synthetic": True,
        "author": "build_challenge_stratum.py",
        "axes": dict(combo),
        "interview": {
            "role": "대학생",
            "season": "학기 중",
            "preferred_time": "저녁",
            "focus_duration_min": FOCUS_DURATION_MIN,
            "activity_start": start,
            "activity_end": end,
            "milestones": _MILESTONES[:n_ms],
            "milestone_cursor": 0,
            "goal": {
                **_GOAL,
                "session_length_min": sess,
                "frequency_per_week": freq,
                "weekly_hours": weekly_hours,
            },
        },
        "notes": (
            f"경계={combo['boundary']}({sess}분/{FOCUS_DURATION_MIN}분) · "
            f"빈도={combo['frequency']}({freq}회) · "
            f"활동창={combo['window']}({start}-{end}) · "
            f"마일스톤={combo['milestone']}({n_ms}개)"
        ),
    }


def build() -> list[dict[str, Any]]:
    """16건. 조합 순서가 고정이라 **몇 번 돌려도 같은 파일**이 나온다."""
    return [
        _case(dict(zip(AXES, levels, strict=True)))
        for levels in product(("easy", "hard"), repeat=len(AXES))
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description="M33 challenge stratum 16건 생성")
    ap.add_argument("--check", action="store_true", help="저장본과 대조만 (쓰지 않는다)")
    args = ap.parse_args()

    cases = build()
    text = "".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases)
    if args.check:
        if not OUT_PATH.exists():
            print(f"[!] {OUT_PATH.relative_to(_ROOT)} 가 없다")
            raise SystemExit(2)
        same = OUT_PATH.read_text(encoding="utf-8") == text
        print("[O] 저장본과 동일" if same else "[!] 저장본과 다르다 — 재생성 필요")
        raise SystemExit(0 if same else 1)

    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"{len(cases)}건 → {OUT_PATH.relative_to(_ROOT)}")
    for c in cases:
        print(f"  {c['case_id']:<12} {c['notes']}")


if __name__ == "__main__":
    main()
