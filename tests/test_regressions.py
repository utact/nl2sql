"""회귀 테스트 — 조용한 오답들.

케이스의 출처는 사고 로그다. 미리 만들지 않는다.
테스트 하나가 실제로 한 번 틀렸던 것 하나와 1:1로 대응한다.

골든셋은 correctness를 보장하지 않는다. 회귀만 막는다.
정답을 아는 질문으로만 구성되는데, 위험한 것은 정답을 모르는 질문이기 때문이다.

전부 모델이 아니라 정의나 조립을 고쳐서 해결했다.
"""

from __future__ import annotations

import dataclasses
import sys

import pytest
from conftest import ExplodingLLM, clarify_route, semantic_query, semantic_route

from demo.defects import absolute_slack, colliding_description, untyped_numeric
from nl2sql import NL2SQL, SqliteExecutor, StubLLM
from nl2sql.pipeline import ABSTAIN_HEADLINE
from nl2sql.semantic import SemanticError, parse_semantic_query

# 합불 판정을 함께 건다.
# 백분위만으로 항목을 묶으면 항목마다 같은 비율이 걸려 수가 전부 같아진다.
# 그 상태로는 바인딩 타입이 무엇을 망가뜨리는지도 안 보인다.
RISK_ROUTE = semantic_route(
    ["measurement_count"],
    ["item_name"],
    [("slack_rank", "<", "0.05"), ("verdict", "=", "합격")],
)


def test_candidate_label_must_match_its_query(make_pipeline):
    """라벨과 질의 불일치 — 라벨은 비율인데 질의가 건수를 담고 있었다.

    사용자는 라벨만 보고 고른다. 질의는 안 보이고 볼 방법도 없다.
    어긋나 있으면 컴파일도 가드도 통과하고 표가 나간다.
    되묻기가 조용한 오답을 만드는 경로다.

    프롬프트가 맞추라고 시키지만 지시는 보증이 아니라 여기서 대조한다.
    """
    route = clarify_route(
        "무엇 기준으로 볼까요?",
        [
            ("불합격률 기준", semantic_query(["fail_count"], ["line_code"])),
            ("불합격 건수 기준", semantic_query(["fail_count"], ["line_code"])),
        ],
    )
    answer = make_pipeline(route).ask("요즘 문제 있는 라인 알려줘")

    labels = [o["label"] for o in answer.options]
    assert "불합격률 기준" not in labels, "라벨이 가리키는 지표가 질의에 없다"
    assert "불합격 건수 기준" in labels, "일치하는 후보까지 버리지는 않는다"


def test_numeric_filter_is_not_silently_disabled(make_pipeline, catalog):
    """바인딩 타입 — 수치 필터를 문자열로 바인딩해 조건이 통째로 무력화됐다.

    같은 SQL, 같은 연산자, 에러 없음. 바인딩 타입만 달랐고 결과가 수백 배 틀렸다.
    모델은 옳은 지표·차원·연산자를 골랐다. 틀린 것은 정의였다.
    """
    fixed = make_pipeline(RISK_ROUTE).ask("아슬아슬한 항목")
    broken = make_pipeline(RISK_ROUTE, untyped_numeric(catalog)).ask("아슬아슬한 항목")

    fixed_total = sum(row[-1] for row in fixed.rows)
    broken_total = sum(row[-1] for row in broken.rows)

    assert fixed.sql == broken.sql, "SQL 텍스트는 같다. 다른 것은 바인딩 타입뿐이다"
    assert broken_total > fixed_total * 10, "결함 버전은 필터가 무력화된다"
    assert isinstance(fixed.params[0], float)


def test_risk_ranking_is_comparable_across_items(make_pipeline, catalog):
    """단위 섞인 절대 임계 — 항목마다 단위가 달라 절대 임계 비교가 통째로 뒤집혔다.

    한 항목 안에서는 맞는 값이었다.
    수치 스케일이 항목마다 네 자릿수씩 차이 나서 단위가 작은 항목이 위험 목록을 독점했다.
    고친 방식은 항목 내 백분위다. 단위가 사라지므로 비교가 정의상 성립한다.
    """
    # 합격 행만 본다.
    # 불합격은 여유가 음수라 어느 축으로 재든 맨 아래에 깔린다.
    absolute_route = semantic_route(
        ["measurement_count"],
        ["item_name"],
        [("spec_slack", "<", "0.05"), ("verdict", "=", "합격")],
    )
    ranked_route = semantic_route(
        ["measurement_count"],
        ["item_name"],
        [("slack_rank", "<", "0.05"), ("verdict", "=", "합격")],
    )
    skewed = make_pipeline(absolute_route, absolute_slack(catalog)).ask("아슬아슬한 항목")
    ranked = make_pipeline(ranked_route).ask("아슬아슬한 항목")

    def share_of_top_item(answer):
        counts = [row[-1] for row in answer.rows]
        return max(counts) / sum(counts)

    assert share_of_top_item(skewed) > 0.9, "절대 임계는 한 항목이 목록을 독점한다"
    assert share_of_top_item(ranked) < 0.9, "백분위는 항목이 섞인다"


def test_metric_descriptions_do_not_collide(catalog):
    """설명문 충돌 — 설명문에 든 단어 하나가 라우팅을 흔들었다.

    원인은 프롬프트도 모델도 아니었다. 다른 지표의 설명문에 겹치는 단어가 하나 있었다.
    설명과 동의어는 그대로 모델의 입력이므로, 정의 관리가 곧 프롬프트 엔지니어링이다.
    """
    count_synonyms = set(catalog.metrics["measurement_count"].synonyms)
    lot_synonyms = set(catalog.metrics["lot_count"].synonyms)

    assert not (count_synonyms & lot_synonyms), "동의어가 겹치면 라우팅이 흔들린다"
    assert "검사" not in catalog.metrics["lot_count"].description

    # 쌍 하나만 잠그면 정의를 늘릴 때 같은 사고가 다시 난다.
    # 설명과 동의어는 그대로 모델의 입력이므로 전수로 본다.
    seen: dict[str, str] = {}
    for name, metric in catalog.metrics.items():
        for synonym in metric.synonyms:
            assert synonym not in seen, (
                f"동의어 {synonym!r} 가 {seen.get(synonym)} 와 {name} 에 겹칩니다"
            )
            seen[synonym] = name

    # 되돌린 버전에서는 실제로 겹친다. 이 테스트가 무엇을 막고 있는지 보인다.
    broken = colliding_description(catalog)
    assert set(broken.metrics["lot_count"].synonyms) & count_synonyms


def test_missing_metric_is_demoted_to_a_question(make_pipeline):
    """표현할 수 없는 질의 — 모델이 목록형 질문에 지표를 비운 채 응답했다.

    컴파일러의 정의 검증이 잡아 되묻기로 강등했고 오답은 0건이었다.
    수정은 라우터 규칙 한 줄. 모델 오류가 오답이 아니라 질문이 되어 돌아온 사례다.
    """
    empty = semantic_route([], ["item_name"])
    answer = make_pipeline(empty).ask("아슬아슬한 항목 목록")

    assert answer.status == "clarification_needed"
    assert not answer.rows


def test_risk_metric_is_not_degenerate_across_items(make_pipeline):
    """무의미한 지표 — 항목 내 백분위의 최솟값을 항목별로 물어 답이 전부 0.0 이었다.

    `slack_rank`는 item_code로 파티션된 백분위라 항목의 최솟값이 정의상 항상 0 이다.
    항목으로 묶는 순간 모든 행이 0.0으로 나왔다.
    실행 정상, 5행, 에러 없음, 정렬까지 맞음 — 답만 무의미했다.

    보여줘도 사용자가 판단할 수 없는 종류라, 표면화가 아니라 정의에서 제거했다.
    하위 꼬리의 크기를 세면 백분위라 단위가 없고 항목 간 비교가 성립한다.
    """
    route = semantic_route(["near_limit_count"], ["item_name"])
    answer = make_pipeline(route).ask("규격에 아슬아슬한 항목")

    values = [row[-1] for row in answer.rows]
    assert len(set(values)) > 1, "항목마다 값이 달라야 비교가 의미를 가진다"
    assert any(v > 0 for v in values)


def test_stated_direction_beats_the_metric_default(make_pipeline):
    """질문이 방향을 말하면 그것이 정의의 기본값을 이긴다.

    방향은 사실이면서 동시에 의도다.
    사용자가 말하지 않으면 정의가 답하고, 말하면 사용자가 이긴다.
    """
    route = semantic_route(["near_limit_count"], ["item_name"], order="asc")
    answer = make_pipeline(route).ask("가장 안전한 항목")

    values = [row[-1] for row in answer.rows]
    assert values == sorted(values)
    assert "ORDER BY near_limit_count ASC" in answer.sql
    assert any("낮은 순으로" in a for a in answer.assumptions), "정렬 방향은 가정으로 나간다"


def test_compiler_does_not_hardcode_the_direction(catalog):
    """박아 둔 정렬 방향 — 컴파일러가 방향을 박아 두고 있었다.

    `ORDER BY 첫지표 DESC`가 하드코딩이라 질문이 방향을 말해도 무시됐다.
    "작을수록 흥미로운" 지표는 답이 목록 맨 끝으로 밀려났다.
    지금은 지표 정의가 방향을 선언한다. 방향이 컴파일러가 아니라 정의에 있다는 것이 핵심이다.
    """
    import dataclasses

    from nl2sql import SemanticQuery, compile_semantic

    ascending = dataclasses.replace(catalog.metrics["fail_rate"], default_order="asc")
    catalog.metrics["fail_rate"] = ascending
    sql, _ = compile_semantic(
        SemanticQuery(metrics=["fail_rate"], dimensions=["line_code"]), catalog
    )

    assert "ORDER BY fail_rate ASC" in sql


def test_ordering_is_deterministic_under_ties(make_pipeline):
    """동률 정렬 + LIMIT은 실행마다 결과가 달라진다.

    컴파일러가 만드는 SQL 자체가 비결정적이었다. 차원으로 2차 정렬해 잠근다.
    """
    route = semantic_route(["measurement_count"], ["category"])
    answer = make_pipeline(route).ask("분류별 건수")

    assert answer.sql.rstrip().splitlines()[-2].endswith("category ASC")


def test_backend_failure_is_a_first_class_output(catalog, db_path):
    """백엔드 사망 — 모델 백엔드가 죽자 예외가 그대로 500으로 나갔다.

    호스팅 모델이 EOL 되어 410이 떨어졌고, 라우터 호출이 감싸여 있지 않아 500으로 나갔다.
    화면에는 아무 사유도 남지 않았다.

    백엔드는 앱의 버그와 무관하게 실패한다. 그것도 사용자에게 보이는 상태여야 한다.
    """
    pipeline = NL2SQL(
        catalog=catalog,
        llm=ExplodingLLM("410 Gone: model reached end of life"),
        executor=SqliteExecutor(db_path),
    )
    answer = pipeline.ask("월별 검사 건수")

    assert answer.status == "error"
    assert "end of life" in answer.message, "원인을 감추면 운영에서 되짚을 수 없다"
    assert pipeline.audit_log[-1]["status"] == "error", "실패도 감사 로그에 남는다"


def test_free_generation_prompt_carries_column_meaning(catalog):
    """프롬프트 조립 누락 — 직접 생성 프롬프트에 컬럼 의미가 안 실려 모델이 범위를 지어냈다.

    `slack_rank`는 0~1 인데 모델이 0~100으로 가정해 `slack_rank <= 5`를 걸었고 전 행이 통과했다.
    SQL 정상, 에러 없음, 결과만 무의미.

    같은 설명이 시맨틱 경로에는 이미 가고 있었다. 구멍을 만든 것은 프롬프트 조립이다.
    """
    prompt = catalog.describe_views()

    assert "slack_rank" in prompt
    assert "0=가장 아슬아슬" in prompt, "값의 범위가 프롬프트에 실려야 한다"


def test_abstain_message_does_not_leak_prompt_words(make_pipeline):
    """내부 용어 누출 — 거절 사유에 프롬프트의 내부 용어가 그대로 새어 나갔다.

    화면에 "... vocab에 해당하는 정보가 없습니다" 가 떴다.
    `vocab`은 라우팅 프롬프트에서만 쓰는 말이고 사용자는 그게 뭔지 알 길이 없다.

    처음에는 시스템이 쓴 머리말을 앞에 붙이는 것으로 고쳤다.
    첫 문장은 멀쩡해졌지만 뒤에 붙은 모델 문장은 그대로 나갔고, 누출은 화면에 남았다.

    사유는 자유 텍스트라 검증할 방법이 없다. 고칠 수 없으면 안 쓴다.
    머리말만으로도 "답할 수 없다"는 말은 완결되므로, 설명이 빠지는 손해가 더 작다.
    대신 감사 로그에는 원문을 남긴다 — 읽는 사람이 다르니 두는 자리도 다르다.
    """
    leaked = "제공된 검사 결과 데이터와 vocab에 해당하는 정보가 없습니다."
    pipeline = make_pipeline(
        {
            "route": "abstain",
            "reason": leaked,
            "semantic_query": None,
            "clarification": None,
            "clarify_candidates": None,
        }
    )
    answer = pipeline.ask("직원 연봉 알려줘")

    assert answer.status == "abstained"
    assert answer.message.startswith(ABSTAIN_HEADLINE), (
        "사용자가 읽을 첫 문장은 모델이 아니라 시스템이 써야 한다"
    )
    assert "vocab" not in answer.message, "내부 용어가 섞인 문장은 화면에 안 나간다"
    assert pipeline.audit_log[-1]["model_reason"] == leaked, "되짚을 수 있어야 한다"


def test_abstain_keeps_a_reason_a_user_can_read(make_pipeline):
    """앞의 규칙이 멀쩡한 설명까지 버리지 않는가.

    누출을 막겠다고 사유를 통째로 버리면 화면이 "답할 수 없습니다" 한 줄로만 남는다.
    사용자는 무엇이 없어서 못 하는지 모른 채 같은 질문을 다시 쓴다.
    """
    plain = "직원 연봉 정보가 데이터에 없습니다"
    pipeline = make_pipeline(
        {
            "route": "abstain",
            "reason": plain,
            "semantic_query": None,
            "clarification": None,
            "clarify_candidates": None,
        }
    )
    answer = pipeline.ask("직원 연봉 알려줘")

    assert plain in answer.message, "읽을 수 있는 설명은 그대로 내보낸다"
    assert answer.message.endswith("."), "머리말 뒤에 이어 붙으므로 문장으로 끝나야 한다"


def test_malformed_model_output_degrades_instead_of_crashing(make_pipeline):
    """형태 깨진 출력 — 소형 모델이 형태가 깨진 구조화 출력을 보내 라우팅이 죽었다.

    로컬 1.5B가 `semantic_query`를 dict가 아니라 문자열로 돌려줬다.
    `data or {}`가 None만 막고 있어 `'str' object has no attribute 'get'`로 터졌다.

    형태가 틀린 것은 예외가 아니라 빈 값이어야 한다.
    그러면 지표가 없으니 컴파일러가 되묻기로 강등하고, 실패가 안전한 쪽으로 떨어진다.
    """
    for broken in ("문제 있는 라인", ["fail_rate"], 42):
        pipeline = make_pipeline(
            {
                "route": "semantic",
                "reason": "",
                "semantic_query": broken,
                "clarification": None,
                "clarify_candidates": None,
            }
        )
        answer = pipeline.ask("요즘 문제 있는 라인 알려줘")
        assert answer.status == "clarification_needed", f"{broken!r} 에서 강등되지 않았다"

    # 리스트 자리에 문자열이 오면 한 글자씩 쪼개져 엉뚱한 정의가 만들어진다.
    assert parse_semantic_query({"metrics": "fail_rate"}).metrics == []
    assert parse_semantic_query({"filters": "line_code=L1"}).filters == []
    assert parse_semantic_query("전부 문자열").metrics == []


def test_session_context_is_cleared_between_requests():
    """세션 신원 잔류 — 세션 컨텍스트를 안 비우면 앞 사용자의 신원이 남는다.

    PostgreSQL 실행기는 연결 하나를 요청 간에 재사용한다.
    RLS가 `current_setting('app.line_code')`를 본다.
    컨텍스트 없는 요청에서 비우지 않으면 직전 사용자의 라인 데이터가 그대로 보인다.

    채우는 것보다 비우는 것이 어렵다.
    안 채우면 0행이라 바로 티가 나지만, 안 비우면 그럴듯한 데이터가 나와서 조용하다.
    """
    from nl2sql.execution import PostgresExecutor

    class _Conn:
        def __init__(self): self.calls = []
        def execute(self, sql, params=None): self.calls.append((sql, params))

    ex = PostgresExecutor.__new__(PostgresExecutor)  # 실제 접속 없이 로직만 본다
    ex._conn = _Conn()
    ex._context_keys = set()

    ex.apply_context({"line_code": "L1"})
    assert ex._conn.calls[-1] == ("SELECT set_config(%s, %s, false)", ("app.line_code", "L1"))

    ex._conn.calls.clear()
    ex.apply_context(None)  # 컨텍스트 없는 요청
    assert ex._conn.calls == [("SELECT set_config(%s, %s, false)", ("app.line_code", ""))], (
        "앞 요청의 신원을 비우지 않으면 다음 사용자에게 남는다"
    )

    ex._conn.calls.clear()
    ex.apply_context(None)  # 이미 비었으면 더 할 일이 없다
    assert ex._conn.calls == []


def test_list_questions_are_not_padded_with_a_count(catalog, make_pipeline):
    """묻지 않은 카운트 — "라인 어디어디 있어?" 에 라인별 측정 건수가 나왔다.

    시맨틱 문법이 `지표 × 차원 × 필터` 뿐이라 열거를 표현할 수 없었다.
    그래서 라우팅 프롬프트가 "지표가 없으면 카운트를 끼워 넣어라"로 때우고 있었다.
    사용자는 묻지 않은 숫자를 받았고, 끼워 넣었다는 사실은 가정에도 안 남았다.

    가정은 컴파일된 질의에서 역산하므로 질의가 되기 전의 결정은 읽을 수 없다.
    머리 경로에도 가정의 구멍이 있다는 뜻이다 — 다만 정의에 없는 것을 못 적는 쪽이다.
    """
    query = semantic_query(metrics=[], dimensions=["line_code"])
    query["distinct"] = True
    answer = make_pipeline({
        "route": "semantic", "reason": "", "semantic_query": query,
        "clarification": None, "clarify_candidates": None,
    }).ask("지금 라인 어디어디 있는지 알려줘")

    assert answer.status == "ok"
    assert answer.columns == ["line_code"], "묻지 않은 지표가 붙으면 안 된다"
    assert [r[0] for r in answer.rows] == ["L1", "L2", "L3"]
    assert any("세지 않았습니다" in a for a in answer.assumptions), (
        "무엇을 하지 않았는지도 가정이다"
    )


def test_enumeration_must_be_asked_for_explicitly(catalog):
    """열거가 '지표 없음'의 기본 동작이 되면 안 된다.

    「형태 깨진 출력」의 안전망이 "지표가 없으면 되묻기" 다.
    열거를 지표 없음으로 표현하면 깨진 출력이 조용히 "전부 나열"로 떨어진다.
    실패가 안전한 쪽에서 위험한 쪽으로 옮겨가므로, 열거는 명시적 플래그로만 켠다.
    """
    from nl2sql.semantic import SemanticQuery, compile_semantic

    with pytest.raises(SemanticError):
        compile_semantic(SemanticQuery(metrics=[], dimensions=["line_code"]), catalog)
    # 문자열 "true" 같은 것을 참으로 받아 주면 그 구멍이 다시 열린다.
    assert parse_semantic_query({"distinct": "true", "dimensions": ["line_code"]}).distinct is False


def test_disabled_metric_is_demoted_not_silently_rerouted(catalog, db_path):
    """정의 드리프트 — 어긋난 지표를 조용히 이웃 지표로 흘려보내면 안 된다.

    드리프트가 나면 배포는 막지만(`strict_catalog=True`) 이미 떠 있는 프로세스는 죽이지 않는다.
    어긋난 항목만 끄고 나머지로 계속 답한다.

    끄는 방식이 중요하다.
    정의에서 지우면 그 지표를 향하던 질문이 가장 가까운 다른 지표로 조용히 라우팅된다.
    드리프트 대응이 조용한 오답 경로를 하나 여는 셈이다.
    이름을 남겨 두어야 그것을 고른 질문을 되묻기로 강등할 수 있다.
    """
    broken = dataclasses.replace(
        catalog,
        metrics={**catalog.metrics, "fail_rate": dataclasses.replace(
            catalog.metrics["fail_rate"], expression="AVG(no_such_column)"
        )},
    )
    pipeline = NL2SQL(
        catalog=broken,
        llm=StubLLM(lambda s, u: semantic_route(["fail_rate"], ["line_code"])),
        executor=SqliteExecutor(db_path),
        strict_catalog=False,          # 이미 떠 있는 프로세스의 자세
    )
    assert "fail_rate" in pipeline.catalog.disabled
    assert "fail_rate" in pipeline.catalog.metrics, "이름은 남아 있어야 강등할 수 있다"
    assert any("fail_rate" in w for w in pipeline.catalog_warnings), "경보 없이 끄면 안 된다"

    answer = pipeline.ask("라인별 불합격률 보여줘")
    assert answer.status == "clarification_needed"
    assert "사용할 수 없습니다" in answer.message
    assert answer.rows == [], "어긋난 지표로 숫자를 내면 안 된다"

    # 멀쩡한 지표는 계속 답한다 — 부분 축소이지 전면 정지가 아니다.
    pipeline.router._llm = StubLLM(lambda s, u: semantic_route(["fail_count"], ["line_code"]))
    ok = pipeline.ask("라인별 불합격 건수 보여줘")
    assert ok.status == "ok" and ok.rows


def test_unreadable_sort_direction_is_asked_back_not_guessed(catalog):
    """못 읽은 정렬 방향 — "방향을 안 말했다"와 "방향을 못 알아들었다"는 다르다.

    `order`가 없으면 지표 정의의 기본 방향이 맞는 답이다.
    그런데 모델이 `"ascending"`처럼 스키마 밖의 값을 내면 다르다.
    사용자가 "가장 낮은"을 물었고 모델이 그것을 옮기려다 형식을 틀린 경우일 수 있다.
    조용히 기본 방향으로 떨어지면 에러도 행수 이상도 없이 목록만 뒤집힌 채 나간다.

    구조화 출력 스키마가 enum을 걸고 있어 큰 모델에서는 안 난다.
    이 안전망은 작은 모델과 백엔드 교체를 위한 것이다.
    """
    from nl2sql.semantic import compile_semantic

    silent = parse_semantic_query({"metrics": ["fail_rate"], "order": None})
    assert silent.order is None and silent.order_unparsed is None
    compile_semantic(silent, catalog)  # 침묵은 정상이다. 정의가 답한다

    garbled = parse_semantic_query({"metrics": ["fail_rate"], "order": "ascending"})
    assert garbled.order is None, "못 읽은 값을 방향으로 쓰면 안 된다"
    assert garbled.order_unparsed == "ascending", "못 읽었다는 사실이 남아야 한다"
    with pytest.raises(SemanticError, match="정렬 방향"):
        compile_semantic(garbled, catalog)


def test_demo_survives_a_non_utf8_console(monkeypatch):
    """콘솔 인코딩 — 한국어 Windows 콘솔(cp949)에서 데모가 죽었다.

    `—` 한 글자에 `UnicodeEncodeError`가 났다.
    안전 문제가 아니라 도달 가능성 문제다.
    이 저장소가 상정하는 독자가 한국어 Windows를 쓰므로 기본 경로에서 죽으면 안 된다.
    """
    import io

    from demo.__main__ import _force_utf8_stdout

    calls = []

    class Cp949Stream(io.StringIO):
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", Cp949Stream())
    monkeypatch.setattr(sys, "stderr", Cp949Stream())
    _force_utf8_stdout()

    assert calls == [{"encoding": "utf-8", "errors": "replace"}] * 2


def _free_llm(sql: str, assumptions: list[str]):
    """라우터에는 free를, 생성기에는 주어진 SQL을 돌려주는 백엔드.

    둘이 같은 LLM을 공유하므로 시스템 프롬프트로 가른다.
    """

    def handler(system: str, user: str) -> dict:
        if "router" in system:
            return {
                "route": "free",
                "reason": "test",
                "semantic_query": None,
                "clarification": None,
                "clarify_candidates": None,
            }
        return {"sql": sql, "assumptions": assumptions}

    return StubLLM(handler)


def test_free_path_never_answers_without_saying_what_it_answered_with(catalog, db_path):
    """빈 해석 가정 — 직접 생성이 대체한 사실을 아무 데도 안 남겼다.

    "검사 설비 점검 이력"을 물었는데 그런 데이터가 없었다.
    모델이 `SELECT * FROM inspection_results LIMIT 1000`을 만들고 가정은 빈 목록을 냈다.
    자기가 대체했다는 것을 몰랐기 때문이다.
    상태 ok, 에러 없음, 표는 그럴듯 — 이 경로의 유일한 방어선이 빈칸이었다.

    프롬프트로 시키는 것과 하네스가 보증하는 것은 다르다.
    그래서 비면 하네스가 채운다. 내용이 정의에서 나오므로 모델이 침묵해도 안 흔들린다.
    """
    pipeline = NL2SQL(
        catalog=catalog,
        llm=_free_llm("SELECT item_name FROM inspection_results", []),
        executor=SqliteExecutor(db_path),
    )
    answer = pipeline.ask("검사 설비 점검 이력 보여줘")

    assert answer.route == "free"
    assert answer.assumptions, "직접 생성이 가정 없이 답하면 안 된다"
    assert any("규격 검사" in a for a in answer.assumptions), "무엇으로 답했는지 말해야 한다"


def test_free_path_assumptions_are_readable_by_a_korean_speaker(catalog, db_path):
    """영어로 나온 해석 가정 — 읽을 수 없는 방어선은 방어선이 아니다.

    생성기 프롬프트는 한국어로 쓰라고 말하지만 시스템 프롬프트가 영어라 자주 안 지켜진다.
    화면에는 이런 문장이 그대로 뜬다:

        "The requested equipment inspection history is not available ..."

    내용은 정확했다. 그러나 사용자가 읽는 유일한 문장이 영어면 가정을 보여준 것이 아니다.
    그래서 첫 문장은 시스템이 쓰고, 모델의 문장은 뒤에 남긴다 (ABSTAIN_HEADLINE과 같은 수).
    """
    english = "Returning all measurement rows because equipment logs do not exist."
    pipeline = NL2SQL(
        catalog=catalog,
        llm=_free_llm("SELECT item_name FROM inspection_results", [english]),
        executor=SqliteExecutor(db_path),
    )
    answer = pipeline.ask("검사 설비 점검 이력 보여줘")

    assert any("규격 검사" in a for a in answer.assumptions), "한국어 문장이 앞에 와야 한다"
    assert english in answer.assumptions, "모델의 설명은 내용이 맞으므로 버리지 않는다"


def test_a_quoted_korean_question_does_not_pass_as_a_korean_assumption(catalog, db_path):
    """인용에 속은 한국어 판정 — 영어 가정이 통째로 화면에 나갔다.

    하네스는 모델이 한국어를 안 쓰면 첫 줄을 대신 써 준다.
    그 판정이 "한글이 한 글자라도 있는가" 였는데, 모델은 영어 문장 안에 질문을 그대로 인용한다.

        Interpreted '검사일별 측정 건수' as the count of rows per inspected_on date.

    한 글자로 보면 이 줄은 한국어다. 그래서 하네스가 손을 뗐고 화면은 영어로만 찼다.
    인용부호 안을 빼고 봐야 문장의 언어를 판정할 수 있다.
    """
    quoted = "Interpreted '검사일별 측정 건수' as the count of rows per inspected_on date."
    pipeline = NL2SQL(
        catalog=catalog,
        llm=_free_llm("SELECT item_name FROM inspection_results", [quoted]),
        executor=SqliteExecutor(db_path),
    )
    answer = pipeline.ask("검사일별 측정 건수 보여줘")

    assert any("규격 검사" in a for a in answer.assumptions), "첫 줄은 시스템이 역산해 쓴다"
    assert quoted in answer.assumptions, "모델의 설명은 내용이 맞으므로 버리지 않는다"


def test_free_assumptions_survive_a_model_that_misdescribes_its_own_filter(catalog, db_path):
    """모델이 "전체를 봤습니다"라고 썼지만 SQL은 라인 하나로 좁혀져 있었다.

    가정은 모델의 문장이 아니라 가드가 이미 파싱해 둔 AST에서 만든다.
    모델이 뭐라고 썼든 실제로 실행될 SQL의 필터·묶음이 그대로 문장이 된다.
    """
    sql = (
        "SELECT item_name, COUNT(*) AS c FROM inspection_results "
        "WHERE line_code = 'L2' AND verdict = 'FAIL' "
        "GROUP BY item_name ORDER BY c DESC"
    )
    pipeline = NL2SQL(
        catalog=catalog,
        llm=_free_llm(sql, ["전체를 봤습니다."]),
        executor=SqliteExecutor(db_path),
    )
    answer = pipeline.ask("항목별로 봐줘")

    assert any("생산 라인" in a and "L2" in a for a in answer.assumptions), (
        "모델이 안 적어도 실제로 건 필터가 문장에 있어야 한다"
    )
    assert any("합불 판정" in a and "FAIL" in a for a in answer.assumptions), (
        "필터가 여럿이면 전부 나와야 한다"
    )
    assert any("항목" in a for a in answer.assumptions), "묶은 축도 문장에 있어야 한다"
    assert "전체를 봤습니다." in answer.assumptions, "모델의 문장은 참고로 뒤에 남긴다"


def test_injected_limit_is_announced_not_just_flagged(catalog, db_path):
    """고지 없는 잘림 — 플래그만 켜고 말은 안 했다.

    직접 생성 경로가 `_execute`가 돌아온 뒤에 truncated를 켰다.
    플래그는 True 인데 알리는 문장은 이미 만들어진 뒤라, 사용자는 1000행을 받고도 못 들었다.

    실행기 혼자서는 못 잡는다.
    가드가 넣은 LIMIT만큼만 SQL이 가져오므로 "잘렸다"와 "이만큼이다"가 구별되지 않는다.
    그래서 가드가 알려 주고, 판정이 메시지보다 먼저 끝나야 한다.
    """
    pipeline = NL2SQL(
        catalog=catalog,
        llm=_free_llm("SELECT measurement_id FROM inspection_results", ["전체를 봤습니다."]),
        executor=SqliteExecutor(db_path),
    )
    answer = pipeline.ask("측정 전부 보여줘")

    assert answer.truncated, "상한에 걸렸으면 플래그가 켜져야 한다"
    assert "잘렸습니다" in answer.message, "플래그만 켜고 말을 안 하면 사용자는 모른다"


def test_percentile_filter_cannot_split_its_own_partition(make_pipeline):
    """항목 안 순위로 항목 가르기 — 필터가 한 건도 못 걸렀는데 목록이 나갔다.

    "규격에 아슬아슬한 항목 알려줘"에 실제 라우터가 값 열거를 골랐다.
    slack_rank는 item_code로 파티션한 백분위라,
    임계가 0.5든 0.001이든 모든 항목이 조건을 만족하는 행을 하나는 갖는다.
    검사 항목 다섯 개가 전부 나왔다. 실행 정상, 다섯 행, 정렬까지 맞음, 필터만 무효였다.

    차원 설명문에 "그룹핑 축으로는 쓰지 않는다"고 적어 두었지만 그건 모델에게 하는 지시다.
    같은 질문을 두 번 물으면 임계가 0.05와 0.1로 갈리는데 답은 똑같이 다섯 항목이었다.
    모델이 지어낸 숫자가 무엇이든 상관이 없었다는 뜻이다.

    정의가 "이 차원은 저 차원 안에서 매긴 값"이라고 선언하고,
    묶는 축을 가를 수 있는 필터가 하나도 없으면 컴파일러가 거부한다.
    """
    enumeration = {
        "route": "semantic",
        "reason": "test",
        "semantic_query": {
            "metrics": [],
            "dimensions": ["item_name"],
            "filters": [{"dimension": "slack_rank", "op": "<", "value": "0.1"}],
            "order": None,
            "distinct": True,
        },
        "clarification": None,
        "clarify_candidates": None,
    }
    answer = make_pipeline(enumeration).ask("규격에 아슬아슬한 항목 알려줘")

    assert answer.status == "clarification_needed", "가를 수 없는 질의는 답이 되면 안 된다"
    assert not answer.rows, "다섯 항목을 그대로 내보내면 안 된다"


def test_percentile_filter_is_allowed_where_it_actually_splits(make_pipeline):
    """앞의 규칙이 멀쩡한 질의까지 막지 않는가.

    거부는 넓을수록 안전해 보이지만, 넓으면 쓸 수 있는 질문이 사라진다.
    백분위는 항목이 아닌 축을 묶을 때는 제대로 갈린다. 그쪽은 통과해야 한다.

    축은 pass_slack_rank를 쓴다.
    slack_rank로 적으면 이 테스트가 초록인 채로 README가 고발한 행을 축성한다 —
    그 축은 불합격까지 포함해 줄을 세우므로 라인별 수를 가르는 것이 규격 밖 측정이고,
    "라인마다 수가 달라야 한다"가 필터가 일했다는 증거가 되지 못한다.
    가드가 통과시켜야 하는 것은 "갈리는 필터"이지 "달라 보이는 숫자"가 아니다.
    """
    by_line = semantic_route(
        ["measurement_count"], ["line_code"], [("pass_slack_rank", "<", "0.1")]
    )
    answer = make_pipeline(by_line).ask("라인별로 여유 하위 10%인 합격품 건수")

    assert answer.status == "ok"
    assert len({row[-1] for row in answer.rows}) > 1, "라인마다 수가 달라야 한다"


def test_rejected_query_offers_somewhere_to_go(make_pipeline):
    """누를 것 없는 되묻기 — 되물어 놓고 선택지를 안 줬다.

    컴파일 실패로 강등된 되묻기에는 후보가 붙지 않았다.
    사유는 "이 지표로 물어봐 주세요"라고 알려 주는데 누를 것이 없다.
    사용자는 질문을 통째로 다시 쓰고, 그때 앞 질문이 이미 정한 축이 사라진다.

    컴파일러가 갈 곳을 알고 있으면 사유와 함께 질의를 들려 보낸다.
    묻는 축은 그대로 둔다. 되묻기가 사용자가 정한 것을 잃으면 되묻는 의미가 없다.
    """
    degenerate = semantic_route(
        ["measurement_count"], ["item_name"], [("slack_rank", "<", "0.1")]
    )
    answer = make_pipeline(degenerate).ask("규격에 아슬아슬한 항목 알려줘")

    assert answer.status == "clarification_needed"
    assert answer.options, "갈 곳을 알면서 안 알려 주면 사용자는 처음부터 다시 쓴다"
    assert answer.options[0]["query"]["dimensions"] == ["item_name"], "묻는 축은 유지한다"


def test_empty_result_does_not_blame_the_wrong_thing(make_pipeline):
    """엉뚱한 0행 사유 — 필터가 못 맞혔는데 접근 범위를 탓했다.

    0행이면 "접근 범위가 비어 있을 수 있다"는 줄이 따라 붙었다.
    그 줄은 행 단위 권한이 걸린 DB에서만 참인데 백엔드를 안 보고 붙었다.
    로그인 없이 쓰면 0행마다 나오므로, 진짜 원인은 가려지고 경고는 읽히지 않게 된다.

    실행기가 컨텍스트를 실제로 강제하는지 선언하고, 그럴 때만 붙인다.
    """
    impossible = semantic_route(
        ["measurement_count"], ["item_name"], [("measured_value", "=", "999999999")]
    )
    answer = make_pipeline(impossible).ask("측정값이 999999999인 항목")

    assert not answer.rows
    assert "조건에 맞는 것이 없습니다" in answer.message, "빈 결과라는 사실은 말해야 한다"
    assert "접근 범위" not in answer.message, "SQLite 에는 행 단위 권한이 없다"


def test_screen_says_which_model_it_is_talking_to():
    """말 없는 화면 — 고정 응답으로 띄운 데모를 실제 응답으로 읽었다.

    같은 UI가 실제 백엔드로도 StubLLM 으로도 뜬다.
    고정 응답은 질문을 무엇으로 바꿔도 같은 표를 돌려주는데 화면은 똑같이 생겼다.
    무슨 질문을 해도 같은 답이 나오길래 오류라고 읽었다. 오류가 아니라 데모였다.

    화면이 스스로 알 방법이 없었다. 그래서 실행기와 마찬가지로 백엔드도 자기를 선언한다.
    """
    from app.server import backend_label

    label, live = backend_label(StubLLM(lambda system, user: {}))
    assert live is False, "모델 이름이 없으면 고정 응답이다"
    assert label == "StubLLM", "옆칸의 모델 이름과 같은 형식이어야 한다"
    assert label.isascii(), "모노스페이스가 한글을 대체 폰트로 그려 굵기가 어긋난다"

    class _Hosted:
        model = "openai/gpt-oss-120b"

    label, live = backend_label(_Hosted())
    assert (label, live) == ("openai/gpt-oss-120b", True)


def test_auto_injected_filter_does_not_disable_the_blind_filter_guard(make_pipeline):
    """꺼진 blind 가드 — 로그인 컨텍스트 하나로 거절이 표가 되었다.

    blind-filter 검사는 "사용자가 건 조건이 전부 헛도는가"를 묻는다.
    그런데 판정의 분모가 query.filters 전체였고, 파이프라인은 컴파일 전에 테넌트 필터를 덧댄다.
    line_code는 relative_to가 없어 blind가 아니므로 분모만 하나 늘고 조건이 깨진다.

    같은 질문, 같은 정의인데 로그인 값 하나로 가드가 꺼졌다.
    나가는 표는 전역 하위 10%에 그 라인 행이 우연히 몇 개 걸렸는지일 뿐 아무 뜻이 없다.
    해석 가정은 정직하게 나가지만 사용자가 그것으로 판단할 수 없다.

    벤치는 컨텍스트 없이 잰다 (tests/bench_ladder.py).
    가드가 켜지는 모드로만 측정하고 꺼지는 모드로 배포하고 있었다.
    표현이 아니라 커버리지 구멍이었다.
    """
    route = semantic_route(["measurement_count"], ["item_name"], [("slack_rank", "<", "0.1")])

    without = make_pipeline(route).ask("항목별로 아슬아슬한 건수")
    with_context = make_pipeline(route).ask(
        "항목별로 아슬아슬한 건수", user_context={"line_code": "L2"}
    )

    assert without.status == "clarification_needed", "항목이 안 갈리는 질의는 거절이다"
    assert with_context.status == without.status, "로그인 값이 가드를 끄면 안 된다"
    assert not with_context.rows, "뜻 없는 순위를 표로 내보내면 안 된다"


def test_injected_filter_still_narrows_when_the_query_is_answerable(make_pipeline):
    """앞의 규칙이 자동 주입 자체를 망가뜨리지 않는가.

    주입 필터를 분모에서 뺀 것이지 필터에서 뺀 것이 아니다.
    범위는 그대로 좁혀져야 하고, 좁혔다는 사실도 가정에 남아야 한다.
    """
    route = semantic_route(["measurement_count"], ["item_name"])
    answer = make_pipeline(route).ask("항목별 측정 건수", user_context={"line_code": "L2"})

    assert answer.status == "ok"
    assert "line_code = ?" in answer.sql, "범위는 그대로 좁혀져야 한다"
    assert all(row[-1] == 88 for row in answer.rows), "L2만 세어야 한다"
    assert any("자동으로 채웠" in a for a in answer.assumptions), "좁혔다는 사실은 말해야 한다"


def test_drilldown_is_not_offered_when_two_metrics_share_a_row(make_pipeline):
    """모순된 드릴다운 — 지표 두 개짜리 표를 누르면 서로 배타적인 조건이 만들어졌다.

    상세 질의는 "이 숫자가 센 것"을 필터로 다시 적은 것인데(Metric.detail_filter),
    지표마다 그 조건이 다르다. 전부 AND로 합치면 verdict가 FAIL 이면서 PASS 여야 한다.

    결과는 0행이고 화면은 "조건에 맞는 것이 없습니다"라며 해석 가정을 의심하라고 한다.
    가정은 멀쩡하다. 원인을 엉뚱한 데로 보내는 것이라 안 여는 편이 낫다.
    """
    two = make_pipeline(semantic_route(["fail_count", "near_limit_count"], ["line_code"]))
    answer = two.ask("라인별 불합격 건수랑 아슬아슬한 건수")

    assert answer.status == "ok", "표 자체는 정상이다"
    assert answer.drill is None, "어느 지표를 여는지 정해지지 않으면 누를 수 없다"

    one = make_pipeline(semantic_route(["near_limit_count"], ["line_code"]))
    assert one.ask("라인별 아슬아슬한 건수").drill, "지표가 하나면 그대로 누를 수 있다"


def test_catalog_roundtrip_keeps_every_field(catalog):
    """유실된 카탈로그 필드 — JSON 왕복이 안전장치를 조용히 버렸다.

    모듈 독스트링은 "카탈로그는 코드가 아니라 데이터"라고 선언한다.
    그런데 to_dict/from_dict가 relative_to·ordinal·detail_filter·detail_columns를 떨어뜨렸다.
    왕복본은 에러 없이 로드되고 행수도 정상인데, 가드가 꺼지고 정렬이 뒤집힌다.

    "같이 고쳐라"를 주석으로 적어 두는 것은 지시이지 보증이 아니다.
    dataclass 필드와 직렬화 키를 여기서 실제로 대조한다 — 필드가 늘면 이 테스트가 먼저 깨진다.
    """
    from nl2sql.catalog import Catalog, CuratedView, Dimension, Metric

    data = catalog.to_dict()
    restored = Catalog.from_dict(data)

    def _keys(rows):
        return set().union(*(set(r) for r in rows)) if rows else set()

    for cls, present in (
        (Catalog, set(data)),
        (CuratedView, _keys(data["views"])),
        (Metric, _keys(data["metrics"])),
        (Dimension, _keys(data["dimensions"])),
    ):
        missing = {f.name for f in dataclasses.fields(cls)} - present
        assert not missing, f"{cls.__name__} 의 {missing} 이(가) 직렬화에서 빠졌다"

    assert restored.to_dict() == data, "왕복이 무손실이어야 한다"

    # 빠졌을 때 실제로 무엇이 깨졌는지 — 필드 이름이 아니라 동작으로 고정한다.
    assert restored.dimensions["slack_rank"].relative_to == "item_name", "blind 가드의 입력"
    assert restored.dimensions["inspection_month"].ordinal is True, "추이가 순위로 뒤집힌다"
    assert restored.metrics["near_limit_count"].detail_filter, "드릴다운이 전 측정을 연다"
    assert restored.detail_columns == catalog.detail_columns, "상세 목록이 꺼진다"


def test_roundtripped_catalog_still_refuses_the_blind_filter(make_pipeline, catalog):
    """왕복본이 원본과 같은 것을 거절하는가.

    필드가 살아 있는지가 아니라 가드가 사는지를 본다.
    relative_to가 사라졌을 때 나던 증상이 바로 이것이었다 — 원본은 거절, 왕복본은 통과.
    """
    from nl2sql.catalog import Catalog

    route = semantic_route(["measurement_count"], ["item_name"], [("slack_rank", "<", "0.1")])
    restored = Catalog.from_dict(catalog.to_dict())

    original = make_pipeline(route).ask("항목별로 아슬아슬한 건수")
    through_json = make_pipeline(route, catalog_override=restored).ask("항목별로 아슬아슬한 건수")

    assert original.status == "clarification_needed"
    assert through_json.status == original.status, "왕복했다고 가드가 꺼지면 안 된다"


def test_hosted_backend_imports_with_its_declared_extra(monkeypatch):
    """선언 안 된 의존성 — 호스팅 백엔드가 깔리지 않는 패키지를 임포트했다.

    `NvidiaLLM`은 생성자 안에서 httpx를 지연 임포트하는데
    그 패키지가 어느 엑스트라에도 없었다. 로컬 엑스트라를 깐 사람만 우연히 돌았고,
    문서대로 키만 꽂은 사람은 ModuleNotFoundError를 봤다.

    테스트가 이 줄을 한 번도 안 밟으면 의존성 선언이 맞는지 아무도 모른다.
    키 없이 만들면 ValueError가 나는데, 그 검사에 닿으려면 임포트를 먼저 지나야 한다.
    그래서 호출을 내보내지 않고도 선언을 검증한다.
    """
    from nl2sql.llm import NvidiaLLM

    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(ValueError):
        NvidiaLLM()
