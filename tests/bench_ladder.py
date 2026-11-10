"""모델을 키우면 조용한 오답이 줄어드는가.

    python tests/bench_ladder.py

"모델이 좋아지면 해결되는 문제 아니냐"가 이 저장소가 가장 자주 받는 질문이다.
`bench_naive.py`는 그 질문에 답하지 못한다. 모델 하나로만 재기 때문이다.
여기서는 모델을 크기순으로 올려 가며 나이브와 구조를 나란히 잰다.

## 계열을 섞지 않는다

같은 계열 안에서 크기만 다른 쌍을 둘 잡는다 — llama 8B/70B와 gpt-oss 20B/120B.
벤더가 섞이면 차이가 크기 때문인지 만든 팀이 달라서인지 구분할 수 없다.
두 계열에서 같은 방향이 나오는지까지 봐야 "그 계열이 원래 그렇다"를 막을 수 있다.

## 실패를 두 종류로 나눠 센다

이 저장소의 논지가 그 구분이므로 측정도 그렇게 해야 한다.

- 시끄러운 실패 — 예외가 나서 답 자체가 없다. 화면이 빨개진다. 아무도 그 답을 안 쓴다
- 조용한 오답 — 무언가 사용자에게 나갔고 그것이 틀렸다. 그대로 쓰인다

총 실패 수만 세면 둘이 상쇄되어 사라진다.
모델이 커지면서 시끄러운 실패가 조용한 오답으로 바뀌는 것이 가설이고,
그것은 두 열로 나눠 세야만 보인다.

## 무엇을 오답으로 세는가

`bench_naive.py`와 같은 잣대를 쓴다. 나이브에 유리한 쪽으로 셌다.

- 시맨틱·값 열거 — 컴파일러가 만든 SQL의 결과와 행 집합을 대조한다
- 되묻기 — 답이 하나로 안 좁혀지는 질문이다. 표가 나가면 추측을 사실로 내보낸 것이다
- 거절 — 데이터에 없는 것을 물었다. 행이 나오면 오답이고, 빈 표는 오답으로 세지 않는다
- 직접 생성 — 정답을 기계로 정할 수 없다. 판정에서 뺀다

## 원자료를 남긴다

호출이 수천 건이라 중간에 끊기면 다시 돌리는 비용이 크다.
칸 하나가 끝날 때마다 `bench_ladder.json`에 적고, 다시 돌리면 있는 칸은 건너뛴다.
표를 다시 그리기만 하려면 `--report`를 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

# 스크립트로 직접 실행하므로 (tests/ 는 패키지가 아니다) 저장소 루트를 얹는다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bench_env  # noqa: E402
from bench_naive import naive_answer, reference_rows  # noqa: E402
from bench_routing import _pad  # noqa: E402
from test_layer3_routing import (  # noqa: E402
    ABSTAIN_CASES,
    CLARIFY_CASES,
    ENUMERATION_CASES,
    SEMANTIC_CASES,
    _today_inside_the_data,
)

from domain import catalog as build_catalog  # noqa: E402
from domain.seed import seed  # noqa: E402
from nl2sql import NL2SQL, SqliteExecutor, load_env_file, resolve_llm  # noqa: E402

# 같은 계열 안에서 크기만 다른 쌍을 셋 잡는다. (라벨, 백엔드, 모델 ID) 순이다.
#
# 계열을 섞으면 차이가 크기 때문인지 만든 팀이 달라서인지 구분할 수 없다.
# 그래서 계열 안에서 비교하고, 세 계열에서 같은 방향이 나오는지로 확인한다.
# 한 계열만 보면 "그 계열이 원래 그런 것"이라는 반론을 막을 수 없다.
#
# 소형 구간은 로컬로 잰다. 호스팅에서는 이 크기대가 응답하지 않아,
# 재면 모델이 아니라 남의 인프라 가동률을 재게 된다.
# 그리고 이 구간이 논지의 핵심이다.
# 못 만들어 시끄럽게 실패하던 것이 그럴듯하게 만들어 조용히 틀리는 쪽으로 넘어간다.
# 그 전이가 여기서 일어난다.
#
# 백엔드가 섞여도 비교는 성립한다.
# 두 백엔드 모두 _prompted_json을 쓴다 — 구조화 출력 강제가 없고,
# 스키마를 프롬프트에 넣어 출력에서 JSON을 뽑는 같은 경로다. 다른 것은 모델뿐이다.
LADDER = [
    ("Qwen 0.5B", "huggingface", "Qwen/Qwen2.5-0.5B-Instruct"),
    ("Qwen 1.5B", "huggingface", "Qwen/Qwen2.5-1.5B-Instruct"),
    ("llama 8B", "nvidia", "meta/llama-3.1-8b-instruct"),
    ("gpt-oss 20B", "nvidia", "openai/gpt-oss-20b"),
    ("llama 70B", "nvidia", "meta/llama-3.1-70b-instruct"),
    ("gpt-oss 120B", "nvidia", "openai/gpt-oss-120b"),
]

# 잰 다음에 못 쓰겠다고 판단한 칸. 지우지 않고 이유와 함께 남긴다.
#
# 숫자를 조용히 빼면 왜 뺐는지가 사라지고, 다음 사람이 같은 함정에 다시 걸린다.
# 모델의 실패와 엔드포인트의 실패를 가르지 않으면 남의 가동률이 내 모델 점수로 들어온다.
EXCLUDED = {
    "meta/llama-3.1-70b-instruct": (
        "엔드포인트가 SELECT 1에 중앙값 33.8초, 타임아웃 17%. "
        "같은 키로 이어 부른 gpt-oss 120B는 0.2초라 키 제한이 아니다"
    ),
}

# 응답이 없는 모델을 오래 기다리지 않는다.
# 기다린 끝에 못 받은 답은 시끄러운 실패로 센다 — 사용자 쪽에서도 그것이 실패다.
TIMEOUT_SECONDS = 60.0

CASES = SEMANTIC_CASES + ENUMERATION_CASES + CLARIFY_CASES + ABSTAIN_CASES
_HERE = os.path.dirname(os.path.abspath(__file__))


def _result_path(backend: str) -> str:
    """백엔드별로 원자료를 나눠 적는다. 두 백엔드를 동시에 돌려도 서로 안 덮는다."""
    return os.path.join(_HERE, f"bench_ladder_{backend}.json")

# 판정 결과. 셋 중 하나로만 떨어진다.
OK = "ok"
LOUD = "loud"
QUIET = "quiet"


def _judge_naive(case, rows, error, expected) -> str:
    """나이브 한 건의 판정.

    Args:
        case: 질문과 기대 갈래.
        rows: 실행 결과. 실패했으면 빈 목록이다.
        error: 생성·실행 실패 사유. 성공했으면 빈 문자열이다.
        expected: 정의가 아는 정답 행. 시맨틱·값 열거에서만 있다.

    Returns:
        OK / LOUD / QUIET 중 하나.
    """
    if error:
        return LOUD
    if case.route == "abstain":
        # 나이브에는 거절이 상태로 없다. 빈 표는 우연이지만 틀린 숫자를 주지는 않았다.
        return QUIET if rows else OK
    if case.route == "clarify":
        # 답이 하나로 안 좁혀지는 질문이다. 무엇을 내든 추측을 사실로 내보낸 것이다.
        return QUIET if rows else OK
    if expected is None:
        return OK
    return OK if set(map(tuple, rows)) == set(map(tuple, expected)) else QUIET


def _judge_ours(case, answer, expected) -> str:
    """구조 한 건의 판정. 나이브와 같은 잣대를 쓴다.

    Args:
        case: 질문과 기대 갈래.
        answer: 파이프라인 응답.
        expected: 정의가 아는 정답 행.

    Returns:
        OK / LOUD / QUIET 중 하나.
    """
    if answer.status != "ok":
        # 되묻기·거절·차단은 표가 안 나갔다. 조용하지 않다.
        # 답할 수 있는 질문에 그랬으면 실패지만, 사용자가 그것을 안다.
        return LOUD if case.route == "semantic" else OK
    if case.route in ("abstain", "clarify"):
        return QUIET
    if expected is None:
        return OK
    return OK if set(map(tuple, answer.rows)) == set(map(tuple, expected)) else QUIET


def _open_llm(backend: str, model_id: str):
    """백엔드에 맞게 모델을 연다.

    로컬은 캐시에 없으면 받아야 하므로 오프라인을 끈다.
    이 스크립트는 측정 도구이고, 앱의 기본값(오프라인)을 바꾸지 않는다.
    """
    if backend == "huggingface":
        return resolve_llm(backend="huggingface", model=model_id, offline=False)
    return resolve_llm(backend="nvidia", model=model_id, timeout=TIMEOUT_SECONDS)


def _measure(backend, model_id, runs, catalog, executor, today, expected_rows) -> dict:
    """모델 하나로 나이브와 구조를 각각 `runs` 회 돌린다.

    Returns:
        {"naive": {...}, "ours": {...}} 형태의 집계. 값은 판정별 건수다.
    """
    llm = _open_llm(backend, model_id)
    ours = NL2SQL(catalog=build_catalog(), llm=llm, executor=executor)

    tally = {
        "naive": {OK: 0, LOUD: 0, QUIET: 0},
        "ours": {OK: 0, LOUD: 0, QUIET: 0},
        "quiet_questions": {},
        "seconds": 0.0,
    }
    started = time.perf_counter()

    for run in range(runs):
        for case in CASES:
            expected = expected_rows.get(case.question)

            _, rows, error = naive_answer(llm, catalog, executor, case.question, today)
            verdict = _judge_naive(case, rows, error, expected)
            tally["naive"][verdict] += 1

            try:
                answer = ours.ask(case.question)
                ours_verdict = _judge_ours(case, answer, expected)
            except Exception:  # noqa: BLE001 — 기준선의 실패도 결과다
                ours_verdict = LOUD
            tally["ours"][ours_verdict] += 1

            if ours_verdict == QUIET:
                key = case.question
                tally["quiet_questions"][key] = tally["quiet_questions"].get(key, 0) + 1

        print(f"    {run + 1}/{runs} 회", flush=True)

    tally["seconds"] = round(time.perf_counter() - started, 1)
    # 칸이 스스로를 설명해야 한다.
    # 언제 몇 번 잰 숫자인지 모르면 읽는 사람이 검증할 수 있는 것이 없다.
    tally["backend"] = backend
    tally["runs"] = runs
    tally["questions"] = len(CASES)
    tally["measured_at"] = datetime.now().isoformat(timespec="seconds")

    # 다음 모델을 올릴 자리를 비운다. 로컬은 VRAM이 6GiB 뿐이라 겹치면 못 올린다.
    if backend == "huggingface":
        import gc

        import torch

        del llm, ours
        gc.collect()
        torch.cuda.empty_cache()
    return tally


def _load(backend: str | None = None) -> dict:
    """이미 잰 칸을 읽어 온다. 백엔드를 안 주면 전부 합친다."""
    backends = [backend] if backend else sorted({b for _, b, _ in LADDER})
    merged: dict = {}
    for name in backends:
        path = _result_path(name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                merged.update(json.load(fp))
    return merged


def _save(backend: str, data: dict) -> None:
    """칸 하나가 끝날 때마다 적는다."""
    with open(_result_path(backend), "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def _report(data: dict, runs: int) -> None:
    """측정 표를 그린다."""
    total = len(CASES) * runs
    print(f"\n질문 {len(CASES)}건 × {runs}회 = 칸마다 {total}건 판정\n")

    print(
        f"{_pad('모델', 15)}{_pad('나이브 시끄러움', 18)}{_pad('나이브 조용한 오답', 20)}"
        f"{_pad('구조 시끄러움', 16)}구조 조용한 오답"
    )
    print("-" * 88)
    for label, _backend, model_id in LADDER:
        cell = data.get(model_id)
        if not cell or model_id in EXCLUDED:
            continue
        n, o = cell["naive"], cell["ours"]
        print(
            f"{_pad(label, 15)}"
            f"{_pad(f'{n[LOUD]} / {total}', 18)}"
            f"{_pad(f'{n[QUIET]} / {total}', 20)}"
            f"{_pad(f'{o[LOUD]} / {total}', 16)}"
            f"{o[QUIET]} / {total}"
        )
    print("-" * 88)

    for label, _backend, model_id in LADDER:
        if model_id in EXCLUDED and model_id in data:
            print(f"\n제외: {label} — {EXCLUDED[model_id]}")
            n, o = data[model_id]["naive"], data[model_id]["ours"]
            print(f"  잰 값은 남아 있다. 나이브 {n[LOUD]}/{n[QUIET]}, 구조 {o[LOUD]}/{o[QUIET]}")

    print("\n조용한 오답이 난 질문 (구조 쪽)")
    for label, _backend, model_id in LADDER:
        cell = data.get(model_id)
        if not cell or model_id in EXCLUDED:
            continue
        quiet = cell.get("quiet_questions") or {}
        if not quiet:
            print(f"  {label}: 없음")
            continue
        for question, count in sorted(quiet.items(), key=lambda kv: -kv[1]):
            print(f"  {label}: {count}회  {question}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="모델 크기별 벤치")
    parser.add_argument("--runs", type=int, default=5, help="칸마다 반복 횟수")
    parser.add_argument("--report", action="store_true", help="재측정 없이 표만 다시 그린다")
    parser.add_argument("--only", help="이 백엔드만 잰다 (huggingface | nvidia)")
    # 두 백엔드를 동시에 돌릴 때 같은 파일을 함께 쓰지 않도록 나눈다.
    parser.add_argument("--db", default="bench.db", help="측정에 쓸 SQLite 경로")
    args = parser.parse_args()

    if args.report:
        _report(_load(), args.runs)
        return

    load_env_file()
    catalog = build_catalog()
    db = seed(args.db)
    executor = SqliteExecutor(db)
    today = _today_inside_the_data(db)

    # 정답 행은 모델과 무관하다. 한 번만 구해 모든 칸에서 돌려 쓴다.
    expected_rows = {c.question: reference_rows(c, catalog, executor) for c in CASES}

    print(bench_env.describe())
    print(f"기준일: {today}")

    for label, backend, model_id in LADDER:
        if args.only and backend != args.only:
            continue
        data = _load(backend)
        if model_id in data:
            print(f"\n[{label}] {model_id} — 이미 잰 칸, 건너뛴다")
            continue
        print(f"\n[{label}] {model_id}  ({backend})", flush=True)
        try:
            data[model_id] = _measure(
                backend, model_id, args.runs, catalog, executor, today, expected_rows
            )
        except Exception as e:  # noqa: BLE001 — 한 칸이 죽어도 나머지는 잰다
            print(f"    칸 실패: {type(e).__name__}: {e}")
            continue
        _save(backend, data)

    _report(_load(), args.runs)


if __name__ == "__main__":
    main()
