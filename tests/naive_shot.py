"""나이브 제품 화면 — 문서와 발표에 쓰는 자산을 만든다.

    python tests/naive_shot.py

에세이의 「그럴듯한 오답」 절이 인용하는 화면이 여기서 나온다.
구조 없는 NL2SQL 제품이 같은 질문에 무엇을 돌려주는지 보이는 것이 목적이다.

## 왜 벤치와 따로 있나

`bench_naive.py`는 SQL과 결과 행만 본다. 채점에 필요한 것이 그뿐이라서다.
실제 제품은 거기서 한 단계를 더 간다. 결과를 자연어로 풀어 주고 후속 조치까지 제안한다.
그 단계가 화면의 대부분을 차지하고, 그 절이 말하는 인지부채가 거기서 생긴다.

그래서 1단계는 벤치와 같은 함수를 부르고 2단계만 여기서 더한다.
프롬프트를 복사해 오면 둘이 조용히 갈라지고, 그러면 "같은 조건에서 쟀다"가 거짓이 된다.

## 일부러 약하게 만들지 않았다

`naive_answer`가 큐레이션 뷰와 컬럼 설명을 그대로 넘긴다.
조인 팬아웃도 소프트 삭제도 이미 없앤 상태에서 시작한다는 뜻이다.
망가진 스키마를 던져 주면 알아서 무너지고, 그것은 이 문서의 논점이 아니다.
"""

from __future__ import annotations

import html
import os
import re
import sys

# 스크립트로 직접 실행하므로 (tests/ 는 패키지가 아니다) 저장소 루트를 얹는다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bench_env  # noqa: E402,F401
from bench_naive import naive_answer  # noqa: E402
from test_layer3_routing import _today_inside_the_data  # noqa: E402

from domain import catalog as build_catalog  # noqa: E402
from domain.seed import seed  # noqa: E402
from nl2sql import SqliteExecutor, load_env_file, resolve_llm  # noqa: E402

DB_PATH = "inspection.db"
OUT_PATH = "docs/figures/naive-answer.html"

# 골든셋의 시맨틱 문항 하나를 그대로 쓴다.
# 여기만 다른 질문을 쓰면 「모델을 키우면 어떻게 되나」의 표와 대조가 안 된다.
QUESTION = "라인별로 규격에 아슬아슬한 건수 알려줘"

# 결과를 사람 말로 풀어 주는 단계.
# 컬럼 이름을 쓰지 말라고 시키는 것이 핵심이다.
# 실제 제품은 사용자에게 spec_slack 같은 것을 보여주지 않는다.
NARRATE_PROMPT = (
    "너는 사내 데이터 분석 도우미다. 사용자는 SQL을 모른다. "
    "질의 결과를 한국어로 친절하게 설명하고, 표로 정리하고, 도움이 될 만한 후속 조치를 제안한다. "
    "컬럼 이름 같은 기술 용어 대신 업무 용어를 쓴다. "
    "소제목, 표, 번호 매긴 목록을 갖춘 보고서 형식으로 충분히 자세히 쓴다. "
    'JSON으로 답한다: {"answer": "마크다운 본문"}'
)

NARRATE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _inline(text: str) -> str:
    """굵게·기울임·인라인 코드만 처리한다. 나머지는 그대로 이스케이프한다."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    # 굵게를 먼저 걷어냈으므로 남은 별표는 기울임뿐이다.
    # 밑줄 문법(`_기울임_`)은 안 받는다. 본문에 `line_code` 같은 이름이 섞여 들어온다.
    out = re.sub(r"\*(.+?)\*", r"<em>\1</em>", out)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", out)


def _table(rows: list[str]) -> str:
    """마크다운 표 한 덩어리를 HTML로 바꾼다. 구분선 행은 버린다."""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    body = [r for r in cells if not all(set(c) <= set("-: ") for c in r)]
    if not body:
        return ""
    head, rest = body[0], body[1:]
    parts = ["<table>", "<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>"]
    for row in rest:
        parts.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    parts.append("</table>")
    return "\n".join(parts)


def render(markdown: str) -> str:
    """모델이 쓰는 만큼의 마크다운만 HTML로 바꾼다.

    완전한 변환기가 아니다.
    모르는 문법을 만나면 문단으로 떨어뜨린다. 깨지더라도 눈에 보이는 쪽이 낫다.

    Args:
        markdown: 모델이 돌려준 본문.

    Returns:
        본문 HTML 조각. 문서 껍데기는 포함하지 않는다.
    """
    out: list[str] = []
    table: list[str] = []
    stack: list[str] = []  # 열려 있는 목록 태그

    def close_lists(depth: int = 0) -> None:
        while len(stack) > depth:
            out.append(f"</{stack.pop()}>")

    def flush_table() -> None:
        if table:
            out.append(_table(table))
            table.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            close_lists()
            table.append(line)
            continue
        flush_table()

        if not line.strip():
            continue
        if re.match(r"^-{3,}$|^\*{3,}$", line.strip()):
            close_lists()
            out.append("<hr>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if heading:
            close_lists()
            level = min(len(heading.group(1)) + 1, 4)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        indent = len(line) - len(line.lstrip())
        item = re.match(r"^\s*(?:([-*+])|(\d+)\.)\s+(.*)$", line)
        if item:
            tag = "ul" if item.group(1) else "ol"
            depth = 1 if indent < 2 else 2
            close_lists(depth)
            if len(stack) == depth and stack[-1] != tag:
                close_lists(depth - 1)
            while len(stack) < depth:
                out.append(f"<{tag}>")
                stack.append(tag)
            out.append(f"<li>{_inline(item.group(3))}</li>")
            continue

        close_lists()
        out.append(f"<p>{_inline(line.strip())}</p>")

    flush_table()
    close_lists()
    return "\n".join(out)


# 이 저장소의 UI와 일부러 다르게 만든다.
# 같은 껍데기를 쓰면 독자가 "우리 제품이 저런 답을 냈다"로 읽는다.
# 반대로 허름하게 만들어도 안 된다. 그러면 실패 원인이 완성도로 돌아가고 논점이 죽는다.
PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>{question}</title>
<style>
  :root {{
    --bg: #ffffff; --shell: #f7f8fc; --ink: #1c1f2e; --dim: #5a6178;
    --faint: #8c93a8; --rule: #e5e7f0; --accent: #5b4ee8; --accent-soft: #eeecff;
    --sans: -apple-system, "Apple SD Gothic Neo", "Noto Sans KR",
            "Malgun Gothic", "Segoe UI", sans-serif;
    --mono: ui-monospace, "Cascadia Mono", Consolas, "D2Coding", monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--shell); font: 15px/1.75 var(--sans); color: var(--ink); }}
  .app {{ width: 1080px; background: var(--bg); }}
  header {{
    display: flex; align-items: center; gap: 9px;
    padding: 13px 24px; border-bottom: 1px solid var(--rule);
  }}
  .logo {{
    width: 22px; height: 22px; border-radius: 7px;
    background: linear-gradient(135deg, #6d5ef0, #9d7bf5);
  }}
  .brand {{ font-weight: 650; font-size: 14.5px; letter-spacing: -.01em; }}
  .brand span {{ color: var(--faint); font-weight: 400; margin-left: 7px; font-size: 13px; }}
  .thread {{ padding: 22px 24px 8px; }}
  .ask {{ display: flex; justify-content: flex-end; margin-bottom: 26px; }}
  .ask div {{
    background: var(--accent); color: #fff; padding: 10px 16px;
    border-radius: 16px 16px 4px 16px; font-size: 14.5px; max-width: 70%;
  }}
  .reply {{ display: flex; gap: 12px; }}
  .avatar {{
    flex: 0 0 auto; width: 28px; height: 28px; border-radius: 50%;
    background: var(--accent-soft); color: var(--accent);
    display: grid; place-items: center; font-size: 14px; margin-top: 2px;
  }}
  .body {{ min-width: 0; flex: 1; }}
  .body h3 {{ font-size: 16.5px; margin: 0 0 14px; letter-spacing: -.01em; }}
  .body h4 {{ font-size: 14.5px; margin: 24px 0 10px; color: var(--dim); }}
  .body p {{ margin: 12px 0; }}
  .body ul, .body ol {{ margin: 10px 0; padding-left: 22px; }}
  .body li {{ margin: 5px 0; }}
  .body ol > li {{ margin: 14px 0; }}
  .body code {{ font-family: var(--mono); font-size: 13px; }}
  hr {{ border: none; border-top: 1px solid var(--rule); margin: 22px 0; }}
  table {{ border-collapse: collapse; margin: 12px 0 18px; font-size: 14.5px; }}
  th, td {{ padding: 8px 22px 8px 0; text-align: left; }}
  th {{ color: var(--dim); font-weight: 550; border-bottom: 1px solid var(--rule); }}
  td {{ border-bottom: 1px solid #f1f2f7; }}
  .sql {{
    margin: 20px 0 0; border: 1px solid var(--rule); border-radius: 9px;
    padding: 11px 15px; color: var(--dim); font-size: 13.5px;
    display: flex; align-items: center; gap: 8px; background: #fbfbfe;
  }}
  .sql .chev {{ color: var(--faint); font-size: 11px; }}
  .sql .mono {{
    font-family: var(--mono); font-size: 12.5px; color: var(--faint); margin-left: auto;
  }}
  form {{
    margin: 26px 24px 20px; border: 1px solid var(--rule); border-radius: 13px;
    padding: 12px 16px; color: var(--faint); font-size: 14.5px;
    display: flex; align-items: center; background: var(--bg);
  }}
  form .send {{
    margin-left: auto; width: 26px; height: 26px; border-radius: 50%;
    background: var(--accent-soft); color: var(--accent);
    display: grid; place-items: center; font-size: 13px;
  }}
</style></head>
<body>
<div class="app">
  <header>
    <div class="logo"></div>
    <div class="brand">AI 분석 어시스턴트<span>검사 데이터베이스 연결됨</span></div>
  </header>
  <div class="thread">
    <div class="ask"><div>{question}</div></div>
    <div class="reply">
      <div class="avatar">&#10022;</div>
      <div class="body">
{answer}
        <div class="sql"><span class="chev">&#9654;</span> 실행된 SQL 보기
          <span class="mono">{sql}</span></div>
      </div>
    </div>
  </div>
  <form>메시지를 입력하세요<div class="send">&#8593;</div></form>
</div>
</body></html>
"""


def main() -> None:
    load_env_file()
    seed(DB_PATH)
    catalog = build_catalog()
    executor = SqliteExecutor(DB_PATH)
    llm = resolve_llm()
    today = _today_inside_the_data(DB_PATH)

    sql, rows, error = naive_answer(llm, catalog, executor, QUESTION, today)
    print(f"질문: {QUESTION}")
    print(f"SQL: {sql}")
    print(f"행 {len(rows)}건" + (f" / {error}" if error else ""))
    for row in rows[:20]:
        print(f"  {row}")
    if error or not rows:
        print("행이 없어 화면을 만들지 않는다. 나이브 경로의 실패도 결과다.")
        return

    body = "질문: " + QUESTION + "\n\n행:\n" + "\n".join(str(r) for r in rows[:50])
    narration = str(llm.complete_json(NARRATE_PROMPT, body, NARRATE_SCHEMA).get("answer", ""))

    teaser = sql.split("\n")[0].strip()
    page = PAGE.format(
        question=html.escape(QUESTION),
        answer=render(narration),
        sql=html.escape(teaser[:60] + (" …" if len(teaser) > 60 else "")),
    )
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        handle.write(page)
    print(f"\n{OUT_PATH} 를 썼다. 화면 캡처는 1080px 폭으로 잡는다.")


if __name__ == "__main__":
    main()
