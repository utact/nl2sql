"""정의에 결함을 되돌려 놓는 함수들.

`domain/catalog.py`는 고쳐진 상태로 배포된다.
데모는 반대로 간다 — 고쳐진 정의에서 한 군데만 되돌려 결함을 재현하고, 다시 앞으로 감는다.

결함 정의을 따로 복사해 두면 "무엇이 달라졌는가"가 두 파일의 diff에 흩어진다.
여기서는 함수 하나가 곧 그 차이다.
"""

from __future__ import annotations

import dataclasses

from nl2sql import Catalog, Dimension, Metric


def _replace(catalog: Catalog, **changes) -> Catalog:
    """정의를 얕게 복제해 일부만 바꾼다."""
    return dataclasses.replace(catalog, **changes)


def absolute_slack(catalog: Catalog) -> Catalog:
    """단위 섞인 절대 임계.

    항목별 백분위(`slack_rank`) 대신 절대 여유(`spec_slack`)를 필터 축으로 노출한다.

    한 항목 안에서는 맞는 값이다.
    항목마다 단위와 스케일이 달라 항목 간 비교만 통째로 뒤집힌다. 실행도 목록도 정상이다.
    """
    dims = dict(catalog.dimensions)
    dims["spec_slack"] = Dimension(
        name="spec_slack",
        expression="spec_slack",
        description="규격 경계까지의 여유. 작을수록 아슬아슬하다",
        synonyms=("여유", "규격 여유", "아슬아슬한 정도"),
        numeric=True,
    )
    return _replace(catalog, dimensions=dims)


def untyped_numeric(catalog: Catalog) -> Catalog:
    """바인딩 타입 — 수치 차원의 타입 선언 누락.

    `slack_rank`의 `numeric` 선언을 지운다.
    필터 값이 문자열로 바인딩되어 비교가 값이 아니라 타입 순서로 일어나고, 조건이 무력화된다.

    모델은 옳은 지표·차원·연산자를 골랐고 숫자만 조용히 틀린다.
    사용자가 판단할 수 있는 종류의 가정이 아니라 표면화로도 안 잡힌다.
    """
    dims = dict(catalog.dimensions)
    old = dims["slack_rank"]
    dims["slack_rank"] = dataclasses.replace(old, numeric=False)
    return _replace(catalog, dimensions=dims)


def colliding_description(catalog: Catalog) -> Catalog:
    """설명문 충돌 — 단어 하나가 라우팅을 흔든다.

    이 결함에는 데모 장면이 없다.
    재현하려면 라우팅이 흔들려야 하는데 `demo/__main__.py`는 StubLLM으로 응답을 고정한다.
    실제 모델을 붙인 `app.server` 에서만 재현된다.

    `lot_count`의 설명에 "검사"라는 단어를 넣는다.
    설명과 동의어는 그대로 모델의 입력이라, "검사 건수"가 로트 수로 라우팅될 여지가 생긴다.
    고치는 곳은 프롬프트가 아니라 정의가다.
    """
    metrics = dict(catalog.metrics)
    old = metrics["lot_count"]
    metrics["lot_count"] = dataclasses.replace(
        old,
        description="검사 로트 수. 검사 단위 개수를 센다",
        synonyms=("로트 수", "검사 수", "검사 건수"),
    )
    return _replace(catalog, metrics=metrics)


def wrong_default_order(catalog: Catalog) -> Catalog:
    """박아 둔 정렬 방향 — 방향을 잘못 선언한다.

    "규격에 아슬아슬한 항목"은 여유가 작은 쪽을 묻는다.
    여유 평균 지표를 내림차순으로 선언하면 가장 여유로운 항목이 맨 위로 온다.
    실행 정상, 행수 그럴듯, 목록만 거꾸로다.

    같은 유형이 컴파일러에도 생길 수 있다 — `ORDER BY 첫지표 DESC`를 박아 두는 경우다.
    방향을 컴파일러가 아니라 지표 정의에 두었기 때문에 결함도 정의 쪽에서 재현된다.
    """
    metrics = dict(catalog.metrics)
    metrics["avg_slack_rank"] = Metric(
        name="avg_slack_rank",
        expression="ROUND(CAST(AVG(slack_rank) AS NUMERIC), 4)",
        description="규격 여유 평균 백분위 (0=아슬아슬, 1=여유)",
        synonyms=("평균 여유", "여유 평균"),
        default_order="desc",  # ← 결함. 작을수록 흥미로운 지표다
    )
    return _replace(catalog, metrics=metrics)


def degenerate_percentile(catalog: Catalog) -> Catalog:
    """무의미한 지표 — 정렬을 고치면 그 아래에서 드러나는 것.

    위험 지표를 `MIN(slack_rank)`로 두고, 방향은 오름차순으로 맞게 선언한다.

    `slack_rank`는 항목별로 파티션된 백분위라 항목의 최솟값이 정의상 항상 0 이다.
    항목으로 묶는 순간 모든 행이 `0.0`으로 나온다.
    실행 정상, 에러 없음, 정렬까지 맞음 — 답만 무의미하다.

    정렬 방향을 고쳐야 이것이 보인다는 점이 이 결함의 요지다.
    목록이 거꾸로일 때는 값이 전부 같다는 것을 아무도 안 본다.

    보여줘도 판단할 수 없는 종류라 고친 수는 정의에서 제거하는 것이었다.
    (docs/rationale.md 「모호성은 두 종류다」)
    """
    metrics = dict(catalog.metrics)
    metrics["min_slack_rank"] = Metric(
        name="min_slack_rank",
        expression="MIN(slack_rank)",
        description="가장 아슬아슬한 측정의 여유 백분위 (0=아슬아슬, 1=여유)",
        synonyms=("최소 여유", "가장 아슬아슬한 정도"),
        default_order="asc",  # 방향은 맞다. 틀린 것은 지표 자체다
    )
    return _replace(catalog, metrics=metrics)

def unguarded_free_assumptions(pipeline):
    """빈 해석 가정 — 직접 생성이 대체한 사실을 안 남긴다.

    앞의 결함들과 달리 정의가 아니라 하네스를 되돌린다.
    데이터에 없는 것을 물었을 때 모델이 가장 가까운 것으로 대체하는 것은 정의로 못 막는다.

    고친 방식은 `NL2SQL._free_assumptions`가 빈 목록을 받으면 정의에서 한 줄을 채우는 것이다.
    여기서는 모델이 낸 것을 그대로 통과시켜 고치기 전 상태를 재현한다.

    Args:
        pipeline: 되돌릴 파이프라인. 제자리에서 바꾼다.

    Returns:
        같은 파이프라인. 데모 장면에서만 쓰고 재사용하지 않는다.
    """
    pipeline._free_assumptions = lambda structure, model_wrote: list(model_wrote)
    return pipeline
