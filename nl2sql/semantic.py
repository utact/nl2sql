"""시맨틱 레이어 컴파일러.

머리 질문은 "지표 × 차원 × 필터" 조합으로 표현되고, 이 모듈이 그 조합을 SQL로 컴파일한다.
쿼리를 미리 만들어 두는 템플릿과 달리 조합 규칙만 정의되어 있다.
조합 공간은 크지만 생성되는 SQL의 형태는 완전히 통제되고, 자유도는 값과 파라미터에만 남는다.

결과가 파라미터화된 (sql, params) 이므로 값 주입은 구조적으로 불가능하다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, spoken

ALLOWED_OPS = ("=", "!=", ">", ">=", "<", "<=")
ALLOWED_ORDERS = ("asc", "desc")

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


class SemanticError(ValueError):
    """카탈로그 정의에 없는 지표/차원, 허용되지 않은 연산자 등.

    거절할 때 갈 곳을 알면 함께 들고 나간다.
    사유만 돌려주면 되묻기에 누를 것이 없고, 사용자는 질문을 통째로 다시 쓴다.
    그러면 앞 질문이 이미 정한 것까지 사라진다.

    Attributes:
        suggestion: 대신 물어볼 수 있는 질의. 없으면 None.
    """

    def __init__(self, message: str, suggestion: SemanticQuery | None = None):
        super().__init__(message)
        self.suggestion = suggestion


@dataclass(frozen=True)
class Filter:
    dimension: str
    op: str
    value: str
    # 이 필터를 파이프라인이 넣었는가 (테넌트 자동 주입 등).
    #
    # 저자가 누구인지가 가드 판정을 바꾼다.
    # 아래 blind-filter 검사가 묻는 것은 "사용자가 건 조건이 전부 헛도는가"다.
    # 파이프라인이 나중에 덧댄 필터는 그 질문의 분모가 아니다.
    # 세면 로그인 컨텍스트 하나로 가드가 꺼지고, 거절이어야 할 것이 표로 나간다.
    injected: bool = False


@dataclass
class SemanticQuery:
    metrics: list[str]
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    limit: int | None = None
    # 첫 지표 기준 정렬 방향. None 이면 지표 정의의 default_order를 쓴다.
    # "가장 낮은 / 최소" 같은 표현은 사용자의 의도라 여기로 온다.
    order: str | None = None
    # 모델이 방향을 말하긴 했는데 읽을 수 없는 경우의 원문.
    # "없음"과 "못 읽음"은 다르다. 없으면 지표 정의의 기본 방향이 맞는 답이다.
    # 못 읽었는데 기본 방향으로 떨어지면 에러도 행수 이상도 없이 목록만 뒤집힌 채 나간다.
    # 그래서 컴파일러가 되묻기로 올린다.
    order_unparsed: str | None = None
    # 값 열거 — "어떤 라인들이 있어?" 처럼 수치가 아니라 목록을 묻는 질문.
    # 이게 없으면 문법(지표 × 차원 × 필터)이 열거를 표현하지 못해 카운트를 끼워 넣게 된다.
    # 사용자는 묻지 않은 숫자를 받고, 끼워 넣었다는 사실은 가정에도 안 남는다.
    #
    # 명시적으로 켤 때만 열거다.
    # 지표가 비었다고 열거로 넘기면 형태가 깨진 출력이 "전부 나열"로 떨어진다.
    # 지표 없음은 되묻기로 강등한다.
    distinct: bool = False
    # 개별 측정을 그대로 보는 형태.
    #
    # 집계는 "몇 건인가"에만 답한다.
    # 그런데 규격에 빠듯한 건수를 묻는 이유는 건수가 아니라 무엇이 얼마나 위험한지를 알기 위해서다.
    # 실측값이 규격 대비 어디인지 보여야 손을 쓴다.
    # 이 형태가 없으면 그 질문의 답이 지표·차원으로 표현되지 않아 직접 생성으로 떨어진다.
    #
    # 보여줄 컬럼은 정의가 정한다 (Catalog.detail_columns). 모델이 고르지 않는다.
    detail: bool = False


def parse_semantic_query(data: dict | None) -> SemanticQuery:
    """dict를 SemanticQuery로 방어적으로 파싱한다.

    입력은 둘 다 신뢰 경계 바깥이다 — 모델의 구조화 출력이거나, 되묻기를 되돌려 보낸 클라이언트다.
    그래서 형태만 맞춰 받고, 정의 검증은 compile_semantic에 맡긴다.

    모든 필드의 타입을 확인한다. 소형 모델은 `metrics`를 문자열 하나로 돌려주기도 한다.
    형태가 틀린 것을 예외가 아니라 빈 값으로 두면 지표가 없어져 되묻기로 강등된다.

    Args:
        data: {"metrics": [...], "dimensions": [...], "filters": [...], "order": ...} dict.
            dict가 아니면 빈 질의를 돌려준다.

    Returns:
        SemanticQuery. 알 수 없는 키와 형태가 깨진 값은 조용히 버린다.
    """
    data = data if isinstance(data, dict) else {}
    raw_order = data.get("order")
    order = str(raw_order).lower() if str(raw_order).lower() in ALLOWED_ORDERS else None
    # 방향을 말했는데 못 읽은 경우만 남긴다. 아예 안 말한 것(None)은 정상이다.
    order_unparsed = None if order is not None or raw_order is None else str(raw_order)

    def _names(key: str) -> list[str]:
        # 문자열이 오면 한 글자씩 쪼개져 엉뚱한 정의가 만들어진다. 리스트만 받는다.
        value = data.get(key)
        return [str(v) for v in value] if isinstance(value, (list, tuple)) else []

    raw_filters = data.get("filters")
    return SemanticQuery(
        metrics=_names("metrics"),
        dimensions=_names("dimensions"),
        # 참(bool) 하나만 열거로 인정한다.
        # 문자열 "true" 같은 것을 받아 주면 깨진 출력이 열거로 새어 들어온다.
        distinct=data.get("distinct") is True,
        detail=data.get("detail") is True,
        filters=[
            Filter(str(f["dimension"]), str(f["op"]), str(f["value"]))
            for f in (raw_filters if isinstance(raw_filters, (list, tuple)) else [])
            if isinstance(f, dict) and {"dimension", "op", "value"} <= f.keys()
        ],
        order=order,
        order_unparsed=order_unparsed,
    )


def semantic_query_to_dict(query: SemanticQuery) -> dict:
    """SemanticQuery를 JSON으로 나갈 dict로 만든다 (되묻기 선택지 운반용)."""
    return {
        "metrics": list(query.metrics),
        "dimensions": list(query.dimensions),
        "filters": [
            {"dimension": f.dimension, "op": f.op, "value": f.value} for f in query.filters
        ],
        "order": query.order,
        "distinct": query.distinct,
        "detail": query.detail,
    }


def _default_order(query, dims, metrics) -> str:
    """질문이 방향을 말하지 않았을 때 쓸 정렬 방향.

    순서 있는 축으로 묶었으면 오래된 것부터 읽는다. 아니면 지표 정의가 답한다.
    """
    if dims and dims[0].ordinal:
        return "asc"
    return metrics[0].default_order


def _where(query: SemanticQuery, catalog: Catalog) -> tuple[list[str], list]:
    """필터를 (WHERE 조각, 바인딩 값) 으로 만든다.

    집계와 상세가 같은 필터 규칙을 써야 한다.
    한쪽만 값 정규화나 타입 바인딩을 빼먹으면 같은 조건이 두 화면에서 다른 결과를 낸다.

    Args:
        query: 시맨틱 질의.
        catalog: 정의.

    Returns:
        (WHERE 조각 목록, 바인딩 값 목록).

    Raises:
        SemanticError: 정의에 없는 차원, 허용되지 않은 연산자, 숫자 차원의 비숫자 값.
    """
    where_parts: list[str] = []
    params: list = []
    for f in query.filters:
        dim = catalog.dimensions.get(f.dimension)
        if dim is None:
            raise SemanticError(
                f"알 수 없는 필터 차원 {f.dimension!r}."
                f" 가능한 차원: {', '.join(catalog.dimensions)}"
            )
        if f.op not in ALLOWED_OPS:
            raise SemanticError(f"허용되지 않은 연산자 {f.op!r}. 허용: {', '.join(ALLOWED_OPS)}")
        where_parts.append(f"{dim.expression} {f.op} ?")
        value = catalog.normalize_value(f.dimension, str(f.value))
        if dim.numeric:
            try:
                value = float(value)
            except ValueError:
                raise SemanticError(
                    f"차원 '{dim.name}' 은 숫자 필터만 받습니다 (받은 값: {value!r})."
                ) from None
        params.append(value)
    return where_parts, params


def compile_semantic(query: SemanticQuery, catalog: Catalog) -> tuple[str, list[str]]:
    """SemanticQuery -> (sql, params). 정의 검증에 실패하면 SemanticError."""
    # 방향을 말했는데 못 읽었으면 추측하지 않는다. 뒤집힌 목록은 아무 층도 못 잡는다.
    if query.order_unparsed is not None:
        raise SemanticError(
            f"정렬 방향 {query.order_unparsed!r} 을 알아듣지 못했습니다."
            " 높은 순인지 낮은 순인지 알려 주세요."
        )
    # 비활성 항목은 "알 수 없는 이름"과 다르게 말한다.
    # 정의에는 있는데 지금 못 쓰는 것이라, 사용자가 의심해야 할 것은 오타가 아니다.
    #
    # 상세 분기보다 먼저 본다. 뒤에 두면 드릴다운이 꺼 둔 차원을 그대로 통과시킨다 —
    # 꺼 둔 이유가 정의와 실스키마의 어긋남이므로 그 질의는 실행에서 깨지거나,
    # 더 나쁘게는 엉뚱한 컬럼으로 답한다. "꺼 둔 것은 어디서도 안 쓴다"가 규약이다.
    blocked = sorted(
        catalog.disabled.intersection(
            set(query.metrics) | set(query.dimensions) | {f.dimension for f in query.filters}
        )
    )
    if blocked:
        raise SemanticError(
            f"{', '.join(repr(b) for b in blocked)} 은(는) 지금 사용할 수 없습니다"
            " (정의가 실제 데이터와 어긋나 잠시 꺼 두었습니다). 다른 기준으로 물어봐 주세요."
        )

    if query.detail:
        if not catalog.detail_columns:
            raise SemanticError("개별 측정을 보여주도록 설정되어 있지 않습니다.")
        limit = min(query.limit or DEFAULT_LIMIT, MAX_LIMIT)
        where, params = _where(query, catalog)
        columns = ", ".join(f'{c} AS "{label}"' for c, label in catalog.detail_columns)
        sql = f"SELECT {columns}\nFROM {catalog.base_view}"
        if where:
            sql += "\nWHERE " + " AND ".join(where)
        if catalog.detail_order:
            sql += f"\nORDER BY {catalog.detail_order} ASC"
        return sql + f"\nLIMIT {limit}", params

    if query.distinct:
        if not query.dimensions:
            raise SemanticError("값 목록을 보려면 차원(dimension)이 최소 1개 필요합니다.")
        if query.metrics:
            raise SemanticError(
                "값 목록과 지표는 함께 쓸 수 없습니다. 목록을 원하면 지표를 비우세요."
            )
    elif not query.metrics:
        raise SemanticError("지표(metric)가 최소 1개 필요합니다.")

    try:
        metrics = [catalog.metrics[m] for m in query.metrics]
    except KeyError as e:
        raise SemanticError(
            f"알 수 없는 지표 {e.args[0]!r}. 가능한 지표: {', '.join(catalog.metrics)}"
        ) from None
    try:
        dims = [catalog.dimensions[d] for d in query.dimensions]
    except KeyError as e:
        raise SemanticError(
            f"알 수 없는 차원 {e.args[0]!r}. 가능한 차원: {', '.join(catalog.dimensions)}"
        ) from None

    select_parts = [f"{d.expression} AS {d.name}" for d in dims]
    select_parts += [f"{m.expression} AS {m.name}" for m in metrics]

    where_parts, params = _where(query, catalog)

    # 묶는 축을 가를 수 있는 필터가 하나도 없으면 거부한다.
    #
    # slack_rank는 item_code로 파티션한 백분위다.
    # 임계가 0.5든 0.001이든 항목마다 같은 비율이 걸리므로 항목끼리 갈리지 않는다.
    # 그 상태로 항목을 묶으면 열거는 다섯 항목 전부, 집계는 전부 같은 수가 나온다.
    # 실행은 정상이고 행수도 그럴듯하다. 필터만 아무 일도 안 한다.
    #
    # 다른 필터가 함께 걸려 있으면 막지 않는다.
    # 합불 판정 같은 축은 항목마다 다른 비율로 걸리므로 그때는 수가 실제로 갈린다.
    #
    # 세는 것은 사용자가 건 필터뿐이다 (Filter.injected).
    # 테넌트 자동 주입은 질문의 일부가 아니라 답의 범위이고, 헛도는 필터를 구제하지 않는다.
    # 분모에 넣으면 "로그인했더니 거절이 표로 바뀌는" 자리가 생긴다 —
    # 같은 질문, 같은 정의인데 컨텍스트 한 칸으로 가드가 꺼지고,
    # 나가는 표는 전역 하위 10%에 그 라인이 몇 개 걸렸는지일 뿐 아무 뜻이 없다.
    authored = [f for f in query.filters if not f.injected]
    if authored and query.dimensions:
        grouped = set(query.dimensions)
        blind = [
            f
            for f in authored
            if (d := catalog.dimensions.get(f.dimension)) is not None
            and d.relative_to in grouped
        ]
        if len(blind) == len(authored):
            axis = catalog.dimensions[blind[0].dimension]
            base = catalog.dimensions[axis.relative_to]
            # 갈 곳을 알려 준다. 막기만 하면 사용자는 같은 질문을 다시 쓴다.
            # 그 차원을 이미 식 안에 담고 있는 지표가 있으면 그것이 제대로 만든 답이다.
            better = next(
                (m for m in catalog.metrics.values() if axis.name in m.expression), None
            )
            # 사용자에게 하는 말이므로 지표·차원 이름도 파티션 얘기도 꺼내지 않는다.
            #
            # 사용자가 쓴 말을 그대로 돌려주면 "방금 그렇게 물었는데"가 된다.
            # 바뀌는 것은 기준이 아니라 답의 모양이다 — 목록이 아니라 건수로 나간다.
            # 그 차이를 말해야 선택지가 대안으로 읽힌다.
            hint = " 대신 건수로 세어 볼까요?" if better else " 다른 기준으로 물어봐 주세요."
            # 못 거른 필터를 빼고 제대로 만든 지표로 갈아 끼운 질의를 함께 들려 보낸다.
            # 묻는 축은 그대로 두어야 한다. 사용자가 이미 정한 것을 되묻기가 잃으면 안 된다.
            raise SemanticError(
                f"그 기준으로는 {spoken(base)} 전부가 걸려서 목록이 좁혀지지 않습니다.{hint}",
                suggestion=(
                    SemanticQuery(metrics=[better.name], dimensions=list(query.dimensions))
                    if better
                    else None
                ),
            )

    limit = query.limit or DEFAULT_LIMIT
    if not (1 <= limit <= MAX_LIMIT):
        limit = min(max(limit, 1), MAX_LIMIT)

    if query.distinct:
        # 값 열거. 집계가 없으므로 GROUP BY도 정렬 방향도 없다.
        # 목록에 "높은 순"이라는 개념이 없으니 차원 순으로 고정한다.
        sql = f"SELECT DISTINCT {', '.join(select_parts)}\nFROM {catalog.base_view}"
        if where_parts:
            sql += "\nWHERE " + " AND ".join(where_parts)
        sql += "\nORDER BY " + ", ".join(f"{d.name} ASC" for d in dims)
        sql += f"\nLIMIT {limit}"
        return sql, params

    # 순서 있는 축으로 묶으면 오래된 것부터 읽는 것이 기본이다.
    order = (query.order or _default_order(query, dims, metrics)).lower()
    if order not in ALLOWED_ORDERS:
        raise SemanticError(
            f"허용되지 않은 정렬 방향 {query.order!r}. 허용: {', '.join(ALLOWED_ORDERS)}"
        )

    sql = f"SELECT {', '.join(select_parts)}\nFROM {catalog.base_view}"
    if where_parts:
        sql += "\nWHERE " + " AND ".join(where_parts)
    if dims:
        sql += "\nGROUP BY " + ", ".join(d.expression for d in dims)
        # 질문이 방향을 말하지 않았고 묶는 축이 순서를 가지면, 정렬은 축이 정한다.
        # 추이 질문에 순위를 돌려주지 않기 위해서다 (Dimension.ordinal).
        lead = dims[0].name if (query.order is None and dims[0].ordinal) else metrics[0].name
        # 동률 정렬 + LIMIT은 실행마다 결과가 달라진다.
        # 차원으로 2차 정렬해 결정론적으로 만든다 — 골든셋이 플레이키해지는 흔한 원인이다.
        tiebreak = "".join(f", {d.name} ASC" for d in dims if d.name != lead)
        sql += f"\nORDER BY {lead} {order.upper()}{tiebreak}"
    sql += f"\nLIMIT {limit}"
    return sql, params
