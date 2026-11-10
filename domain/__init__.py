"""도메인 어댑터 — 계측·규격 검사.

이 패키지만 갈아 끼우면 새 도메인이 된다.
코어(`nl2sql`)는 여기를 임포트하지 않는다. 의존은 한 방향으로만 흐른다.

`seed`는 여기서 재수출하지 않는다.
`domain.seed`는 `python -m`으로 직접 실행하는 스크립트 모듈이다.
여기서 미리 임포트하면 `-m` 실행 시 모듈이 두 번 적재된다.
필요하면 `from domain.seed import seed`로 직접 가져온다.
"""

from .catalog import BASE_VIEW, VIEW_COLUMNS, VIEW_DDL, catalog

__all__ = [
    "catalog",
    "BASE_VIEW",
    "VIEW_COLUMNS",
    "VIEW_DDL",
]
