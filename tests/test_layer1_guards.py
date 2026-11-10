"""골든셋 1층 — 가드 테스트.

LLM을 부르지 않는다. 매 PR에서 돌고 비용이 0이다.

"골든셋"이라고 하면 대개 4층(질문 → 실행 결과)만 떠올리지만, 실제 회귀의 대부분은 1~2층에서 잡힌다.
이 두 층은 모델이 무엇을 하든 무관하게 성립하므로 플레이키하지 않다.

여기서 확인하는 것은 하나다 — 모델이 무엇을 내놓든 위험한 실행이 불가능한가.
"""

from __future__ import annotations

import pytest

WRITE_STATEMENTS = [
    "DELETE FROM inspection_results",
    "UPDATE inspection_results SET verdict = 'PASS'",
    "DROP VIEW inspection_results",
    "INSERT INTO inspection_results VALUES (1)",
    "CREATE TABLE x (a INT)",
]


@pytest.mark.parametrize("sql", WRITE_STATEMENTS)
def test_write_statements_are_rejected(guard, sql):
    """쓰기와 DDL은 앱 층에서 먼저 막힌다.

    이건 두 겹 중 첫 겹일 뿐이다.
    뚫려도 읽기전용 연결이 두 번째로 막는다 (test_readonly_connection_rejects_write).
    """
    result = guard.validate(sql)
    assert not result.ok
    assert result.violations


def test_multi_statement_is_rejected(guard):
    """세미콜론으로 붙인 두 번째 문장이 실행되면 안 된다."""
    result = guard.validate("SELECT 1 FROM inspection_results; DROP VIEW inspection_results")
    assert not result.ok


def test_write_nested_in_a_cte_is_rejected(catalog):
    """CTE 안에 숨은 쓰기는 금지 노드 목록만 잡는다.

    최상위가 SELECT 라 "SELECT 문만 허용"을 통과하고, 대상이 큐레이션 뷰라 화이트리스트도 통과한다.
    이 질의를 막는 것은 walk() 의 전수 스캔 하나뿐이라, 그 목록이 비면 그대로 나간다.
    쓰기 가능 CTE는 방언 기능이므로 postgres로 파싱해야 재현된다.
    """
    from nl2sql import AstGuard

    result = AstGuard(catalog, dialect="postgres").validate(
        "WITH x AS (INSERT INTO inspection_results VALUES (1) RETURNING *) SELECT * FROM x"
    )
    assert not result.ok, "CTE 안의 쓰기가 통과하면 안 된다"
    assert any("Insert" in v for v in result.violations), "무엇 때문에 막혔는지 말해야 한다"


def test_table_outside_curated_view_is_rejected(guard):
    """큐레이션 뷰 밖의 물리 테이블은 접근 자체가 거부된다.

    직접 생성 경로에도 물리 스키마를 노출하지 않는다는 원칙의 강제다.
    """
    result = guard.validate("SELECT * FROM measurements")
    assert not result.ok


def test_unknown_column_is_rejected(guard):
    """정의에 없는 컬럼은 통과하지 못한다."""
    result = guard.validate("SELECT nonexistent_column FROM inspection_results")
    assert not result.ok


def test_limit_is_injected_when_missing(guard):
    """LIMIT이 없으면 강제로 주입된다.

    안전 장치지만 잘린 결과를 전체로 믿게 하는 부작용이 있다.
    그래서 파이프라인이 잘림을 반드시 표면화한다 (test_truncation_is_surfaced).
    """
    result = guard.validate("SELECT item_name FROM inspection_results")
    assert result.ok
    assert "LIMIT" in result.sql.upper()


def test_plain_select_passes(guard):
    """정상적인 조회는 통과해야 한다. 가드가 과하게 막으면 제품이 죽는다."""
    result = guard.validate("SELECT item_name FROM inspection_results LIMIT 10")
    assert result.ok, result.violations


def test_readonly_connection_rejects_write(executor):
    """두 번째 겹 — 앱 가드를 우회해도 DB 연결이 거부한다.

    같은 위험을 서로 다른 두 층에서 막는다.
    한 층의 버그가 그대로 사고가 되지 않게 하는 가장 값싼 심층 방어다.
    """
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        executor.run("DELETE FROM measurements")


# 이 DB 에만 있는 확장 함수들. sqlglot이 뜻을 모르므로 exp.Anonymous로 파싱된다.
# 표준 집계·분석 함수는 전부 고유 노드로 파싱되므로 이 경계가 화이트리스트가 된다.
EXTENSION_FUNCTIONS = [
    # RLS 정책이 읽는 세션 설정에 질의 언어가 쓸 수 있으면 안 된다.
    "SELECT set_config('app.line_code', 'L1', false), COUNT(*) FROM inspection_results",
    "SELECT current_setting('app.line_code') FROM inspection_results",
    "SELECT pg_sleep(30) FROM inspection_results",       # 자원 점유
    "SELECT pg_read_file('/etc/passwd') FROM inspection_results",  # 파일 접근
    "SELECT lo_import('/etc/passwd') FROM inspection_results",
    "SELECT dblink('host=x', 'SELECT 1') FROM inspection_results",  # 외부 연결
]


@pytest.mark.parametrize("sql", EXTENSION_FUNCTIONS)
def test_unknown_functions_are_rejected(guard, sql):
    """테이블·컬럼만 화이트리스트고 함수는 비어 있었다.

    "뷰 밖 접근을 막는 것은 DB가 아니라 앱의 AST 가드"라고 적어 두고 함수는 아무도 안 봤다.
    RLS가 읽는 current_setting('app.line_code') 에 질의 언어가 쓸 수 있으면,
    그 층은 평가 순서에 기대게 된다. 기대는 보증이 아니다.
    """
    result = guard.validate(sql)
    assert not result.ok, "정의가 열어 주지 않은 함수는 통과하면 안 된다"
    assert any("함수" in v for v in result.violations), "무엇 때문에 막혔는지 말해야 한다"


# 자유 생성이 실제로 쓰는 모양들. 여기가 막히면 제품이 죽는다.
STANDARD_FUNCTION_QUERIES = [
    "SELECT item_name, COUNT(*) FROM inspection_results GROUP BY item_name",
    "SELECT item_name, ROUND(AVG(measured_value), 4) FROM inspection_results GROUP BY item_name",
    "SELECT MIN(measured_value), MAX(measured_value), ABS(MIN(spec_slack)) "
    "FROM inspection_results",
    "SELECT COALESCE(AVG(spec_slack), 0) FROM inspection_results",
    "SELECT CAST(AVG(spec_slack) AS NUMERIC) FROM inspection_results",
    "SELECT substr(inspected_on, 1, 7) AS m, COUNT(*) FROM inspection_results GROUP BY m",
    "SELECT item_name, stddev(measured_value) FROM inspection_results GROUP BY item_name",
    "SELECT inspection_month, SUM(CASE WHEN verdict = 'FAIL' THEN 1 ELSE 0 END) "
    "FROM inspection_results GROUP BY inspection_month",
]


@pytest.mark.parametrize("sql", STANDARD_FUNCTION_QUERIES)
def test_standard_functions_still_pass(guard, sql):
    """함수 화이트리스트가 정상 질의를 죽이지 않는가.

    거부는 넓을수록 안전해 보이지만, 넓으면 답할 수 있는 질문이 사라진다.
    자유 생성 경로의 존재 이유가 정의에 없는 통계를 묻는 것이라 이쪽이 특히 얇다.
    """
    result = guard.validate(sql)
    assert result.ok, result.violations


def test_cost_guard_multiplies_joins_instead_of_adding_them(executor):
    """비용 가드가 카테시안 곱을 덧셈으로 셌다.

    README는 3층을 "카테시안 곱 차단"이라고 적어 두었는데 스캔 행수를 합산하고 있었다.
    곱셈이 덧셈으로 잡히니 3중 크로스 조인이 정상 질의의 세 배로 추정되어 그대로 통과했고,
    실제로 막은 것은 그 뒤의 타임아웃이었다.

    같은 부모 아래 나란히 선 스캔은 서로 중첩 루프로 돈다 — 그 자리에서만 곱한다.
    뷰가 4중 조인 위에 있어 평범한 질의도 SCAN이 여러 줄 나온다.
    그것까지 곱하면 아무것도 못 지나간다.
    """
    from nl2sql.cost_guard import SqliteCostGuard

    guard = SqliteCostGuard(executor.connection)

    normal = guard.check("SELECT item_name, COUNT(*) FROM inspection_results GROUP BY item_name")
    assert normal.ok, normal.message

    cross = guard.check(
        "SELECT COUNT(*) FROM inspection_results a, inspection_results b, inspection_results c"
    )
    assert not cross.ok, "조인 조건 없는 다중 스캔은 막아야 한다"
    assert "조인" in cross.message, "왜 막혔는지가 처방으로 이어져야 한다"
    assert cross.estimated_scanned_rows > normal.estimated_scanned_rows * 1000, (
        "곱셈이 곱셈으로 잡혀야 한다 — 덧셈이면 배수가 이만큼 벌어지지 않는다"
    )


def test_structure_extraction_reads_filters_group_and_order(guard):
    """구조 역산의 재료 — 필터·묶음·정렬이 AST에서 그대로 나오는가.

    `NL2SQL._free_assumptions`는 모델의 설명이 아니라 이 구조만 본다.
    여기서 틀리면 그 위의 모든 문장이 같이 틀린다.
    """
    result = guard.validate(
        "SELECT item_name, COUNT(*) AS c FROM inspection_results "
        "WHERE line_code = 'L2' AND verdict = 'FAIL' "
        "GROUP BY item_name ORDER BY c DESC"
    )
    assert result.ok, result.violations
    s = result.structure
    assert s.tables == ("inspection_results",)
    assert ("line_code", "=", "L2") in s.filters
    assert ("verdict", "=", "FAIL") in s.filters
    assert s.group_by == ("item_name",)
    assert s.order_by == (("c", "desc"),)


def test_structure_extraction_splits_between_into_two_bounds(guard):
    """BETWEEN은 기간 질문의 흔한 모양이다. 하나로 두면 절반이 사라진다."""
    result = guard.validate(
        "SELECT item_name FROM inspection_results "
        "WHERE inspection_month BETWEEN '2026-01' AND '2026-03'"
    )
    assert result.ok, result.violations
    assert ("inspection_month", ">=", "2026-01") in result.structure.filters
    assert ("inspection_month", "<=", "2026-03") in result.structure.filters


def test_structure_extraction_keeps_unparseable_filters_as_raw_sql(guard):
    """OR로 묶인 조건 — AND 분해기로 나누면 "둘 다"가 "둘 중 하나"로 뜻이 바뀐다.

    분해에 실패해도 조건 자체를 지우면 안 된다.
    지우는 순간 필터가 있었다는 사실 자체가 사라져 이 저장소가 세는 그 오답과 같은 모양이 된다.
    원문 SQL을 그대로 들고 있어야 한다.
    """
    result = guard.validate(
        "SELECT item_name FROM inspection_results WHERE line_code = 'L1' OR line_code = 'L2'"
    )
    assert result.ok, result.violations
    op_kinds = {op for _, op, _ in result.structure.filters}
    assert op_kinds == {"raw"}, "OR는 분해하지 않고 원문으로 남아야 한다"
    raw_sql = result.structure.filters[0][2]
    assert "L1" in raw_sql and "L2" in raw_sql, "원문에 두 값이 그대로 있어야 한다"
