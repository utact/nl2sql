"""세션 데모 — 일곱 장면.

데모의 목적은 "질문에 잘 답한다"를 보여주는 것이 아니다.
그건 누구나 보여주고 아무것도 증명하지 않는다. 순서가 반대다.

    1. 질문 → 그럴듯한 숫자. 아무 문제 없어 보인다
    2. 정답을 옆에 놓는다. 틀렸다. 에러는 없었다
    3. 해석 가정을 켠다. 한 문장에서 틀린 지점이 짚힌다
    4. 정의를 한 줄 고친다. 유형 자체가 사라진다
    5. 모델이 정의를 지어낸다. 되묻기로 되돌아온다
    6. 하나를 고치면 그 아래가 드러난다. 결함은 층으로 쌓여 있다
    7. 데이터에 없는 것을 물으면 가장 가까운 것이 답으로 나온다

모델은 `StubLLM`으로 고정한다.
보여주려는 것이 모델의 실력이 아니라 하네스의 동작이고, 매번 같은 결과가 나와야 하기 때문이다.
실제 모델로 돌리려면 `app.server`를 띄우면 된다.

사용법:
    python -m demo  # 일곱 장면 전부
    python -m demo --scene 2  # 한 장면만
"""

from __future__ import annotations

import argparse
import sys

from domain import catalog as build_catalog
from domain.seed import seed
from nl2sql import NL2SQL, Answer, SqliteExecutor, StubLLM

from .defects import (
    absolute_slack,
    degenerate_percentile,
    unguarded_free_assumptions,
    untyped_numeric,
    wrong_default_order,
)

DB_PATH = "demo.db"
RISK_QUESTION = "규격에 아슬아슬한 항목 알려줘"


def _route(metrics, dimensions, filters=(), reason="") -> dict:
    """라우터가 돌려줄 시맨틱 질의를 고정한다."""
    return {
        "route": "semantic",
        "reason": reason,
        "semantic_query": {
            "metrics": list(metrics),
            "dimensions": list(dimensions),
            "filters": [
                {"dimension": d, "op": o, "value": v} for d, o, v in filters
            ],
        },
        "clarification": None,
    }


def _pipeline(catalog, route: dict) -> NL2SQL:
    """정해진 라우팅을 돌려주는 파이프라인을 만든다."""
    return NL2SQL(
        catalog=catalog,
        llm=StubLLM(lambda system, user: route),
        executor=SqliteExecutor(DB_PATH),
    )


def _row(values: list, width: int = 100) -> str:
    """한 행을 폭 안에서 자른다. 자른 것은 자른 티를 낸다.

    `SELECT *`로 나온 행은 열이 열여섯이라 한 줄에 안 들어가고,
    터미널이 접으면 어디까지가 한 행인지 안 보인다.
    이 데모가 보이려는 것은 값이 아니라 표가 나왔다는 사실이므로 잘라도 논지가 안 상한다.
    """
    text = str(values)
    if len(text) <= width:
        return text
    return f"{text[:width]} ... ({len(values)}열)"


def _show(answer: Answer, limit: int = 6, indent: int = 2) -> None:
    """응답을 사람이 읽을 수 있게 출력한다.

    들여쓰기는 공백 개수로만 정한다.
    화살표(→)는 폰트에 따라 한 칸도 두 칸도 되므로 그 뒤에 열을 맞추지 않는다.

    SQL은 컴파일러가 만든 줄 구조를 그대로 둔다.
    한 줄로 접으면 터미널이 폭에 맞춰 아무 데서나 다시 접어서 절이 어디서 끊기는지 안 보인다.

    Args:
        answer: 출력할 응답.
        limit: 보여줄 결과 행수.
        indent: 바깥 들여쓰기. 부제 아래에서는 한 단계(2칸) 들어간다.
    """
    pad = " " * indent
    deep = pad + "  "
    print(f"{pad}상태: {answer.status} / 경로: {answer.route}")
    if answer.message:
        print(f"{pad}메시지: {answer.message}")
    if answer.sql:
        print(f"{pad}SQL:")
        for line in answer.sql.strip().splitlines():
            print(f"{deep}{line}")
    if answer.params:
        print(f"{pad}바인딩: {answer.params}  ({[type(p).__name__ for p in answer.params]})")
    if answer.assumptions:
        print(f"{pad}해석 가정:")
        for a in answer.assumptions:
            print(f"{deep}- {a}")
    if answer.rows:
        print(f"{pad}결과 {len(answer.rows)}행:")
        for row in answer.rows[:limit]:
            print(f"{deep}{_row(list(row))}")


# 위험 구간 질문의 라우팅.
# 결함 버전은 절대 여유에, 고친 버전은 항목 내 백분위에 필터를 건다.
ROUTE_ABSOLUTE = _route(
    metrics=["measurement_count"],
    dimensions=["item_name", "unit"],
    filters=[("spec_slack", "<", "0.05"), ("verdict", "=", "합격")],
    reason="절대 여유가 작은 항목",
)
ROUTE_RANK = _route(
    metrics=["measurement_count"],
    dimensions=["item_name", "unit"],
    filters=[("slack_rank", "<", "0.05"), ("verdict", "=", "합격")],
    reason="항목 내 여유 백분위가 낮은 항목",
)


def scene1() -> None:
    print("\n[장면 1] 질문에 답한다. 아무 문제 없어 보인다.")
    print(f'  질문: "{RISK_QUESTION}"')
    answer = _pipeline(absolute_slack(build_catalog()), ROUTE_ABSOLUTE).ask(RISK_QUESTION)
    _show(answer)
    print("  → 실행 정상, 에러 없음, 목록도 그럴듯하다.")


def scene2() -> None:
    print("\n[장면 2] 정답을 옆에 놓는다.")
    answer = _pipeline(build_catalog(), ROUTE_RANK).ask(RISK_QUESTION)
    _show(answer)
    print("  → 장면 1과 항목 구성이 다르다. 둘 중 하나는 틀렸고, 틀린 쪽은 조용했다.")
    print("    원인: 항목마다 단위와 수치 스케일이 다른데 절대 임계로 비교했다.")
    print("    단위가 작은 항목이 목록을 독점한다.")


def scene3() -> None:
    print("\n[장면 3] 해석 가정을 켠다.")
    answer = _pipeline(build_catalog(), ROUTE_RANK).ask(RISK_QUESTION)
    if answer.assumptions:
        print("  해석 가정:")
        for a in answer.assumptions:
            print(f"    - {a}")
    print("  → 사용자는 SQL을 읽을 수 없다. 그래서 이 시스템을 만들었다.")
    print("    하지만 '무엇으로 이해했는가'는 한 문장이고, 자기 의도라서 판단할 수 있다.")
    print("    검증 지점이 출력이 아니라 입력으로 올라간 것이다.")


def scene4() -> None:
    print("\n[장면 4] 보여줘도 사용자가 판단할 수 없는 가정은 정의 시점에 없앤다.")
    print(f'  질문: "{RISK_QUESTION}" (백분위 < 0.05)')

    print("\n  (a) 차원에 타입 선언이 없을 때")
    broken = _pipeline(untyped_numeric(build_catalog()), ROUTE_RANK).ask(RISK_QUESTION)
    _show(broken, limit=3, indent=4)

    print("\n  (b) 차원에 '이 차원은 숫자다'를 선언했을 때")
    fixed = _pipeline(build_catalog(), ROUTE_RANK).ask(RISK_QUESTION)
    _show(fixed, limit=3, indent=4)

    broken_rows = sum(r[-1] for r in broken.rows) if broken.rows else 0
    fixed_rows = sum(r[-1] for r in fixed.rows) if fixed.rows else 0
    print(f"\n  → 걸러진 건수: {broken_rows} vs {fixed_rows}")
    print("    (a)는 필터가 통째로 무력화됐다. 에러는 없고 숫자만 틀렸다.")
    print("    모델은 옳은 지표·차원·연산자를 골랐다. 틀린 것은 정의였다.")
    print("    조용한 오답의 일부는 모델이 아니라 하네스가 만든다.")


def scene5() -> None:
    print("\n[장면 5] 모델이 정의를 지어내면 오답이 아니라 질문이 되어 돌아온다.")
    print('  질문: "규격에 아슬아슬한 항목 알려줘" (모델이 없는 지표를 고름)')
    invented = _route(metrics=["danger_score"], dimensions=["item_name"])
    answer = _pipeline(build_catalog(), invented).ask(RISK_QUESTION)
    _show(answer)
    print("  → 컴파일러가 정의된 지표·차원으로 다시 검증해 되묻기로 강등했다. 오답 0건.")
    print("    '모르겠다'를 말할 정의가 없는 시스템은 그럴듯한 오답으로 빈칸을 채운다.")


def scene6() -> None:
    print("\n[장면 6] 하나를 고치면 그 아래가 드러난다.")
    print(f'  질문: "{RISK_QUESTION}" (항목별)')

    print("\n  (a) 여유 평균 · 방향을 내림차순으로 선언했을 때")
    reversed_ = _pipeline(
        wrong_default_order(build_catalog()),
        _route(metrics=["avg_slack_rank"], dimensions=["item_name"]),
    ).ask(RISK_QUESTION)
    _show(reversed_, limit=4, indent=4)
    print("    → 가장 여유로운 항목이 맨 위다. 아슬아슬한 쪽을 물었는데.")

    print("\n  (b) 방향을 고치고, 지표를 최솟값으로 뒀을 때")
    flat = _pipeline(
        degenerate_percentile(build_catalog()),
        _route(metrics=["min_slack_rank"], dimensions=["item_name"]),
    ).ask(RISK_QUESTION)
    _show(flat, limit=4, indent=4)
    print("    → 방향은 맞다. 그런데 값이 전부 같다.")
    print("      항목별 백분위의 항목 내 최솟값은 정의상 항상 0이다.")
    print("      (a)를 고치기 전에는 안 보인다. 거꾸로인 목록에서는 값이 같다는 것이 안 띈다.")

    print("\n  (c) 정의에서 제거하고, 하위 꼬리의 크기를 세는 지표로 바꿨을 때")
    fixed = _pipeline(
        build_catalog(),
        _route(metrics=["near_limit_count"], dimensions=["item_name"]),
    ).ask(RISK_QUESTION)
    _show(fixed, limit=4, indent=4)
    print("    → 백분위라 단위가 없고, 세면 항목 간 비교가 성립한다.")
    print("      (b)는 보여줘도 판단할 수 없는 종류라 표면화가 답이 아니었다.")
    print("      정의 시점에 없애는 수다.")


# 직접 생성이 대체한 사실을 안 밝히는 장면.
# 앞의 장면들과 달리 라우터가 아니라 생성기를 고정한다.
# 이 유형은 갈래를 잘못 골라서가 아니라, 갈래는 맞는데 답할 것이 없을 때 온다.
ABSENT_QUESTION = "검사 설비 점검 이력 보여줘"
ABSENT_SQL = "SELECT * FROM inspection_results"


def _free_pipeline(catalog) -> NL2SQL:
    """라우터에는 free를, 생성기에는 고정 SQL을 돌려주는 파이프라인."""

    def handler(system: str, user: str) -> dict:
        if "router" in system:
            return {
                "route": "free",
                "reason": "고정",
                "semantic_query": None,
                "clarification": None,
                "clarify_candidates": None,
            }
        # 모델이 대체했다는 사실을 안 적은 상태. 실제로 이렇게 나온다.
        return {"sql": ABSENT_SQL, "assumptions": []}

    return NL2SQL(
        catalog=catalog, llm=StubLLM(handler), executor=SqliteExecutor(DB_PATH)
    )


def scene7() -> None:
    print("\n[장면 7] 데이터에 없는 것을 물으면 가장 가까운 것이 답으로 나온다.")
    print(f'  질문: "{ABSENT_QUESTION}"  (설비 점검 데이터는 이 스키마에 없다)')

    print("\n  (a) 하네스가 가정을 보증하지 않을 때")
    before = unguarded_free_assumptions(_free_pipeline(build_catalog())).ask(ABSENT_QUESTION)
    _show(before, limit=2, indent=4)
    print("    → 상태 ok, 에러 없음, 표는 그럴듯하다.")
    print("      그리고 이 경로의 유일한 방어선인 해석 가정이 비어 있다.")
    print("      사용자는 이것이 설비 점검 이력이 아니라는 것을 알 방법이 없다.")

    print("\n  (b) 비면 정의에서 채울 때")
    after = _free_pipeline(build_catalog()).ask(ABSENT_QUESTION)
    _show(after, limit=2, indent=4)
    print("    → SQL도 결과도 (a)와 똑같다. 달라진 것은 첫 두 줄뿐이다.")
    print("      틀린 답을 막은 것이 아니라 틀렸다는 것을 말하게 한 것이다.")
    print("      정의 시점에 없앨 수 없는 유형이라 표면화가 답이다.")


SCENES = {1: scene1, 2: scene2, 3: scene3, 4: scene4, 5: scene5, 6: scene6, 7: scene7}


def _force_utf8_stdout() -> None:
    """콘솔 인코딩을 UTF-8로 고정한다.

    한국어 Windows의 기본 콘솔 코드페이지는 cp949 라 '—' 하나에 UnicodeEncodeError로 죽는다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description="세션 데모 일곱 장면.")
    parser.add_argument("--scene", type=int, choices=sorted(SCENES), help="한 장면만 실행")
    args = parser.parse_args()

    seed(DB_PATH)
    for number in [args.scene] if args.scene else sorted(SCENES):
        SCENES[number]()


if __name__ == "__main__":
    main()
