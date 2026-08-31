"""비용 가드 (가드레일 3층).

AST는 "1억 행 풀스캔"을 잡지 못한다. 실행 전 비용은 EXPLAIN 추정으로만 알 수 있다.

- SqliteCostGuard: EXPLAIN QUERY PLAN에서 인덱스 없는 SCAN을 찾아
  대상 테이블 행수를 센다. 같은 레벨의 SCAN은 곱하고 레벨끼리는 더한다 (아래 참고).
- PostgresCostGuard: EXPLAIN (FORMAT JSON) 으로 Seq Scan 추정 행수를 합산하고,
  계획 트리에서 가장 넓은 중간 산출 행수를 함께 본다. 플래너 통계 기반이라 즉시 추정된다.

조인은 더하기가 아니라 곱하기다.
스캔 행수를 전부 합산하면 카테시안 곱이 덧셈으로 잡혀 `1440+1440+1440`이 된다.
임계치를 한참 밑돌아 통과하고, 실제로 막는 것은 그 뒤의 타임아웃뿐이다.
그러면 이 층은 이름만 비용 가드이고 비용을 안 보는 것이 된다.

그래서 "같은 레벨"이라는 단위가 필요하다.
뷰 하나를 읽는 평범한 질의도 SCAN이 여러 줄 나온다.
뷰가 4중 조인 위에 있어 서브쿼리·코루틴마다 한 줄씩 붙기 때문이다.
그것들까지 곱하면 아무 질의도 못 지나간다.
반면 조인 조건 없는 다중 스캔은 실행 계획에서 같은 부모 아래 형제로 나란히 선다.
그 자리에서만 곱하고, 레벨끼리는 더한다.

추정은 추정일 뿐이라(통계가 낡으면 크게 틀린다) 최종 방어는 executor의 타임아웃이다.
시맨틱 경로는 실행 계획을 설계 시점에 알기 때문에 이 가드를 건너뛴다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class CostReport:
    ok: bool
    estimated_scanned_rows: int
    full_scan_tables: list[str] = field(default_factory=list)
    message: str = ""


class SqliteCostGuard:
    def __init__(self, conn: sqlite3.Connection, max_scanned_rows: int = 1_000_000):
        self._conn = conn
        self._max = max_scanned_rows
        self._counts: dict[str, int] | None = None  # 실제 테이블 행수 캐시 (지연 로드)

    def _load_counts(self) -> dict[str, int]:
        # 운영에서는 COUNT(*) 대신 통계 테이블(pg_class, information_schema 등)을 쓴다.
        if self._counts is None:
            self._counts = {}
            tables = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for (name,) in tables:
                self._counts[name] = self._conn.execute(
                    f'SELECT COUNT(*) FROM "{name}"'
                ).fetchone()[0]
        return self._counts

    def _table_rows(self, name: str) -> int:
        counts = self._load_counts()
        if name in counts:
            return counts[name]
        # EXPLAIN이 별칭("SCAN o")을 보고할 수 있다 — 가장 큰 테이블로 보수 추정
        return max(counts.values(), default=0)

    def check(self, sql: str, params: tuple | list = ()) -> CostReport:
        """실행 계획을 뽑아 인덱스 없는 스캔 비용을 추정한다.

        Args:
            sql: 검사할 SELECT 문.
            params: 바인딩 값.

        Returns:
            CostReport. ok=False 면 message에 거부 사유가 담긴다.
        """
        try:
            plan = self._conn.execute(f"EXPLAIN QUERY PLAN {sql}", tuple(params)).fetchall()
        except sqlite3.Error as e:
            return CostReport(False, 0, [], f"실행 계획 확인 실패: {e}")

        # 같은 부모 아래 나란히 선 스캔은 서로 중첩 루프로 돈다 — 비용이 곱해진다.
        # 부모가 다르면 서브쿼리·코루틴 경계라 한 번씩 훑고 끝난다 — 더한다.
        by_level: dict[int, list[str]] = {}
        for row in plan:
            parent, detail = row[1], row[3]
            if detail.startswith("SCAN ") and "USING INDEX" not in detail:
                by_level.setdefault(parent, []).append(detail[5:].split(" ")[0])

        scanned = 0
        full_scans: list[str] = []
        crossed: list[str] = []
        for level in by_level.values():
            product = 1
            for table in level:
                product *= max(self._table_rows(table), 1)
            scanned += product
            full_scans.extend(level)
            if len(level) > 1:
                crossed.extend(level)

        if scanned > self._max:
            if crossed:
                return CostReport(
                    False,
                    scanned,
                    full_scans,
                    f"조인 조건 없이 {len(crossed)}개 테이블을 함께 훑습니다 "
                    f"({', '.join(crossed)}). 예상 스캔량 {scanned:,}행이 "
                    f"임계치 {self._max:,}행을 초과합니다. 조인 조건을 넣어 주세요.",
                )
            return CostReport(
                False,
                scanned,
                full_scans,
                f"예상 스캔량 {scanned:,}행이 임계치 {self._max:,}행을 초과합니다 "
                f"(풀스캔: {', '.join(full_scans)}). 조건을 좁혀 다시 질문해 주세요.",
            )
        return CostReport(True, scanned, full_scans)


class PostgresCostGuard:
    """PostgreSQL 비용 가드 — EXPLAIN (FORMAT JSON) 의 플래너 추정을 쓴다."""

    def __init__(self, conn, max_scanned_rows: int = 1_000_000):
        """
        Args:
            conn: psycopg 연결 (executor.connection 공유).
            max_scanned_rows: Seq Scan 추정 행수 합산 임계치.
        """
        self._conn = conn
        self._max = max_scanned_rows

    def check(self, sql: str, params: tuple | list = ()) -> CostReport:
        """실행 계획을 뽑아 순차 스캔 추정 행수를 검사한다.

        Args:
            sql: 검사할 SELECT 문 (자리표시자 `?` 허용).
            params: 바인딩 값.

        Returns:
            CostReport. ok=False 면 message에 거부 사유가 담긴다.
        """
        if params:
            sql = sql.replace("?", "%s")
        try:
            with self._conn.cursor() as cur:
                # 빈 튜플이 아니라 None — PostgresExecutor.run과 같은 이유다.
                # 여기서 깨지면 "실행 계획 확인 실패"로 거부되어 원인이 한 겹 더 가려진다.
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql}", tuple(params) or None)
                plan_root = cur.fetchone()[0]
        except Exception as e:  # psycopg.Error — 지연 임포트 의존을 피한다
            return CostReport(False, 0, [], f"실행 계획 확인 실패: {e}")

        if isinstance(plan_root, str):
            plan_root = json.loads(plan_root)

        scanned = 0
        full_scans: list[str] = []
        # 계획 트리에서 가장 넓은 중간 산출과, 그것을 만든 노드.
        #
        # Seq Scan 합산만으로는 카테시안 곱을 못 잡는다.
        # 조인 조건이 없어도 바탕 테이블은 각각 한 번씩만 훑히므로 합이 그대로이기 때문이다
        # (3중 크로스 조인의 Seq Scan 합은 정상 질의의 세 배에 불과하다).
        # 곱셈은 스캔이 아니라 조인 노드에서 일어나고, 플래너는 그 값을 이미 알고 있다 —
        # 같은 데이터에서 정상 질의는 1,440행, 3중 크로스 조인은 64,000,000행으로 추정된다.
        widest = 0
        widest_node = ""

        def walk(node: dict) -> None:
            nonlocal scanned, widest, widest_node
            rows = int(node.get("Plan Rows", 0))
            if rows > widest:
                widest, widest_node = rows, str(node.get("Node Type", ""))
            if node.get("Node Type") == "Seq Scan":
                scanned += rows
                relation = node.get("Relation Name")
                if relation:
                    full_scans.append(relation)
            for child in node.get("Plans", []):
                walk(child)

        walk(plan_root[0]["Plan"])

        estimated = max(scanned, widest)
        if estimated > self._max:
            # 곱셈이 일어난 자리를 지목한다. "조건을 좁혀라"는 크로스 조인의 처방이 아니다.
            is_join = "Join" in widest_node or widest_node == "Nested Loop"
            if is_join and widest > scanned:
                return CostReport(
                    False,
                    estimated,
                    full_scans,
                    f"조인 결과가 {widest:,}행으로 추정됩니다 "
                    f"(임계치 {self._max:,}행). 조인 조건이 빠졌는지 확인해 주세요.",
                )
            return CostReport(
                False,
                estimated,
                full_scans,
                f"예상 스캔량 {estimated:,}행이 임계치 {self._max:,}행을 초과합니다 "
                f"(순차 스캔: {', '.join(full_scans)}). 조건을 좁혀 다시 질문해 주세요.",
            )
        return CostReport(True, estimated, full_scans)


def make_cost_guard(executor, max_scanned_rows: int = 1_000_000):
    """executor의 dialect에 맞는 비용 가드를 만든다."""
    if getattr(executor, "dialect", "sqlite") == "postgres":
        return PostgresCostGuard(executor.connection, max_scanned_rows)
    return SqliteCostGuard(executor.connection, max_scanned_rows)
