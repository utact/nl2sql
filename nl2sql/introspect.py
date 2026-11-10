"""데이터베이스 인트로스펙션.

카탈로그는 손으로 쓴 문서가 아니라 실제 DB와 대조 가능한 계약이어야 한다.
이 모듈은 실제 데이터베이스에 접근해:

- 실제 테이블/뷰와 컬럼 구조를 읽고 (`inspect_schema`),
- 카탈로그 선언이 실제 스키마와 어긋나지 않는지 검증하며 (`validate_catalog`),
- 저차원 차원 컬럼의 실제 값을 수확해 값 정규화 표을 보강하고 (`harvest_dimension_values`),

검증은 기동 시점에 실행된다.
드리프트는 런타임의 조용한 오답이 되기 전에 기동 실패로 드러나야 한다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

try:  # psycopg는 선택 의존성 — 없으면 SQLite 오류만 잡는다
    import psycopg

    _DB_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, psycopg.Error)
except ImportError:  # pragma: no cover
    _DB_ERRORS = (sqlite3.Error,)

from .catalog import Catalog


class CatalogValidationError(RuntimeError):
    """카탈로그 선언이 실제 DB 스키마와 어긋날 때 (기동 차단 오류)."""


@dataclass
class ValidationReport:
    """카탈로그-실스키마 대조 결과.

    Attributes:
        errors: 기동을 차단해야 하는 불일치 (뷰/컬럼 없음, 식 실행 불가).
        warnings: 동작은 하지만 점검이 필요한 항목 (미선언 컬럼, 없는 정규화 값 등).
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 식이 실행되지 않는 지표·차원의 이름.
    # 배포는 errors로 막고, 런타임은 이 항목만 비활성으로 돌린 뒤 나머지로 계속 답한다.
    broken: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_postgres(conn) -> bool:
    """연결 객체가 psycopg(PostgreSQL) 인지 판별한다."""
    return type(conn).__module__.split(".")[0] == "psycopg"


def inspect_schema(conn) -> dict[str, list[str]]:
    """실제 DB의 테이블/뷰와 컬럼 목록을 읽는다.

    SQLite(PRAGMA)와 PostgreSQL(information_schema)을 모두 지원한다.

    Args:
        conn: sqlite3 또는 psycopg 연결 (읽기전용이면 충분하다).

    Returns:
        {객체 이름: [컬럼 이름, ...]}. 테이블과 뷰를 모두 포함한다.
    """
    schema: dict[str, list[str]] = {}
    if _is_postgres(conn):
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
        ).fetchall()
        for table_name, column_name in rows:
            schema.setdefault(table_name, []).append(column_name)
        return schema
    objects = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in objects:
        columns = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        schema[name] = [c[1] for c in columns]
    return schema


def _base_view_has_rows(catalog: Catalog, conn) -> bool:
    """base view가 지금 이 연결에서 한 행이라도 보이는가.

    행 단위 권한(RLS)이 걸린 DB 에서는 세션 컨텍스트가 비면 뷰가 0행을 돌려준다.
    그것은 데이터가 없다는 뜻이 아니라 지금 못 본다는 뜻이라, 값 대조의 전제가 성립하지 않는다.

    Args:
        catalog: 대상 카탈로그.
        conn: 실제 DB 연결.

    Returns:
        한 행이라도 읽히면 True. 0행이거나 조회가 실패하면 False.
    """
    try:
        return conn.execute(f"SELECT 1 FROM {catalog.base_view} LIMIT 1").fetchone() is not None
    except _DB_ERRORS:
        return False


def validate_catalog(catalog: Catalog, conn: sqlite3.Connection) -> ValidationReport:
    """카탈로그 선언을 실제 DB 스키마·데이터와 대조한다.

    검사 항목:
        1. 선언된 큐레이션 뷰가 실제로 존재하는가.
        2. 선언된 컬럼이 실제 뷰에 모두 있는가 (없으면 error),
           실제 뷰에만 있는 미선언 컬럼은 warning.
        3. 지표/차원 SQL 식이 base view 위에서 실제로 실행 가능한가.
        4. 값 정규화 표의 정본 값이 실제 데이터에 존재하는가 (warning).

    Args:
        catalog: 검증할 카탈로그.
        conn: 실제 DB 연결.

    Returns:
        ValidationReport. `report.ok`가 False 면 파이프라인 기동을 중단해야 한다.
    """
    report = ValidationReport()
    actual = inspect_schema(conn)

    for view in catalog.views.values():
        if view.name not in actual:
            report.errors.append(f"큐레이션 뷰 '{view.name}' 가 DB에 존재하지 않습니다.")
            continue
        actual_cols = set(actual[view.name])
        declared_cols = set(view.columns)
        for missing in sorted(declared_cols - actual_cols):
            report.errors.append(
                f"뷰 '{view.name}' 에 선언된 컬럼 '{missing}' 이 실제 스키마에 없습니다."
            )
        for extra in sorted(actual_cols - declared_cols):
            report.warnings.append(
                f"뷰 '{view.name}' 의 실제 컬럼 '{extra}' 가 카탈로그에 선언되지 않았습니다."
            )

    if catalog.base_view in actual:
        for kind, items in (("지표", catalog.metrics), ("차원", catalog.dimensions)):
            for item in items.values():
                try:
                    conn.execute(f"SELECT {item.expression} FROM {catalog.base_view} LIMIT 0")
                except _DB_ERRORS as e:
                    report.errors.append(
                        f"{kind} '{item.name}' 의 식 '{item.expression}' 을 "
                        f"'{catalog.base_view}' 위에서 실행할 수 없습니다: {e}"
                    )
                    report.broken.add(item.name)

        # 관측 자체가 가능한지 먼저 본다. "없음"과 "못 읽음"은 다르다.
        #
        # RLS가 걸린 PostgreSQL에서 기동 시점에는 app.line_code가 비어 있고,
        # 정책이 fail-closed 라 뷰가 0행을 돌려준다.
        # 그 상태로 값 대조를 돌면 정상적인 정규화 표 전부가 "관측되지 않습니다"로 뜬다.
        # 전부 거짓이고, 진짜 경고가 그 사이에 묻힌다.
        #
        # 관측이 안 되면 대조를 건너뛰고 그 사실 하나만 말한다.
        # 못 읽었다는 것을 못 읽었다고 말해야 운영자가 고칠 것을 안다.
        if not _base_view_has_rows(catalog, conn):
            report.warnings.append(
                f"'{catalog.base_view}' 가 현재 접근 범위에서 0행이라 "
                "값 정규화 표를 대조하지 못했습니다 (관측 불가이지 불일치가 아닙니다). "
                "행 단위 권한이 걸린 DB 라면 기동 시점 컨텍스트가 비어 있는지 확인해 주세요 "
                "— 이 상태에서는 실제 값 수확도 함께 꺼져 값 오매핑 방어가 약해집니다."
            )
            return report

        for dim_name, mapping in catalog.value_mappings.items():
            dim = catalog.dimensions.get(dim_name)
            if dim is None:
                report.warnings.append(f"값 정규화 표의 차원 '{dim_name}' 이 카탈로그에 없습니다.")
                continue
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT {dim.expression} FROM {catalog.base_view}"
                ).fetchall()
            except _DB_ERRORS:
                continue
            actual_values = {str(r[0]) for r in rows}
            for user_term, canonical in mapping.items():
                if canonical not in actual_values:
                    report.warnings.append(
                        f"값 정규화 표 {dim_name}: '{user_term}' → '{canonical}' 의 정본 값이 "
                        f"실제 데이터에 관측되지 않습니다."
                    )
    return report


def harvest_dimension_values(
    catalog: Catalog,
    conn: sqlite3.Connection,
    max_distinct: int = 30,
) -> dict[str, tuple[str, ...]]:
    """저차원(distinct 값이 적은) 차원의 실제 값을 수확한다.

    수확된 값은 `known_values`로 들어가 라우터·생성기 프롬프트에 실제 관측 값으로 실린다.
    값 오매핑("불합격" vs 'FAIL')을 줄이는 가장 효과적인 수단이다.

    주의(보안): 수확된 값은 프롬프트에 실린다.
    외부 API 백엔드를 쓰는 보안 사업장에서는 harvest_values=False로 끄거나 로컬 모델을 쓴다.

    Args:
        catalog: 대상 카탈로그.
        conn: 실제 DB 연결.
        max_distinct: 이 개수를 넘는 고차원 컬럼(id, 날짜 등)은 수확하지 않는다.

    Returns:
        {차원 이름: (실제 값, ...)}. 고차원 컬럼은 제외된다.
    """
    harvested: dict[str, tuple[str, ...]] = {}
    for name, dim in catalog.dimensions.items():
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {dim.expression} FROM {catalog.base_view} "
                f"ORDER BY 1 LIMIT {max_distinct + 1}"
            ).fetchall()
        except _DB_ERRORS:
            continue
        if len(rows) > max_distinct:
            continue
        values = tuple(str(r[0]) for r in rows if r[0] is not None)
        if values:
            harvested[name] = values
    return harvested
