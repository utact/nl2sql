"""실행 격리 — 가드레일 4층.

같은 층이라 백엔드 둘을 한 모듈에 둔다. 둘은 같은 인터페이스를 제공한다:

- `dialect`: 컴파일러와 가드가 방언을 맞추는 데 쓴다.
- `connection`: 비용 가드와 인트로스펙션이 같은 읽기전용 연결을 공유한다.
- `run(sql, params) -> ExecutionResult`
- `close()`

권한은 무엇을 만질지를 막고, 타임아웃은 얼마나 오래 돌지를 막는다.
축이 다르므로 둘 다 필요하다.

행수 상한은 안전 장치지만 잘린 결과를 전체로 믿게 한다.
그래서 자르는 것으로 끝내지 않고 `truncated`로 표면화한다.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


class QueryTimeoutError(RuntimeError):
    """실행 시간 상한을 넘겨 DB가 쿼리를 중단한 경우."""


@dataclass
class ExecutionResult:
    """실행 결과와, 그 결과를 어떻게 읽어야 하는지에 필요한 사실."""

    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_ms: float


class SqliteExecutor:
    """SQLite 읽기전용 실행기.

    - `mode=ro` URI + `PRAGMA query_only`로 쓰기를 막는다.
    - progress handler로 데드라인을 강제해 타임아웃을 구현한다.
    """

    dialect = "sqlite"
    # 행 단위 권한이 없다. 세션 컨텍스트를 넘겨도 받을 곳이 없으므로 0행의 원인이 되지 못한다.
    enforces_context = False

    def __init__(self, db_path: str, timeout_ms: int = 3000, max_rows: int = 1000):
        """읽기전용으로 접속한다.

        Args:
            db_path: SQLite 파일 경로.
            timeout_ms: 실행 시간 상한 (밀리초).
            max_rows: 반환 행수 상한.
        """
        # check_same_thread=False는 웹 서버의 워커 스레드에서도 쓸 수 있게 한다.
        # 동시 접근은 상위에서 직렬화한다.
        self._conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.execute("PRAGMA query_only = ON")
        self.timeout_ms = timeout_ms
        self.max_rows = max_rows

    @property
    def connection(self) -> sqlite3.Connection:
        """비용 가드·인트로스펙션이 공유하는 읽기전용 연결."""
        return self._conn

    def run(self, sql: str, params: tuple | list = ()) -> ExecutionResult:
        """SELECT 문을 읽기전용으로 실행한다.

        Args:
            sql: 실행할 SELECT 문. 자리표시자는 `?`.
            params: 바인딩 값.

        Raises:
            QueryTimeoutError: 데드라인 초과로 중단된 경우.
            sqlite3.OperationalError: 그 외 실행 오류.
        """
        deadline = time.monotonic() + self.timeout_ms / 1000
        self._conn.set_progress_handler(lambda: time.monotonic() > deadline, 5000)
        start = time.monotonic()
        try:
            cursor = self._conn.execute(sql, tuple(params))
            rows = cursor.fetchmany(self.max_rows + 1)
        except sqlite3.OperationalError as e:
            if "interrupt" in str(e).lower():
                raise QueryTimeoutError(
                    f"쿼리가 제한 시간 {self.timeout_ms}ms를 초과해 중단되었습니다."
                ) from e
            raise
        finally:
            self._conn.set_progress_handler(None, 0)

        elapsed_ms = (time.monotonic() - start) * 1000
        columns = [d[0] for d in cursor.description] if cursor.description else []
        truncated = len(rows) > self.max_rows
        return ExecutionResult(columns, rows[: self.max_rows], truncated, elapsed_ms)

    def apply_context(self, context: dict | None) -> None:
        """세션 컨텍스트를 DB에 넘긴다 — SQLite 에는 받을 곳이 없다.

        행 단위 권한이 없으므로 아무것도 하지 않는다.
        여기서 격리는 앱이 넣은 WHERE 절 하나로 끝나고, 그건 편의 장치이지 보증이 아니다.
        보증은 PostgresExecutor 쪽에 있다.
        """

    def close(self) -> None:
        self._conn.close()


class PostgresExecutor:
    """PostgreSQL 읽기전용 실행기 (psycopg).

    - `default_transaction_read_only`로 세션 수준에서 쓰기를 막는다.
      배포 시에는 SELECT 권한만 가진 롤로 접속해 DB 권한과 이중으로 방어한다.
    - `statement_timeout`으로 서버가 장기 쿼리를 중단한다.

    파라미터 자리표시자는 파이프라인 공통 표기인 `?`를 받아 psycopg의 `%s`로 바꾼다.
    시맨틱 컴파일러 출력에는 리터럴 `?`가 없으므로 이 치환은 안전하다.
    """

    dialect = "postgres"
    # RLS 정책이 세션 설정을 읽는다. 컨텍스트가 비면 정책이 0행을 돌려준다 (fail-closed).
    enforces_context = True

    def __init__(self, dsn: str, timeout_ms: int = 5000, max_rows: int = 1000):
        """접속하고 세션 수준 안전장치를 건다.

        Args:
            dsn: 접속 문자열. 배포 시 읽기전용 롤을 쓴다.
            timeout_ms: statement_timeout (밀리초).
            max_rows: 반환 행수 상한.
        """
        import psycopg

        self._psycopg = psycopg
        # autocommit은 문장 단위 암묵 트랜잭션을 쓴다.
        # 실패한 EXPLAIN이 이후 문장을 오염시키지 않게 하려는 것이다.
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute("SET default_transaction_read_only = on")
        self._conn.execute(f"SET statement_timeout = {int(timeout_ms)}")
        self.timeout_ms = timeout_ms
        self.max_rows = max_rows
        self._context_keys: set[str] = set()  # 다음 요청에서 비워야 할 설정 이름

    @property
    def connection(self):
        """비용 가드·인트로스펙션이 공유하는 읽기전용 연결."""
        return self._conn

    def apply_context(self, context: dict | None) -> None:
        """세션 컨텍스트를 `app.<이름>` 설정으로 넘긴다 (RLS가 읽는 자리).

        `deploy/init.sql`의 정책이 `current_setting('app.line_code', true)`를 본다.
        앱의 WHERE 절 주입과 DB의 RLS는 서로 다른 층이고, 둘을 잇는 것이 이 메서드다.

        비우는 것이 채우는 것만큼 중요하다.
        연결이 요청 간에 재사용되므로 초기화하지 않으면 앞 사용자의 값이 그대로 남는다.
        빈 값이면 정책이 0행을 돌려준다 (fail-closed).

        Args:
            context: {차원 이름: 값}. None 이거나 비면 설정을 비운다.
        """
        context = context or {}
        for name in self._context_keys | set(context):
            # 이름·값 모두 바인딩한다. 설정 키에 사용자 입력이 이어 붙지 않게 한다.
            self._conn.execute(
                "SELECT set_config(%s, %s, false)",
                (f"app.{name}", str(context.get(name, ""))),
            )
        self._context_keys = set(context)

    def run(self, sql: str, params: tuple | list = ()) -> ExecutionResult:
        """SELECT 문을 읽기전용으로 실행한다.

        Args:
            sql: 실행할 SELECT 문. 자리표시자는 `?`.
            params: 바인딩 값.

        Raises:
            QueryTimeoutError: statement_timeout 초과로 서버가 중단한 경우.
            psycopg.Error: 그 외 실행 오류 (읽기전용 위반 포함).
        """
        if params:
            sql = sql.replace("?", "%s")
        start = time.monotonic()
        try:
            with self._conn.cursor() as cur:
                # 바인딩 값이 없으면 None을 넘긴다. 빈 튜플이 아니다.
                #
                # psycopg는 params가 None이 아니면 자리표시자를 파싱하고,
                # 그러면 SQL 본문의 `%`가 전부 자리표시자 문법으로 읽힌다.
                # 자유 생성이 LIKE '%기계%' 를 쓰는 순간 실행 전에 깨진다 —
                # 시맨틱 경로는 항상 값을 바인딩하므로 이 구멍은 자유 생성에만 열려 있고,
                # 자유 생성이 값을 인라인하는 쪽이라 하필 정확히 그 경로가 항상 빈 params 다.
                cur.execute(sql, tuple(params) or None)
                rows = cur.fetchmany(self.max_rows + 1)
                columns = [d.name for d in cur.description] if cur.description else []
        except self._psycopg.errors.QueryCanceled as e:
            raise QueryTimeoutError(
                f"쿼리가 제한 시간 {self.timeout_ms}ms를 초과해 중단되었습니다."
            ) from e

        elapsed_ms = (time.monotonic() - start) * 1000
        truncated = len(rows) > self.max_rows
        return ExecutionResult(
            columns, [tuple(r) for r in rows[: self.max_rows]], truncated, elapsed_ms
        )

    def close(self) -> None:
        self._conn.close()
