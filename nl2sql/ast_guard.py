"""AST 정적 검증 (가드레일 2층).

결정론적으로 닫을 수 있는 것들을 여기서 닫는다:
- 단일 문장만 허용 (multi-statement 차단)
- SELECT(질의) 이외의 문 차단: INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH ...
- 허용된 큐레이션 뷰 이외의 테이블 참조 차단
- 컬럼 대조: sqlglot qualify로 스키마에 없는 컬럼 사용 차단
- 함수 대조: sqlglot이 알아보지 못한 함수 호출 차단 (set_config, pg_sleep, dblink ...)
- LIMIT 강제 주입 / 상한 캡 (주입 여부를 기록해 결과 잘림을 표면화)

검증을 통과한 질의는 구조(테이블·필터·묶음·정렬)도 함께 뽑는다 (QueryStructure).
직접 생성 경로의 해석 가정이 모델의 설명이 아니라 이 구조에서 역산되도록 하기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from .catalog import Catalog
from .semantic import ALLOWED_OPS

# 이름이 아니라 클래스를 직접 참조한다.
#
# getattr로 받으면 sqlglot이 노드를 지우거나 이름을 바꿨을 때 그 규칙만 조용히 빠진다.
# 하한이 25 였을 때 실제로 그랬다 — 25.0 에는 Alter·Attach·Detach·Grant가 없어 열한 개만 돌았다.
# 직접 참조하면 없어진 노드가 import에서 죽으므로, 이 목록은 pyproject의 하한과 한 몸이다.
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Merge,
    exp.TruncateTable,
    exp.Command,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.Grant,
    exp.Set,
    exp.Transaction,
)

DEFAULT_MAX_LIMIT = 1000

# sqlglot이 이름만 알고 뜻은 모르는 함수 호출은 exp.Anonymous로 파싱된다.
#
# 이 경계가 유용한 이유는 우연이 아니다.
# sqlglot은 방언을 번역해야 해서 표준 집계·분석·문자열·날짜 함수를 전부 고유 노드로 알아본다
# (COUNT, AVG, ROUND, SUM, CAST, substr, PERCENT_RANK, date_trunc, stddev ...).
# 그래서 Anonymous로 남는 것은 대체로 그 DB 에만 있는 확장 함수이고,
# 위험한 것들이 정확히 거기 모여 있다 —
# set_config·current_setting(세션 GUC 쓰기/읽기), pg_sleep(자원 점유),
# pg_read_file·lo_import(파일 접근), dblink(외부 연결).
#
# 테이블은 화이트리스트로, 컬럼은 qualify로 닫아 두고 함수만 비워 두는 것은 앞뒤가 안 맞는다.
# "뷰 밖 접근을 막는 것은 앱의 AST 가드"라는 주장이 딱 함수 하나만큼 헐거워지기 때문이다.
# RLS가 읽는 current_setting('app.line_code') 에 질의 언어가 쓸 수 있다면,
# 그 층은 평가 순서에 기대게 된다. 기대는 보증이 아니다.
#
# 기본값은 빈 집합이다 — 정의가 필요하다고 선언한 것만 열린다.
# 막히면 조용히 틀리는 것이 아니라 사유와 함께 거절되므로(시끄러운 실패),
# 좁게 잡고 필요할 때 여는 쪽이 맞다.
DEFAULT_ALLOWED_FUNCTIONS: frozenset[str] = frozenset()

# 시맨틱 레이어가 필터에 쓰는 것과 같은 여섯 개다 (README "여덟 토큰").
# 같은 집합으로 맞추는 이유: 직접 생성의 필터도 결국 이 여섯 안에서만 뜻을 가지므로,
# 여기서 못 알아본 연산자는 새 뜻이 아니라 분해에 실패한 것으로 다룬다 (원문 SQL로 대체).
_COMPARISON_OPS: dict[type, str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}
assert set(_COMPARISON_OPS.values()) == set(ALLOWED_OPS), "ALLOWED_OPS와 어긋난다"


def _leaf_to_filters(node: exp.Expression, dialect: str) -> list[tuple[str, str, str]]:
    """조건 하나를 (컬럼, 연산자, 값) 튜플로 편다.

    BETWEEN은 두 개로 갈라진다 — 기간 질문이 대개 이 모양이라서다.
    분해할 수 없는 모양(OR, 서브쿼리, 함수, 컬럼 간 비교 …)은 지우지 않고
    연산자 자리에 "raw"를 넣어 원문 SQL 조각을 그대로 값에 담는다.
    """
    if isinstance(node, exp.Between):
        col = node.this
        low, high = node.args.get("low"), node.args.get("high")
        bounds_are_literal = isinstance(low, exp.Literal) and isinstance(high, exp.Literal)
        if isinstance(col, exp.Column) and bounds_are_literal:
            return [(col.name, ">=", low.name), (col.name, "<=", high.name)]
        return [("", "raw", node.sql(dialect=dialect))]

    if isinstance(node, exp.In):
        col = node.this
        values = node.args.get("expressions") or []
        values_are_literal = values and all(isinstance(v, exp.Literal) for v in values)
        if isinstance(col, exp.Column) and values_are_literal:
            return [(col.name, "in", ", ".join(v.name for v in values))]
        return [("", "raw", node.sql(dialect=dialect))]

    op = _COMPARISON_OPS.get(type(node))
    if op:
        left, right = node.this, node.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            return [(left.name, op, right.name)]
        if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            return [(right.name, op, left.name)]

    return [("", "raw", node.sql(dialect=dialect))]


def _split_and(node: exp.Expression) -> list[exp.Expression]:
    """AND로 묶인 조건을 평평한 목록으로 편다.

    OR·NOT은 안 나눈다 — 나누면 "둘 다"가 "둘 중 하나"로 뜻이 바뀐다.
    나누지 않은 채로 `_leaf_to_filters`에 넘기면 raw로 떨어져 원문이 보존된다.
    """
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def _extract_structure(
    stmt: exp.Query, cte_names: set[str], dialect: str
) -> QueryStructure:
    """검증을 통과한 질의에서 테이블·필터·묶음·정렬을 뽑는다."""
    tables = tuple(
        sorted({t.name for t in stmt.find_all(exp.Table)} - cte_names)
    )

    filters: list[tuple[str, str, str]] = []
    where = stmt.args.get("where")
    if where is not None:
        for leaf in _split_and(where.this):
            filters.extend(_leaf_to_filters(leaf, dialect))

    group_by: list[str] = []
    group_node = stmt.args.get("group")
    if group_node is not None:
        for e in group_node.expressions:
            group_by.append(e.name if isinstance(e, exp.Column) else e.sql(dialect=dialect))

    order_by: list[tuple[str, str]] = []
    order_node = stmt.args.get("order")
    if order_node is not None:
        for e in order_node.expressions:
            col = e.this
            name = col.name if isinstance(col, exp.Column) else col.sql(dialect=dialect)
            order_by.append((name, "desc" if e.args.get("desc") else "asc"))

    return QueryStructure(
        tables=tables,
        filters=tuple(filters),
        group_by=tuple(group_by),
        order_by=tuple(order_by),
    )


@dataclass
class QueryStructure:
    """검증을 통과한 SELECT 문에서 뽑은 구조 — 테이블·필터·묶음·정렬.

    파이프라인의 해석 가정이 이 값만 본다 (`NL2SQL._free_assumptions`).
    모델이 뭐라고 썼든 여기 없는 것은 가정에도 없고, 여기 있는 것은 어긋날 수 없다.
    실행될 그 SQL을 파싱해서 만든 것이기 때문이다.
    """

    tables: tuple[str, ...]
    # (컬럼, 연산자, 값). 연산자가 "raw" 면 분해에 실패한 것이고 값 자리에 원문 SQL 조각이 온다.
    # 분해 실패를 조건 삭제로 처리하지 않는다 — 지우면 필터가 있었다는 사실 자체가 사라진다.
    filters: tuple[tuple[str, str, str], ...]
    group_by: tuple[str, ...]
    order_by: tuple[tuple[str, str], ...]  # (컬럼, "asc" | "desc")


@dataclass
class ValidationResult:
    ok: bool
    sql: str  # 정규화된(LIMIT 주입 후) SQL. ok=False 면 원문
    violations: list[str] = field(default_factory=list)
    limit_injected: bool = False
    # ok=True 일 때만 채워진다. False 면 실행되지 않을 SQL이라 구조를 보증할 이유가 없다.
    structure: QueryStructure | None = None


class AstGuard:
    def __init__(
        self,
        catalog: Catalog,
        max_limit: int = DEFAULT_MAX_LIMIT,
        dialect: str = "sqlite",
        allowed_functions: frozenset[str] | set[str] | None = None,
    ):
        """
        Args:
            catalog: 허용 뷰/컬럼의 출처.
            max_limit: LIMIT 강제 주입/캡 상한.
            dialect: sqlglot 방언 ("sqlite" | "postgres").
            allowed_functions: sqlglot이 못 알아보는 함수 중 허용할 이름 (소문자).
                None 이면 DEFAULT_ALLOWED_FUNCTIONS (빈 집합).
        """
        self._catalog = catalog
        self._max_limit = max_limit
        self._dialect = dialect
        self._allowed_functions = {
            f.lower()
            for f in (DEFAULT_ALLOWED_FUNCTIONS if allowed_functions is None else allowed_functions)
        }

    def validate(self, sql: str) -> ValidationResult:
        """생성된 SQL을 정적으로 검증하고 LIMIT을 강제한다.

        Args:
            sql: LLM이 생성한 SQL 원문.

        Returns:
            ValidationResult. ok=True 면 sql은 LIMIT이 보장된 정규화본이다.
        """
        violations: list[str] = []

        # 1) 파싱 + 단일 문장
        try:
            statements = [s for s in sqlglot.parse(sql, read=self._dialect) if s is not None]
        except ParseError as e:
            return ValidationResult(False, sql, [f"SQL 파싱 실패: {e}"])
        if len(statements) != 1:
            found = len(statements)
            return ValidationResult(False, sql, [f"단일 문장만 허용됩니다 (감지: {found}개)."])
        stmt = statements[0]

        # 2) 질의문(SELECT/UNION)만 허용 + 금지 노드 전수 스캔
        if not isinstance(stmt, exp.Query):
            found = type(stmt).__name__
            return ValidationResult(False, sql, [f"SELECT 문만 허용됩니다 (감지: {found})."])
        for node in stmt.walk():
            if isinstance(node, _FORBIDDEN_NODES):
                violations.append(f"금지된 구문: {type(node).__name__}")
        if violations:
            return ValidationResult(False, sql, violations)

        # 3) 함수 대조 — sqlglot이 못 알아본 호출(exp.Anonymous)은 정의가 열어 준 것만 통과
        unknown_functions = sorted(
            {
                node.name.lower()
                for node in stmt.find_all(exp.Anonymous)
                if node.name and node.name.lower() not in self._allowed_functions
            }
        )
        if unknown_functions:
            return ValidationResult(
                False,
                sql,
                [
                    f"허용되지 않은 함수: {', '.join(unknown_functions)}"
                    " (표준 집계·분석 함수만 쓸 수 있습니다)"
                ],
            )

        # 4) 테이블 화이트리스트 (CTE 별칭은 제외)
        cte_names = {cte.alias_or_name for cte in stmt.find_all(exp.CTE)}
        referenced = {t.name for t in stmt.find_all(exp.Table)}
        unknown_tables = referenced - self._catalog.allowed_tables() - cte_names
        if unknown_tables:
            allowed = ", ".join(sorted(self._catalog.allowed_tables()))
            return ValidationResult(
                False,
                sql,
                [f"허용되지 않은 테이블: {', '.join(sorted(unknown_tables))} (허용: {allowed})"],
            )

        # 5) 컬럼 대조 — 스키마에 없는 컬럼이면 qualify가 실패한다
        try:
            qualify(
                stmt.copy(),
                schema=self._catalog.schema_for_validation(),
                dialect=self._dialect,
            )
        except OptimizeError as e:
            return ValidationResult(False, sql, [f"컬럼 검증 실패: {e}"])

        # 6) LIMIT 강제
        limit_injected = False
        limit_node = stmt.args.get("limit")
        if limit_node is None:
            stmt = stmt.limit(self._max_limit)
            limit_injected = True
        else:
            try:
                current = int(limit_node.expression.this)
            except (TypeError, ValueError, AttributeError):
                current = None
            if current is None or current > self._max_limit:
                stmt = stmt.limit(self._max_limit)
                limit_injected = True

        # 구조는 실행될 이 stmt에서 뽑는다 (모델이 뭐라고 설명했든 안 본다).
        structure = _extract_structure(stmt, cte_names, self._dialect)

        # 여기서 나온 문자열이 그대로 실행되고 그대로 화면·감사 로그에 남는다.
        # 보여주려고 따로 예쁘게 만들면 보여준 것과 실행한 것이 달라진다.
        # 가드가 이미 AST를 들고 있으므로 정규형으로 내보내 둘을 같은 문자열로 둔다.
        return ValidationResult(
            True, stmt.sql(dialect=self._dialect, pretty=True), [], limit_injected, structure
        )
