"""나이브 기준선 — 같은 모델, 구조 없이.

    python tests/bench_naive.py

이 저장소는 "구조가 조용한 오답을 막는다"고 주장한다.
비교 대상이 없으면 그 주장은 확인할 수 없다. 이 스크립트가 비교 대상이다.

## 나이브란 무엇인가

대부분의 NL2SQL이 이렇게 생겼다.

    스키마를 프롬프트에 넣는다 -> 모델이 SQL을 쓴다 -> 실행한다 -> 표를 보여준다

라우터도, 시맨틱 레이어도, 되묻기도, 거절도, 해석 가정도 없다.
답이 나오면 표가 뜨고, 틀려도 똑같이 표가 뜬다.

## 일부러 약하게 만들지 않았다

나이브 쪽에도 큐레이션 뷰를 준다.
물리 스키마를 던져 주면 조인·소프트삭제·단위 혼재로 알아서 무너지고, 그건 논점이 아니다.
뷰는 이미 사실의 모호성을 정의 시점에 없앤 결과물이므로 그것까지 주고 시작한다.

같은 모델, 같은 질문, 같은 데이터, 같은 뷰. 다른 것은 구조뿐이다.

## 무엇을 오답으로 세는가

자연어 질문의 정답을 기계로 정하는 것은 일반적으로 불가능하다.
그래서 판정할 수 있는 것만 판정하고 나머지는 보류한다.

- 시맨틱·값 열거 — 우리 컴파일러가 만든 SQL의 결과와 행 집합을 대조한다.
  정의가 정답을 갖고 있는 유일한 구간이다.
- 되묻기 — 답이 하나로 안 좁혀지는 질문이다. 나이브는 되물을 수 없으니 무엇을 답하든 추측이다.
- 거절 — 데이터에 없는 것을 물었다. 행이 나오면 오답이다.
- 직접 생성 — 정답을 기계로 정할 수 없다. 판정 보류로 두고 세지 않는다.

보류를 오답에 넣지 않는 것이 중요하다. 유리한 쪽으로 세면 숫자가 죽는다.
"""

from __future__ import annotations

import os
import sys
import time

# 스크립트로 직접 실행하므로 (tests/ 는 패키지가 아니다) 저장소 루트를 얹는다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bench_env  # noqa: E402
from bench_routing import _pad  # noqa: E402
from test_layer3_routing import (  # noqa: E402
    ABSTAIN_CASES,
    CLARIFY_CASES,
    ENUMERATION_CASES,
    FREE_CASES,
    SEMANTIC_CASES,
    Case,
    _today_inside_the_data,
)

from domain import catalog as build_catalog  # noqa: E402
from domain.seed import seed  # noqa: E402
from nl2sql import NL2SQL, SqliteExecutor, load_env_file, resolve_llm  # noqa: E402
from nl2sql.semantic import SemanticQuery, compile_semantic  # noqa: E402

# 나이브 구현이 쓸 프롬프트.
# 안전 규칙(SELECT만, 뷰만)은 남긴다. 그것까지 빼면 재는 것이 조용한 오답이 아니라 인젝션이 된다.
NAIVE_PROMPT = """\
You answer questions about a database by writing a single SQLite SELECT
statement over the views below. Return JSON: {"sql": "..."}.
Use only the listed views and columns. Resolve relative dates from the
current date given.
"""

NAIVE_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
    "additionalProperties": False,
}

JUDGED = {
    "시맨틱": SEMANTIC_CASES,
    "값 열거": ENUMERATION_CASES,
    "되묻기": CLARIFY_CASES,
    "거절": ABSTAIN_CASES,
}


def naive_answer(llm, catalog, executor, question: str, today: str):
    """스키마 -> SQL -> 실행 -> 표. 그 사이에 아무것도 없다.

    Returns:
        (sql, rows, error). 어느 것도 사용자에게 경고를 남기지 않는다.
    """
    user = (
        f"Current date: {today}\n\n"
        f"# Views\n{catalog.describe_views()}\n\n"
        f"# Question\n{question}"
    )
    try:
        raw = llm.complete_json(NAIVE_PROMPT, user, NAIVE_SCHEMA)
        sql = str(raw.get("sql", "")) if isinstance(raw, dict) else ""
    except Exception as e:  # noqa: BLE001 — 기준선의 실패도 결과다
        return "", [], f"생성 실패: {e}"
    try:
        result = executor.run(sql, [])
    except Exception as e:  # noqa: BLE001
        return sql, [], f"실행 실패: {e}"
    return sql, result.rows, ""


def reference_rows(case: Case, catalog, executor) -> list[tuple] | None:
    """정의가 아는 정답. 시맨틱·값 열거에서만 존재한다."""
    if case.route != "semantic":
        return None
    query = SemanticQuery(
        metrics=list(case.metrics),
        dimensions=list(case.dimensions),
        distinct=case.distinct,
    )
    sql, params = compile_semantic(query, catalog)
    return executor.run(sql, params).rows


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    load_env_file()
    llm = resolve_llm()
    catalog = build_catalog()
    db = seed("bench.db")
    executor = SqliteExecutor(db)
    today = _today_inside_the_data(db)
    ours = NL2SQL(catalog=build_catalog(), llm=llm, executor=SqliteExecutor(db))

    print(bench_env.describe(llm))
    print(f"기준일: {today}")
    print(f"질문: {sum(len(v) for v in JUDGED.values())}건 판정 + {len(FREE_CASES)}건 보류\n")

    naive_wrong: list[str] = []
    ours_wrong: list[str] = []
    timings: list[float] = []

    header = f"{_pad('갈래', 10)}{_pad('질문', 30)}{_pad('나이브', 22)}우리 구조"
    print(header)
    print("-" * 96)

    for family, cases in JUDGED.items():
        for case in cases:
            started = time.perf_counter()
            _, rows, error = naive_answer(llm, catalog, executor, case.question, today)
            timings.append(time.perf_counter() - started)
            answer = ours.ask(case.question)

            # 나이브 판정
            if error:
                naive_verdict = "에러"
            elif case.route == "abstain":
                naive_verdict = f"{len(rows)}행 ← 오답" if rows else "0행"
                if rows:
                    naive_wrong.append(case.question)
            elif case.route == "clarify":
                naive_verdict = f"{len(rows)}행 ← 추측" if rows else "0행"
                if rows:
                    naive_wrong.append(case.question)
            else:
                expected = reference_rows(case, catalog, executor)
                same = expected is not None and set(map(tuple, rows)) == set(map(tuple, expected))
                naive_verdict = f"{len(rows)}행" + ("" if same else " ← 오답")
                if not same:
                    naive_wrong.append(case.question)

            # 우리 구조 판정. 같은 잣대를 쓴다.
            if answer.status != "ok":
                ours_verdict = {"clarification_needed": "되묻기",
                                "abstained": "거절",
                                "rejected": "차단",
                                "error": "에러"}.get(answer.status, answer.status)
                if case.route == "semantic":
                    ours_wrong.append(case.question)  # 답할 수 있는데 못 했다
            elif case.route in ("abstain", "clarify"):
                ours_verdict = f"{len(answer.rows)}행 ← 오답"
                ours_wrong.append(case.question)
            else:
                expected = reference_rows(case, catalog, executor)
                same = expected is not None and set(map(tuple, answer.rows)) == set(
                    map(tuple, expected)
                )
                ours_verdict = f"{len(answer.rows)}행" + ("" if same else " ← 오답")
                if not same:
                    ours_wrong.append(case.question)

            print(
                f"{_pad(family, 10)}{_pad(case.question, 30)}"
                f"{_pad(naive_verdict, 22)}{ours_verdict}"
            )

    print("-" * 96)

    # 두 묶음을 섞어서 세면 안 된다. 서로 다른 것을 말하기 때문이다.
    #
    # 앞 묶음은 둘 다 답할 수 있는 문제라 정확도의 차이가 나온다.
    # 뒤 묶음은 나이브에 되묻기도 거절도 상태로 없어서 무조건 표가 나가는 구간이다.
    #
    # 합쳐서 한 숫자로 내면 정확도 이야기로 오해된다.
    # 나눠야 "이건 정확도로 안 줄어든다"가 보인다.
    answerable = len(SEMANTIC_CASES) + len(ENUMERATION_CASES)
    open_ended = len(CLARIFY_CASES) + len(ABSTAIN_CASES)
    front = set(c.question for c in SEMANTIC_CASES + ENUMERATION_CASES)

    def split(wrong):
        return sum(1 for q in wrong if q in front), sum(1 for q in wrong if q not in front)

    naive_front, naive_back = split(naive_wrong)
    ours_front, ours_back = split(ours_wrong)

    print(f"\n나이브 호출당 평균 {sum(timings)/len(timings):.1f}초")
    print(f"직접 생성 {len(FREE_CASES)}건은 정답을 기계로 정할 수 없어 판정에서 뺐다.\n")
    print(f"{_pad('', 34)}{_pad('나이브', 12)}우리 구조")
    print(
        f"{_pad('답이 하나로 정해지는 질문', 34)}"
        f"{_pad(f'{naive_front}/{answerable} 오답', 12)}{ours_front}/{answerable} 오답"
    )
    print(
        f"{_pad('되물어야 하거나 답이 없는 질문', 34)}"
        f"{_pad(f'{naive_back}/{open_ended} 오답', 12)}{ours_back}/{open_ended} 오답"
    )
    print("\n위 줄은 정확도의 차이다. 둘 다 답할 수 있는 문제인데 결과가 갈렸다.")
    print("아래 줄은 정확도의 문제가 아니다 — 나이브에는 되묻기도 거절도")
    print("상태로 존재하지 않으므로 무엇을 물어도 표가 나간다.")
    print("실무 질문의 상당수가 이쪽이고, 이 여섯 건은 정확도로 안 줄어든다.")
    print("\n그리고 나이브 쪽 표에는 어느 것이 그런 경우인지 알려 주는 신호가 없다.")


if __name__ == "__main__":
    main()
