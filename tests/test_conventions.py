"""문서와 소스가 이 저장소의 규약을 지키는지 검사한다.

이 저장소의 주장은 문서에 있고, 문서가 틀리면 주장이 틀린다.
그런데 문서는 테스트가 없어서 코드보다 훨씬 조용히 낡는다.
링크가 죽고, 숫자가 밀리고, 표가 깨져도 아무 일도 일어나지 않는다.
그래서 기계로 확인할 수 있는 것만 여기서 잠근다.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "docs/data.md"]
SOURCE = ("*.py", "*.html", "*.sql", "*.sql.in")

# docstring의 항목 절. 이 아래로는 항목만 오고 설명 문단은 오지 않는다.
_SECTIONS = ("Args:", "Returns:", "Raises:", "Yields:", "Attributes:")
_DOCUMENTABLE = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

# 폭에 기대는 정렬을 금지한다.
#
# 한글과 괘선(├─)·화살표(→)는 폰트에 따라 차지하는 칸이 달라진다.
# 그래서 한글이 낀 열을 공백이나 괘선으로 맞추면 쓴 사람 화면에서만 맞는다.
# 한국어 열이 둘 이상 필요하면 마크다운 표를 쓴다. 렌더러가 폭을 알아서 맞춘다.
#
# 정렬이 안전한 경우는 열의 왼쪽이 순수 ASCII 일 때뿐이다 (사용법 블록, DDL 컬럼).
_MD_PADDING = re.compile(r"[가-힣][^\n]*?  +\S")
_SRC_PADDING = re.compile(r"[가-힣][^\n]*?   +\S")  # 2칸은 PEP8 주석 간격이라 뺀다
_HANGUL_RULE = re.compile(r"[가-힣].*?─{3,}")
_BOLD = re.compile(r"\*\*(?=\S)[^*\n]+?\*\*")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _source_files():
    for pattern in SOURCE:
        for path in ROOT.rglob(pattern):
            if "__pycache__" not in path.parts and path.name != "test_conventions.py":
                yield path


def _numbered(path: Path):
    return enumerate(path.read_text(encoding="utf-8").splitlines(), 1)


def _code_blocks(text: str):
    """(줄번호, 줄) 을 코드 펜스 안쪽만 돌려준다."""
    inside = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            inside = not inside
            continue
        if inside:
            yield number, line


def _bullets(text: str):
    """(시작 줄번호, 이어붙인 본문) 으로 불릿 하나를 돌려준다.

    불릿이 여러 줄에 걸치면 마지막 줄이 그 불릿의 끝이다.
    물리 줄만 보면 이어지는 줄을 끝으로 착각한다.
    """
    inside = False
    start = None
    parts: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            inside = not inside
            continue
        if inside:
            continue
        if re.match(r"^\s*[-*] ", line):
            if start:
                yield start, " ".join(parts)
            start, parts = number, [line.strip()]
        elif start:
            if line.strip() and not re.match(r"^[#|>]", line) and not line.startswith("```"):
                parts.append(line.strip())
            else:
                yield start, " ".join(parts)
                start, parts = None, []
    if start:
        yield start, " ".join(parts)


def _shell_blocks(text: str):
    """```bash 블록을 (시작 줄번호, 줄 목록) 으로 돌려준다."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].startswith("```") and lines[index][3:].strip() in ("bash", "sh"):
            end = index + 1
            while end < len(lines) and not lines[end].startswith("```"):
                end += 1
            yield index + 2, lines[index + 1 : end]
            index = end
        index += 1


def _commands(block: list[str]) -> list[str]:
    """셸 블록에서 주석을 걷어 낸 명령부만 돌려준다. 빈 줄은 뺀다."""
    out = []
    for line in block:
        if not line.strip():
            continue
        m = re.match(r"^(.*?)\s+# ", line)
        out.append((m.group(1) if m else line).rstrip())
    return out


@pytest.mark.parametrize("name", DOCS)
def test_shell_comments_align_two_spaces_after_the_longest_command(name: str):
    """사용법 블록의 주석은 그 블록에서 가장 긴 명령 뒤 두 칸에 선다.

    열을 눈대중으로 잡으면 블록마다 달라지고, 달라진 것은 아무도 못 고친다.
    기준을 하나로 정하면 계산으로 확인할 수 있다.
    왼쪽이 순수 ASCII 인 명령줄이라 폭이 고정이고, 그래서 이 정렬은 폰트에 안 흔들린다.
    """
    offenders = []
    for start, block in _shell_blocks(_read(name)):
        pairs = [(n, re.match(r"^(.*?)\s+# ", line)) for n, line in enumerate(block)]
        commented = [n for n, m in pairs if m]
        if not commented:
            continue
        # 주석 달린 줄이 아니라 블록의 모든 명령을 기준으로 삼는다.
        # 주석 없는 긴 명령 위에 주석이 얹히면 열이 블록 한가운데로 파고든다.
        column = max(len(cmd) for cmd in _commands(block)) + 2
        for n in commented:
            if block[n].index("# ") != column:
                offenders.append(f"{name}:{start + n}  # 은 {column}열에 서야 한다")
    assert not offenders, "사용법 주석 열이 어긋난다:\n" + "\n".join(offenders)


@pytest.mark.parametrize("name", DOCS)
def test_bullets_end_with_a_period(name: str):
    """불릿은 마침표로 끝낸다.

    한글 맞춤법 문장부호 규정은 서술문에도, 명사형으로 끝나는 문장에도
    마침표를 원칙으로 둔다. 불릿이 문장인 이상 끝나는 자리도 문장의 끝이다.
    한 항목이 두 문장이면 앞만 찍고 끝을 비우게 되는데,
    그 모양은 규칙이 아니라 누락으로 읽힌다.
    """
    offenders = [
        f"{name}:{n}  ...{text.rstrip()[-40:]}"
        for n, text in _bullets(_read(name))
        if not text.rstrip().endswith(".")
    ]
    assert not offenders, "불릿이 마침표로 끝나지 않는다:\n" + "\n".join(offenders)


@pytest.mark.parametrize("name", DOCS)
def test_em_dash_keeps_one_space_on_each_side(name: str):
    """줄표는 앞뒤로 공백 한 칸을 둔다. 붙여 쓰면 다른 문자로 보인다."""
    offenders = [
        f"{name}:{n}  {line.strip()}"
        for n, line in enumerate(_read(name).splitlines(), 1)
        if re.search(r"\S—|—\S", line)
    ]
    assert not offenders, "줄표 앞뒤 공백이 없다:\n" + "\n".join(offenders)


@pytest.mark.parametrize("name", DOCS)
def test_code_blocks_do_not_align_columns_after_hangul(name: str):
    """마크다운 코드블록에서 한글 뒤를 공백으로 정렬하지 않는다."""
    offenders = [
        f"{name}:{n}  {line}" for n, line in _code_blocks(_read(name)) if _MD_PADDING.search(line)
    ]
    assert not offenders, "한글 뒤 공백 정렬:\n" + "\n".join(offenders)


def test_source_does_not_align_columns_after_hangul():
    """소스에서도 한글 뒤를 공백이나 괘선으로 정렬하지 않는다.

    문자열 리터럴까지 본다.
    화면에 나가는 줄도 같은 이유로 어긋나고, 벤치 머리말은 그대로 문서에 들어간다.
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{n}  {line.strip()}"
        for path in _source_files()
        for n, line in _numbered(path)
        if _SRC_PADDING.search(line) or _HANGUL_RULE.search(line)
    ]
    assert not offenders, "한글 뒤 폭 의존 정렬:\n" + "\n".join(offenders)


def test_source_carries_no_markdown_emphasis():
    """소스에 `**강조**`를 쓰지 않는다.

    소스는 마크다운으로 렌더되지 않아 별표가 그대로 보인다.
    모델에게 나가는 프롬프트도 마찬가지로 쓰지 않는다. 강조가 필요하면 문장을 다시 쓴다.
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{n}  {line.strip()}"
        for path in _source_files()
        for n, line in _numbered(path)
        if _BOLD.search(line)
    ]
    assert not offenders, "소스의 마크다운 강조:\n" + "\n".join(offenders)


@pytest.mark.parametrize("name", DOCS)
def test_relative_links_resolve(name: str):
    """문서가 가리키는 파일이 실제로 있는가."""
    base = (ROOT / name).parent
    broken = [
        link
        for link in re.findall(r"\]\(([^)]+)\)", _read(name))
        if not link.startswith(("http", "#")) and not (base / link.split("#")[0]).exists()
    ]
    assert not broken, f"{name} 의 깨진 링크: {broken}"


def test_incident_count_in_docs_matches_the_test_suite():
    """문서에 적힌 사고 건수가 회귀 테스트의 실제 개수와 같은가.

    사고를 하나 추가하고 문서를 안 고치면 숫자가 조용히 낡는다.
    이 저장소가 반대하는 바로 그 모양이라, 숫자를 손으로 관리하지 않는다.
    """
    suite = (ROOT / "tests" / "test_regressions.py").read_text(encoding="utf-8")
    actual = sum(
        1 for m in re.finditer(r'def test_\w+\([^)]*\):\n    """(.+)', suite) if " — " in m.group(1)
    )
    for name in DOCS:
        for claimed in re.findall(r"사고(?: 로그)? \*{0,2}(\d+)건", _read(name)):
            assert int(claimed) == actual, (
                f"{name} 은 사고 {claimed}건이라 하는데 실제로는 {actual}건이다"
            )


def test_no_stale_section_numbers_in_code():
    """코드 주석은 절 번호가 아니라 제목으로 문서를 가리킨다.

    번호는 문서를 고칠 때마다 밀리고, 밀려도 아무 데서도 안 걸린다.
    제목은 바뀌면 눈에 띄고 grep으로 찾을 수 있다.
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{n}  {line.strip()}"
        for path in _source_files()
        for n, line in _numbered(path)
        if re.search(r"\d+절", line)
    ]
    assert not offenders, "코드가 절 번호를 가리킨다:\n" + "\n".join(offenders)


def test_hangul_width_helper_is_correct():
    """벤치가 쓰는 폭 계산이 실제 표시 폭과 맞는가.

    이 함수가 틀리면 측정 결과 표가 어긋나고, 어긋난 표는 읽히지 않는다.
    """
    from bench_routing import _pad, _width

    assert _width("시맨틱") == 6
    assert _width("free") == 4
    assert _width("값 열거") == 7  # 한글 3자(6) + 공백 1
    assert _width(_pad("시맨틱", 10)) == 10
    assert _width(_pad("free", 10)) == 10
    # 폭이 애매한 문자는 정렬에 쓰지 않는다는 전제를 밝혀 둔다.
    assert unicodedata.east_asian_width("→") == "A"


def test_first_sentence_of_a_definition_is_a_noun_phrase():
    """정의 설명문의 첫 문장이 해석 가정 문장에 그대로 끼워진다.

    _headline이 첫 문장만 뽑아 "… 를 구했습니다" 안에 넣으므로 그 자리는 명사구여야 한다.
    문서를 다듬으려고 문장 순서를 바꾸면 "로트 단위가 아니다를 구했습니다" 가 화면에 나간다.
    절단이 위치에 기대는 대가라, 대가를 치르는 자리를 여기서 잠근다.
    """
    from domain import catalog as build_catalog
    from nl2sql.pipeline import _headline

    catalog = build_catalog()
    offenders = [
        f"{name}  {_headline(definition.description)!r}"
        for group in (catalog.metrics, catalog.dimensions)
        for name, definition in group.items()
        if _headline(definition.description).endswith(("다", "요", "음", "까"))
    ]
    assert not offenders, "첫 문장이 명사구가 아니다:\n" + "\n".join(offenders)


def test_docstring_prose_stays_above_the_sections():
    """설명 문단이 Args 아래로 내려가지 않았는가.

    Args와 Returns 사이에 문단이 끼면 한쪽은 반드시 안 읽힌다.
    항목을 훑는 사람은 문단을 건너뛰고, 문단을 읽는 사람은 항목에서 멈춘다.
    눈으로는 잘 안 보이는 어긋남이라 기계로 잠근다.
    """
    offenders = []
    for path in _source_files():
        if path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, _DOCUMENTABLE):
                continue
            doc = ast.get_docstring(node) or ""
            opened = False
            for line in doc.split("\n"):
                if line in _SECTIONS:
                    opened = True
                elif opened and line.strip() and not line.startswith("    "):
                    name = getattr(node, "name", path.stem)
                    offenders.append(f"{path.relative_to(ROOT)}:{name}  {line.strip()[:40]}")
                    break
    assert not offenders, "설명 문단이 Args 아래로 내려갔다:\n" + "\n".join(offenders)
