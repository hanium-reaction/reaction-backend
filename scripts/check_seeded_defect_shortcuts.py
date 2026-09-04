"""심은 결함이 검토기에게 **의미 판단 없는 지름길**을 주는지 검사한다 (L1-7B).

`tests/test_golden_first_plan_cases.py` 의 두 지름길 테스트와 **같은 판정**을 pytest 없이
돌린다. 결함을 쓰는 사람이 자기 산출물을 직접 검사할 수 있어야 하기 때문이다 —
루브릭 작성자가 눈으로 걸러 주면 그 과정이 감춘 기준을 산출물로 흘려보낸다(rejection
sampling). 판정은 사람이 아니라 이 스크립트가 한다.

실행:
    uv run python scripts/check_seeded_defect_shortcuts.py
    uv run python scripts/check_seeded_defect_shortcuts.py --verbose   # 통과한 것도 표시

**종료 코드 0 은 "지름길이 없다" 가 아니라 "새 지름길이 없다" 는 뜻이다.** 아래 래칫 참조.
결함 파일을 고칠 때마다 `build_golden_first_plan_cases.py` 로 골든셋을 다시 만든 뒤 돌린다
(안 만들고 돌리면 낡은 JSONL 을 검사하게 되므로, 그 상태는 종료 코드 2 로 막는다).

## 무엇을 보는가

세 층이다. 자세한 사유는 `tests/test_golden_first_plan_cases.py` §6 주석.

1. **정확히 맞출 수 있는 값** — 카테고리·분량·앵커·목록 위치·숫자/영문 포함·항목 수.
   한 결함 유형 안에서 easy 와 boundary 가 **같아야** 한다.
2. **연속형 값** — 제목/`first_step` 길이, 토큰 수, 목표 제목과의 토큰 겹침.
   한 결함 유형 안에서 easy 범위와 boundary 범위가 **겹쳐야** 한다.
3. **어휘** — 한쪽 레벨의 계획 **전부**에만 나오는 토큰이 있으면 안 된다. 검토기는 어느
   카드가 심긴 것인지 모르는 채로 계획 전체를 받으므로, 비채움 카드 텍스트 전부를 본다.

그리고 easy/boundary 를 가르지는 않지만 **M28(주입 지점 지목)의 정답 키를 노출**하는
경우를 따로 센다 — 심은 카드가 계획에서 유일한 비최빈 분량이면 `argmin(분량)` 이 곧 답이다.

## 왜 0 을 목표로 하지 않는가 (래칫)

2026-09-02 에 세 번 재의뢰하며 배운 것: **검사기가 세는 특징만 없애면 최적화가 안 세는
특징으로 흘러간다.** `category` 를 없앴더니 `다시` 가 생겼고, 그걸 없앴더니 `마저` 와
`SQL` 이 남았다 — 후자는 작성자가 같은 문구에서 숫자만 지우고 영문은 남긴 것이다
(검사기가 `first_step` 의 숫자만 세고 영문은 안 셌다).

특징을 아무리 늘려도 "안 재는 것" 은 항상 남고, 표본이 유형당 2 대 2 라 우연한 분리도
흔하다(연속형 신호면 대략 1/3). 그래서 네 번째 재의뢰 대신 **아는 것을 사유와 함께
등록하고 새로 생기는 것만 막는다.** 등록된 항목은 사라진 게 아니라 **M27 해석의 한계로
보고된다** (`eval/README.md` 「M27·M28 을 읽을 때」).

⚠️ 이 검사는 어느 쪽으로도 "우연이 아님" 을 증명하지 못한다. 지키는 것은 더 약한 것이다 —
**한 유형에 완전 분리자가 있으면 그 유형의 M27 을 "검토기가 의미를 판단한 결과" 라고
말할 수 없고, 이 스크립트는 그 말을 할 수 없게 되는 지점을 목록으로 만든다.**
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Windows 기본 콘솔은 cp949 라 `—`(em dash) 에서 print 가 UnicodeEncodeError 로 죽는다
# (2026-09-02 결함 작성자가 실측 보고). 통과·실패 경로 모두에 그 글자가 있어 어느 쪽으로도
# 죽었고, 예외는 `main()` 안에서 나므로 `sys.exit(main())` 이 실행되지 않아 **종료 코드 1**
# 로 끝났다 — 즉 CI 는 이걸 실패로 읽는다.
#
# ⚠️ 이 자리에 원래 "통과 경로에만 그 글자가 있어 종료 코드가 0 으로 남았고 CI 가 성공으로
# 읽을 수 있었다" 고 적었는데 **둘 다 거짓이다**(감사 4차, cp949 로 직접 재현). 버그는
# 조용하지 않고 시끄러웠다. 심각성을 과장한 서술을 그대로 두지 않는다.
# 출력 스트림을 UTF-8 로 강제한다 — 진단 문구 때문에 검사가 죽으면 안 된다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = _ROOT / "eval" / "golden_first_plan_cases.jsonl"
DEFECTS_PATH = _ROOT / "eval" / "first_plan_seeded_defects.json"

# ── 알려진 지름길 기준선 (래칫) ────────────────────────────────────────────
#
# **이 검사는 0 을 목표로 하지 않는다.** 2026-09-02 에 세 번 재의뢰하며 배운 것: 검사기가
# 세는 특징만 없애면 최적화가 안 세는 특징으로 흘러간다. 실제로 `category` 를 없앴더니
# `다시` 가 생겼고, 그걸 없앴더니 `마저`·`SQL` 이 남았다. 특징을 아무리 늘려도 "안 재는 것"
# 은 항상 남고, 표본이 유형당 2 대 2 라 우연한 분리도 흔하다.
#
# 그래서 네 번째 재의뢰 대신 **아는 것을 등록하고 새로 생기는 것만 막는다.** 등록된 항목은
# 사라진 게 아니라 **M27 해석의 한계로 보고된다** (`eval/README.md` 「M27 을 읽을 때」).
# 목록이 줄면 좋고, 늘면 그 커밋이 새 지름길을 만든 것이다.
KNOWN_SHORTCUTS: dict[str, str] = {
    "D1/control-portfolio-long: first_step_has_latin": (
        "easy 의 first_step 에 `README` 가 있고 boundary 에는 영문이 없다. "
        "3차 재의뢰가 `title_has_latin` 만 보던 검사기를 통과하며 남긴 것"
    ),
    "D4/control-cert-standard: first_step_has_latin": (
        "boundary 의 `SQL 기출문제집 펼치기` 에만 영문이 있다. 작성자가 같은 문구에서 "
        "**숫자는 지우고 영문은 남겼다** — 검사기가 숫자만 세고 있었기 때문"
    ),
    "D2/control-cert-standard: target_position": (
        "easy 는 목록 3번째, boundary 는 0번째. `after_anchor` 는 insert_item 에만 있어서 "
        "replace_* 8건이 위치 검사 밖이었다"
    ),
    "D2/control-portfolio-long: target_position": "〃 (2 vs 0)",
    "D3/control-cert-standard: target_position": "〃 (2 vs 0)",
    "D3/control-portfolio-long: target_position": "〃 (2 vs 1)",
    "D4/control-cert-standard: target_position": "〃 (0 vs 2)",
    "D4/control-portfolio-long: target_position": "〃 (0 vs 1)",
    # 어휘 키는 토큰·방향까지 포함한다 — `_key_of` 주석 참조.
    "D2: lexical easy ['마저']": (
        "`마저` 가 easy 두 계획에만 있고 무결함 대조군 12건에는 **0건**. 채움 카드를 넣고 "
        "다시 세도 0건이라 **오탐 없는 완전 규칙**이다 — 남은 것 중 유일하게 진짜다"
    ),
}


def _injected_text(case: dict) -> tuple[str, str]:
    """이 케이스가 심은 제목과 first_step. 연산마다 어디에 들어있는지가 다르다."""
    op = case["seeded"]["operation"]
    kind = op["op"]
    if kind == "replace_title":
        return op["value"], ""
    if kind == "replace_first_step":
        return "", op["value"]
    if kind == "insert_item":
        return op["title"], op["first_step"]
    return "", ""  # swap_order 는 문구를 안 바꾼다


_FILLER_NODE_PREFIX = "tmp-continue-"


def _non_filler_items(case: dict) -> list[dict]:
    return [
        a for a in case["plan"]["action_items"] if not a["node_id"].startswith(_FILLER_NODE_PREFIX)
    ]


def _target_position(case: dict) -> int:
    """심은/건드린 카드가 항목 목록에서 몇 번째인가 — `after_anchor` 의 replace_* 판."""
    targets = set(case["seeded"]["target_node_ids"])
    for i, item in enumerate(case["plan"]["action_items"]):
        if item["node_id"] in targets:
            return i
    return -1


def _is_unique_non_modal_minutes(case: dict) -> bool:
    """심은 카드가 계획에서 **유일하게 최빈값이 아닌 분량**인가.

    참이면 `argmin`/`argmax` 하나로 M28(주입 지점 지목)의 정답 키가 나온다 — 내용을 한
    글자도 안 읽고. `e92287b` 이 `xN` id 를 지운 이유와 같은 부류의 누출이다.
    """
    minutes = [a["estimated_minutes"] for a in case["plan"]["action_items"]]
    if not minutes:
        return False
    modal = Counter(minutes).most_common(1)[0][0]
    odd = [a for a in case["plan"]["action_items"] if a["estimated_minutes"] != modal]
    return len(odd) == 1 and odd[0]["node_id"] in set(case["seeded"]["target_node_ids"])


def m28_leaked_case_ids(cases: list[dict]) -> list[str]:
    """M28b(위치 지목)의 정답 키가 **분량 하나로 새는** 케이스 id.

    `argmin(분량)` 만으로 심은 카드를 짚을 수 있으므로, 이 케이스들에서 검토기가 위치를
    맞혀도 "내용을 읽고 짚었다" 는 증거가 못 된다. L1-7B v4 하네스가 M28b **분모에서
    제외**하는 목록이고, 제외 사실과 대상 id 를 결과에 함께 출력한다.

    ⚠️ 하네스가 이 목록을 **자기가 다시 계산하지 않는다.** 두 곳에 두면 갈린다 —
    `eval/README.md` 「M27·M28 을 읽을 때」가 인용하는 숫자도 이 함수 하나에서 나온다.
    """
    return [
        c["case_id"]
        for c in cases
        if c.get("block") == "seeded_defect" and _is_unique_non_modal_minutes(c)
    ]


def exact_features(case: dict) -> dict[str, object]:
    op = case["seeded"]["operation"]
    title, first_step = _injected_text(case)
    return {
        "category": op.get("category"),
        "estimated_minutes": op.get("estimated_minutes"),
        "after_anchor": op.get("after"),
        "title_has_digit": any(c.isdigit() for c in title),
        "first_step_has_digit": any(c.isdigit() for c in first_step),
        "title_has_latin": any("a" <= c.lower() <= "z" for c in title),
        # ⚠️ `first_step_has_latin` 은 2026-09-02 감사 4차가 찾은 **빈틈**이다. 위 세 개는
        # 있는데 이 대칭짝만 없어서, 재의뢰 작성자가 `d4-boundary-cert` 의 first_step 에서
        # **숫자는 지우고 `SQL` 은 남겼다**. 검사하는 것만 지운 것이다.
        "first_step_has_latin": any("a" <= c.lower() <= "z" for c in first_step),
        # 심은 카드의 목록 위치. `after_anchor` 는 insert_item 에만 있어서 replace_* 8건이
        # 위치 검사에서 통째로 빠져 있었다.
        "target_position": _target_position(case),
        "item_count": len(case["plan"]["action_items"]),
    }


def range_features(case: dict, goal_title: str) -> dict[str, int]:
    title, first_step = _injected_text(case)
    return {
        "title_len": len(title),
        "first_step_len": len(first_step),
        "title_tokens": len(title.split()),
        "goal_token_overlap": len(set(title.split()) & set(goal_title.split())),
    }


def lexical_offenders(seeded: list[dict]) -> list[str]:
    """한 결함 유형 안에서 **한쪽 레벨에만** 나타나는 어휘가 있는가.

    `계획에 '마저' 가 있으면 D2 결함` 같은 한 줄짜리 문자열 규칙으로 그 유형의 M27 을
    통째로 얻을 수 있으면, 그 수치는 "검토기가 의미를 판단했다" 는 뜻이 아니다.

    ⚠️ **채움 카드를 포함한 계획 전부**를 본다. 심은 카드만 보면 안 되는 것은 물론이고,
    채움(`tmp-continue-*`)을 빼도 안 된다 — `_review_variables` 가 검토기에게
    `[a.model_dump() for a in gp.action_items]` 를 **통째로** 넘기기 때문이다.

    ⚠️ 2026-09-02 감사 5차까지 이 함수는 채움을 **뺐고**, 그 탓에 두 개의 가짜 분리자를
    보고했다. 채움 제목이 `{목표} N회차` 라서 `개발 포트폴리오 **사이트** 완성 5회차` 가
    통째로 빠졌고, 채움 `first_step` 이 전부 `…시작**하기**` 라 `하기` 접미도 빠졌다.
    실제 범위로 다시 세면 `사이트` 는 무결함 대조군 1/12·심은결함 11/20 에서, `하기` 는
    대조군 **12/12** 에서 발화한다 — 둘 다 분리자가 아니다. 진짜는 `마저` 하나뿐이다
    (두 범위 모두 대조군 0/12).
    """
    by_defect: dict[str, dict[str, list[set[str]]]] = {}
    for case in seeded:
        tokens: set[str] = set()
        for item in case["plan"]["action_items"]:
            tokens.update(item["title"].split())
            tokens.update(item["first_step"].split())
            if item["first_step"].endswith("하기"):
                tokens.add("<first_step-suffix:하기>")
        by_defect.setdefault(case["seeded"]["defect"], {}).setdefault(
            case["seeded"]["level"], []
        ).append(tokens)

    offenders: list[str] = []
    for defect, levels in sorted(by_defect.items()):
        easy, boundary = levels.get("easy", []), levels.get("boundary", [])
        if not easy or not boundary:
            continue
        for name, present, absent in (("easy", easy, boundary), ("boundary", boundary, easy)):
            only = set.intersection(*present) - set.union(*absent)
            if only:
                offenders.append(
                    f"{defect}: {sorted(only)[:6]} 가 {name} **전부**에만 나온다 "
                    "— 문자열 규칙 하나로 그 유형이 갈린다"
                )
    return offenders


def find_offenders(cases: list[dict]) -> tuple[list[str], list[str], list[str]]:
    seeded = [c for c in cases if c.get("block") == "seeded_defect"]
    goal_by_case = {c["case_id"]: c["interview"]["goal"]["title"] for c in cases}

    exact: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    ranges: dict[tuple[str, str], dict[str, list[int]]] = {}
    for case in seeded:
        defect = case["seeded"]["defect"]
        level = case["seeded"]["level"]
        exact.setdefault((defect, case["seeded"]["base_plan"]), {})[level] = exact_features(case)
        for feature, value in range_features(case, goal_by_case[case["case_id"]]).items():
            ranges.setdefault((defect, feature), {}).setdefault(level, []).append(value)

    exact_bad = [
        f"{defect}/{base}: {feature} = {pair['easy'][feature]!r}(easy) vs "
        f"{pair['boundary'][feature]!r}(boundary)"
        for (defect, base), pair in sorted(exact.items())
        if not {"easy", "boundary"} - set(pair)
        for feature in pair["easy"]
        if pair["easy"][feature] != pair["boundary"][feature]
    ]

    range_bad: list[str] = []
    for (defect, feature), levels in sorted(ranges.items()):
        easy, boundary = levels.get("easy", []), levels.get("boundary", [])
        if not easy or not boundary:
            continue
        if max(easy) < min(boundary) or max(boundary) < min(easy):
            range_bad.append(
                f"{defect}: {feature} easy={sorted(easy)} boundary={sorted(boundary)} "
                "— 범위가 안 겹쳐 임계값 하나로 갈린다"
            )
    return exact_bad, range_bad, lexical_offenders(seeded)


def _key_of(line: str) -> str:
    """기준선 대조용 키.

    수치형 특징은 `그룹: 특징` 까지만 자른다 — 값은 바뀔 수 있고, 값이 바뀌어도 "그 자리에
    분리자가 있다" 는 사실은 그대로이기 때문이다.

    ⚠️ **어휘는 다르다. 토큰과 방향까지 키에 넣는다.** 2026-09-02 감사 5차가 실증한 결함:
    `D2: lexical` 로 뭉뚱그리면 `마저` 를 다른 낱말로 바꿔도 같은 키라 **새 지름길이
    조용히 통과**한다(실측: `마저`→`총력전` 으로 바꿔도 NEW=0, exit 0). 게다가 등록된
    사유는 옛 낱말을 가리켜 **거짓 설명**이 붙는다. 어휘가 등록된 유형(D2·D4·D5)이 셋이라
    5개 중 3개가 무방비였다 — 래칫이 장식이 되는 자리다.
    """
    if "가 " in line and "에만 나온다" in line:
        defect = line.split(":")[0]
        tokens = line[line.index("[") : line.index("]") + 1] if "[" in line else "[]"
        side = "easy" if " easy **전부**에만" in line else "boundary"
        return f"{defect}: lexical {side} {tokens}"
    return line.split(" = ")[0].split(" easy=")[0].strip()


def split_by_baseline(offenders: list[str]) -> tuple[list[str], list[str]]:
    """(기준선에 없는 새 것, 이미 아는 것)."""
    new, known = [], []
    for line in offenders:
        (known if _key_of(line) in KNOWN_SHORTCUTS else new).append(line)
    return new, known


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="통과한 항목도 표시")
    args = parser.parse_args()

    if not CASES_PATH.exists():
        print(f"[X] {CASES_PATH} 이 없다 — build_golden_first_plan_cases.py 를 먼저 돌릴 것")
        return 2

    # 결함 파일만 고치고 골든셋을 다시 안 만들면 **낡은 JSONL** 을 검사하게 된다. 이
    # 검사기는 게이트로 쓰이므로(작성자가 스스로 돌린다) 그 상태를 통과시키면 안 된다.
    if DEFECTS_PATH.exists() and DEFECTS_PATH.stat().st_mtime > CASES_PATH.stat().st_mtime:
        print(
            "[X] 결함 파일이 골든셋보다 새롭다 — 낡은 JSONL 을 검사할 뻔했다.\n"
            "    uv run python scripts/build_golden_first_plan_cases.py 를 먼저 돌릴 것."
        )
        return 2

    cases = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seeded_count = sum(1 for c in cases if c.get("block") == "seeded_defect")
    if seeded_count == 0:
        print("[X] `seeded_defect` 케이스가 0 건이다 — 빈 데이터를 통과시킬 뻔했다.")
        return 2

    exact_bad, range_bad, lexical_bad = find_offenders(cases)
    new, known = split_by_baseline(exact_bad + range_bad + lexical_bad)

    if new:
        print("[X] **새** 지름길이 생겼다 — 이 커밋이 만든 것이다:")
        for line in new:
            print(f"    {line}")

    if known:
        print(f"[!] 이미 아는 지름길 {len(known)} 건 (기준선 등록, M27 해석의 한계로 보고):")
        for line in sorted(known):
            print(f"    {line}")
            print(f"        └ {KNOWN_SHORTCUTS[_key_of(line)]}")
    elif args.verbose:
        print("[O] 기준선에 등록된 지름길이 전부 사라졌다 — KNOWN_SHORTCUTS 를 비울 것")

    # M28 누출은 easy/boundary 분리자가 **아니다** — 양쪽 다 참이라 위 검사에 안 걸린다.
    # 가르는 게 아니라 **정답 키의 위치**를 알려주는 것이라 따로 센다.
    m28 = m28_leaked_case_ids(cases)
    if m28:
        print(
            f"\n[!] M28 위치 누출 {len(m28)} 건 — 심은 카드가 계획에서 유일한 비최빈 분량이라 "
            "`argmin(분량)` 하나로 정답 키가 나온다:"
        )
        print(f"    {', '.join(m28)}")

    stale = sorted(set(KNOWN_SHORTCUTS) - {_key_of(line) for line in known})
    if stale:
        print(f"[!] 기준선에 있는데 이제 안 잡히는 항목 {len(stale)} 건 — 목록에서 지울 것:")
        for key in stale:
            print(f"    {key}")

    if new:
        print(f"\n새 지름길 {len(new)} 건. 결함 파일을 고치고 골든셋을 다시 만들 것.")
        return 1

    print(f"\n[O] 새 지름길 없음 (아는 것 {len(known)} 건은 기준선에 등록돼 있다).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
