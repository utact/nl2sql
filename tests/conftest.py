"""공용 픽스처.

데이터는 세션 단위로 한 번만 만든다.
시드가 고정 난수를 쓰므로 매 실행에서 같은 DB가 나온다.
그래서 결과 비교 테스트가 성립한다.
"""

from __future__ import annotations

import pytest

from domain import catalog as build_catalog
from domain.seed import seed
from nl2sql import NL2SQL, AstGuard, SqliteExecutor, StubLLM


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> str:
    """합성 데이터가 든 SQLite 파일 경로."""
    path = tmp_path_factory.mktemp("data") / "inspection.db"
    return seed(str(path))


@pytest.fixture
def catalog():
    """고쳐진 상태의 정의."""
    return build_catalog()


@pytest.fixture
def executor(db_path):
    ex = SqliteExecutor(db_path)
    yield ex
    ex.close()


@pytest.fixture
def guard(catalog, executor):
    return AstGuard(catalog, max_limit=executor.max_rows, dialect="sqlite")


@pytest.fixture
def make_pipeline(catalog, db_path):
    """정해진 라우팅을 돌려주는 파이프라인을 만드는 팩토리.

    라우팅을 고정하는 것은 모델을 시험하지 않기 위해서다.
    여기서 검증하는 것은, 모델이 무엇을 내놓든 하네스가 어떻게 처리하는가다.
    """

    def _make(route: dict, catalog_override=None) -> NL2SQL:
        return NL2SQL(
            catalog=catalog_override or catalog,
            llm=StubLLM(lambda system, user: route),
            executor=SqliteExecutor(db_path),
        )

    return _make


def semantic_query(metrics, dimensions=(), filters=(), order=None) -> dict:
    """시맨틱 질의 dict 하나를 만든다."""
    return {
        "metrics": list(metrics),
        "dimensions": list(dimensions),
        "filters": [{"dimension": d, "op": o, "value": v} for d, o, v in filters],
        "order": order,
    }


def semantic_route(metrics, dimensions=(), filters=(), order=None) -> dict:
    """라우터가 돌려줄 시맨틱 질의를 만든다."""
    return {
        "route": "semantic",
        "reason": "test",
        "semantic_query": semantic_query(metrics, dimensions, filters, order),
        "clarification": None,
        "clarify_candidates": None,
    }


def clarify_route(question: str, candidates) -> dict:
    """라우터가 돌려줄 되묻기를 만든다.

    Args:
        question: 되물을 한 문장.
        candidates: (라벨, semantic_query dict) 쌍의 목록.
    """
    return {
        "route": "clarify",
        "reason": "test",
        "semantic_query": None,
        "clarification": question,
        "clarify_candidates": [
            {"label": label, "semantic_query": query} for label, query in candidates
        ],
    }


class ExplodingLLM:
    """호출되는 즉시 터지는 백엔드.

    호스팅 모델 EOL, 키 만료, 네트워크 단절을 한 줄로 대신한다.
    """

    def __init__(self, message: str = "410 Gone"):
        self._message = message

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        raise RuntimeError(self._message)
