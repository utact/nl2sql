"""챗봇 웹 서버.

이 파일은 조립만 한다.
도메인은 `domain`에서, 처리 논리는 `nl2sql`에서 가져와 둘을 묶어 HTTP로 노출한다.

기본 구성은 SQLite와 로컬 오픈소스 모델이다.
실무에서 NL2SQL이 붙는 데이터는 대개 밖으로 못 내보내므로 API 구성이 기본값이면 안 된다.
오프라인 모드가 기본으로 켜져 있어 외부 백엔드는 선택 자체가 차단된다.
PostgreSQL이나 외부 API로 바꾸는 것은 환경변수 한 줄이지만, 그것은 명시적 선택이어야 한다.

응답 규약이 이 저장소의 주장을 그대로 담고 있다.

- 실패는 1급 출력이다. 되묻기·거절·가드 위반·실행 오류가 전부 HTTP 200 + `status`로 나온다.
- 해석 가정이 항상 함께 나간다. 사용자가 검증할 수 있는 유일한 지점이다.
- 잘림을 숨기지 않는다. 상한에 걸린 결과를 전체로 믿지 않도록 `truncated`로 표면화한다.
"""

from __future__ import annotations

import dataclasses
import decimal
import logging
import os
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from domain import catalog as build_catalog
from nl2sql import NL2SQL, PostgresExecutor, SqliteExecutor, load_env_file, resolve_llm

load_env_file()

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_logger = logging.getLogger("nl2sql")

_API_DESCRIPTION = """
자연어 질문을 SQL로 바꿔 실행하는 NL2SQL 서비스.

반복되는 질문은 시맨틱 레이어가 결정론적으로 컴파일하고,
희귀한 질문만 큐레이션 뷰 위 직접 생성 + 가드레일으로 처리한다.
답할 수 없으면 거절하고, 모호하면 되묻는다.
"""

# 프론트 초기화 메타데이터.
# 도메인 정의가 코드에 박히는 유일한 자리이므로 정의와 어긋나지 않게 주의한다.
_TITLE = "NL2SQL — 계측·규격 검사"

# 예시 질문은 커버리지 안내이지 자랑이 아니다.
# 잘 되는 질문만 놓으면 되묻기·거절이 1급 경로라는 주장이 화면에서 사라진다.
# 그래서 네 갈래가 각각 한 번씩 나오도록 고른다 — 누르는 순서가 곧 설명이 된다.
# 경로를 버튼에 적지는 않는다. 어디로 갈지는 라우터가 정하고, 간 경로는 배지가 말한다.
_EXAMPLE_QUESTIONS = [
    "월별 검사 건수 추이 보여줘",  # semantic
    "라인별로 규격 빠듯한 거 몇 건이야?",  # semantic — 시맨틱 레이어 밖 표현이 지표로 붙는 자리
    "항목별 측정값 산포 어떤지 봐줘",  # free — 정의에 없는 통계라 모델이 SQL을 쓴다
    "요즘 문제 있는 라인 알려줘",  # clarify — '문제'가 정의되지 않았다
    "작업자별 불합격 건수 알려줘",  # abstain — 로트에는 있지만 뷰에 안 올린 축이다
]
# 화면의 "라인"이 두 뜻으로 읽힌다.
# 이 라벨은 접속한 사람의 소속이고, 답의 "생산 라인으로 나눠서 봤습니다"는 묶는 축이다.
# 소속을 안 고르고도 라인별 답이 나오면 "없음인데 왜 나오지"가 된다.
_CONTEXT_LABEL = "소속 라인"
# 값을 못 수확했을 때만 쓰는 폴백.
#
# 평소에는 DB에서 관측한 값(Catalog.known_values)이 이 목록이 된다.
# 여기 박아 둔 값이 실제 라인과 어긋나면 고를 수 없는 소속이 화면에 남고,
# 새로 생긴 라인은 화면에서 사라진다. 정의 한 벌로 새 도메인에 붙는다는 말과도 어긋난다.
#
# 그래도 폴백은 필요하다. 수확이 비는 경우가 실제로 있다.
# harvest_values=False로 껐거나, 행 단위 권한 때문에 기동 시점에 0행만 보일 때다
# (nl2sql/introspect.py).
# 그때 목록을 비워 두면 소속을 아예 못 고르고, 컨텍스트 없이 묻는 것과 같아진다.
_CONTEXT_FALLBACK = ["L1", "L2", "L3"]

# compose로 띄우는 데모 DB. 운영 자격증명을 코드에 두지 않는다.
DEFAULT_PG_DSN = "postgresql://nl2sql_app:demo-only-not-a-secret@127.0.0.1:5432/inspection"


class AskRequest(BaseModel):
    """질의 요청."""

    question: str = Field(description="사용자의 자연어 질문", examples=["월별 검사 건수 추이"])
    tenant_id: str | None = Field(
        default=None,
        description="자동 주입 컨텍스트 값. 정의의 자동 주입 차원에 필터로 붙는다",
        examples=["L1"],
    )
    resolution: dict | None = Field(
        default=None,
        description=(
            "직전 되묻기에서 고른 선택지의 semantic query. "
            "주면 라우팅을 건너뛰고 결정론적으로 실행한다 (모델 호출 0회). "
            "신뢰 경계 바깥이므로 정의는 컴파일러가 다시 검증한다"
        ),
    )


class MetaContext(BaseModel):
    """자동 주입 컨텍스트의 프론트 표시 정보."""

    dimension: str = Field(description="정의의 자동 주입 차원 이름")
    label: str = Field(description="화면에 보여줄 이름")
    options: list[str] = Field(description="선택 가능한 값 목록")


class MetaResponse(BaseModel):
    """프론트 초기화용 메타데이터."""

    title: str = Field(description="UI 헤더 제목")
    example_questions: list[str] = Field(description="힌트 버튼에 보여줄 예시 질문")
    context: MetaContext | None = Field(
        default=None, description="자동 주입 컨텍스트 (없으면 null)"
    )
    backend: str = Field(
        default="",
        description=(
            "지금 붙어 있는 모델 이름. 고정 응답으로 띄웠으면 그렇다고 적는다. "
            "같은 UI가 실제 모델로도 스텁으로도 뜨므로 화면이 구별할 수 있어야 한다."
        ),
    )
    live: bool = Field(
        default=True,
        description="실제 모델이 붙어 있는가. False 면 화면이 그 사실을 표시한다.",
    )
    vocabulary: list[str] = Field(
        default_factory=list,
        description=(
            "정의의 이름들 (지표·차원·뷰 컬럼·정본 값). "
            "화면이 '기계가 고른 말'을 사람이 쓴 말과 구분해 표시하는 데 쓴다. "
            "시맨틱 경로의 가정은 컴파일러가 따옴표로 감싸 주지만 직접 생성 경로의 "
            "가정은 모델이 쓴 문장이라 그 표시가 없다 — 그래서 화면이 직접 대조한다."
        ),
    )


class AnswerResponse(BaseModel):
    """질의 처리 결과."""

    status: str = Field(
        description=(
            "처리 상태: ok(성공) | clarification_needed(되묻기) | "
            "abstained(답변 불가) | rejected(가드 거부) | error(실행 오류)"
        )
    )
    route: str | None = Field(
        default=None,
        description="라우팅 결과: semantic | free | clarify | abstain",
    )
    message: str = Field(default="", description="사용자에게 보여줄 안내나 사유")
    sql: str | None = Field(default=None, description="실행된(또는 거부된) SQL")
    params: list[str | float] = Field(
        default_factory=list,
        description="바인딩 값. 숫자 차원은 숫자로 바인딩된다",
    )
    columns: list[str] = Field(default_factory=list, description="결과 컬럼 이름")
    rows: list[list] = Field(default_factory=list, description="결과 행 (행수 상한 적용)")
    assumptions: list[str] = Field(
        default_factory=list,
        description="시스템이 세운 해석 가정. 사용자가 검증할 수 있는 유일한 지점이다",
    )
    truncated: bool = Field(default=False, description="결과가 행수 상한에서 잘렸는지")
    guard_violations: list[str] = Field(
        default_factory=list, description="가드레일 위반 사유 (status=rejected 일 때)"
    )
    elapsed_ms: int = Field(default=0, description="라우팅부터 실행까지 소요 시간 (ms)")
    options: list[dict] = Field(
        default_factory=list,
        description=(
            "되묻기의 선택지 [{label, query}]. 고른 query를 다음 요청의 "
            "resolution으로 되돌려 보내면 모델 없이 실행된다"
        ),
    )
    drill: dict | None = Field(
        default=None,
        description=(
            "집계 한 줄에 해당하는 개별 측정을 열 수 있으면 {label, by, query}. "
            "화면이 by 순서대로 누른 줄의 값을 필터로 붙여 resolution으로 되돌려 보낸다. "
            "모델 호출 없이 컴파일러가 만든다"
        ),
    )


class AuditEntry(BaseModel):
    """감사 로그 항목.

    질문 원문과 필터 값이 그대로 남는다.
    운영에서는 이 로그 자체가 민감 자산이라 접근 통제와 보존 기간을 정해야 한다.
    """

    at: str = Field(description="처리 시각 (ISO 8601)")
    question: str = Field(description="사용자 질문 원문")
    route: str | None = Field(default=None, description="라우팅 결과")
    status: str = Field(description="처리 상태")
    sql: str | None = Field(default=None, description="생성된 SQL. ok 일 때만 실행된 것이다")
    rows: int = Field(description="반환 행수")
    elapsed_ms: int = Field(default=0, description="처리 소요 시간 (ms)")
    message: str = Field(default="", description="사용자에게 나간 안내나 사유")
    model_reason: str = Field(
        default="",
        description=(
            "모델이 쓴 사유 원문. 내부 용어가 섞이면 화면에는 안 나가지만 "
            "왜 그렇게 판단했는지 되짚으려면 원문이 남아 있어야 한다"
        ),
    )


def _cell(value):
    """DB 셀 값을 JSON으로 직렬화할 수 있는 형태로 정규화한다."""
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    return str(value)


def _build_default_pipeline() -> NL2SQL:
    """환경변수를 읽어 파이프라인을 조립한다.

    모델은 인터페이스 하나 뒤의 변수다.
    `resolve_llm`이 환경변수를 보고 백엔드를 고르므로 여기에는 모델 이름이 없다.

    Returns:
        조립된 NL2SQL 파이프라인.
    """
    backend = os.environ.get("NL2SQL_DB_BACKEND", "sqlite")
    if backend == "postgres":
        executor = PostgresExecutor(os.environ.get("NL2SQL_PG_DSN", DEFAULT_PG_DSN))
    elif backend == "sqlite":
        executor = SqliteExecutor(os.environ.get("NL2SQL_DB", "inspection.db"))
    else:
        raise ValueError(f"알 수 없는 DB 백엔드: {backend!r} (sqlite | postgres)")

    # 라우팅은 분류에 가까우므로 더 싼 모델로 분리할 수 있다.
    router_model = os.environ.get("NL2SQL_ROUTER_MODEL")

    pipeline = NL2SQL(
        catalog=build_catalog(),
        llm=resolve_llm(),
        executor=executor,
        harvest_values=os.environ.get("NL2SQL_HARVEST", "1") != "0",
        router_llm=resolve_llm(model=router_model) if router_model else None,
    )
    # 기동을 막을 정도는 아니지만 점검이 필요한 정의와 실스키마의 불일치.
    # 어긋난 지표를 조용히 쓰는 것만 아니면 된다.
    for warning in pipeline.catalog_warnings:
        _logger.warning("정의 경고: %s", warning)
    return pipeline


def _context_options(catalog) -> list[str]:
    """소속 드롭다운에 올릴 값 목록.

    DB에서 관측한 값이 있으면 그것을 쓴다. 도메인 상수를 서버에 박아 두지 않기 위해서다.
    관측이 비는 경우가 실제로 있어서(수확 끔, 행 단위 권한으로 기동 시점 0행)
    그때만 폴백으로 내려간다 — 목록이 비면 소속을 못 고르고 컨텍스트 없이 묻는 것과 같아진다.

    Args:
        catalog: 파이프라인이 들고 있는 카탈로그 (기동 시 수확 결과가 반영된 것).

    Returns:
        드롭다운 값 목록. 테넌트 차원이 없으면 빈 목록.
    """
    tenant = catalog.tenant_dimension
    if not tenant:
        return []
    return list(catalog.known_values.get(tenant) or _CONTEXT_FALLBACK)


def backend_label(llm) -> tuple[str, bool]:
    """화면에 적을 모델 이름과, 그것이 실제 모델인가.

    같은 UI가 실제 백엔드로도 고정 응답으로도 뜬다.
    고정 응답은 질문을 바꿔도 같은 답을 돌려주는데 화면은 똑같이 생겼다.
    적어 두지 않으면 데모를 실제 응답으로 읽게 된다.

    Args:
        llm: 파이프라인이 쓰는 LLM 백엔드.

    Returns:
        (표시할 이름, 실제 모델인가). 모델 이름이 없으면 고정 응답으로 본다.
    """
    model = getattr(llm, "model", None) or getattr(llm, "model_id", None)
    if model:
        return str(model), True
    # 한글을 섞지 않는다. 모노스페이스 폰트가 한글을 대체 폰트로 그려 굵기가 달라 보인다.
    # 옆칸의 모델 이름과 같은 자리·같은 형식이어야 하므로 클래스 이름만 낸다.
    # "실제 모델이 아니다"는 잠긴 입력창과 툴팁이 말한다.
    return type(llm).__name__, False


def create_app(pipeline: NL2SQL | None = None, questions: list[str] | None = None) -> FastAPI:
    """FastAPI 앱을 만든다.

    Args:
        pipeline: 주입할 파이프라인. None 이면 첫 요청 때 환경변수를 보고 지연 조립한다.
            로컬 모델 적재가 수십 초 걸릴 수 있기 때문이다.
        questions: 화면에 띄울 예시 질문. None 이면 기본 목록을 쓴다.
            고정 응답으로 띄울 때 그 응답이 답이 되는 질문만 남기는 자리다.
            화면이 입력을 잠그므로 여기 없는 질문은 물어볼 수 없다.

    Returns:
        구성된 FastAPI 애플리케이션.
    """
    application = FastAPI(
        title="NL2SQL Chat API",
        description=_API_DESCRIPTION,
        version="0.1.0",
        docs_url="/docs",
        openapi_tags=[
            {"name": "query", "description": "자연어 질의"},
            {"name": "observability", "description": "런타임 관측 — 감사 로그"},
            {"name": "ui", "description": "채팅 프론트"},
        ],
    )
    state = {"pipeline": pipeline}
    # 단일 DB 연결과 파이프라인 접근을 직렬화한다.
    lock = threading.Lock()

    def get_pipeline() -> NL2SQL:
        if state["pipeline"] is None:
            state["pipeline"] = _build_default_pipeline()
        return state["pipeline"]

    @application.post(
        "/api/ask",
        response_model=AnswerResponse,
        tags=["query"],
        summary="자연어 질문 처리",
        description=(
            "질문을 네 갈래로 라우팅하고 필요한 가드를 거쳐 읽기전용으로 실행한다. "
            "실패 사유도 HTTP 200 + status 필드로 표면화된다."
        ),
    )
    def ask(request: AskRequest) -> AnswerResponse:
        with lock:
            pipeline = get_pipeline()
            user_context = None
            if request.tenant_id:
                key = pipeline.catalog.tenant_dimension
                if key:
                    user_context = {key: request.tenant_id}
            if request.resolution is not None:
                # 되묻기의 답은 라우팅하지 않는다.
                # 질문을 다시 해석하면 첫 질문이 이미 확정한 축이 사라진다.
                answer = pipeline.ask_resolved(request.resolution, user_context=user_context)
            else:
                answer = pipeline.ask(request.question, user_context=user_context)
        payload = dataclasses.asdict(answer)
        payload["rows"] = [[_cell(v) for v in r] for r in payload["rows"]]
        return AnswerResponse(**payload)

    @application.get(
        "/api/meta",
        response_model=MetaResponse,
        tags=["ui"],
        summary="프론트 초기화 메타데이터",
        description=(
            "제목, 예시 질문, 자동 주입 컨텍스트, 백엔드 이름을 돌려준다. "
            "백엔드 이름을 읽으려면 파이프라인이 필요하므로 아직 없으면 여기서 조립된다 "
            "(로컬 모델이면 첫 호출이 모델 적재만큼 걸린다)."
        ),
    )
    def meta() -> MetaResponse:
        # 파이프라인을 적재한다. 화면이 어느 백엔드와 말하는지가 이 응답에 실리기 때문이다.
        # 고정 응답 데모를 실제 응답으로 읽은 사고가 있었다 (tests/test_regressions.py).
        # 조립을 미루려면 백엔드 이름을 빼야 하는데, 그러면 그 사고가 돌아온다.
        pipeline = get_pipeline()
        backend, live = backend_label(pipeline.llm)
        # 카탈로그는 파이프라인이 들고 있는 것을 쓴다.
        # build_catalog() 로 새로 만들면 기동 시 수확한 실제 값도, 꺼 둔 항목도 없는 사본이 온다.
        catalog = pipeline.catalog
        names = set(catalog.metrics) | set(catalog.dimensions)
        for view in catalog.views.values():
            names.update(view.columns)
        for mapping in catalog.value_mappings.values():
            names.update(mapping.values())
        return MetaResponse(
            title=_TITLE,
            example_questions=list(questions or _EXAMPLE_QUESTIONS),
            context=MetaContext(
                dimension=catalog.tenant_dimension or "",
                label=_CONTEXT_LABEL,
                options=_context_options(catalog),
            ),
            backend=backend,
            live=live,
            vocabulary=sorted(names),
        )

    @application.get(
        "/api/audit",
        response_model=list[AuditEntry],
        tags=["observability"],
        summary="감사 로그 조회",
        description="이 프로세스가 처리한 모든 질의의 감사 로그를 돌려준다.",
    )
    def audit() -> list[AuditEntry]:
        return [AuditEntry(**entry) for entry in get_pipeline().audit_log]

    @application.get("/", tags=["ui"], summary="채팅 UI", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    application.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return application


app = create_app()
