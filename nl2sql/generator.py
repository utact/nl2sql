"""꼬리 경로의 직접 생성기.

큐레이션 뷰 위에서만 SQL을 생성하게 한다. 물리 스키마는 프롬프트에 노출하지 않는다.
생성 결과에는 해석 가정이 반드시 붙는다 — 이 경로에서 해석 오류를 잡을 유일한 수단이다.

가드레일에서 가장 약한 층이라 출력을 그대로 믿지 않는다.
ast_guard → cost_guard → 읽기전용 실행기를 반드시 통과한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog
from .llm import LLM

GENERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sql", "assumptions"],
    "additionalProperties": False,
}

_DIALECT_NAMES = {"sqlite": "SQLite", "postgres": "PostgreSQL"}

SYSTEM_PROMPT = """\
You write a single {dialect} SELECT statement answering the user's question.
Use ONLY the curated views listed below.

- One statement, SELECT only. Never write to or alter anything.
- Use only listed views and their columns. Do not invent tables or columns.
- Resolve relative dates using the current date provided.
- If user context is non-empty, filter by every dimension = value pair in it.

`assumptions` is shown to the user verbatim, above the result.
It is the ONLY thing between them and a confident wrong answer, so it is never empty.

EVERY line of `assumptions` is written in Korean, whatever language these instructions use.
A Korean reader must be able to read every line without translating it.
Every line ends in the polite declarative form "-습니다".
The rest of the product speaks that way, and a change of register reads as a different voice.

The FIRST line states in one sentence what data you actually answered with.
Name the view, and say what one row of your result means.
Then list every interpretive choice you made.
That means ambiguous business terms, date ranges, value spellings,
and anything the question asked for that the data does not have.

If the question asks about something these views do not contain, say so in that first line.
Answering "설비 점검 이력" with rows of measurements is wrong even when the SQL is valid.
Never let a substitution pass silently.
"""


@dataclass
class Generation:
    sql: str
    assumptions: list[str]


class FreeGenerator:
    def __init__(self, llm: LLM, catalog: Catalog, dialect: str = "sqlite"):
        self._llm = llm
        self._catalog = catalog
        self._system_prompt = SYSTEM_PROMPT.format(
            dialect=_DIALECT_NAMES.get(dialect, dialect)
        )

    def generate(self, question: str, today: str, user_context: dict | None = None) -> Generation:
        """질문에 대한 후보 SQL과 해석 가정을 생성한다.

        Args:
            question: 자연어 질문 (꼬리로 라우팅된 것).
            today: 상대 날짜("이번 달") 해석 기준일 (ISO 문자열).
            user_context: 신뢰된 자동 주입 컨텍스트.

        Returns:
            Generation(sql, assumptions).
            이 출력은 신뢰하지 않으며 반드시 가드레일(AST → 비용 → 실행 격리)을 통과시킨다.
        """
        user_prompt = (
            f"Current date: {today}\n"
            f"User context (trusted, auto-injected): {user_context or {}}\n\n"
            f"# Curated views\n{self._catalog.describe_views()}\n\n"
            f"{self._catalog.describe_value_dictionary()}\n\n"
            f"# Question\n{question}"
        )
        raw = self._llm.complete_json(self._system_prompt, user_prompt, GENERATION_SCHEMA)
        return Generation(sql=raw["sql"], assumptions=list(raw.get("assumptions", [])))
