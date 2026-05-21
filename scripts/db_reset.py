"""DB 전체 reset — local/dev 만.

흐름:
1. APP_ENV 검증 (prod 면 거부)
2. alembic downgrade base — 모든 테이블/enum drop
3. alembic upgrade head — 다시 생성 + 마스터 seed 자동 적용 (a96678e9ffe5)

실행:
  uv run python -m scripts.db_reset

⚠️ 모든 사용자 데이터 삭제. .env 의 DATABASE_URL 이 가리키는 DB 가 맞는지 확인 후 실행.
"""

from __future__ import annotations

import subprocess
import sys

from reaction_backend.config import get_settings


def main() -> int:
    settings = get_settings()

    if settings.app_env == "prod":
        print("REFUSED: app_env=prod 에서는 db_reset 금지.", file=sys.stderr)
        return 2

    if not settings.database_url:
        print("REFUSED: DATABASE_URL 이 비어있음.", file=sys.stderr)
        return 2

    # 안전 확인 — 호스트 표시
    masked = settings.database_url.split("@", 1)[-1] if "@" in settings.database_url else "?"
    print(f"target: ...@{masked}")
    print(f"app_env: {settings.app_env}")
    confirm = input("정말 모든 테이블을 drop 하고 재생성할까요? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("취소됨.")
        return 1

    print("\n[1/2] alembic downgrade base ...")
    r = subprocess.run(["uv", "run", "alembic", "downgrade", "base"], check=False)
    if r.returncode != 0:
        print("downgrade 실패.", file=sys.stderr)
        return r.returncode

    print("\n[2/2] alembic upgrade head ...")
    r = subprocess.run(["uv", "run", "alembic", "upgrade", "head"], check=False)
    if r.returncode != 0:
        print("upgrade 실패.", file=sys.stderr)
        return r.returncode

    print("\n[OK] DB reset 완료. 마스터 seed 도 적용됨.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
