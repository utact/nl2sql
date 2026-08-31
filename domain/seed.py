"""합성 데이터 생성기.

이 스키마는 일부러 망가뜨린 것이 아니라 제대로 설계된 쪽이다.
로트 1:N 시료 1:N 측정은 정규화의 결과이고, `is_void`로 폐기 로트를 남기는 것은 이력 보존이다.
`MECH_TENSILE` 같은 조합 코드는 제조 MES/ERP에서 흔한 표기이고, 단위가 다른 것은 그냥 물리다.

조용한 오답을 만드는 것은 망가진 스키마가 아니라 정상적인 스키마다.
정규화는 조인 팬아웃을 만들고, 소프트 삭제는 안 거르면 유령 행을 만들고,
조합 코드는 사용자의 정의와 어긋나고, 단위 혼재는 항목 간 비교를 깨뜨린다.
어느 것도 DBA의 실수가 아니라 DB를 고쳐서 없앨 수 없다. 그래서 큐레이션 뷰가 그 위에 온다.

난수는 고정 시드를 쓴다. 데모가 매번 같은 숫자를 내야 하고, 골든셋도 그 위에서만 성립한다.
데이터의 실제 규모와 수치는 docs/data.md에 있다.

사용법:
    python -m domain.seed  # inspection.db 생성
    python -m domain.seed --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path

from .catalog import VIEW_DDL

DEFAULT_DB = "inspection.db"
RANDOM_SEED = 20260812

SCHEMA_DDL = """
CREATE TABLE item_master (
    item_code       TEXT PRIMARY KEY,
    item_name       TEXT NOT NULL,
    unit            TEXT NOT NULL,
    category_code   TEXT NOT NULL,
    lower_limit     REAL,
    upper_limit     REAL,
    limit_operator  TEXT NOT NULL
);

CREATE TABLE inspection_lots (
    lot_id          TEXT PRIMARY KEY,
    line_code       TEXT NOT NULL,
    inspected_on    TEXT NOT NULL,
    operator_code   TEXT NOT NULL,
    is_void         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE samples (
    sample_id       TEXT PRIMARY KEY,
    lot_id          TEXT NOT NULL REFERENCES inspection_lots(lot_id),
    sample_no       INTEGER NOT NULL
);

CREATE TABLE measurements (
    measurement_id  TEXT PRIMARY KEY,
    sample_id       TEXT NOT NULL REFERENCES samples(sample_id),
    item_code       TEXT NOT NULL REFERENCES item_master(item_code),
    measured_value  REAL NOT NULL,
    measured_at     TEXT NOT NULL
);
"""

# 항목별 규격과 분포.
# (코드, 이름, 단위, 분류코드, 하한, 상한, 연산자, 중심값, 표준편차)
# 단위와 스케일이 서로 다르다는 점이 이 표의 핵심이다.
ITEMS = [
    ("TENSILE", "인장강도", "MPa", "MECH_TENSILE", 380.0, None, "GTE", 430.0, 28.0),
    ("OUTER_DIA", "외경", "mm", "DIM_OUTER", 11.95, 12.05, "BETWEEN", 12.0, 0.022),
    ("INSULATION", "절연저항", "MΩ", "ELEC_INSUL", 100.0, None, "GTE", 780.0, 260.0),
    ("ROUGHNESS", "표면조도", "µm", "DIM_SURFACE", None, 1.6, "LTE", 1.05, 0.24),
    ("WITHSTAND", "내전압", "kV", "ELEC_WITHSTAND", 2.5, None, "GTE", 3.4, 0.42),
]

LINES = ["L1", "L2", "L3"]
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07"]
LOTS_PER_MONTH_PER_LINE = 6
SAMPLES_PER_LOT = 4
# 폐기 로트 비율. 뷰가 걸러내는지 확인할 수 있을 만큼만 섞는다.
VOID_RATE = 0.08


def _build_rows(rng: random.Random) -> tuple[list, list, list]:
    """로트·시료·측정 행을 만든다.

    Args:
        rng: 시드가 고정된 난수 생성기.

    Returns:
        (lots, samples, measurements) 세 목록.
    """
    lots: list[tuple] = []
    samples: list[tuple] = []
    measurements: list[tuple] = []

    for month in MONTHS:
        for line in LINES:
            for n in range(LOTS_PER_MONTH_PER_LINE):
                lot_id = f"{line}-{month.replace('-', '')}-{n + 1:02d}"
                day = rng.randint(1, 27)
                inspected_on = f"{month}-{day:02d}"
                is_void = 1 if rng.random() < VOID_RATE else 0
                lots.append(
                    (lot_id, line, inspected_on, f"OP{rng.randint(1, 6):02d}", is_void)
                )

                for s in range(SAMPLES_PER_LOT):
                    sample_id = f"{lot_id}-S{s + 1}"
                    samples.append((sample_id, lot_id, s + 1))

                    for code, _, _, _, lower, upper, op, center, sigma in ITEMS:
                        value = rng.gauss(center, sigma)
                        # 규격 위반이 아예 없으면 합불·여유 이야기가 성립하지 않는다.
                        # 드물게 경계 밖으로 밀어 낸다.
                        if rng.random() < 0.04:
                            value = _push_out_of_spec(rng, value, lower, upper, op, sigma)
                        value = round(value, 4)
                        measurements.append(
                            (
                                f"{sample_id}-{code}",
                                sample_id,
                                code,
                                value,
                                f"{inspected_on}T09:00:00+09:00",
                            )
                        )
    return lots, samples, measurements


def _push_out_of_spec(
    rng: random.Random,
    value: float,
    lower: float | None,
    upper: float | None,
    op: str,
    sigma: float,
) -> float:
    """측정값을 규격 밖으로 밀어 낸다."""
    offset = abs(rng.gauss(0, sigma))
    if op == "GTE":
        return lower - offset
    if op == "LTE":
        return upper + offset
    return (lower - offset) if rng.random() < 0.5 else (upper + offset)


def seed(db_path: str = DEFAULT_DB) -> str:
    """데이터베이스를 새로 만들고 물리 테이블·데이터·큐레이션 뷰를 설치한다.

    뷰 DDL은 정의(`domain.catalog.VIEW_DDL`)에서 가져온다.
    뷰 정의가 두 곳에 있으면 반드시 드리프트하고, 그 드리프트는 조용한 오답으로 나타난다.

    Args:
        db_path: 만들 SQLite 파일 경로. 이미 있으면 지우고 다시 만든다.

    Returns:
        만들어진 파일 경로.
    """
    path = Path(db_path)
    if path.exists():
        path.unlink()

    rng = random.Random(RANDOM_SEED)
    lots, samples, measurements = _build_rows(rng)

    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.executemany(
            "INSERT INTO item_master"
            " (item_code, item_name, unit, category_code, lower_limit, upper_limit, limit_operator)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(c, n, u, cat, lo, up, op) for c, n, u, cat, lo, up, op, _, _ in ITEMS],
        )
        conn.executemany(
            "INSERT INTO inspection_lots"
            " (lot_id, line_code, inspected_on, operator_code, is_void)"
            " VALUES (?, ?, ?, ?, ?)",
            lots,
        )
        conn.executemany(
            "INSERT INTO samples (sample_id, lot_id, sample_no) VALUES (?, ?, ?)",
            samples,
        )
        conn.executemany(
            "INSERT INTO measurements"
            " (measurement_id, sample_id, item_code, measured_value, measured_at)"
            " VALUES (?, ?, ?, ?, ?)",
            measurements,
        )
        conn.executescript(VIEW_DDL)
        conn.commit()
    finally:
        conn.close()

    return str(path)


def seed_postgres(dsn: str) -> int:
    """이미 만들어진 PostgreSQL 스키마에 같은 합성 데이터를 넣는다.

    스키마와 뷰는 `deploy/init.sql`이 컨테이너 기동 때 만든다.
    여기서는 행만 채운다.

    Args:
        dsn: 관리자 권한 접속 문자열. 읽기전용 롤로는 넣을 수 없다.

    Returns:
        넣은 측정 행수.

    Raises:
        RuntimeError: psycopg가 설치되지 않은 경우.
    """
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "PostgreSQL 시드에는 psycopg가 필요합니다: pip install -e \".[postgres]\""
        ) from e

    rng = random.Random(RANDOM_SEED)
    lots, samples, measurements = _build_rows(rng)

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE measurements, samples, inspection_lots, item_master")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO item_master"
                " (item_code, item_name, unit, category_code,"
                "  lower_limit, upper_limit, limit_operator)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(c, n, u, cat, lo, up, op) for c, n, u, cat, lo, up, op, _, _ in ITEMS],
            )
            cur.executemany(
                "INSERT INTO inspection_lots"
                " (lot_id, line_code, inspected_on, operator_code, is_void)"
                " VALUES (%s, %s, %s, %s, %s)",
                lots,
            )
            cur.executemany(
                "INSERT INTO samples (sample_id, lot_id, sample_no) VALUES (%s, %s, %s)",
                samples,
            )
            cur.executemany(
                "INSERT INTO measurements"
                " (measurement_id, sample_id, item_code, measured_value, measured_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                measurements,
            )
    return len(measurements)


def main() -> None:
    parser = argparse.ArgumentParser(description="계측·규격 검사 합성 데이터를 생성한다.")
    parser.add_argument(
        "--target",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="시드 대상. postgres는 deploy/compose.yaml로 띄운 DB를 가정한다",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="생성할 SQLite 파일 경로")
    parser.add_argument(
        "--dsn",
        default="postgresql://postgres:demo-only-not-a-secret@127.0.0.1:5432/inspection",
        help="PostgreSQL 접속 문자열 (관리자 권한)",
    )
    args = parser.parse_args()

    if args.target == "postgres":
        rows = seed_postgres(args.dsn)
        print(f"PostgreSQL 시드 완료: 측정 {rows}건")
        return

    path = seed(args.db)
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM inspection_results").fetchone()[0]
        fails = conn.execute(
            "SELECT COUNT(*) FROM inspection_results WHERE verdict = 'FAIL'"
        ).fetchone()[0]
        lots = conn.execute("SELECT COUNT(*) FROM inspection_lots").fetchone()[0]
        voids = conn.execute(
            "SELECT COUNT(*) FROM inspection_lots WHERE is_void = 1"
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"생성: {path}")
    print(f"  로트 {lots}건 (폐기 {voids}건은 뷰에서 제외됨)")
    print(f"  뷰 행수 {rows}건, 그중 불합격 {fails}건")


if __name__ == "__main__":
    main()
