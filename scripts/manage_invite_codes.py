"""초대코드 발급/현황 조회 (#324) — 가입 게이트(`POST /auth/google`)가 소비하는 코드.

admin API 를 새로 만들지 않는다 — 이 레포의 다른 운영 작업(`db_seed_demo.py`,
`cancel_stale_plan_cards.py` 등)과 같은 관례로, EC2 self-hosted runner 에서
workflow_dispatch 로 실행하는 CLI 스크립트다. `--dry-run` 없이도 안전한 이유:
`create` 는 순수 INSERT(중복 코드는 DB unique 제약이 막음), `list` 는 SELECT 뿐이다.

실행:
  uv run python -m scripts.manage_invite_codes create --count 5 --note "Play 리뷰어"
  uv run python -m scripts.manage_invite_codes create --code REACTION-REVIEWER --note "Play 리뷰어"
  uv run python -m scripts.manage_invite_codes list
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string

from reaction_backend.config import get_settings
from reaction_backend.repositories.invite_code_repo import InviteCodeRepo, normalize_code

_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 8
# 혼동하기 쉬운 문자 제외 — 발급 코드를 사람이 직접 옮겨 적을 일이 있다(리뷰어 등).
_CONFUSING = set("0O1IL")


def _random_code() -> str:
    alphabet = [c for c in _CODE_ALPHABET if c not in _CONFUSING]
    return "".join(secrets.choice(alphabet) for _ in range(_CODE_LENGTH))


async def create(*, count: int, code: str | None, note: str | None) -> None:
    from reaction_backend.db.session import get_sessionmaker

    if code is not None and count != 1:
        raise SystemExit("--code 는 --count 1 과만 같이 쓸 수 있어요(직접 지정은 1개씩).")

    factory = get_sessionmaker()
    async with factory() as session:
        repo = InviteCodeRepo(session)
        created: list[str] = []
        for _ in range(count):
            raw = code or _random_code()
            row = await repo.create(raw, note=note)
            created.append(row.code)
        await session.commit()

    print(f"[OK] 초대코드 {len(created)}개 발급:")
    for c in created:
        print(f"  {c}")


async def list_codes() -> None:
    from reaction_backend.db.session import get_sessionmaker

    factory = get_sessionmaker()
    async with factory() as session:
        repo = InviteCodeRepo(session)
        rows = await repo.list_all()

    if not rows:
        print("발급된 초대코드가 없습니다.")
        return

    used = [r for r in rows if r.used_at is not None]
    print(f"전체 {len(rows)}개 — 소진 {len(used)}개 / 미사용 {len(rows) - len(used)}개\n")
    for r in rows:
        status = (
            f"사용됨 ({r.used_at:%Y-%m-%d %H:%M}, user={r.used_by_user_id})"
            if r.used_at
            else "미사용"
        )
        note = f" [{r.note}]" if r.note else ""
        print(f"  {r.code}{note} — {status}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="초대코드 발급/현황 조회 (#324)")
    sub = p.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="새 초대코드 발급")
    p_create.add_argument("--count", type=int, default=1, help="발급 개수 (기본 1)")
    p_create.add_argument(
        "--code", default=None, help="직접 지정할 코드 문자열 (미지정 시 무작위 8자 생성)"
    )
    p_create.add_argument("--note", default=None, help="발급 목적 메모 (예: 'Play 리뷰어')")

    sub.add_parser("list", help="발급·소진 현황 조회")

    return p.parse_args()


def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL 이 설정되어 있지 않습니다.")

    args = _parse_args()
    if args.command == "create":
        code = normalize_code(args.code) if args.code else None
        asyncio.run(create(count=args.count, code=code, note=args.note))
    elif args.command == "list":
        asyncio.run(list_codes())


if __name__ == "__main__":
    main()
