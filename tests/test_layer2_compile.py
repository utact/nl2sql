"""골든셋 2층 — 컴파일 테스트.

LLM을 부르지 않는다. 매 PR에서 돌고 비용이 0이다.

지표 × 차원 × 필터 튜플을 넣고 기대한 SQL과 결과가 나오는지 본다.
자연어가 개입하지 않으므로 채점이 결정론적이다.

정의를 고칠 때 실제로 무너지는 것이 이 층이다.
집계식을 바꾸거나 뷰의 파생 컬럼을 건드리면 여기서 즉시 걸린다.
"""

from __future__ import annotations

import pytest

from nl2sql import (
    Filter,
    SemanticError,
    SemanticQuery,
    SqliteExecutor,
    compile_semantic,
)


def _run(catalog, executor, metrics, dimensions=(), filters=()):
    query = SemanticQuery(
        metrics=list(metrics),
        dimensions=list(dimensions),
        filters=[Filter(d, o, v) for d, o, v in filters],
    )
    sql, params = compile_semantic(query, catalog)
    return sql, params, executor.run(sql, params)


def test_compiles_to_parameterized_sql(catalog, executor):
    """값은 파라미터로 나간다. 값 주입이 구조적으로 불가능하다."""
    sql, params, _ = _run(
        catalog, executor, ["measurement_count"], ["item_name"], [("verdict", "=", "FAIL")]
    )
    assert "?" in sql
    assert params == ["FAIL"]
    assert "FAIL" not in sql


def test_value_dictionary_normalizes_user_term(catalog, executor):
    """사용자 표현이 저장된 정본 값으로 바뀐다.

    실무 오매핑의 다수가 여기서 생긴다. "불합격"과 'FAIL' 은 같은 것을 가리킨다.
    """
    _, params, _ = _run(
        catalog, executor, ["measurement_count"], [], [("verdict", "=", "불합격")]
    )
    assert params == ["FAIL"]


def test_numeric_dimension_binds_as_number(catalog, executor):
    """수치 차원은 숫자로 바인딩된다.

    문자열로 바인딩되면 비교가 값이 아니라 타입 순서로 일어나 조건이 통째로 무력화된다.
    에러는 없고 숫자만 조용히 틀린다.
    """
    _, params, _ = _run(
        catalog, executor, ["measurement_count"], [], [("slack_rank", "<", "0.05")]
    )
    assert params == [0.05]
    assert isinstance(params[0], float)


def test_non_numeric_value_on_numeric_dimension_is_refused(catalog):
    """수치 차원에 숫자가 아닌 값이 오면 조용히 넘어가지 않는다."""
    query = SemanticQuery(
        metrics=["measurement_count"], filters=[Filter("slack_rank", "<", "낮음")]
    )
    with pytest.raises(SemanticError):
        compile_semantic(query, catalog)


def test_unknown_metric_raises_with_vocabulary(catalog):
    """없는 지표는 실패하되, 가능한 정의를 알려준다.

    파이프라인은 이 예외를 되묻기로 강등한다.
    모델이 지표를 지어내는 사건이 오답이 아니라 질문이 되어 돌아온다.
    """
    query = SemanticQuery(metrics=["danger_score"])
    with pytest.raises(SemanticError) as e:
        compile_semantic(query, catalog)
    assert "measurement_count" in str(e.value)


def test_metric_without_dimension_is_refused(catalog):
    """지표가 없으면 컴파일 자체가 안 된다."""
    with pytest.raises(SemanticError):
        compile_semantic(SemanticQuery(metrics=[]), catalog)


def test_view_hides_voided_lots(catalog, executor):
    """폐기 로트는 뷰가 걸러낸다. 소프트 삭제를 매번 기억할 필요가 없다."""
    _, _, result = _run(catalog, executor, ["measurement_count"])
    view_total = result.rows[0][0]

    raw = executor.run("SELECT COUNT(*) FROM measurements").rows[0][0]
    assert view_total < raw


def test_view_fixes_grain_so_lot_count_is_not_inflated(catalog, executor):
    """뷰가 grain을 측정 1건으로 고정한다.

    로트 1:N 시료 1:N 측정이라 조인 순서를 잘못 잡으면 로트가 중복 계산된다.
    합계가 조용히 부풀어 오르는 가장 흔한 유형이다.
    """
    _, _, result = _run(catalog, executor, ["lot_count"])
    counted = result.rows[0][0]

    actual = executor.run("SELECT COUNT(*) FROM inspection_lots WHERE is_void = 0").rows[0][0]
    assert counted == actual


def test_deploy_sql_does_not_drift_from_the_dictionary():
    """배포 SQL의 뷰 정의가 정의와 어긋나지 않는다.

    같은 뷰가 두 곳에 있으면 반드시 드리프트하고, 그 드리프트는 조용한 오답이 된다.
    데모와 배포가 서로 다른 뷰 위에서 돌아도 양쪽 다 정상적으로 실행되기 때문이다.

    그래서 init.sql을 생성물로 만들고, 안 돌린 채 커밋하면 여기서 걸리게 한다.
    "배포에서는 차단, 런타임에서는 부분 축소" 원칙의 앞쪽 절반이다.
    """
    from deploy.render_init_sql import OUTPUT_PATH, render

    assert OUTPUT_PATH.read_text(encoding="utf-8") == render(), (
        "deploy/init.sql이 낡았습니다. python -m deploy.render_init_sql을 돌리세요."
    )


def test_derived_column_absorbs_composite_code(catalog, executor):
    """분류는 조합 코드로 저장되는데 사용자는 상위 범주로 묻는다.

    뷰의 파생 컬럼이 그 불일치를 흡수한다.
    """
    _, _, result = _run(catalog, executor, ["measurement_count"], ["category"])
    categories = {row[0] for row in result.rows}
    assert categories == {"기계", "전기", "치수"}


def test_detail_filter_selects_exactly_what_the_metric_counted(catalog, db_path):
    """지표가 센 것과 상세 목록이 여는 것이 같은가.

    집계는 `SUM(CASE WHEN ...)` 안에, 상세는 `WHERE ...`에 조건이 따로 적힌다.
    한쪽만 고치면 "3건"이라고 해 놓고 5건을 펼친다.
    실행도 정상이고 두 화면 다 그럴듯해서, 눌러 본 사람만 어긋난 것을 본다.

    주석으로 "둘을 같이 고쳐라"라고 적어 두는 것은 지시다.
    같은지 여기서 대조하면 어긋난 채로는 머지가 안 된다.
    """
    executor = SqliteExecutor(db_path)
    total = executor.run(f"SELECT COUNT(*) FROM {catalog.base_view}").rows[0][0]

    checked = 0
    for name, metric in catalog.metrics.items():
        if not metric.detail_filter:
            continue
        checked += 1
        aggregate = executor.run(
            f"SELECT {metric.expression} FROM {catalog.base_view}"
        ).rows[0][0]

        query = SemanticQuery(
            metrics=[],
            detail=True,
            filters=[Filter(d, op, v) for d, op, v in metric.detail_filter],
        )
        sql, params = compile_semantic(query, catalog)
        opened = len(executor.run(sql.replace("LIMIT 100", "LIMIT 100000"), params).rows)

        # 건수 지표는 그대로, 비율 지표는 전체를 곱하면 같은 수가 나와야 한다.
        expected = aggregate if float(aggregate).is_integer() else round(aggregate * total)
        assert opened == expected, (
            f"{name} 은 {expected}건을 셌는데 상세는 {opened}건을 연다"
        )

    assert checked, "조건이 있는 지표가 하나는 있어야 이 검사가 의미를 갖는다"
