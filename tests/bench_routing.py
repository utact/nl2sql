"""라우팅 실측 — 모델을 바꿔 가며 무엇이 어떻게 실패하는지 본다.

    python tests/bench_routing.py

pytest가 아니라 스크립트인 이유가 있다.
골든셋은 통과/실패를 묻지만, 여기서 알고 싶은 것은 통과율이 아니다.

    1. 실패가 어느 갈래에서 나는가
    2. 실패의 방향이 안전한 쪽인가 (뒤에 받쳐 주는 층이 있는가)
    3. 그래서 사용자에게 조용한 오답이 나갔는가 — 이게 유일하게 중요한 숫자다

3번이 핵심이다.
라우팅이 틀려도 컴파일러·가드가 잡으면 사용자는 오답 대신 질문을 받는데, "28/30" 은 그걸 못 말한다.
이 저장소의 주장은 정확도가 아니라 틀렸을 때 어디로 떨어지는가다.

pytest는 `test_*.py`만 수집하므로 이 파일은 골든셋에 섞이지 않는다.
케이스는 골든셋과 같은 것을 쓴다. 두 벌로 두면 어긋난다.
"""

from __future__ import annotations

import os
import sys
import time
import unicodedata
from collections import defaultdict

# 스크립트로 직접 실행하므로 (tests/ 는 패키지가 아니다) 저장소 루트를 얹는다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_layer3_routing import (  # noqa: E402
    ABSTAIN_CASES,
    ALL_CASES,
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

FAMILIES = {
    "시맨틱": SEMANTIC_CASES,
    "값 열거": ENUMERATION_CASES,
    "되묻기": CLARIFY_CASES,
    "거절": ABSTAIN_CASES,
    "직접 생성": FREE_CASES,
}

# 갈래를 잘못 갔을 때 뒤에 무엇이 받쳐 주는가.
# 주장이 "틀리지 않는다"가 아니라 "틀렸을 때 안전한 쪽으로 떨어진다"이므로, 재야 할 것은 이 표다.
CAUGHT_BY = {
    ("clarify", "semantic"): "없음 — 물어봐야 할 때 추측했다",
    ("abstain", "semantic"): "정의 검증 (컴파일 실패 → 되묻기)",
    # 가드레일은 SQL이 위험한지만 보지 그 SQL이 질문에 답하는지는 안 본다.
    # 직접 생성은 답할 것이 없어도 가장 가까운 것을 내놓는다.
    ("abstain", "free"): "가드레일은 못 잡는다 — 해석 가정만 남는다",
    ("free", "semantic"): "정의 검증 (컴파일 실패 → 되묻기)",
    ("free", "abstain"): "없음 — 답할 수 있는데 거절했다 (오답은 아니다)",
    ("semantic", "free"): "가드레일 5겹 (신뢰도만 하락)",
    ("semantic", "clarify"): "없음 — 물어도 됐는데 물었다 (오답은 아니다)",
}


def _width(text: str) -> int:
    """터미널에서 차지하는 칸 수를 센다.

    한글·한자는 한 글자가 두 칸이라 `len()`과 어긋난다.
    그래서 `f"{'시맨틱':<8}"` 같은 패딩은 한글 표에서 열을 못 맞춘다.
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, align: str = "<") -> str:
    """폭 기준으로 채운다. `_width`가 있어야 한글 표의 열이 맞는다."""
    fill = " " * max(0, width - _width(text))
    return text + fill if align == "<" else fill + text


def _vocabulary_matches(case: Case, decision) -> tuple[bool, str]:
    """시맨틱으로 갔을 때 고른 이름이 기대와 같은가."""
    query = decision.semantic_query
    if query is None:
        return False, "semantic_query가 비었다"
    if query.distinct is not case.distinct:
        return False, f"distinct {query.distinct} (기대 {case.distinct})"
    if case.distinct and query.metrics:
        return False, f"목록만 물었는데 지표 {query.metrics} 를 끼워 넣었다"
    missing = [m for m in case.metrics if m not in query.metrics]
    missing += [d for d in case.dimensions if d not in query.dimensions]
    if missing:
        return False, f"{missing} 없음 (실제 {query.metrics} × {query.dimensions})"
    return True, ""


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    load_env_file()
    llm = resolve_llm()
    name = getattr(llm, "model", None) or getattr(llm, "model_id", "?")
    db = seed("bench.db")
    today = _today_inside_the_data(db)
    pipeline = NL2SQL(catalog=build_catalog(), llm=llm, executor=SqliteExecutor(db))

    print(f"백엔드: {type(llm).__name__} / {name}")
    print(f"기준일: {today}")
    print(f"케이스: {len(ALL_CASES)}건\n")

    results: dict[str, list] = defaultdict(list)
    timings: list[float] = []
    silent_wrong: list[str] = []

    for family, cases in FAMILIES.items():
        for case in cases:
            started = time.perf_counter()
            decision = pipeline.router.route(case.question, today, None)
            elapsed = time.perf_counter() - started
            timings.append(elapsed)

            if decision.route != case.route:
                caught = CAUGHT_BY.get((case.route, decision.route), "미분류")
                results[family].append((case, "갈래", f"{decision.route} ← {caught}"))
                # 갈래가 틀렸을 때 사용자에게 무엇이 나갔는지가 진짜 질문이다.
                # ask를 다시 부르면 모델이 한 번 더 불려 잰 결정과 본 결과가 달라진다.
                # 그래서 dispatch를 쓴다.
                answer = pipeline.dispatch(decision, case.question, today)
                if answer.status == "ok" and answer.rows:
                    silent_wrong.append(f"{case.question}  →  {len(answer.rows)}행")
                continue

            if decision.route == "semantic":
                ok, why = _vocabulary_matches(case, decision)
                if not ok:
                    results[family].append((case, "정의", why))
                    answer = pipeline.dispatch(decision, case.question, today)
                    if answer.status == "ok" and answer.rows:
                        silent_wrong.append(f"{case.question}  →  {len(answer.rows)}행")
                    continue

            if decision.route == "clarify":
                lost = [
                    d
                    for d in case.dimensions
                    for c in decision.candidates
                    if d not in c.query.dimensions
                ]
                if not decision.candidates:
                    results[family].append((case, "후보", "되물으면서 고를 것을 안 줬다"))
                    continue
                if lost:
                    results[family].append((case, "후보", f"차원 {set(lost)} 를 잃었다"))
                    continue

            results[family].append((case, None, ""))

    print(f"{_pad('갈래', 10)}{_pad('통과', 8)}실패한 자리")
    print("-" * 72)
    for family in FAMILIES:
        rows = results[family]
        passed = sum(1 for _, kind, _ in rows if kind is None)
        failures = [f"{kind}: {case.question}" for case, kind, _ in rows if kind]
        score = f"{passed}/{len(rows)}"
        print(f"{_pad(family, 10)}{_pad(score, 8)}{failures[0] if failures else ''}")
        for extra in failures[1:]:
            print(f"{_pad('', 18)}{extra}")
    print("-" * 72)

    total = sum(len(v) for v in results.values())
    passed = sum(1 for v in results.values() for _, kind, _ in v if kind is None)
    print(f"{_pad('합계', 10)}{passed}/{total}")
    print(f"\n호출당 평균 {sum(timings)/len(timings):.1f}초 / 최대 {max(timings):.1f}초")

    print("\n실패의 상세 — 무엇이 받쳤는가")
    for family, rows in results.items():
        for case, kind, detail in rows:
            if kind:
                print(f"  [{family}/{kind}] {case.question}")
                print(f"      {detail}")

    # 이 저장소가 재야 하는 유일한 숫자.
    print(f"\n>>> 사용자에게 나간 조용한 오답: {len(silent_wrong)}건")
    for line in silent_wrong:
        print(f"    {line}")


if __name__ == "__main__":
    main()
