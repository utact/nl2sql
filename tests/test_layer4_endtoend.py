"""골든셋 4층 — 종단.

여기서부터 라우팅 결과가 개입한다. 다만 이 파일은 모델을 고정해서 돌린다.
검증 대상이 모델의 실력이 아니라, 모델이 무엇을 내놓든 하네스가 어떻게 처리하는가이기 때문이다.
실제 모델을 태우는 종단 비교는 `test_layer3_routing.py` 쪽에 있고 기본으로 건너뛴다.

이 층에서 확인하는 것은 두 가지다.

- 실패가 1급 출력인가. 되묻기·거절·가드 위반이 예외로 새지 않고 상태로 나오는가.
- 가정과 잘림이 표면화되는가. 사용자가 검증할 수 있는 형태로 나가는가.
"""

from __future__ import annotations

from conftest import clarify_route, semantic_query, semantic_route


def test_semantic_path_always_surfaces_assumptions(make_pipeline):
    """머리 경로도 해석 가정을 낸다.

    직접 생성 경로의 가정은 모델이 써서 인지하지 못한 가정이 빠지지만, 머리 경로는 정의에서 읽는다.
    트래픽 대부분이 이 경로라, 여기가 비면 "가정을 항상 보여준다"가 거짓이 된다.

    문장은 지표·차원 이름이 아니라 설명으로 쓴다.
    읽는 사람은 검사원이라 `fail_count`를 보여주고 확인하라고 하면 확인할 수 없다.
    """
    pipeline = make_pipeline(semantic_route(["fail_count"], ["item_name"]))
    answer = pipeline.ask("항목별 불합격 건수")

    assert answer.status == "ok"
    assert answer.assumptions
    assert any("규격을 벗어난 측정 건수" in a for a in answer.assumptions), "지표를 설명으로 말한다"
    assert any("검사 항목 이름" in a for a in answer.assumptions), "차원도 설명으로 말한다"
    assert not any("fail_count" in a for a in answer.assumptions), "이름은 SQL 패널에 있다"


def test_normalized_value_appears_in_assumptions(make_pipeline):
    """값이 바뀌었으면 바뀌었다고 말한다.

    "불합격"을 'FAIL'로 읽은 것은 시스템의 해석이다.
    맞을 수도 틀릴 수도 있으므로 사용자가 볼 수 있어야 한다.
    """
    route = semantic_route(["measurement_count"], [], [("verdict", "=", "불합격")])
    answer = make_pipeline(route).ask("불합격 몇 건이야")

    assert any("FAIL" in a and "불합격" in a for a in answer.assumptions)


def test_auto_injection_is_disclosed(make_pipeline):
    """자동으로 채운 것은 자동으로 채웠다고 말한다.

    사용자가 묻지 않은 조건이 붙었다면 그건 결과의 의미를 바꾼다.
    조용히 붙이면 사용자는 전사 수치로 오해한다.
    """
    pipeline = make_pipeline(semantic_route(["measurement_count"]))
    answer = pipeline.ask("검사 건수", user_context={"line_code": "L1"})

    assert answer.params == ["L1"]
    assert any("자동으로" in a for a in answer.assumptions)


def test_auto_injection_is_disclosed_even_when_the_model_wrote_the_filter(make_pipeline):
    """모델이 같은 필터를 먼저 써 두어도 고지는 나온다.

    라우터 프롬프트는 컨텍스트를 모델에게 보여 준다.
    그래서 모델이 그 필터를 직접 써서 돌려주는 일이 잦다.
    파이프라인이 "이미 있으니 주입이 아니다"로 세면 범위는 그대로 좁혀지는데 고지만 사라진다.

    표도 SQL도 맞고 빠진 것은 문장 하나뿐이라 아무도 모른다.
    이 저장소가 세는 실패가 정확히 그 모양이다.
    """
    route = semantic_route(["measurement_count"], [], [("line_code", "=", "L1")])
    answer = make_pipeline(route).ask("검사 건수", user_context={"line_code": "L1"})

    assert answer.params == ["L1"]
    assert any("자동으로" in a for a in answer.assumptions)


def test_injected_context_overrides_a_conflicting_filter(make_pipeline):
    """컨텍스트와 다른 값을 말해도 컨텍스트가 이긴다.

    PostgresExecutor의 RLS는 이미 그렇게 동작한다.
    두 실행기가 다른 답을 주면 격리는 보증이 아니다.
    """
    route = semantic_route(["measurement_count"], [], [("line_code", "=", "L3")])
    answer = make_pipeline(route).ask("L3 검사 건수", user_context={"line_code": "L1"})

    assert answer.params == ["L1"]
    assert answer.sql.count("line_code") == 1


def test_invented_metric_becomes_a_question_not_an_answer(make_pipeline):
    """모델이 정의를 지어내면 오답이 아니라 되묻기로 돌아온다.

    "모르겠다"를 말할 정의가 없는 시스템은 그럴듯한 오답으로 빈칸을 채운다.
    """
    answer = make_pipeline(semantic_route(["danger_score"])).ask("위험 점수 알려줘")

    assert answer.status == "clarification_needed"
    assert "danger_score" in answer.message
    assert not answer.rows


def test_abstain_reason_is_terminated(make_pipeline):
    """거절 사유는 문장으로 끝난다.

    머리말은 시스템이 쓰고 사유는 모델이 쓴다. 둘이 한 줄에 이어 붙는다.
    앞은 마침표로 끝나는데 뒤가 안 끝나면 한 줄 안에서 문체가 부딪친다.
    """
    route = {"route": "abstain", "reason": "작업자 정보가 데이터에 없습니다",
             "semantic_query": None, "clarification": None, "clarify_candidates": None}
    answer = make_pipeline(route).ask("작업자별 불합격 건수 알려줘")

    assert answer.status == "abstained"
    assert answer.message.endswith("데이터에 없습니다.")


def test_abstain_is_a_first_class_output(make_pipeline):
    """답할 수 없는 질문은 답하지 않는다."""
    route = {"route": "abstain", "reason": "스키마에 인사 정보가 없습니다.",
             "semantic_query": None, "clarification": None}
    answer = make_pipeline(route).ask("직원 연봉 알려줘")

    assert answer.status == "abstained"
    assert answer.message
    assert not answer.rows


def test_clarify_is_a_first_class_output(make_pipeline):
    """모호한 질문은 추측 실행하지 않는다."""
    route = {"route": "clarify", "reason": "기준이 모호합니다.",
             "semantic_query": None,
             "clarification": "합불 기준인가요, 규격 여유 기준인가요?"}
    answer = make_pipeline(route).ask("품질 안 좋은 거 보여줘")

    assert answer.status == "clarification_needed"
    assert "?" in answer.message


def test_clarification_can_actually_be_answered(make_pipeline):
    """되묻기는 물어보는 것으로 끝나지 않는다. 답을 받을 자리가 있어야 한다.

    없으면 사용자는 질문을 통째로 다시 쓰고, 그 순간 첫 질문이 확정한 것이 사라진다.
    "요즘 문제 있는 라인"에 되묻고 답했더니 `line_code`가 빠진 채 전체 평균 한 줄이 나왔다.
    되물어 놓고 답이 더 나빠진 것이다.

    선택지는 완결된 질의라 묶는 축이 사라질 수가 없고, 이 턴은 모델을 부르지 않는다.
    """
    by_line = ("불합격률 기준", semantic_query(["fail_rate"], ["line_code"]))
    by_count = ("불합격 건수 기준", semantic_query(["fail_count"], ["line_code"]))
    pipeline = make_pipeline(clarify_route("어떤 기준으로 볼까요?", [by_line, by_count]))

    asked = pipeline.ask("요즘 문제 있는 라인 알려줘")
    assert asked.status == "clarification_needed"
    assert [o["label"] for o in asked.options] == ["불합격률 기준", "불합격 건수 기준"]

    answered = pipeline.ask_resolved(asked.options[0]["query"])
    assert answered.status == "ok"
    assert answered.columns[0] == "line_code", "묶는 축이 살아남는다"
    assert len(answered.rows) == 3, "라인이 세 줄로 나온다"
    assert answered.assumptions


def test_answering_a_clarification_costs_no_model_call(make_pipeline):
    """되묻기의 답은 결정론적으로 처리된다.

    모호성이 SQL 공간이 아니라 시맨틱 레이어 공간으로 옮겨간 덕에 가능한 일이다.
    후보가 이미 정의된 이름들로 되어 있으므로 다시 해석할 것이 없다.
    """
    candidate = ("불합격률 기준", semantic_query(["fail_rate"], ["line_code"]))
    pipeline = make_pipeline(clarify_route("어떤 기준으로 볼까요?", [candidate]))
    pipeline.ask("요즘 문제 있는 라인 알려줘")

    calls = []
    pipeline.router._llm = _Counting(calls)
    pipeline.generator._llm = _Counting(calls)
    pipeline.ask_resolved(semantic_query(["fail_rate"], ["line_code"]))

    assert calls == [], "되묻기의 답은 모델을 부르지 않는다"


def test_resolution_is_revalidated_against_the_vocabulary(make_pipeline):
    """되돌아온 선택지도 신뢰 경계 바깥이다.

    클라이언트가 보낸 값이므로 정의 검증을 건너뛰면 안 된다.
    컴파일러가 카탈로그로 다시 보고, 어긋나면 다시 되묻기로 내려간다.
    """
    pipeline = make_pipeline(semantic_route(["fail_rate"]))
    answer = pipeline.ask_resolved(semantic_query(["지어낸지표"], ["line_code"]))

    assert answer.status == "clarification_needed"
    assert not answer.rows


class _Counting:
    """호출되면 기록하는 가짜 백엔드."""

    def __init__(self, sink):
        self._sink = sink

    def complete_json(self, system, user, schema):
        self._sink.append(user)
        return {}


def test_zero_rows_is_surfaced(make_pipeline):
    """0행은 조용한 성공이 아니다.

    조건이 무력화됐거나 값을 잘못 읽었을 때 흔히 나오는 모습이므로 신호로 다룬다.
    """
    route = semantic_route(
        ["measurement_count"], ["item_name"], [("item_name", "=", "존재하지않는항목")]
    )
    answer = make_pipeline(route).ask("없는 항목 건수")

    assert answer.status == "ok"
    assert not answer.rows
    assert "조건에 맞는 것이 없습니다" in answer.message


def test_aggregate_without_grouping_hides_the_empty_case(make_pipeline):
    """알려진 한계 — 그룹핑 없는 집계는 0행 신호에 걸리지 않는다.

    `SELECT COUNT(*)`는 조건이 아무것도 못 맞혀도 0행이 아니라 값이 0인 1행을 돌려준다.
    그래서 상식 검사가 조용히 지나간다.

    고쳐야 할 버그가 아니라 알고 있는 구멍을 잠그는 테스트다. 상식 검사는 신호이지 방벽이 아니다.
    나중에 이 동작을 바꾸면 여기서 걸리고, 그때 의도된 변경인지 판단하면 된다.
    """
    route = semantic_route(["measurement_count"], [], [("item_name", "=", "존재하지않는항목")])
    answer = make_pipeline(route).ask("없는 항목 건수")

    assert answer.status == "ok"
    assert answer.rows == [(0,)]
    assert "조건에 맞는 것이 없습니다" not in answer.message


def test_audit_log_records_every_question(make_pipeline):
    """성공하든 실패하든 감사 로그에 남는다."""
    pipeline = make_pipeline(semantic_route(["measurement_count"]))
    pipeline.ask("검사 건수")
    pipeline.ask("검사 건수 또")

    assert len(pipeline.audit_log) == 2
    assert all(entry["question"] for entry in pipeline.audit_log)


def test_aggregate_row_opens_the_measurements_behind_it(make_pipeline):
    """집계 한 줄을 누르면 그 건들의 실측값이 규격과 함께 나온다.

    "라인별로 빠듯한 거 몇 건이야"에 3건이라고만 답하면 그 사람은 아무것도 못 한다.
    묻는 이유가 건수가 아니라 무엇이 얼마나 위험한지이기 때문이다.
    건수를 항목별로 다시 쪼개도 1건·1건·1건이 될 뿐 같은 질문이 남는다.

    지표가 무엇을 셌는지도 함께 넘어가야 한다.
    near_limit_count의 조건은 집계식 안에 있어서, 상세를 뽑으면 그 라인의 전 측정이 나온다.
    """
    route = semantic_route(["near_limit_count"], ["line_code"])
    pipeline = make_pipeline(route)
    answer = pipeline.ask("라인별로 규격 빠듯한 거 몇 건이야?")

    assert answer.drill, "누를 수 있으면 알려 준다"
    top = answer.rows[0]

    # 화면이 하는 일 — 누른 줄의 차원 값을 필터로 굳힌다.
    query = dict(answer.drill["query"])
    query["filters"] = list(query["filters"]) + [
        {"dimension": d, "op": "=", "value": str(top[i])}
        for i, d in enumerate(answer.drill["by"])
    ]
    detail = pipeline.ask_resolved(query)

    assert detail.status == "ok"
    assert len(detail.rows) == top[-1], "센 건수와 보여준 건수가 같아야 한다"
    for column in ("측정값", "여유"):
        assert column in detail.columns, "실측값과 남은 여유가 있어야 판단할 수 있다"
    assert all(c.isascii() is False for c in detail.columns), (
        "헤더에 컬럼 이름을 그대로 올리면 검사원이 spec_slack을 읽어야 한다"
    )


def test_detail_view_is_not_offered_where_it_means_nothing(make_pipeline):
    """이미 개별 측정을 보고 있으면 다시 누를 것이 없다.

    누를 수 있다고 해 놓고 아무 일도 안 일어나면 그다음부터 아무도 안 누른다.
    """
    route = semantic_route(["near_limit_count"], ["line_code"])
    pipeline = make_pipeline(route)
    answer = pipeline.ask("라인별로 규격 빠듯한 거 몇 건이야?")
    detail = pipeline.ask_resolved(dict(answer.drill["query"]))

    assert detail.status == "ok"
    assert detail.drill is None, "상세 목록에는 더 들어갈 곳이 없다"
