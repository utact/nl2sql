"""NL2SQL 코어 — 도메인을 모르는 부분.

이 패키지 어디에도 도메인 정의가 없다.
도메인은 전부 `domain` 패키지의 정의(Catalog) 하나에 들어 있다.
여기 있는 모듈들은 그 정의를 매개로만 동작한다.
새 도메인 적용이 코드 수정이 아니라 정의 작성인 이유가 이것이다.

모듈 배치는 문서의 가드레일 층 구분을 그대로 따른다.

- `catalog`    정의의 자료구조
- `router`     네 갈래 라우팅 (머리 / 거절 / 되묻기 / 직접 생성)
- `semantic`   결정론적 컴파일 + 정의 재검증
- `generator`  직접 생성 (꼬리 경로)
- `ast_guard`  가드레일 2층 — AST 정적 검증
- `cost_guard` 가드레일 3층 — 비용 추정
- `execution`  가드레일 4층 — 읽기전용 실행 격리
- `introspect` 정의 ↔ 실스키마 대조, 값 수확
- `llm`        모델을 인터페이스 하나 뒤의 변수로 두는 어댑터
- `pipeline`   위를 조립하고 감사 로그를 남긴다
"""

from .ast_guard import AstGuard, ValidationResult
from .catalog import (
    Catalog,
    CuratedView,
    Dimension,
    Metric,
    load_catalog,
    save_catalog,
)
from .cost_guard import CostReport, PostgresCostGuard, SqliteCostGuard, make_cost_guard
from .env import load_env_file
from .execution import (
    ExecutionResult,
    PostgresExecutor,
    QueryTimeoutError,
    SqliteExecutor,
)
from .generator import FreeGenerator, Generation
from .introspect import (
    CatalogValidationError,
    ValidationReport,
    harvest_dimension_values,
    inspect_schema,
    validate_catalog,
)
from .llm import (
    LLM,
    HuggingFaceLLM,
    NvidiaLLM,
    StubLLM,
    offline_mode,
    resolve_llm,
)
from .pipeline import NL2SQL, Answer
from .router import ClarifyCandidate, RouteDecision, Router
from .semantic import (
    Filter,
    SemanticError,
    SemanticQuery,
    compile_semantic,
    parse_semantic_query,
    semantic_query_to_dict,
)

__all__ = [
    # 조립
    "NL2SQL",
    "Answer",
    # 정의
    "Catalog",
    "CuratedView",
    "Dimension",
    "Metric",
    "load_catalog",
    "save_catalog",
    # 라우팅과 생성
    "Router",
    "RouteDecision",
    "ClarifyCandidate",
    "SemanticQuery",
    "Filter",
    "SemanticError",
    "compile_semantic",
    "parse_semantic_query",
    "semantic_query_to_dict",
    "FreeGenerator",
    "Generation",
    # 가드레일
    "AstGuard",
    "ValidationResult",
    "CostReport",
    "SqliteCostGuard",
    "PostgresCostGuard",
    "make_cost_guard",
    "SqliteExecutor",
    "PostgresExecutor",
    "ExecutionResult",
    "QueryTimeoutError",
    # 드리프트와 값 수확
    "CatalogValidationError",
    "ValidationReport",
    "validate_catalog",
    "inspect_schema",
    "harvest_dimension_values",
    # 모델과 설정
    "LLM",
    "HuggingFaceLLM",
    "NvidiaLLM",
    "StubLLM",
    "resolve_llm",
    "offline_mode",
    "load_env_file",
]
