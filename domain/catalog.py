"""계측·규격 검사 도메인의 정의.

이 파일 하나가 도메인의 전부다.
코어(`nl2sql`)는 도메인을 모르고 이 정의를 매개로만 동작한다.
새 도메인 적용은 코드 수정이 아니라 이 파일을 새로 쓰는 일이다.

정의에는 성격이 다른 두 가지가 섞여 있다.

- 메타데이터 — 설명, 동의어, 값 매핑. 도메인 전문가가 고칠 수 있다.
- 식(expression) — 집계식, 차원식, 뷰 DDL. 그대로 SQL이 되어 실행되는 코드다.

식을 편집할 수 있는 사람은 가장 안전하다고 선언한 경로에 임의 SQL을 넣을 수 있다.
그래서 이 파일은 저장소의 파일로 두고, 편집을 코드 리뷰로 만든다.

## 이 뷰가 정의 시점에 제거하는 오답 유형

- 중복 집계 — 로트 1:N 시료 1:N 측정이라 로트와 측정을 함께 조인하면 집계가 부풀어 오른다.
  뷰가 grain을 측정 1건으로 고정한다.
- 표기 불일치 — 분류는 `MECH_TENSILE` 같은 조합 코드인데 사용자는 "기계 항목"으로 묻는다.
  파생 컬럼 `category`가 흡수한다.
- 단위 혼재 — 항목마다 단위가 달라 `spec_slack`(절대 여유)의 항목 간 비교는 성립하지 않는다.
  항목 내 백분위인 `slack_rank`를 함께 파생시켜 단위 없는 비교 축을 준다.
- 소프트 삭제 누락 — 폐기된 로트를 뷰가 미리 걸러낸다.
"""

from __future__ import annotations

from nl2sql import Catalog, CuratedView, Dimension, Metric

BASE_VIEW = "inspection_results"

# 물리 스키마의 조인·소프트 삭제·조합 코드를 전부 숨기고 grain을 측정 1건으로 고정한다.
# 창 함수는 같은 SELECT 안에서 파생 별칭을 참조할 수 없으므로 안쪽 서브쿼리로 한 단계 감싼다.
VIEW_DDL = """
CREATE VIEW inspection_results AS
SELECT
    b.measurement_id,
    b.lot_id,
    b.line_code,
    b.inspected_on,
    b.inspection_month,
    b.item_code,
    b.item_name,
    b.unit,
    b.category,
    b.measured_value,
    b.lower_limit,
    b.upper_limit,
    b.verdict,
    b.spec_slack,
    PERCENT_RANK() OVER (PARTITION BY b.item_code ORDER BY b.spec_slack) AS slack_rank,
    -- 합격품 안에서만 매긴 순위.
    --
    -- slack_rank는 불합격까지 포함해 줄을 세운다.
    -- 그런데 불합격은 여유가 음수라 하위 구간을 통째로 차지한다.
    -- 이 데이터에서는 항목당 하위 5% 열세 칸 중 열~열셋이 이미 규격 밖이라,
    -- 거기서 합격만 골라내면 1,220건 중 다섯 건이 남는다. 지표가 아니라 우연이 된다.
    --
    -- "규격 안에 있으나 경계에 아슬아슬한" 것을 세려면 합격품끼리 줄을 세워야 한다.
    -- 불합격 행에는 뜻이 없으므로 NULL로 둔다.
    CASE WHEN b.verdict = 'PASS'
         THEN PERCENT_RANK() OVER (PARTITION BY b.item_code, b.verdict ORDER BY b.spec_slack)
    END AS pass_slack_rank
FROM (
    SELECT
        m.measurement_id,
        l.lot_id,
        l.line_code,
        l.inspected_on,
        substr(l.inspected_on, 1, 7) AS inspection_month,
        m.item_code,
        i.item_name,
        i.unit,
        CASE
            WHEN i.category_code LIKE 'MECH%' THEN '기계'
            WHEN i.category_code LIKE 'ELEC%' THEN '전기'
            WHEN i.category_code LIKE 'DIM%'  THEN '치수'
            ELSE '기타'
        END AS category,
        m.measured_value,
        i.lower_limit,
        i.upper_limit,
        CASE
            WHEN i.limit_operator = 'GTE' AND m.measured_value >= i.lower_limit THEN 'PASS'
            WHEN i.limit_operator = 'LTE' AND m.measured_value <= i.upper_limit THEN 'PASS'
            WHEN i.limit_operator = 'BETWEEN'
                 AND m.measured_value >= i.lower_limit
                 AND m.measured_value <= i.upper_limit THEN 'PASS'
            ELSE 'FAIL'
        END AS verdict,
        CASE
            WHEN i.limit_operator = 'GTE' THEN m.measured_value - i.lower_limit
            WHEN i.limit_operator = 'LTE' THEN i.upper_limit - m.measured_value
            WHEN (m.measured_value - i.lower_limit) < (i.upper_limit - m.measured_value)
                THEN m.measured_value - i.lower_limit
            ELSE i.upper_limit - m.measured_value
        END AS spec_slack
    FROM measurements m
    JOIN samples s ON s.sample_id = m.sample_id
    JOIN inspection_lots l ON l.lot_id = s.lot_id
    JOIN item_master i ON i.item_code = m.item_code
    WHERE l.is_void = 0
) b
"""

VIEW_COLUMNS = (
    "measurement_id",
    "lot_id",
    "line_code",
    "inspected_on",
    "inspection_month",
    "item_code",
    "item_name",
    "unit",
    "category",
    "measured_value",
    "lower_limit",
    "upper_limit",
    "verdict",
    "spec_slack",
    "slack_rank",
    "pass_slack_rank",
)


def catalog() -> Catalog:
    """계측·규격 검사 정의를 만든다.

    Returns:
        Catalog. 라우팅·컴파일·가드가 전부 이 값을 매개로 동작한다.
    """
    view = CuratedView(
        name=BASE_VIEW,
        columns=VIEW_COLUMNS,
        description=(
            "규격 검사 측정 결과. 1행 = 측정 1건이며 로트·시료·항목 정보가 함께 붙어 있다. "
            "verdict는 항목별 규격에 따른 합불, spec_slack은 규격 경계까지의 거리다. "
            "spec_slack은 단위가 항목마다 다르므로 같은 항목 안에서만 비교할 수 있고, "
            "항목 간 비교에는 항목 내 백분위인 slack_rank를 쓴다."
        ),
        ddl=VIEW_DDL,
    )

    # 집계식은 그대로 SQL이 되어 실행되므로 이 정의는 방언에 묶여 있다.
    # 다른 층은 sqlglot이 방언을 맞춰 주지만 여기 적은 문자열은 아무도 안 고쳐 준다.
    # `ROUND(AVG(x), 4)`는 SQLite에서 돌지만 PostgreSQL 에는 그 시그니처가 없다.
    # NUMERIC으로 캐스팅해야 양쪽에서 같은 값이 나온다.
    #
    # 이 유형은 기동 시 드리프트 검사가 잡는다 (introspect.validate_catalog).
    # 식을 실제 DB에서 한 번씩 실행하므로, 엔진을 둘 다 돌려야 보인다.
    metrics = {
        "measurement_count": Metric(
            name="measurement_count",
            expression="COUNT(*)",
            # 첫 문장이 그대로 해석 가정이 된다 ("… 를 구했습니다").
            # 뒤 문장은 잘리므로 세는 단위를 첫 문장 안에 넣어야 한다.
            # "측정 건수"만 남으면 로트 단위와 구별되지 않아 사용자가 대조할 수 없다.
            description="개별 측정 건수. 로트나 시료 단위가 아니다",
            synonyms=("건수", "측정 건수", "검사 건수", "몇 건"),
        ),
        "lot_count": Metric(
            name="lot_count",
            expression="COUNT(DISTINCT lot_id)",
            description="서로 다른 로트의 개수. 측정 건수가 아니라 로트 단위 개수다",
            synonyms=("로트 수", "로트 개수"),
        ),
        "fail_count": Metric(
            name="fail_count",
            expression="SUM(CASE WHEN verdict = 'FAIL' THEN 1 ELSE 0 END)",
            description="규격을 벗어난 측정 건수",
            synonyms=("불합격 건수", "부적합 건수", "탈락 건수"),
            detail_filter=(("verdict", "=", "FAIL"),),
        ),
        "fail_rate": Metric(
            name="fail_rate",
            expression=(
                "ROUND(CAST(AVG(CASE WHEN verdict = 'FAIL'"
                " THEN 1.0 ELSE 0.0 END) AS NUMERIC), 4)"
            ),
            description="측정 건수 대비 불합격 비율 (0~1)",
            synonyms=("불합격률", "부적합률", "불량률"),
            detail_filter=(("verdict", "=", "FAIL"),),
        ),
        "avg_measured_value": Metric(
            name="avg_measured_value",
            expression="ROUND(CAST(AVG(measured_value) AS NUMERIC), 4)",
            description="평균 측정값. 항목마다 단위가 다르므로 같은 항목 안에서만 의미가 있다",
            synonyms=("평균값", "평균 측정값"),
        ),
        # 위험 구간 질문("규격에 아슬아슬한 …")은 이 두 지표로 답한다.
        #
        # 여기에 MIN(slack_rank) 를 두면 안 된다.
        # slack_rank는 항목별로 파티션된 백분위라 항목의 최솟값이 정의상 항상 0 이다.
        # 항목으로 묶는 순간 모든 행이 0.0이 된다 — 실행 정상, 에러 없음, 답만 무의미하다.
        #
        # 보여줘도 사용자가 판단할 수 없는 종류라, 표면화가 아니라 정의에서 빼는 것이 답이다.
        # 하위 꼬리의 크기를 세면 백분위라 단위가 없고 항목 간 비교가 성립한다.
        "near_limit_count": Metric(
            name="near_limit_count",
            expression=(
                "SUM(CASE WHEN verdict = 'PASS' AND pass_slack_rank <= 0.05 THEN 1 ELSE 0 END)"
            ),
            description=(
                "규격 안에 있으나 경계에 아슬아슬한 측정 건수"
                " (항목 내 여유 백분위 하위 5%)."
                " 이미 규격을 벗어난 건은 세지 않는다"
            ),
            synonyms=("아슬아슬한 건수", "경계 근접 건수", "위험 건수"),
            detail_filter=(("verdict", "=", "PASS"), ("pass_slack_rank", "<=", "0.05")),
        ),
        "near_limit_rate": Metric(
            name="near_limit_rate",
            expression=(
                "ROUND(CAST(AVG(CASE WHEN verdict = 'PASS' AND pass_slack_rank <= 0.05"
                " THEN 1.0 ELSE 0.0 END) AS NUMERIC), 4)"
            ),
            description=(
                "측정 건수 대비 경계 근접 비율 (0~1)."
                " 물량이 다른 대상끼리 위험도를 견줄 때 쓴다"
            ),
            synonyms=("경계 근접 비율", "아슬아슬한 비율"),
            detail_filter=(("verdict", "=", "PASS"), ("pass_slack_rank", "<=", "0.05")),
        ),
    }

    dimensions = {
        "item_name": Dimension(
            name="item_name",
            expression="item_name",
            description="검사 항목 이름 (인장강도, 외경 등)",
            synonyms=("항목", "검사 항목", "측정 항목"),
        ),
        "category": Dimension(
            name="category",
            expression="category",
            description="항목 분류. 기계 / 전기 / 치수",
            synonyms=("분류", "카테고리", "항목 분류"),
        ),
        "line_code": Dimension(
            name="line_code",
            expression="line_code",
            description="생산 라인",
            synonyms=("라인", "생산 라인", "공정"),
        ),
        "verdict": Dimension(
            name="verdict",
            expression="verdict",
            description="합불 판정. PASS 또는 FAIL",
            synonyms=("판정", "합불", "결과"),
        ),
        "inspection_month": Dimension(
            name="inspection_month",
            expression="inspection_month",
            description="검사 연월 (YYYY-MM). 추이 질문의 기본 축이다",
            synonyms=("월", "월별", "연월", "추이"),
            ordinal=True,  # 추이 질문은 순위가 아니라 흐름을 묻는다
        ),
        "unit": Dimension(
            name="unit",
            expression="unit",
            description="측정 단위. 항목마다 다르다",
            synonyms=("단위",),
        ),
        "slack_rank": Dimension(
            name="slack_rank",
            expression="slack_rank",
            description=(
                "항목 내 여유 백분위 (0=가장 아슬아슬, 1=가장 여유). "
                "단위가 없으므로 항목 간 비교가 가능하다. "
                "불합격 측정까지 포함해 줄을 세우므로 하위 구간은 대부분 이미 규격 밖이다. "
                "따라서 '규격 안에 있으나 경계에 아슬아슬한' 질문에는 답하지 않는다 "
                "— 그 질문에는 near_limit_count 지표나 pass_slack_rank 축을 쓴다. "
                "이 차원은 합불을 가리지 않는 '여유 하위 몇 퍼센트'를 물을 때만 쓴다. "
                "그룹핑 축으로는 쓰지 않는다"
            ),
            # "아슬아슬한"을 동의어로 두지 않는다.
            # 그 말이 뜻하는 것은 "규격 안에서 경계에 가까운"이고 이 축은 거기에 답하지 않는다.
            # 동의어는 라우터가 축을 고르는 근거라, 여기 적어 두면 설명문으로 막은 것을 도로 연다.
            synonyms=("여유 백분위", "위험도"),
            numeric=True,
            # 항목 안에서 매긴 순위다. 항목별로 물으면서 이 필터를 걸면 항목이 안 갈린다.
            relative_to="item_name",
        ),
        "pass_slack_rank": Dimension(
            name="pass_slack_rank",
            expression="pass_slack_rank",
            description=(
                "합격품 안에서만 매긴 여유 백분위 (0=가장 아슬아슬, 1=가장 여유). "
                "불합격 행은 값이 없다. "
                "'규격 안에 있으나 경계에 아슬아슬한' 것을 고를 때 이 축을 쓴다. "
                "그룹핑 축으로는 쓰지 않는다"
            ),
            synonyms=("합격품 여유 백분위", "아슬아슬한 정도"),
            numeric=True,
            relative_to="item_name",
        ),
        "measured_value": Dimension(
            name="measured_value",
            expression="measured_value",
            description=(
                "측정값. 단위가 항목마다 다르므로 반드시 특정 항목 필터와 함께 쓴다. "
                "그룹핑 축으로는 쓰지 않는다"
            ),
            synonyms=("측정값", "값"),
            numeric=True,
        ),
    }

    # 사용자가 쓰는 표현과 저장된 정본 값의 차이를 흡수한다.
    # 실무 오매핑의 다수가 여기서 생긴다.
    value_mappings = {
        "verdict": {
            "합격": "PASS",
            "통과": "PASS",
            "정상": "PASS",
            "불합격": "FAIL",
            "부적합": "FAIL",
            "불량": "FAIL",
            "탈락": "FAIL",
        },
        "category": {
            "기계적": "기계",
            "전기적": "전기",
            "치수/외관": "치수",
        },
    }

    return Catalog(
        views={view.name: view},
        metrics=metrics,
        dimensions=dimensions,
        base_view=BASE_VIEW,
        value_mappings=value_mappings,
        # 라인 컨텍스트는 세션에서 자동 주입한다.
        # 이것은 편의 장치이지 보안 장치가 아니다. 보증은 DB 권한이 선다.
        tenant_dimension="line_code",
        # 측정값만으로는 부족하다. 규격 한계와 남은 여유가 같은 줄에 있어야 판단이 된다.
        # 단위는 항목마다 다르므로 숫자 옆에 붙어야 읽히고,
        # 로트와 검사일은 현장에서 그 건을 실제로 찾아가는 데 쓴다.
        detail_columns=(
            ("item_name", "항목"),
            ("unit", "단위"),
            ("measured_value", "측정값"),
            ("lower_limit", "하한"),
            ("upper_limit", "상한"),
            ("spec_slack", "여유"),
            ("lot_id", "로트"),
            ("inspected_on", "검사일"),
        ),
        # 여유가 적은 것이 제일 위험하다. 그것이 맨 위로 온다.
        detail_order="spec_slack",
        detail_order_label="규격 경계까지 남은 여유가 적은 순",
    )
