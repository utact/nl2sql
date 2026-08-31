-- ─────────────────────────────────────────────────────────────
-- 이 파일은 생성물이다. 직접 고치지 말 것.
-- 정본: domain/catalog.py (VIEW_DDL)
-- 템플릿: deploy/init.sql.in
-- 생성: python -m deploy.render_init_sql
-- ─────────────────────────────────────────────────────────────
-- PostgreSQL 초기화 — 안전 축을 주장이 아니라 실물로 만드는 자리.
--
-- SQLite 경로에서는 증명할 수 없는 것들이 여기에 있다.
-- 읽기전용 롤, 객체 단위 GRANT, 행 단위 권한(RLS), 서버측 statement_timeout.
-- 데모의 장면들은 전부 correctness 이야기라 SQLite로 충분하다.
-- 다만 "접근 통제는 DB가 닫는다"는 주장은 진짜 DB가 있어야 보인다.

-- ── 물리 스키마
-- 망가뜨린 스키마가 아니라 제대로 설계된 쪽이다.
-- 로트 1:N 시료 1:N 측정은 정규화의 결과이고, is_void 로 폐기 로트를 남기는 것은 이력 보존이다.
-- 그런데 이 정상적인 것들이 조인 팬아웃·유령 행·표기 불일치를 만든다.
-- DBA 의 실수가 아니라 DB 를 고쳐서 없앨 수 없다. 그래서 뷰가 그 위에 온다.

CREATE TABLE item_master (
    item_code       TEXT PRIMARY KEY,
    item_name       TEXT NOT NULL,
    unit            TEXT NOT NULL,
    category_code   TEXT NOT NULL,
    lower_limit     DOUBLE PRECISION,
    upper_limit     DOUBLE PRECISION,
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
    measured_value  DOUBLE PRECISION NOT NULL,
    measured_at     TEXT NOT NULL
);

-- ── 큐레이션 뷰
-- 아래 DDL 은 domain/catalog.py 의 VIEW_DDL 에서 그대로 옮겨 붙는다.
-- 손으로 고치면 다음 생성 때 덮어써진다.

CREATE VIEW inspection_results AS
SELECT
    b.measurement_id,
    b.lot_id,
    b.line_code,
    b.inspected_on,
    b.inspection_month,
    b.item_code,
    b.item_name,
    b.unit,
    b.category,
    b.measured_value,
    b.lower_limit,
    b.upper_limit,
    b.verdict,
    b.spec_slack,
    PERCENT_RANK() OVER (PARTITION BY b.item_code ORDER BY b.spec_slack) AS slack_rank,
    -- 합격품 안에서만 매긴 순위.
    --
    -- slack_rank는 불합격까지 포함해 줄을 세운다.
    -- 그런데 불합격은 여유가 음수라 하위 구간을 통째로 차지한다.
    -- 이 데이터에서는 항목당 하위 5% 열세 칸 중 열~열셋이 이미 규격 밖이라,
    -- 거기서 합격만 골라내면 1,220건 중 다섯 건이 남는다. 지표가 아니라 우연이 된다.
    --
    -- "규격 안에 있으나 경계에 아슬아슬한" 것을 세려면 합격품끼리 줄을 세워야 한다.
    -- 불합격 행에는 뜻이 없으므로 NULL로 둔다.
    CASE WHEN b.verdict = 'PASS'
         THEN PERCENT_RANK() OVER (PARTITION BY b.item_code, b.verdict ORDER BY b.spec_slack)
    END AS pass_slack_rank
FROM (
    SELECT
        m.measurement_id,
        l.lot_id,
        l.line_code,
        l.inspected_on,
        substr(l.inspected_on, 1, 7) AS inspection_month,
        m.item_code,
        i.item_name,
        i.unit,
        CASE
            WHEN i.category_code LIKE 'MECH%' THEN '기계'
            WHEN i.category_code LIKE 'ELEC%' THEN '전기'
            WHEN i.category_code LIKE 'DIM%'  THEN '치수'
            ELSE '기타'
        END AS category,
        m.measured_value,
        i.lower_limit,
        i.upper_limit,
        CASE
            WHEN i.limit_operator = 'GTE' AND m.measured_value >= i.lower_limit THEN 'PASS'
            WHEN i.limit_operator = 'LTE' AND m.measured_value <= i.upper_limit THEN 'PASS'
            WHEN i.limit_operator = 'BETWEEN'
                 AND m.measured_value >= i.lower_limit
                 AND m.measured_value <= i.upper_limit THEN 'PASS'
            ELSE 'FAIL'
        END AS verdict,
        CASE
            WHEN i.limit_operator = 'GTE' THEN m.measured_value - i.lower_limit
            WHEN i.limit_operator = 'LTE' THEN i.upper_limit - m.measured_value
            WHEN (m.measured_value - i.lower_limit) < (i.upper_limit - m.measured_value)
                THEN m.measured_value - i.lower_limit
            ELSE i.upper_limit - m.measured_value
        END AS spec_slack
    FROM measurements m
    JOIN samples s ON s.sample_id = m.sample_id
    JOIN inspection_lots l ON l.lot_id = s.lot_id
    JOIN item_master i ON i.item_code = m.item_code
    WHERE l.is_void = 0
) b;

-- ── 계정과 객체 권한
-- 여기서부터가 SQLite로는 보여줄 수 없는 부분이다.

CREATE ROLE nl2sql_app LOGIN PASSWORD 'demo-only-not-a-secret';

-- 쓰기를 막는다. 읽기전용 계정이면 앱에 버그가 있어도 DB가 거부한다.
ALTER ROLE nl2sql_app SET default_transaction_read_only = on;

-- 장기 쿼리를 서버가 중단한다. 클라이언트 사정과 무관하게 걸린다.
ALTER ROLE nl2sql_app SET statement_timeout = '5s';

-- 쓰기를 막는 것만으로는 절반이다. 무엇을 볼지는 객체 GRANT 가 정한다.
-- 앱이 질의하는 것은 큐레이션 뷰 하나뿐이다.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nl2sql_app;
GRANT SELECT ON inspection_results TO nl2sql_app;

-- ── 행 단위 권한
-- 프롬프트 지시도, 앱의 자동 주입도 방어가 아니다.
-- 자동 주입이 fail-open 으로 빠져 필터가 안 붙어도 이 정책은 남는다.
--
-- 뷰는 기본적으로 소유자 권한으로 돌아 아래 정책을 건너뛴다.
-- 정책이 실제로 걸리려면 뷰를 security_invoker 로 바꿔야 한다 (PostgreSQL 15+).
-- 그러면 뷰가 바탕 테이블을 호출자 권한으로 읽으므로 아래 GRANT 가 함께 필요해진다.
-- 그 대가로 물리 테이블이 롤에 열린다. 뷰 밖 접근을 막는 것은 DB 가 아니라 앱의 AST 가드다.

ALTER VIEW inspection_results SET (security_invoker = true);
GRANT SELECT ON inspection_lots TO nl2sql_app;
GRANT SELECT ON samples TO nl2sql_app;
GRANT SELECT ON measurements TO nl2sql_app;
GRANT SELECT ON item_master TO nl2sql_app;

ALTER TABLE inspection_lots ENABLE ROW LEVEL SECURITY;

-- 세션 변수 app.line_code 에 담긴 라인만 보인다.
-- 값이 비어 있으면 아무 행도 보이지 않는다 (fail-closed).
CREATE POLICY line_isolation ON inspection_lots
    FOR SELECT TO nl2sql_app
    USING (line_code = current_setting('app.line_code', true));
