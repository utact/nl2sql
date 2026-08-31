"""파이프라인 오케스트레이션 — 라우팅 → (컴파일 | 생성) → 가드 → 실행.

    질문
     ├─ abstain  : 스키마로 답할 수 없다. 못 한다고 말한다
     ├─ clarify  : 답은 되지만 입력이 미결정이다. 되묻는다
     ├─ semantic : 시맨틱 레이어로 컴파일한다 (결정론적 SQL)
     └─ free     : 직접 생성 + AST 검증 → 비용 가드 → 읽기전용 실행

가드는 경로마다 다르다.
시맨틱 SQL은 컴파일러가 만든 것이라 AST·비용 가드를 건너뛰고 실행 격리만 걸린다.

해석이 틀리는 것은 가드로 막지 못한다.
그래서 SQL과 해석 가정을 항상 결과에 실어 사람이 대조하게 하고, 0행·잘림은 메시지로 표면화한다.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from dataclasses import dataclass, field

from .ast_guard import AstGuard, QueryStructure
from .catalog import Catalog, spoken
from .cost_guard import make_cost_guard
from .execution import QueryTimeoutError, SqliteExecutor
from .generator import FreeGenerator
from .introspect import (
    CatalogValidationError,
    harvest_dimension_values,
    validate_catalog,
)
from .llm import LLM
from .router import RouteDecision, Router
from .semantic import (
    ALLOWED_OPS,
    Filter,
    SemanticError,
    compile_semantic,
    parse_semantic_query,
    semantic_query_to_dict,
)

ABSTAIN_HEADLINE = "지금 가진 데이터로는 답할 수 없습니다."

# 프롬프트 안에서만 쓰는 말. 사용자 화면에 나오면 시스템 내부가 새어 나온 것이다.
_PROMPT_JARGON = ("vocab", "schema", "prompt", "catalog", "semantic", "metric", "dimension")


def _sentence(text: str) -> str:
    """모델이 쓴 한 줄을 문장으로 끝맺는다.

    거절 사유는 시스템이 쓴 문장 뒤에 이어 붙는다.
    앞은 마침표로 끝나는데 뒤가 안 끝나면 한 줄 안에서 두 문체가 부딪친다.
    문장부호는 프롬프트로 시킬 것이 아니라 여기서 보증할 수 있는 것이다.
    """
    text = text.strip()
    if text and text[-1] not in ".!?…":
        return text + "."
    return text


def _speakable(reason: str, names: set[str]) -> str:
    """모델이 쓴 거절 사유를 그대로 내보낼 수 있는가.

    사유는 자유 텍스트라 무엇이 들어올지 모른다.
    실제로 "판단할 수 있는 지표가 vocab에 없으며" 가 화면까지 나갔다.
    사용자는 vocab이 무엇인지 모르고, 그 한 단어가 나머지 문장까지 못 믿게 만든다.

    고칠 수 없으면 안 쓴다. 머리말만으로도 "답할 수 없다"는 말은 완결된다.
    설명이 빠지는 손해가 내부 용어가 새는 손해보다 작다.

    Args:
        reason: 모델이 쓴 거절 사유.
        names: 정의의 지표·차원 이름들. 이것이 문장에 있으면 사용자 말이 아니다.

    Returns:
        내보낼 수 있으면 문장으로 끝맺어서, 아니면 빈 문자열.
    """
    if not reason.strip():
        return ""
    lowered = reason.lower()
    if any(word in lowered for word in _PROMPT_JARGON):
        return ""
    if any(name in reason for name in names):
        return ""
    return _sentence(reason)


def _headline(description: str) -> str:
    """설명문의 첫 문장만 뽑는다.

    정의의 설명은 모델용이라 길고 조건이 붙는다. 사용자가 읽을 가정은 한 눈에 들어와야 한다.
    """
    head = description.split(".")[0].split("(")[0]
    return head.strip().rstrip(",")


# 로마자를 한글로 읽었을 때 받침이 남는 글자는 l·m·n·r 뿐이다.
# rank→랭크(없음), count→카운트(없음), fail→페일(ㄹ).
_CODA_LATIN = set("lmnr")
_CODA_DIGIT = set("01368")  # 영·일·삼·육·팔


def _has_coda(word: str) -> bool:
    """단어의 마지막 글자에 받침이 있는가 (조사 선택용).

    로마자는 한글 음독을 따른다. 외래어 표기는 관용이라 완벽하지 않다.
    다만 정의의 이름은 영어 단어 조합이라 아래 두 규칙으로 거의 덮인다.
    """
    tail = word.strip().strip("'\"").lower()
    if not tail:
        return False
    # 어말 묵음 e는 읽지 않는다: name→네임(ㅁ), file→파일(ㄹ), code→코드(없음).
    if tail[-1] == "e" and len(tail) > 1:
        tail = tail[:-1]
    ch = tail[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    if ch.isdigit():
        return ch in _CODA_DIGIT
    return ch in _CODA_LATIN


# 문구는 여기서만 필요하지만 키셋은 semantic.ALLOWED_OPS와 같은 여섯 개여야 한다.
# 따로 들고 있으면 한쪽만 늘었을 때(연산자 추가·삭제) 조용히 어긋난다 — 그래서 매 시작 시 대조한다.
_COMPARISON = {
    "=": "{v}인",
    "!=": "{v}이 아닌",
    ">": "{v}보다 큰",
    ">=": "{v} 이상인",
    "<": "{v}보다 작은",
    "<=": "{v} 이하인",
}
assert set(_COMPARISON) == set(ALLOWED_OPS), "필터 연산자 문구가 semantic.ALLOWED_OPS와 어긋난다"


def _condition(label: str, op: str, value: object) -> str:
    """필터 하나를 사람이 읽는 구절로 바꾼다.

    `>=`를 그대로 보여주면 읽는 사람이 기호를 먼저 해석해야 한다.
    확인해 보라고 할 것이면 확인할 수 있는 형태로 줘야 한다.
    """
    text = str(value)
    tail = _COMPARISON.get(op, f"{text} ({op})").format(v=text)
    return f"{label}{_josa(label, '이', '가')} {tail}"


def _euro(word: str) -> str:
    """'로' 인가 '으로' 인가.

    다른 조사와 달리 ㄹ받침은 받침이 없는 것처럼 '로'를 쓴다 — 서울로, 'FAIL'로(페일).
    """
    tail = word.strip().strip("'\"").lower()
    if tail.endswith("e") and len(tail) > 1:
        tail = tail[:-1]
    if tail:
        last = tail[-1]
        riul = (ord(last) - 0xAC00) % 28 == 8 if "가" <= last <= "힣" else last == "l"
        if riul:
            return "로"
    return "으로" if _has_coda(word) else "로"


def _josa(word: str, with_coda: str, without_coda: str) -> str:
    """단어 뒤에 붙일 조사를 고른다.

    가정 목록은 사용자가 실제로 읽는 유일한 문장이라 조사가 어긋나면 바로 눈에 띈다.
    이름이 로마자라 더 그렇다 — `'fail_count' 은`이 아니라 `'fail_count' 는` 이다(카운트).
    """
    return with_coda if _has_coda(word) else without_coda


@dataclass
class Answer:
    status: str  # ok | clarification_needed | abstained | rejected | error
    route: str | None = None
    message: str = ""
    sql: str | None = None
    params: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    truncated: bool = False
    guard_violations: list[str] = field(default_factory=list)
    elapsed_ms: int = 0  # 라우팅→실행 전체 소요 시간
    # 되묻기의 선택지 [{"label": str, "query": {...}}].
    # 고른 query를 그대로 되돌려 보내면 ask_resolved가 처리한다.
    options: list[dict] = field(default_factory=list)
    # 집계 한 줄을 눌러 그 건들을 열 수 있으면 그 정보. 없으면 None.
    # {"label": 누를 때 보일 말, "by": 지금 묶은 차원들, "query": 상세 질의의 바탕}
    drill: dict | None = None


class NL2SQL:
    def __init__(
        self,
        catalog: Catalog,
        llm: LLM,
        executor: SqliteExecutor,
        max_scanned_rows: int = 1_000_000,
        introspect: bool = True,
        harvest_values: bool = True,
        router_llm: LLM | None = None,
        strict_catalog: bool = True,
    ):
        """파이프라인을 조립하고, 실제 DB와 카탈로그를 대조한다.

        Args:
            catalog: 시맨틱 레이어 카탈로그 (정의).
            llm: 직접 생성에 쓸 LLM 백엔드.
            router_llm: 라우팅 전용 LLM. None 이면 llm을 같이 쓴다.
                라우팅은 분류라서 작고 싼 모델로 분리하면 토큰 비용이 준다.
            executor: 읽기전용 실행기 (실제 DB 연결 보유).
            max_scanned_rows: 비용 가드의 예상 스캔량 임계치.
            introspect: True 면 기동 시 카탈로그를 실제 스키마와 대조한다.
            strict_catalog: True(기본)면 드리프트가 기동 실패다 — 배포에서 막는 자리다.
                False 면 어긋난 지표·차원만 끄고 나머지로 계속 답한다.
                어느 쪽이든 어긋난 것을 조용히 쓰지는 않는다.
            harvest_values: True 면 저차원 차원의 실제 값을 수확해 프롬프트에 싣는다.
                값 오매핑을 가장 크게 줄이지만 관측 값이 프롬프트로 나간다.
                외부 API 백엔드를 쓰는 보안 사업장에서는 끈다.

        Raises:
            CatalogValidationError: introspect=True 이고 카탈로그가 스키마와 어긋날 때.
        """
        self.catalog = catalog
        self.catalog_warnings: list[str] = []
        if introspect:
            report = validate_catalog(catalog, executor.connection)
            if not report.ok and strict_catalog:
                raise CatalogValidationError(
                    "카탈로그가 실제 DB 스키마와 어긋납니다:\n- " + "\n- ".join(report.errors)
                )
            # 런타임은 부분 축소한다.
            # "어긋나면 안 뜬다"를 정답으로 두면 컬럼 하나 바뀐 것에 서비스가 내려간다.
            # 어긋난 항목만 끄고 나머지로 답하되, 그것을 고른 질문에는 되묻기로 말해 준다.
            catalog.disabled = set(report.broken)
            self.catalog_warnings = report.warnings + (report.errors if not strict_catalog else [])
        if harvest_values:
            self.catalog.known_values = harvest_dimension_values(
                catalog, executor.connection
            )
        dialect = getattr(executor, "dialect", "sqlite")
        # 화면이 "지금 어느 모델과 말하고 있는가"를 물을 수 있어야 한다.
        # 고정 응답으로 띄운 데모를 실제 응답으로 착각하면 그 화면 전체가 거짓이 된다.
        self.llm = llm
        self.router = Router(router_llm or llm, catalog)
        self.generator = FreeGenerator(llm, catalog, dialect=dialect)
        self.ast_guard = AstGuard(catalog, max_limit=executor.max_rows, dialect=dialect)
        self.cost_guard = make_cost_guard(executor, max_scanned_rows)
        self.executor = executor
        self.audit_log: list[dict] = []  # 5층 런타임 관측

    def ask(self, question: str, user_context: dict | None = None) -> Answer:
        """자연어 질문 하나를 라우팅→(컴파일|생성)→가드→실행까지 처리한다.

        Args:
            question: 사용자의 자연어 질문.
            user_context: 신뢰된 자동 주입 컨텍스트 (예: {"region": "EAST"}).

        Returns:
            Answer. ok가 아니어도 예외를 던지지 않고 사유를 message·guard_violations로 낸다.
        """
        started = _time.perf_counter()
        today = _dt.date.today().isoformat()

        try:
            decision = self.router.route(question, today, user_context)
        except Exception as e:  # noqa: BLE001 — 백엔드 장애도 1급 출력이다
            answer = Answer(status="error", message=self._backend_failure("라우팅", e))
            answer.elapsed_ms = int((_time.perf_counter() - started) * 1000)
            self._audit(question, answer)
            return answer

        answer = self.dispatch(decision, question, today, user_context)
        answer.elapsed_ms = int((_time.perf_counter() - started) * 1000)
        self._audit(question, answer, decision.reason)
        return answer

    def dispatch(
        self,
        decision: RouteDecision,
        question: str,
        today: str,
        user_context: dict | None = None,
    ) -> Answer:
        """이미 내려진 라우팅 결정 하나를 사용자에게 나갈 답으로 만든다.

        `ask`에서 떼어 둔 것은 결정과 결과를 한 호출로 묶기 위해서다.
        라우팅을 재 놓고 `ask`를 다시 부르면 모델이 한 번 더 불리고, 모델은 비결정적이다.
        잰 결정과 본 결과가 서로 다른 것이 된다. 측정 도구는 이 함수를 쓴다.
        """
        if decision.route == "abstain":
            # 거절 사유는 모델이 쓴 자유 텍스트라 검증할 방법이 없다.
            # 그대로 내보내면 프롬프트의 내부 용어가 샌다 ("vocab에 해당하는 정보가 없습니다").
            # 그래서 첫 문장은 시스템이 쓰고, 모델의 문장은 뒤에 붙는 보조 설명으로 둔다.
            return Answer(
                status="abstained",
                route="abstain",
                message=" ".join(
                    filter(
                        None,
                        [
                            ABSTAIN_HEADLINE,
                            _speakable(
                                decision.reason,
                                set(self.catalog.metrics) | set(self.catalog.dimensions),
                            ),
                        ],
                    )
                ),
            )
        if decision.route == "clarify":
            return Answer(
                status="clarification_needed",
                route="clarify",
                message=decision.clarification or decision.reason,
                options=[
                    {"label": c.label, "query": semantic_query_to_dict(c.query)}
                    for c in decision.candidates
                ],
            )
        if decision.route == "semantic":
            return self._run_semantic(decision, user_context)
        return self._run_free(question, today, user_context)

    def ask_resolved(self, query: dict, user_context: dict | None = None) -> Answer:
        """되묻기의 선택지 하나를 받아 실행한다. 모델을 부르지 않는다.

        되묻기가 1급 경로이려면 묻는 자리만으로는 부족하고 답을 받을 자리가 있어야 한다.
        없으면 사용자는 질문을 통째로 다시 쓰고, 그 순간 처음 질문이 확정했던 축이 사라진다.

        그래서 답을 자유 텍스트가 아니라 지표·차원으로 받는다.
        선택지 하나가 완결된 시맨틱 질의라서 이 턴은 라우팅도 토큰도 필요 없다.

        Args:
            query: 선택된 시맨틱 질의 dict. 신뢰 경계 바깥이다.
                클라이언트가 보낸 값이므로 compile_semantic이 카탈로그로 다시 검증한다.
            user_context: 신뢰된 자동 주입 컨텍스트.

        Returns:
            Answer. 정의 검증에 실패하면 다시 되묻기로 내려간다.
        """
        started = _time.perf_counter()
        decision = RouteDecision(
            route="semantic", reason="되묻기 선택", semantic_query=parse_semantic_query(query)
        )
        answer = self._run_semantic(decision, user_context)
        answer.elapsed_ms = int((_time.perf_counter() - started) * 1000)
        self._audit("(되묻기 선택)", answer)
        return answer

    # ── 머리: 시맨틱 레이어
    def _run_semantic(self, decision, user_context: dict | None) -> Answer:
        query = decision.semantic_query
        # 자동 주입: 로그인 컨텍스트로 답의 범위를 좁힌다 (예: 테넌트 격리)
        # 이 필터의 저자는 파이프라인이다. 모델이 쓴 같은 이름의 필터는 걷어내고 다시 넣는다.
        #
        # 라우터 프롬프트가 컨텍스트를 모델에게 보여 주므로 모델이 이 필터를 먼저 써 두는 일이 잦다.
        # 그것을 "이미 있으니 주입 아님"으로 세면 범위는 그대로 좁혀지는데 좁혔다는 고지만 사라진다.
        # 사용자는 전사 수치로 읽는다 — 표도 SQL도 맞고 빠진 것은 문장 하나뿐인 조용한 오답이다.
        #
        # 걷어내는 쪽이 안전 방향이기도 하다. 사용자가 다른 값을 말했더라도 컨텍스트가 이긴다.
        # PostgresExecutor의 RLS는 이미 그렇게 동작하므로 SQLite 경로가 그 보증에 맞춰진다.
        tenant_dim = self.catalog.tenant_dimension
        injected = None
        if tenant_dim and user_context and tenant_dim in user_context:
            query.filters = [f for f in query.filters if f.dimension != tenant_dim]
            # injected=True로 표시해 둔다. 컴파일러의 blind-filter 검사가 이 필터를 분모에서 뺀다.
            # 표시가 없으면 "사용자가 건 필터가 전부 헛돈다"는 판정이 컨텍스트 한 칸으로 뒤집힌다.
            query.filters.append(
                Filter(tenant_dim, "=", str(user_context[tenant_dim]), injected=True)
            )
            injected = tenant_dim

        try:
            sql, params = compile_semantic(query, self.catalog)
        except SemanticError as e:
            # 정의 검증 실패는 라우터의 오분류 신호다. 되묻기로 내린다.
            #
            # 되물으면서 누를 것을 함께 준다.
            # 사유만 돌려주면 사용자는 질문을 통째로 다시 쓰고, 그때 앞 질문이 정한 것이 사라진다.
            # 컴파일러가 갈 곳을 알고 있을 때만 나가므로, 없으면 사유만 나간다.
            options = []
            if e.suggestion is not None and e.suggestion.metrics:
                # 라벨은 받게 될 표를 그대로 적는다.
                # "그 기준으로 보기"는 사용자가 이미 말한 것을 되풀이할 뿐이라 대안으로 안 읽힌다.
                label = spoken(self.catalog.metrics[e.suggestion.metrics[0]])
                if e.suggestion.dimensions:
                    axis = self.catalog.dimensions[e.suggestion.dimensions[0]]
                    label = f"{spoken(axis)}별 {label}"
                options = [
                    {"label": label, "query": semantic_query_to_dict(e.suggestion)}
                ]
            return Answer(
                status="clarification_needed",
                route="semantic",
                message=str(e),
                options=options,
            )

        assumptions = self._semantic_assumptions(query, injected)
        answer = self._execute(
            sql, params, route="semantic", assumptions=assumptions, user_context=user_context
        )
        if answer.status == "ok" and answer.rows:
            answer.drill = self._drill_for(query)
        return answer

    def _drill_for(self, query) -> dict | None:
        """집계 한 줄을 눌렀을 때 열 상세 질의를 미리 만들어 둔다.

        지표가 무엇을 셌는지(Metric.detail_filter)를 함께 넣는 것이 핵심이다.
        near_limit_count의 조건은 집계식 안에 있어서,
        그것 없이 상세를 뽑으면 3건이 아니라 그 라인의 전 측정이 나온다.
        같은 줄을 눌렀는데 다른 답이 되는 것이다.

        Args:
            query: 방금 실행한 시맨틱 질의.

        Returns:
            상세 질의 정보. 보여줄 컬럼이 선언되어 있지 않거나 누를 줄이 없으면 None.
            지표가 둘 이상이면 None — 아래 참조.
        """
        if not self.catalog.detail_columns:
            return None
        if query.detail or query.distinct or not query.dimensions:
            return None
        # 지표가 정확히 하나일 때만 누를 수 있다.
        #
        # 상세 질의는 "이 숫자가 센 것"을 필터로 다시 적은 것인데(Metric.detail_filter),
        # 지표가 둘이면 어느 숫자를 여는지가 정해지지 않는다.
        # 두 조건을 AND로 합치면 서로 모순되기까지 한다 —
        # fail_count와 near_limit_count를 같이 물으면 verdict가 FAIL 이면서 PASS 여야 한다.
        # 그러면 0행이 나오고 화면은 "조건에 맞는 것이 없습니다"라며 해석 가정을 의심하라고 한다.
        # 가정은 멀쩡한데 원인을 엉뚱한 데로 보내는 것이라, 안 여는 편이 낫다.
        #
        # 화면도 마지막 컬럼만 보고 누를 수 있는지를 정하므로(app/static/index.html),
        # 지표가 둘이면 클릭 판정과 필터가 서로 다른 지표를 보게 된다.
        if len(query.metrics) != 1:
            return None
        base = semantic_query_to_dict(query)
        base["metrics"] = []
        base["dimensions"] = []
        base["detail"] = True
        for name in query.metrics:
            for dimension, op, value in self.catalog.metrics[name].detail_filter:
                base["filters"].append({"dimension": dimension, "op": op, "value": value})
        return {"label": "이 줄에 해당하는 측정 보기", "by": list(query.dimensions), "query": base}

    def _semantic_assumptions(self, query, injected: str | None) -> list[str]:
        """컴파일된 질의에서 해석 가정을 만든다.

        이 경로의 가정은 모델이 쓰지 않는다.
        무엇을 골랐는지가 지표·차원으로 확정되어 있어 정의에서 그대로 읽어 낸다.
        직접 생성 쪽은 모델이 인지하지 못한 가정이 빠지지만, 여기에는 그 구멍이 없다.

        문장에 지표·차원 이름을 넣지 않는다.
        읽는 사람은 검사원이지 이 정의를 쓴 사람이 아니다.
        `fail_count`나 `>=`를 보여주고 "확인하세요"라고 하면 확인할 수 없는 것을 시킨 것이고,
        그러면 보여주지 않은 것과 같다. 이름은 SQL 패널에 있다.

        Args:
            query: 컴파일에 쓰인 SemanticQuery.
            injected: 자동 주입된 차원 이름. 주입이 없었으면 None.

        Returns:
            사용자에게 보여줄 한 문장짜리 가정 목록.
        """
        lines: list[str] = []

        def label(name: str) -> str:
            return _headline(self.catalog.dimensions[name].description)

        if query.metrics:
            what = ", ".join(_headline(self.catalog.metrics[m].description) for m in query.metrics)
            lines.append(f"{what}{_josa(what, '을', '를')} 구했습니다.")

        if query.dimensions:
            labels = ", ".join(label(d) for d in query.dimensions)
            lines.append(f"{labels}{_euro(labels)} 나눠서 봤습니다.")

        for f in query.filters:
            normalized = self.catalog.normalize_value(f.dimension, str(f.value))
            condition = _condition(label(f.dimension), f.op, normalized)
            if str(normalized) != str(f.value):
                # 사용자의 말을 저장된 표기로 바꾼 것은 시스템의 해석이다. 틀릴 수 있으니 밝힌다.
                said = str(f.value)
                read_as = (
                    f"'{said}'{_josa(said, '을', '를')}"
                    f" '{normalized}'{_euro(str(normalized))} 읽고"
                )
                lines.append(f"{read_as}, {condition} 것만 봤습니다.")
            else:
                lines.append(f"{condition} 것만 봤습니다.")

        if injected:
            who = label(injected)
            lines.append(
                f"{who}{_josa(who, '은', '는')} 로그인 정보에서 자동으로 채웠습니다."
                " 다른 범위를 보려면 알려 주세요."
            )

        # 값 열거에는 지표도 정렬도 없다. "높은 순"이라는 개념 자체가 없다.
        # 대신 세지 않았다는 것을 말해 준다 — 목록을 물었는데 숫자를 받으면 묻지 않은 답이다.
        if query.distinct:
            lines.append("어떤 값들이 있는지만 보여드립니다. 개수는 세지 않았습니다.")
            return lines

        # 개별 측정을 보는 형태. 집계가 없으니 "무엇을 구했다"가 없다.
        # 대신 세지 않고 하나씩 보여준다는 것과, 무엇을 위로 올렸는지를 말한다.
        if query.detail:
            lines.append("조건에 맞는 측정을 하나씩 보여드립니다.")
            if self.catalog.detail_order_label:
                lines.append(f"{self.catalog.detail_order_label}으로 정렬했습니다.")
            return lines

        # 정렬 방향은 목록형 답의 의미를 통째로 뒤집는다.
        # 사용자가 판단할 수 있는 가정이므로 감추지 않는다.
        dims = [self.catalog.dimensions[d] for d in query.dimensions]
        by_axis = query.order is None and dims and dims[0].ordinal
        head = self.catalog.metrics[query.metrics[0]]
        order = (query.order or ("asc" if by_axis else head.default_order)).lower()
        if by_axis:
            axis = label(dims[0].name)
            lines.append(f"{axis} 순서대로 정렬했습니다.")
        else:
            axis = _headline(head.description)
            high = "높은" if order == "desc" else "낮은"
            lines.append(f"{axis}{_josa(axis, '이', '가')} {high} 순으로 정렬했습니다.")

        # 행수 상한은 여기 넣지 않는다.
        # 이 목록에는 사용자가 자기 의도와 대조해 틀렸다고 말할 수 있는 것만 들어간다.
        # 상한은 해석이 아니라 실행 조건이라 대조할 대상이 없다.
        # 실제로 물린 경우만 _execute가 경고로 낸다.
        return lines

    # ── 꼬리: 직접 생성 + 풀 가드레일
    def _free_assumptions(
        self, structure: QueryStructure | None, model_wrote: list[str]
    ) -> list[str]:
        """직접 생성의 해석 가정. 실행될 SQL의 AST에서 역산한다.

        `_semantic_assumptions`와 같은 이유로 모델이 쓴 문장을 신뢰하지 않는다.
        차이는 컴파일러가 없다는 것뿐이다.
        가드가 이미 파싱해 둔 구조(테이블·필터·묶음·정렬)를 그대로 옮긴다.

        다만 semantic보다 한 칸 낮다.
        거기는 축을 정의가 고르고 여기는 모델이 아무 컬럼이나 고른다.
        "어떤 컬럼으로 걸렀는가"는 맞아도 "그 축이 유효한가"는 말 못한다.

        구조가 없으면 기준 뷰 이름과 모델 문장으로 돌아간다.
        검증에 실패해 실행 안 될 SQL이라 잃을 게 없다.
        """
        if structure is None:
            view = self.catalog.views.get(self.catalog.base_view)
            what = _headline(view.description) if view else self.catalog.base_view
            return [
                f"{what}에서 답했습니다.",
                "질문이 이 데이터에 없는 것을 물었다면 답이 아닐 수 있습니다.",
                *model_wrote,
            ]

        by_column = {d.expression: d for d in self.catalog.dimensions.values()}

        def label(column: str) -> str:
            dim = by_column.get(column)
            return _headline(dim.description) if dim else column

        def table_label(name: str) -> str:
            view = self.catalog.views.get(name)
            return _headline(view.description) if view else name

        lines = [
            f"{', '.join(table_label(t) for t in structure.tables) or self.catalog.base_view}"
            "에서 답했습니다.",
            "질문이 이 데이터에 없는 것을 물었다면 답이 아닐 수 있습니다.",
        ]

        for column, op, value in structure.filters:
            if op == "raw":
                lines.append(f"`{value}` 조건으로 걸렀습니다.")
            elif op == "in":
                axis = label(column)
                lines.append(f"{axis}{_josa(axis, '이', '가')} {value} 중 하나인 것만 봤습니다.")
            else:
                lines.append(f"{_condition(label(column), op, value)} 것만 봤습니다.")

        if structure.group_by:
            labels = ", ".join(label(c) for c in structure.group_by)
            lines.append(f"{labels}{_euro(labels)} 나눠서 봤습니다.")

        if structure.order_by:
            parts = []
            for column, direction in structure.order_by:
                axis = label(column)
                high = "높은" if direction == "desc" else "낮은"
                parts.append(f"{axis}{_josa(axis, '이', '가')} {high} 순")
            lines.append(f"{', '.join(parts)}으로 정렬했습니다.")

        lines.extend(model_wrote)
        return lines

    def _run_free(self, question: str, today: str, user_context: dict | None) -> Answer:
        try:
            generation = self.generator.generate(question, today, user_context)
        except Exception as e:  # noqa: BLE001 — 백엔드 장애도 1급 출력이다
            return Answer(status="error", route="free", message=self._backend_failure("생성", e))

        validation = self.ast_guard.validate(generation.sql)
        assumptions = self._free_assumptions(validation.structure, generation.assumptions)

        if not validation.ok:
            return Answer(
                status="rejected",
                route="free",
                sql=generation.sql,
                message="생성된 SQL이 정적 검증을 통과하지 못했습니다.",
                guard_violations=validation.violations,
                assumptions=assumptions,
            )

        cost = self.cost_guard.check(validation.sql)
        if not cost.ok:
            return Answer(
                status="rejected",
                route="free",
                sql=validation.sql,
                message=cost.message,
                guard_violations=[cost.message],
                assumptions=assumptions,
            )

        return self._execute(
            validation.sql, [], route="free",
            assumptions=assumptions, user_context=user_context,
            limit_injected=validation.limit_injected,
        )

    # ── 공통 실행 + 상식 검사
    def _execute(
        self,
        sql: str,
        params: list[str],
        route: str,
        assumptions: list[str],
        user_context: dict | None = None,
        limit_injected: bool = False,
    ) -> Answer:
        """질의를 실행하고 상식 검사 결과를 메시지로 붙인다.

        Args:
            limit_injected: 가드가 LIMIT을 강제로 넣었는가.
                실행기는 자기가 넣지 않은 LIMIT에 걸린 것을 알아채지 못한다.
                SQL이 이미 상한만큼만 가져와서 "잘렸다"와 "이만큼이다"가 구별되지 않는다.
        """
        try:
            # 앱의 WHERE 절 주입은 편의 장치다.
            # 같은 컨텍스트를 DB에도 넘겨야 행 단위 권한(RLS)이 실제로 걸린다.
            # 컨텍스트가 비면 정책이 0행을 돌려준다 (fail-closed).
            self.executor.apply_context(user_context)
            result = self.executor.run(sql, params)
        except QueryTimeoutError as e:
            return Answer(status="rejected", route=route, sql=sql, params=params, message=str(e))
        except Exception as e:  # noqa: BLE001 — 실행 오류는 그대로 표면화
            return Answer(status="error", route=route, sql=sql, params=params, message=str(e))

        notes = []
        if not result.rows:
            # 화면에서 가정은 이 메시지보다 위에 있다.
            # 방향을 말하면 배치가 바뀔 때마다 틀리므로 아예 가리키지 않는다.
            notes.append("조건에 맞는 것이 없습니다. 해석 가정이 의도와 같은지 확인해 주세요.")
            # RLS가 걸린 DB에서 컨텍스트가 비면 정책이 전부 막는다.
            # 그때 "해석을 확인하라"고만 하면 원인을 엉뚱한 데로 보낸다 — 가정은 멀쩡하다.
            # 행 단위 권한이 없는 백엔드에서는 컨텍스트가 0행의 원인이 될 수 없다.
            # 그런데도 매번 이 줄을 붙이면 진짜 원인(필터가 아무것도 못 맞힘)을 가린다.
            # 늘 나오는 경고는 곧 아무도 안 읽는 경고가 된다.
            tenant = self.catalog.tenant_dimension
            enforced = getattr(self.executor, "enforces_context", False)
            if enforced and tenant and not (user_context or {}).get(tenant):
                label = _headline(self.catalog.dimensions[tenant].description)
                notes.append(
                    f"'{label}'{_josa(label, '이', '가')} 정해지지 않았습니다."
                    " 접근 범위가 비어 있으면 아무 행도 보이지 않을 수 있습니다."
                )
        # 잘림 판정은 메시지를 만들기 전에 끝나야 한다.
        # 나중에 플래그만 켜면 알리는 문장은 이미 만들어진 뒤다.
        hit_injected_limit = limit_injected and len(result.rows) >= self.executor.max_rows
        truncated = result.truncated or hit_injected_limit
        if truncated:
            notes.append(f"결과가 {self.executor.max_rows}행에서 잘렸습니다.")

        return Answer(
            status="ok",
            route=route,
            sql=sql,
            params=params,
            columns=result.columns,
            rows=result.rows,
            assumptions=assumptions,
            truncated=truncated,
            message=" ".join(notes),
        )

    @staticmethod
    def _backend_failure(stage: str, error: Exception) -> str:
        """모델 백엔드 장애를 사용자에게 보여줄 한 문장으로 만든다.

        백엔드는 앱의 버그와 무관하게 실패한다 — 모델이 EOL 되고, 키가 만료되고, 망이 끊긴다.
        예외를 그대로 던지면 HTTP 500이 되어 "실패도 1급 출력"이 이 자리에서 깨진다.
        원인 문자열은 그대로 싣는다. 감추면 운영에서 되짚을 수 없다.
        """
        return f"{stage} 단계에서 모델 백엔드가 실패했습니다: {type(error).__name__}: {error}"

    def _audit(self, question: str, answer: Answer, model_reason: str = "") -> None:
        """5층 관측. 화면에서 뺀 것도 여기에는 남긴다.

        모델이 쓴 거절 사유는 내부 용어가 섞이면 사용자에게 안 내보낸다.
        그렇다고 버리면 나중에 왜 거절했는지 되짚을 방법이 사라진다.
        읽는 사람이 다르므로 두는 자리도 다르다.

        Args:
            question: 사용자의 질문.
            answer: 사용자에게 나간 응답.
            model_reason: 모델이 쓴 사유 원문. 화면에 나갔든 안 나갔든 그대로 남긴다.
        """
        self.audit_log.append(
            {
                "at": _dt.datetime.now().isoformat(timespec="seconds"),
                "question": question,
                "route": answer.route,
                "status": answer.status,
                "sql": answer.sql,
                "rows": len(answer.rows),
                "elapsed_ms": answer.elapsed_ms,
                "message": answer.message,
                "model_reason": model_reason,
            }
        )
