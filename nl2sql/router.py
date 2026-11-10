"""라우터: 질문을 모집단으로 나눠 제약 스펙트럼 위의 서로 다른 점에 배치한다.

- semantic : 머리. 시맨틱 레이어(지표 × 차원 × 필터)로 완전히 표현되는 질문.
- free     : 꼬리. 시맨틱 레이어 밖의 탐색적 질문. 큐레이션 뷰 위 직접 생성 + 풀 가드레일.
- clarify  : 스키마로 답할 수 있으나 입력이 미결정이다. 되묻는다.
- abstain  : 스키마로 답할 수 없는 질문. 못 한다고 말한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog
from .llm import LLM
from .semantic import SemanticQuery, parse_semantic_query

_SEMANTIC_QUERY_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "metrics": {"type": "array", "items": {"type": "string"}},
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string"},
                    "op": {"type": "string", "enum": ["=", "!=", ">", ">=", "<", "<="]},
                    "value": {"type": "string"},
                },
                "required": ["dimension", "op", "value"],
                "additionalProperties": False,
            },
        },
        "order": {"type": ["string", "null"], "enum": ["asc", "desc", None]},
        "distinct": {"type": "boolean"},
        # SemanticQuery.detail은 일부러 여기 없다 (additionalProperties: False 라 도달 불가).
        #
        # 상세 목록은 모델이 고르는 형태가 아니라 사용자가 표의 한 줄을 눌러서 가는 곳이다.
        # 어느 줄인지가 정해져야 무엇을 펼칠지도 정해지므로, 질문만 보고는 만들 수 없다.
        # 그 경로는 파이프라인이 만들어 내보내고(_drill_for) 클라이언트가 그대로 돌려보낸다
        # (ask_resolved). parse_semantic_query가 detail을 읽는 것은 그 왕복을 받기 위해서다.
        #
        # 라우팅으로 열어 주면 "몇 건이야"에 100행짜리 목록이 나가는 길이 생긴다.
    },
    "required": ["metrics", "dimensions", "filters", "order", "distinct"],
    "additionalProperties": False,
}

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["semantic", "free", "clarify", "abstain"]},
        "reason": {"type": "string"},
        "semantic_query": _SEMANTIC_QUERY_SCHEMA,
        "clarification": {"type": ["string", "null"]},
        # 되묻기의 선택지. 각 후보는 완결된 시맨틱 질의여야 한다.
        # 슬롯을 채워 가는 방식도 가능하지만, 완결된 후보를 주면 다음 턴이 모델 없이 끝난다.
        "clarify_candidates": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "semantic_query": _SEMANTIC_QUERY_SCHEMA,
                },
                "required": ["label", "semantic_query"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "route",
        "reason",
        "semantic_query",
        "clarification",
        "clarify_candidates",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are the router of a production NL2SQL system.
Classify the user's question into exactly one route.

- "semantic": the question maps cleanly onto the vocabulary below (metrics x dimensions x filters).
  Fill in `semantic_query` using ONLY names from the vocabulary.
  Resolve relative dates using the current date.
  Filter values may arrive in Korean. Use the canonical value mapping when one exists.
  Otherwise pass the user's value through unchanged.
- "free": answerable from the curated views, but not expressible with the vocabulary.
  Exploratory or long-tail analysis.
- "clarify": answerable in principle, but a key term is ambiguous or information is missing.
  Write ONE short clarifying question in Korean in `clarification`.
  Fill `clarify_candidates` with 2-4 complete semantic queries, one per plausible reading.

  Each candidate MUST be shape 1 below: at least one metric, and `distinct` false.
  What is ambiguous is which metric or which period, and a bare list answers neither.

  Each candidate MUST also be independently executable.
  Carry over EVERYTHING the question already fixed, especially the grouping dimensions.
  Differ only in the ambiguous part.
  `label` is a short Korean phrase naming that reading, e.g. "불합격률 기준, 최근 1개월".
  The label must match its query. If the label says 건수, the query carries a count metric.

  Example: for "요즘 문제 있는 라인 알려줘" every candidate keeps dimensions ["line_code"].
  They differ only in metric and/or month filter.
- "abstain": the question cannot be answered from the available data at all.
  Explain briefly in `reason` (Korean).

`clarification` and `reason` are both shown to the END USER verbatim.
Write plain Korean naming business terms only.
NEVER mention internal machinery in either one.
That means no "vocab", "semantic layer", "schema", "catalog", "route", and no raw column names.
Say "규격에 얼마나 가까운지 기준", not "slack_rank < 0.1".
Say "직원 급여 정보가 없습니다", not "vocab에 해당하는 정보가 없습니다".

Prefer "semantic" whenever possible. It is the most reliable route.
Set `semantic_query` to null unless route is "semantic".
A semantic_query is one of two shapes. Pick by what the user wants back.

1. NUMBERS (`distinct: false`) — at least one metric, plus grouping dimensions.
   "how many / rate / trend / compare / most / worst".
2. A LIST OF VALUES (`distinct: true`) — dimensions only, `metrics` EMPTY.
   "which lines exist / what items do we measure / list the X that are Y".
   Filters still apply: "which items failed" -> dimensions ["item_name"], metrics [],
   filters [verdict = FAIL], distinct true.

NEVER pad a list question with a count the user did not ask for.
A measurement count per line does not answer "which lines exist".
Use `distinct: true` instead. If they want both the list and a number, that is shape 1.

`order` sorts by the FIRST metric. Set it only when the question states a direction.
"highest / most / worst" -> "desc". "lowest / smallest / closest to the limit" -> "asc".
Leave it null when the question does not say. The metric definition then supplies its own.
"""


@dataclass
class ClarifyCandidate:
    """되묻기의 선택지 하나 — 사람이 읽을 라벨과, 실행 가능한 완결 질의."""

    label: str
    query: SemanticQuery


@dataclass
class RouteDecision:
    route: str  # semantic | free | clarify | abstain
    reason: str
    semantic_query: SemanticQuery | None = None
    clarification: str | None = None
    candidates: list[ClarifyCandidate] = field(default_factory=list)


class Router:
    def __init__(self, llm: LLM, catalog: Catalog):
        self._llm = llm
        self._catalog = catalog

    def _label_contradicts(self, label: str, query: SemanticQuery) -> bool:
        """라벨이 가리키는 지표가 질의에 없는가.

        사용자는 라벨만 보고 고른다. 질의는 안 보이고 볼 방법도 없다.
        라벨이 "불합격률 기준"인데 질의가 건수를 담고 있으면 컴파일도 가드도 통과하고 표가 나간다.
        고른 것과 받은 것이 다른데 아무도 모른다.

        프롬프트가 라벨과 질의를 맞추라고 시키지만 지시는 보증이 아니다.
        정의의 동의어로 여기서 대조한다.

        다만 이것은 보증이 아니라 부분 검출이다.
        지표만 보므로 라벨이 말하는 기간과 질의의 필터는 대조하지 않는다.
        동의어를 자유 텍스트에 부분일치시키는 방식이다.
        그래서 시맨틱 레이어 밖의 말("비율 기준")도 못 잡는다.
        컴파일러가 닫힌 집합을 대조하는 것과 달리 여기는 빠져나갈 구멍이 있다.

        Args:
            label: 모델이 쓴 후보 이름.
            query: 그 후보의 시맨틱 질의.

        Returns:
            라벨이 지표를 가리키는데 그 지표가 질의에 없으면 True.
            라벨이 지표를 안 가리키면 False — 대조할 것이 없다는 뜻이지 옳다는 뜻이 아니다.
        """
        named = {
            name
            for name, metric in self._catalog.metrics.items()
            if any(word in label for word in metric.synonyms)
        }
        return bool(named) and named.isdisjoint(query.metrics)

    def route(self, question: str, today: str, user_context: dict | None = None) -> RouteDecision:
        user_prompt = (
            f"Current date: {today}\n"
            f"User context (trusted, auto-injected): {user_context or {}}\n\n"
            f"# Semantic layer vocabulary\n{self._catalog.describe_semantic_layer()}\n\n"
            f"# Curated views (for judging answerability)\n{self._catalog.describe_views()}\n\n"
            f"# Question\n{question}"
        )
        raw = self._llm.complete_json(SYSTEM_PROMPT, user_prompt, ROUTE_SCHEMA)

        route = raw.get("route", "clarify")
        if route not in ("semantic", "free", "clarify", "abstain"):
            route = "clarify"
        # 스키마 미준수 출력(로컬 소형 모델 등)에 대비한 방어적 파싱.
        # 정의 검증은 컴파일러(semantic.py)가 다시 한다.
        semantic_query = (
            parse_semantic_query(raw.get("semantic_query")) if route == "semantic" else None
        )

        candidates: list[ClarifyCandidate] = []
        if route == "clarify":
            for item in raw.get("clarify_candidates") or []:
                if not isinstance(item, dict) or not item.get("label"):
                    continue
                query = parse_semantic_query(item.get("semantic_query"))
                # 컴파일이 안 되는 후보는 선택지로 내보내지 않는다.
                # 내보내면 사용자가 고른 뒤에야 실패한다.
                #
                # 값 열거도 후보가 될 수 없다.
                # 되묻는 이유는 어느 지표·어느 기간이냐이고, 목록은 그 물음의 답이 아니다.
                # 라벨이 "불합격 건수 기준"인데 질의에 지표가 없으면 고른 것과 받은 것이 달라진다.
                if not query.metrics or query.distinct:
                    continue
                label = str(item["label"])
                if self._label_contradicts(label, query):
                    continue
                candidates.append(ClarifyCandidate(label, query))

        return RouteDecision(
            route=route,
            reason=raw.get("reason", ""),
            semantic_query=semantic_query,
            clarification=raw.get("clarification"),
            candidates=candidates,
        )
