"""골든셋 3층 — 라우팅.

실제 모델을 태우는 유일한 층이라 기본으로 건너뛴다.

    NL2SQL_TEST_LLM=1 pytest tests/test_layer3_routing.py -v

비교 대상이 SQL이 아니라 라우팅 튜플(경로 + 지표·차원 이름)이라는 점이 핵심이다.
같은 답을 내는 SQL은 여러 개라 텍스트 비교는 플레이키하지만, 지표·차원 이름은 정의에 고정되어 있다.
CI에서 플레이키 테스트는 곧 무시되고, 무시되는 순간 골든셋은 죽는다.

## 케이스 수

16건이다. 이보다 적으면 통과율이 아무것도 결정하지 못한다.
4건에서 "2건 통과"는 동전 던지기와 구별되지 않는다.

## 무엇을 겨누는가

라우팅 오류는 대칭이 아니다. 잘못 가는 방향마다 뒤에 받쳐 주는 것이 다르다.

    free     -> semantic : 컴파일이 실패해 되묻기로 강등된다. 안전
    abstain  -> semantic : 정의에 없으므로 컴파일이 실패한다. 안전
    semantic -> free     : 신뢰도는 떨어지지만 가드레일 5겹이 받는다
    clarify  -> semantic : 뒤가 없다. 컴파일도 가드도 통과하고 숫자도 그럴듯하다
    abstain  -> free     : 뒤가 없다. 가드레일은 SQL이 위험한지만 보고 답이 되는지는 안 본다

아래 두 줄이 이 층의 존재 이유다.
직접 생성은 답할 것이 없어도 가장 가까운 것을 내놓으므로 해석 가정 하나만 남는다.
그래서 케이스 배분이 균등하지 않다 — 되묻기(C)와 거절(D)이 두껍다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from domain import catalog as build_catalog
from domain.seed import seed
from nl2sql import NL2SQL, SqliteExecutor, load_env_file, resolve_llm

pytestmark = pytest.mark.skipif(
    os.environ.get("NL2SQL_TEST_LLM") != "1",
    reason="실제 모델이 필요하다. NL2SQL_TEST_LLM=1로 켠다",
)

def _today_inside_the_data(db_path: str) -> str:
    """시드 데이터의 마지막 달 안쪽 날짜를 기준일로 쓴다.

    "이번 달", "요즘" 같은 상대 표현은 기준일에 걸려 있다.
    기준일을 손으로 박아 두면 데이터가 늘거나 줄 때 범위 밖으로 밀려난다.
    그러면 모델이 옳게 거절하는데 골든셋이 틀렸다고 채점한다.

    기대값이 틀린 골든셋은 통과율을 못 믿게 하는 데서 끝나지 않고, 맞는 동작을 고치라고 시킨다.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        last_month = conn.execute(
            "SELECT MAX(inspection_month) FROM inspection_results"
        ).fetchone()[0]
    finally:
        conn.close()
    return f"{last_month}-15"


@dataclass(frozen=True)
class Case:
    """라우팅 골든 케이스 하나.

    Attributes:
        question: 사용자 질문.
        route: 기대 경로.
        metrics: 반드시 포함되어야 할 지표 이름.
        dimensions: 반드시 포함되어야 할 차원 이름.
            되묻기에서는 모든 후보가 이 차원을 가져야 한다.
            질문이 이미 정한 것을 후보가 잃으면 사용자는 고르고 나서 엉뚱한 답을 받는다.
        distinct: 값 열거 질문인가. True 면 지표가 비어 있어야 한다.
    """

    question: str
    route: str
    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    distinct: bool = False
    note: str = ""


# ── A. 시맨틱 — 정의가 흔들리지 않는가
SEMANTIC_CASES = [
    Case("월별 검사 건수 추이 보여줘", "semantic",
         ("measurement_count",), ("inspection_month",)),
    Case("항목별 불합격 건수 알려줘", "semantic",
         ("fail_count",), ("item_name",)),
    Case("라인별 불합격률 비교해줘", "semantic",
         ("fail_rate",), ("line_code",)),
    Case("분류별로 로트가 몇 개나 있어?", "semantic",
         ("lot_count",), ("category",),
         note="lot_count와 measurement_count는 설명으로만 갈린다. "
              "'로트 개수'를 '측정 건수'로 답하면 숫자가 조용히 부풀어 오른다"),
    Case("라인별로 규격에 아슬아슬한 건수 알려줘", "semantic",
         ("near_limit_count",), ("line_code",),
         note="slack_rank를 그룹핑 축으로 쓰려는 유혹이 있는 자리다. "
              "정의가 '그룹핑 축으로는 쓰지 않는다'고 못 박아 두었다"),
]

# ── B. 값 열거 — 묻지 않은 숫자를 끼워 넣지 않는가
#
# 시맨틱 문법에 열거가 없던 시절, 프롬프트가 "지표가 없으면 카운트를 끼워 넣어라"로 때웠다.
# "라인 어디어디 있어?"에 라인별 측정 건수가 나왔고, 끼워 넣은 사실은 가정에도 안 남았다.
ENUMERATION_CASES = [
    Case("지금 라인 어디어디 있는지 알려줘", "semantic",
         (), ("line_code",), distinct=True),
    Case("우리가 재고 있는 검사 항목 뭐뭐 있어?", "semantic",
         (), ("item_name",), distinct=True),
    Case("규격 벗어난 항목 목록만 뽑아줘", "semantic",
         (), ("item_name",), distinct=True,
         note="필터는 살아 있어야 한다 — verdict = FAIL"),
]

# ── C. 되묻기 — 추측하지 않는가
#
# 뒤가 없는 유일한 구간. 여기서 semantic으로 새면 아무 층도 못 잡는다.
CLARIFY_CASES = [
    Case("요즘 문제 있는 라인 알려줘", "clarify", dimensions=("line_code",),
         note="'문제'가 불합격률인지 경계 근접인지 건수인지 미결정. "
              "'요즘'의 범위도 미결정. 그래도 라인별이라는 것은 정해졌다"),
    Case("품질 안 좋은 항목 알려줘", "clarify", dimensions=("item_name",),
         note="'품질'이 정의에 없는 업무 용어다"),
    Case("이번 달 상태 어때?", "clarify",
         note="측정 대상 자체가 미결정. 차원조차 안 정해진 경우라 "
              "후보가 서로 다른 차원을 들고 와도 된다"),
]

# ── D. 거절 — 빈칸을 그럴듯하게 채우지 않는가
ABSTAIN_CASES = [
    Case("직원 연봉 알려줘", "abstain"),
    Case("작업자별 불합격률 뽑아줘", "abstain",
         note="작업자 컬럼이 뷰에 없다. line_code로 바꿔 답하면 조용한 오답이다"),
    Case("검사 설비 점검 이력 보여줘", "abstain"),
]

# ── E. 직접 생성 — 시맨틱 레이어 밖인데 뷰로는 답이 되는가
#
# 뷰에는 있으나 시맨틱 레이어에 없는 것을 겨눈다.
# inspected_on은 뷰 컬럼이지만 차원으로 선언되어 있지 않다.
FREE_CASES = [
    Case("항목별 측정값 분포에서 이상치 찾아줘", "free",
         note="표준편차·백분위가 필요하다. 지표 × 차원으로 표현되지 않는다"),
    Case("불합격이 특정 요일에 몰리는지 봐줘", "free",
         note="inspected_on은 뷰에 있지만 요일 차원은 없다"),
]

ALL_CASES = (
    SEMANTIC_CASES + ENUMERATION_CASES + CLARIFY_CASES + ABSTAIN_CASES + FREE_CASES
)


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    """실제 백엔드를 붙인 파이프라인.

    `load_env_file()`을 여기서 부르는 것이 이 픽스처의 요점이다.
    빠뜨리면 `.env`를 못 읽어 오프라인 기본값으로 떨어지고, 앱이 쓰는 것과 다른 모델을 재게 된다.
    그때 이 층은 실패하지 않는다. 다른 것을 잴 뿐이다.

    그래서 어느 백엔드를 쟀는지 항상 출력한다. 모델 이름이 없는 통과율은 아무 뜻이 없다.
    """
    load_env_file()
    llm = resolve_llm()
    name = getattr(llm, "model", None) or getattr(llm, "model_id", "?")
    print(f"\n[3층 골든셋] 백엔드: {type(llm).__name__} / {name}")
    db = seed(str(tmp_path_factory.mktemp("llm") / "inspection.db"))
    today = _today_inside_the_data(db)
    print(f"[3층 골든셋] 기준일: {today}")
    return today, NL2SQL(catalog=build_catalog(), llm=llm, executor=SqliteExecutor(db))


@pytest.fixture(scope="module")
def decide(pipeline):
    """질문 하나당 라우팅을 한 번만 부르고 그 결과를 재사용한다.

    캐시가 없으면 같은 질문이 갈래 검사와 정의 검사에서 각각 한 번씩 호출된다.
    모델이 비결정적이라 두 검사가 서로 다른 답을 채점하게 되고, 통과율이 자기 자신과 모순된다.

    모델의 비결정성 자체는 여기서 재지 않는다.
    그건 같은 골든셋을 여러 번 돌려서 재야 할 것이지, 우연히 두 번 불려서 재질 것이 아니다.
    """
    today, nl2sql = pipeline
    cache: dict[str, object] = {}

    def _decide(question: str):
        if question not in cache:
            cache[question] = nl2sql.router.route(question, today, None)
        return cache[question]

    return _decide


def _id(case: Case) -> str:
    return f"{case.route}:{case.question}"


@pytest.mark.parametrize("case", ALL_CASES, ids=_id)
def test_route_is_as_expected(decide, case: Case):
    """질문이 기대한 갈래로 가는가.

    정의보다 먼저 본다. 갈래가 틀리면 정의 비교는 의미가 없다.
    실패 메시지도 "지표가 없다"가 아니라 "되물어야 하는데 실행했다"여야 한다.
    """
    decision = decide(case.question)
    assert decision.route == case.route, (
        f"{case.question!r}\n"
        f"  기대: {case.route}  실제: {decision.route}\n"
        f"  모델 사유: {decision.reason}\n"
        f"  {case.note}"
    )


@pytest.mark.parametrize(
    "case", SEMANTIC_CASES + ENUMERATION_CASES, ids=_id
)
def test_semantic_vocabulary_is_as_expected(decide, case: Case):
    """시맨틱으로 갔을 때 고른 이름이 맞는가.

    실패했을 때 고칠 곳은 거의 항상 모델이 아니라 정의의 설명·동의어다.
    프롬프트를 손보기 전에 description을 먼저 읽는다.
    """
    decision = decide(case.question)
    if decision.route != "semantic":
        pytest.skip(f"갈래가 이미 틀렸다 ({decision.route}). test_route_is_as_expected를 본다")

    query = decision.semantic_query
    assert query.distinct is case.distinct, (
        f"{case.question!r}: distinct 기대 {case.distinct}, 실제 {query.distinct}"
    )
    if case.distinct:
        # 열거에 지표가 붙으면 사용자가 묻지 않은 숫자를 받는다.
        assert not query.metrics, (
            f"{case.question!r}: 목록만 물었는데 지표 {query.metrics} 를 끼워 넣었다"
        )
    for metric in case.metrics:
        assert metric in query.metrics, (
            f"{case.question!r}: 지표 {metric!r} 기대, 실제 {query.metrics}. {case.note}"
        )
    for dimension in case.dimensions:
        assert dimension in query.dimensions, (
            f"{case.question!r}: 차원 {dimension!r} 기대, 실제 {query.dimensions}. {case.note}"
        )


@pytest.mark.parametrize("case", CLARIFY_CASES, ids=_id)
def test_clarify_carries_over_what_the_question_already_fixed(decide, case: Case):
    """되묻기 후보가 이미 정해진 것을 잃어버리지 않는가.

    "요즘 문제 있는 라인"에서 '문제'는 미결정이지만 '라인별'은 결정됐다.
    후보가 line_code를 잃으면 사용자는 고르고 나서 전체 합계를 받는다.
    되물었는데도 답이 틀리면 되묻기 자체가 신뢰를 잃는다.
    """
    decision = decide(case.question)
    if decision.route != "clarify":
        pytest.skip(f"갈래가 이미 틀렸다 ({decision.route})")

    assert decision.candidates, f"{case.question!r}: 되물으면서 고를 후보를 안 줬다"
    assert decision.clarification, f"{case.question!r}: 되물을 문장이 비어 있다"
    for candidate in decision.candidates:
        for dimension in case.dimensions:
            assert dimension in candidate.query.dimensions, (
                f"{case.question!r} 후보 {candidate.label!r}: "
                f"차원 {dimension!r} 를 잃었다 (실제 {candidate.query.dimensions})"
            )


@pytest.mark.parametrize("case", ABSTAIN_CASES, ids=_id)
def test_abstain_reason_is_readable_by_a_human(decide, case: Case):
    """거절 사유가 사용자에게 그대로 보이므로, 내부 용어가 새면 안 된다.

    실제로 "vocab에 해당하는 정보가 없습니다"가 화면에 나온 적이 있다.
    라우터 프롬프트가 금지어를 걸고 있고, 이 테스트가 그것을 지킨다.
    """
    decision = decide(case.question)
    if decision.route != "abstain":
        pytest.skip(f"갈래가 이미 틀렸다 ({decision.route})")

    leaked = [
        word
        for word in ("vocab", "schema", "catalog", "semantic", "route", "prompt")
        if word in decision.reason.lower()
    ]
    assert not leaked, (
        f"{case.question!r}: 거절 사유에 내부 용어 {leaked} 가 샜다 — {decision.reason}"
    )
