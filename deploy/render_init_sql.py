"""`deploy/init.sql` 을 정의에서 생성한다.

뷰 정의가 두 곳에 있으면 반드시 드리프트하고, 그 드리프트는 조용한 오답으로 나타난다.
SQLite 데모와 PostgreSQL 배포가 서로 다른 뷰 위에서 돌아도 둘 다 정상적으로 실행되기 때문이다.

정본은 `domain/catalog.py` 의 `VIEW_DDL` 하나이고, `init.sql` 은 생성물이다.

    python -m deploy.render_init_sql  # init.sql 을 다시 쓴다
    python -m deploy.render_init_sql --check  # 어긋나면 종료코드 1

`--check` 는 CI 용이다. 정의를 고치고 이 파일을 안 돌리면 거기서 걸린다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from domain.catalog import VIEW_DDL

_HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = _HERE / "init.sql.in"
OUTPUT_PATH = _HERE / "init.sql"
MARKER = "-- {{VIEW_DDL}}"

_BANNER = """\
-- ─────────────────────────────────────────────────────────────
-- 이 파일은 생성물이다. 직접 고치지 말 것.
-- 정본: domain/catalog.py (VIEW_DDL)
-- 템플릿: deploy/init.sql.in
-- 생성: python -m deploy.render_init_sql
-- ─────────────────────────────────────────────────────────────
"""


def render() -> str:
    """템플릿의 마커를 정의의 뷰 DDL 로 채운다."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if MARKER not in template:
        raise SystemExit(f"템플릿에 {MARKER} 마커가 없습니다: {TEMPLATE_PATH}")
    return _BANNER + template.replace(MARKER, VIEW_DDL.strip() + ";")


def main() -> None:
    parser = argparse.ArgumentParser(description="init.sql 을 정의에서 생성한다.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="쓰지 않고 대조만 한다. 어긋나면 종료코드 1 (CI 용)",
    )
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(
                "deploy/init.sql 이 domain/catalog.py 와 어긋납니다.\n"
                "  python -m deploy.render_init_sql 을 돌리고 다시 커밋하세요.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print("deploy/init.sql 이 정의와 일치합니다.")
        return

    OUTPUT_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"생성: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
