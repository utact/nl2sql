"""시맨틱 레이어 카탈로그.

NL2SQL이 다루는 "좁은 공간"의 정의가 전부 여기에 있다.

- CuratedView: 물리 스키마의 지저분한 조인·soft-delete를 미리 숨긴 뷰.
  직접 생성도 물리 스키마가 아니라 이 뷰 위에서만 SQL을 쓴다.
- Metric / Dimension: 시맨틱 레이어가 쓰는 지표와 차원.
  머리 질문은 "지표 × 차원 × 필터" 조합으로만 표현되고, semantic.py가 SQL로 컴파일한다.
- value_mappings: 값 정규화 표.
  실무 오매핑의 다수가 값 불일치("불합격" vs 'FAIL')에서 오므로 여기서 흡수한다.
- known_values: 실제 DB에서 수확한 관측 값 (introspect.harvest_dimension_values).

카탈로그는 코드가 아니라 데이터다.
`load_catalog`/`save_catalog`로 JSON 파일과 상호 변환되고, 기동 시 실스키마와 대조된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


def spoken(entry) -> str:
    """사용자에게 보일 이름.

    지표·차원 이름은 사용자가 본 적 없는 말이다.
    화면에 `slack_rank`를 그대로 내보내면 무엇을 고쳐야 하는지 알 수가 없다.
    동의어는 사람이 실제로 쓰는 말이라 그쪽을 먼저 쓴다.

    Args:
        entry: 지표 또는 차원.

    Returns:
        동의어가 있으면 첫 동의어, 없으면 지표·차원 이름.
    """
    return entry.synonyms[0] if entry.synonyms else entry.name


@dataclass(frozen=True)
class Metric:
    name: str
    expression: str  # 집계 SQL 식. 예: "SUM(amount)"
    description: str
    synonyms: tuple[str, ...] = ()
    # 이 지표를 정렬할 때 흥미로운 쪽.
    # 최솟값·여유·지연처럼 작을수록 흥미로운 지표를 내림차순으로 두면 답이 목록 맨 끝으로 밀린다.
    # 에러도 없고 행수도 그럴듯한 채로 목록만 뒤집힌다.
    # 사용자가 방향을 말하면 그쪽이 이기고(SemanticQuery.order), 아무 말이 없으면 이 값이 쓰인다.
    default_order: str = "desc"  # "desc" | "asc"
    # 이 지표가 무엇을 셌는지를 필터로 다시 적은 것. (차원, 연산자, 값) 목록.
    #
    # 집계식 안의 CASE 조건은 SQL 문자열이라 재사용할 수 없다.
    # 그런데 "그 3건이 뭔데"에 답하려면 그 3건을 고른 조건이 그대로 필요하다.
    # 이것이 없으면 상세 목록이 조건 없는 전체 행이 되어 전혀 다른 답을 낸다.
    #
    # 식과 여기가 어긋나면 "3건"이라 해 놓고 5건을 펼친다. 실행은 양쪽 다 정상이다.
    # 같이 고치라고 적어 두는 것은 지시라, 테스트가 둘을 실제로 대조한다
    # (지표가 센 것과 상세 목록이 여는 것이 같은가).
    detail_filter: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Dimension:
    name: str
    expression: str  # 뷰 컬럼 기반 SQL 식. 예: "source_department"
    description: str
    synonyms: tuple[str, ...] = ()
    # 숫자 컬럼이면 True — 필터 값을 숫자로 바인딩한다.
    # 문자열로 바인딩하면 SQLite가 타입 순서(숫자<문자열)로 비교해 수치 필터가 조용히 틀어진다.
    numeric: bool = False
    # 값 자체에 읽는 순서가 있는 차원 (연월, 등급 …).
    #
    # 추이 질문의 정렬은 지표가 아니라 이 축이 정한다.
    # "월별 검사 건수 추이"에 건수 많은 순으로 답하면 달이 뒤죽박죽 나오고,
    # 사용자가 물은 것은 순위가 아니라 흐름이라 그 목록은 답이 아니다.
    #
    # 질문이 방향을 말하면("가장 많은 달") 그쪽이 이긴다. 이건 침묵했을 때의 기본값이다.
    ordinal: bool = False
    # 이 차원의 값이 다른 차원 안에서만 뜻을 갖는 경우, 그 다른 차원의 이름.
    #
    # 항목 내 백분위가 그렇다.
    # 값 0.05는 "이 항목 안에서 하위 5%"라는 뜻이라,
    # 어떤 임계를 걸어도 모든 항목이 조건을 만족하는 행을 갖는다.
    # 그래서 이 차원으로 거른 뒤 기준 차원으로 묶으면 아무것도 걸러지지 않는다.
    #
    # 설명문에 적어 두는 것으로는 부족하다. 그건 모델에게 하는 지시일 뿐이고,
    # 지키는지는 실행마다 다르다. 컴파일러가 대조할 수 있도록 여기에 둔다.
    relative_to: str | None = None


@dataclass(frozen=True)
class CuratedView:
    name: str
    columns: tuple[str, ...]
    description: str
    ddl: str  # CREATE VIEW 문 전체 (seed 시점에 설치)


@dataclass
class Catalog:
    views: dict[str, CuratedView]
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    base_view: str  # 시맨틱 레이어가 컴파일 대상으로 삼는 뷰
    value_mappings: dict[str, dict[str, str]] = field(default_factory=dict)
    tenant_dimension: str | None = None  # 자동 주입 대상 차원
    known_values: dict[str, tuple[str, ...]] = field(default_factory=dict)  # DB 관측 값
    # 상세 목록(SemanticQuery.detail)이 보여줄 (뷰 컬럼, 화면에 적을 이름) 쌍.
    #
    # 무엇을 보여줄지는 사실이라 정의가 정한다. 모델도 사용자도 고르지 않는다.
    # 뷰 컬럼을 전부 뿌리면 내부 키까지 나와 또 못 읽는 표가 되고,
    # 컬럼 이름을 그대로 헤더에 올리면 검사원이 spec_slack을 읽어야 한다.
    # 비워 두면 상세 목록이 꺼진다.
    detail_columns: tuple[tuple[str, str], ...] = ()
    # 상세 목록의 정렬 컬럼과, 그 정렬을 사용자에게 뭐라고 말할지.
    # 위험한 것이 위로 와야 하고, 컬럼 이름은 사용자의 말이 아니다.
    detail_order: str = ""
    detail_order_label: str = ""
    # 실스키마와 어긋나 런타임에 비활성으로 돌린 지표·차원 (introspect가 채운다).
    # 정의에서 지우지 않는다. 지우면 그 지표를 향하던 질문이 가장 가까운 다른 지표로 조용히 간다.
    # 이름을 남겨 두어야 그것을 고른 질문을 되묻기로 강등할 수 있다.
    disabled: set[str] = field(default_factory=set)

    def normalize_value(self, dimension: str, value: str) -> str:
        """사용자 표현을 DB 정본 값으로 정규화한다.

        Args:
            dimension: 차원 이름 (예: "pass_fail").
            value: 사용자 표현 (예: "불합격").

        Returns:
            정규화된 값 (예: 'FAIL'). 정의에 없으면 원값을 그대로 돌려준다.
        """
        mapping = self.value_mappings.get(dimension, {})
        return mapping.get(value, mapping.get(str(value).strip(), value))

    def allowed_tables(self) -> set[str]:
        return set(self.views)

    def schema_for_validation(self) -> dict[str, dict[str, str]]:
        """AST 컬럼 대조(sqlglot qualify)에 쓰는 {view: {col: type}} 스키마."""
        return {v.name: {c: "TEXT" for c in v.columns} for v in self.views.values()}

    def describe_value_dictionary(self) -> str:
        """값 정규화 표(정규화 매핑 + 실제 관측 값)을 프롬프트용 텍스트로 만든다.

        라우터와 생성기가 공유한다.
        known_values가 채워져 있으면 실제 관측 값이 함께 실려 값 오매핑을 줄인다.

        주의(보안): 이 텍스트는 프롬프트에 포함된다.
        외부 API 백엔드를 쓰면 관측 값이 그대로 나가므로, 문제되는 환경이면 `NL2SQL_HARVEST=0`.
        """
        lines: list[str] = []
        if self.value_mappings:
            lines.append("## Canonical values (user term -> stored value)")
            for dim, mapping in self.value_mappings.items():
                pairs = ", ".join(f"{k} -> {v}" for k, v in mapping.items())
                lines.append(f"- {dim}: {pairs}")
        if self.known_values:
            lines.append("## Observed values in the database")
            for dim, values in self.known_values.items():
                lines.append(f"- {dim}: {', '.join(values)}")
        return "\n".join(lines)

    def describe_semantic_layer(self) -> str:
        """라우터 프롬프트에 넣을 지표/차원/값 정규화 설명을 만든다."""
        lines = ["## Metrics"]
        for m in self.metrics.values():
            syn = f" (synonyms: {', '.join(m.synonyms)})" if m.synonyms else ""
            lines.append(f"- {m.name}: {m.description}{syn}")
        lines.append("## Dimensions")
        for d in self.dimensions.values():
            syn = f" (synonyms: {', '.join(d.synonyms)})" if d.synonyms else ""
            lines.append(f"- {d.name}: {d.description}{syn}")
        value_dict = self.describe_value_dictionary()
        if value_dict:
            lines.append(value_dict)
        return "\n".join(lines)

    def describe_views(self) -> str:
        """직접 생성 프롬프트에 넣을 큐레이션 뷰 설명을 만든다.

        물리 스키마는 노출하지 않는다.
        직접 생성도 큐레이션 뷰 위에서만 이뤄지게 하는 1층 제약이다.
        """
        lines = []
        for v in self.views.values():
            lines.append(f"- {v.name}({', '.join(v.columns)}): {v.description}")
        notes = self.describe_column_notes()
        if notes:
            lines.append(notes)
        return "\n".join(lines)

    def describe_column_notes(self) -> str:
        """뷰 컬럼 중 정의가 설명을 가진 것들의 주석을 만든다.

        컬럼 이름만 주면 모델은 값의 범위와 단위를 지어낸다.
        `slack_rank`가 0~1 백분위인데 이름만 받은 모델은 0~100으로 읽고 `slack_rank <= 5`를 건다.
        필터가 전 행을 통과하고, SQL은 정상이며 에러도 없다.
        """
        columns = {c for v in self.views.values() for c in v.columns}
        lines = [
            f"  - {d.expression}: {d.description}"
            for d in self.dimensions.values()
            if d.expression in columns and d.description
        ]
        if not lines:
            return ""
        return "\n".join(["  컬럼 주석 (값의 범위·단위를 지어내지 말 것):", *lines])

    # ── 직렬화: 카탈로그는 코드가 아니라 데이터
    #
    # 왕복은 무손실이어야 한다. 이 규약은 편의가 아니라 안전장치다.
    #
    # 빠뜨리기 쉬운 것이 하필 전부 가드의 입력이다 —
    # relative_to가 없으면 blind-filter 검사가 꺼지고, ordinal이 없으면 추이가 순위로 뒤집히고,
    # detail_filter가 없으면 드릴다운이 3건 대신 전 측정을 연다.
    # 셋 다 에러도 행수 이상도 없이 답만 달라진다 — 이 저장소가 조용한 오답이라 부르는 그것이다.
    # 그래서 필드가 늘면 여기도 같이 늘어야 한다.
    # 그것을 지시로 두지 않고 테스트가 dataclass 필드와 직렬화 키를 실제로 대조한다
    # (tests/test_regressions.py::test_catalog_roundtrip_keeps_every_field).
    def to_dict(self) -> dict:
        """JSON 직렬화 가능한 dict로 변환한다 (save_catalog에서 사용)."""
        return {
            "base_view": self.base_view,
            "tenant_dimension": self.tenant_dimension,
            "views": [
                {
                    "name": v.name,
                    "columns": list(v.columns),
                    "description": v.description,
                    "ddl": v.ddl,
                }
                for v in self.views.values()
            ],
            "metrics": [
                {
                    "name": m.name,
                    "expression": m.expression,
                    "description": m.description,
                    "synonyms": list(m.synonyms),
                    "default_order": m.default_order,
                    "detail_filter": [list(f) for f in m.detail_filter],
                }
                for m in self.metrics.values()
            ],
            "dimensions": [
                {
                    "name": d.name,
                    "expression": d.expression,
                    "description": d.description,
                    "synonyms": list(d.synonyms),
                    "numeric": d.numeric,
                    "ordinal": d.ordinal,
                    "relative_to": d.relative_to,
                }
                for d in self.dimensions.values()
            ],
            "value_mappings": self.value_mappings,
            "known_values": {k: list(v) for k, v in self.known_values.items()},
            "detail_columns": [list(c) for c in self.detail_columns],
            "detail_order": self.detail_order,
            "detail_order_label": self.detail_order_label,
            "disabled": sorted(self.disabled),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Catalog:
        """to_dict 출력(또는 손으로 쓴 정의 JSON)에서 카탈로그를 복원한다.

        Raises:
            KeyError: 필수 필드(base_view, views, metrics, dimensions) 누락 시.
        """
        views = {
            v["name"]: CuratedView(
                name=v["name"],
                columns=tuple(v["columns"]),
                description=v.get("description", ""),
                ddl=v.get("ddl", ""),
            )
            for v in data["views"]
        }
        metrics = {
            m["name"]: Metric(
                name=m["name"],
                expression=m["expression"],
                description=m.get("description", ""),
                synonyms=tuple(m.get("synonyms", ())),
                default_order=m.get("default_order", "desc"),
                detail_filter=tuple(tuple(f) for f in m.get("detail_filter", ())),
            )
            for m in data["metrics"]
        }
        dimensions = {
            d["name"]: Dimension(
                name=d["name"],
                expression=d["expression"],
                description=d.get("description", ""),
                synonyms=tuple(d.get("synonyms", ())),
                numeric=d.get("numeric", False),
                ordinal=d.get("ordinal", False),
                relative_to=d.get("relative_to"),
            )
            for d in data["dimensions"]
        }
        return cls(
            views=views,
            metrics=metrics,
            dimensions=dimensions,
            base_view=data["base_view"],
            value_mappings=data.get("value_mappings", {}),
            tenant_dimension=data.get("tenant_dimension"),
            known_values={k: tuple(v) for k, v in data.get("known_values", {}).items()},
            detail_columns=tuple(tuple(c) for c in data.get("detail_columns", ())),
            detail_order=data.get("detail_order", ""),
            detail_order_label=data.get("detail_order_label", ""),
            disabled=set(data.get("disabled", ())),
        )


def load_catalog(path: str) -> Catalog:
    """JSON 정의 파일에서 카탈로그를 읽는다."""
    with open(path, encoding="utf-8") as f:
        return Catalog.from_dict(json.load(f))


def save_catalog(catalog: Catalog, path: str) -> None:
    """카탈로그를 JSON 정의 파일로 저장한다."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(catalog.to_dict(), f, ensure_ascii=False, indent=2)

