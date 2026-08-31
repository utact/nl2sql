# 데이터 구조

계측·규격 검사 도메인의 합성 데이터. `python -m domain.seed`로 만든다.

고정 시드를 쓰므로, 누가 어디서 돌려도 같은 숫자가 나온다.
이 문서의 모든 수치는 그렇게 만든 `inspection.db`를 실제로 조회한 값이다.

---

## 규모

| | 행 |
|---|---:|
| `item_master` — 검사 항목 정의 | 5 |
| `inspection_lots` — 검사 로트 | 72 |
| `samples` — 시료 | 288 |
| `measurements` — 측정 | 1,440 |
| `inspection_results` — 큐레이션 뷰 | **1,220** |

측정 1,440건 중 뷰에 남는 것은 1,220건이다.
폐기 로트 11건이 걸러지는데, 로트 하나에 시료 넷과 시료마다 측정 다섯이 달려 있어, 빠지는 행은 11 × 4 × 5 = 220이다.
로트를 세는 단위와 뷰의 행 단위가 다르다.

기간은 2026-04-02 ~ 2026-07-27, 라인은 L1, L2, L3.
판정은 PASS 1,147 / FAIL 73.

---

## 물리 스키마

로트 하나에 시료 넷, 시료 하나에 측정 다섯이 달린다.

```
item_master ──┐
              │
inspection_lots ──1:N── samples ──1:N── measurements
```

| 테이블 | 열 |
|---|---|
| `item_master` | `item_code` `item_name` `unit` `category_code` `lower_limit` `upper_limit` `limit_operator` |
| `inspection_lots` | `lot_id` `line_code` `inspected_on` `operator_code` `is_void` |
| `samples` | `sample_id` `lot_id` `sample_no` |
| `measurements` | `measurement_id` `sample_id` `item_code` `measured_value` `measured_at` |

**이 스키마는 일부러 망가뜨린 것이 아니다.**
정규화는 교과서대로이고, 폐기 로트를 지우지 않고 `is_void`로 남기는 것은 이력 보존이며,
`MECH_TENSILE` 같은 조합 코드는 제조 MES·ERP에서 흔한 표기다.

그래서 여기서 나오는 오답은 DB를 고쳐서 없앨 수 없다. 뷰가 그 위에 온다.

---

## 검사 항목 다섯

| 항목 | 단위 | 규격 | 관측 범위 |
|---|---|---|---|
| 인장강도 | MPa | ≥ 380 | 326.06 ~ 492.91 |
| 외경 | mm | 11.95 ~ 12.05 | 11.92 ~ 12.09 |
| 절연저항 | MΩ | ≥ 100 | −268.12 ~ 1651.28 |
| 표면조도 | µm | ≤ 1.6 | 0.44 ~ 1.89 |
| 내전압 | kV | ≥ 2.5 | 1.38 ~ 4.45 |

**단위와 수치 스케일이 항목마다 다르다.** 이것이 이 데이터의 핵심 성질이다.

외경은 0.01mm 단위로 움직이고 절연저항은 수백 MΩ 단위로 움직인다.
규격까지의 여유를 절대값으로 재서 항목끼리 줄 세우면, 스케일이 작은 항목이 목록을 통째로 차지한다.
실행은 정상이고 행수도 그럴듯하고 에러도 없다. 순위만 틀린다.

규격 형태도 셋이다 — 하한만(`GTE`), 상한만(`LTE`), 양쪽(`BETWEEN`).
그래서 "여유"의 계산식이 항목마다 다르고, 뷰가 그것을 흡수한다.

---

## 큐레이션 뷰

정본은 `domain/catalog.py`의 `VIEW_DDL` 하나다.
SQLite는 `domain/seed.py`가, PostgreSQL은 `deploy/init.sql`이 같은 문자열을 설치한다.
`init.sql`은 생성물이고 `python -m deploy.render_init_sql --check`가 어긋남을 막는다.

### 열

| 열 | 출처 | 설명 |
|---|---|---|
| `measurement_id` `lot_id` | 물리 | 식별자 |
| `line_code` `inspected_on` | 로트 | 생산 라인, 검사일 |
| `inspection_month` | **파생** | `inspected_on` 앞 7자. 추이 질문의 축 |
| `item_code` `item_name` `unit` | 항목 | 항목 식별자·이름·단위 |
| `category` | **파생** | 조합 코드 → 기계 / 전기 / 치수 |
| `measured_value` `lower_limit` `upper_limit` | 측정·항목 | 값과 규격 |
| `verdict` | **파생** | 규격 형태별로 판정 → `PASS` / `FAIL` |
| `spec_slack` | **파생** | 규격 경계까지의 여유. **단위가 붙어 있다** |
| `slack_rank` | **파생** | 항목 내 여유 백분위 (0 = 가장 아슬아슬, 1 = 가장 여유) |
| `pass_slack_rank` | **파생** | 합격품 안에서만 매긴 여유 백분위. 불합격 행은 값이 없다 |

### 뷰가 정의 시점에 없애는 것

| 성질 | 그냥 두면 | 뷰가 하는 일 |
|---|---|---|
| 조인 팬아웃 | 로트 61건이 측정과 조인하면 **1,220건** — 집계가 20배로 부푼다 | 입도를 측정 1건으로 고정 |
| 소프트 삭제 | 폐기 로트 11건이 그대로 섞인다 | `is_void = 0`을 미리 건다 |
| 조합 코드 | 사용자는 "기계 항목"으로 묻는데 저장은 `MECH_TENSILE`이다 | `category`로 흡수 |
| 단위 혼재 | 항목 간 여유 비교가 뒤집힌다 | `slack_rank`로 무단위 축 제공 |

앞의 셋은 뷰만으로 닫힌다. **네 번째는 뷰만으로 안 닫힌다** — `spec_slack`도 함께 있으므로,
어느 축을 노출할지는 시맨틱 레이어가 정한다.

---

## 시맨틱 레이어

라우터와 시맨틱 레이어가 쓰는 이름은 이것이 전부다. 여기 없는 이름은 SQL이 되지 않는다.
정본은 `domain/catalog.py`이고 아래는 그 요약이다.

### 지표 — 세는 것

| 이름 | 식 | 뜻 |
|---|---|---|
| `measurement_count` | `COUNT(*)` | 측정 건수 |
| `lot_count` | `COUNT(DISTINCT lot_id)` | 로트 개수 |
| `fail_count` | `SUM(verdict = 'FAIL')` | 규격을 벗어난 건수 |
| `fail_rate` | `AVG(verdict = 'FAIL')` | 불합격 비율 (0~1) |
| `avg_measured_value` | `AVG(measured_value)` | 평균 측정값. 같은 항목 안에서만 의미가 있다 |
| `near_limit_count` | `SUM(verdict = 'PASS' AND pass_slack_rank <= 0.05)` | 규격 안이지만 경계에 아슬아슬한 건수 |
| `near_limit_rate` | `AVG(verdict = 'PASS' AND pass_slack_rank <= 0.05)` | 경계 근접 비율 (0~1) |

`near_limit_*`는 이미 규격을 벗어난 건을 세지 않는다. 그건 `fail_count`의 몫이다.

### 차원 — 묶거나 거르는 것

| 이름 | 값 | 비고 |
|---|---|---|
| `item_name` | 인장강도, 외경, 절연저항, 표면조도, 내전압 | |
| `category` | 기계, 전기, 치수 | |
| `line_code` | L1, L2, L3 | **자동 주입 대상** |
| `verdict` | PASS, FAIL | |
| `inspection_month` | 2026-04 ~ 2026-07 | 추이 질문의 기본 축 |
| `unit` | MPa, mm, MΩ, µm, kV | |
| `slack_rank` | 0.0 ~ 1.0 | 수치. **필터 전용**, 묶는 축으로 안 쓴다 |
| `pass_slack_rank` | 0.0 ~ 1.0 | 수치. 합격품끼리만 매긴다. `near_limit_*`가 쓰는 축 |
| `measured_value` | 항목마다 다름 | 수치. 반드시 항목 필터와 함께 쓴다 |

`slack_rank`를 묶는 축으로 쓰지 않는 데는 이유가 있다.
항목별로 파티션된 백분위라 **항목의 최솟값이 정의상 항상 0** 이다.
항목으로 묶는 순간 모든 행이 `0.0`이 된다 — 실행 정상, 에러 없음, 답만 무의미하다.

### 값 정규화

사용자가 쓰는 말과 저장된 값의 차이를 여기서 흡수한다.

| 차원 | 사용자 표현 → 저장 값 |
|---|---|
| `verdict` | 합격, 통과, 정상 → `PASS` / 불합격, 부적합, 불량, 탈락 → `FAIL` |
| `category` | 기계적 → 기계 / 전기적 → 전기 / 치수, 외관 → 치수 |

바뀐 값은 답에 그대로 적힌다 — "'불합격'은 저장된 값 'FAIL'로 보고 조건을 걸었습니다."

---

## 데이터가 없는 것

이 스키마에 **작업자별 성적, 설비 점검 이력, 사용자 계정, 원가**는 없다.

`operator_code`는 로트에 있지만 뷰에도 정의에도 올리지 않았다.
개인 단위 평가로 쓰이는 컬럼을 자연어 질문에 열어 두는 것은 별개의 결정이라, 기본값을 닫힘으로 두었다.

없는 것을 물으면 거절로 간다. 가장 가까운 데이터로 대신 답하지 않는다.

---

## 다시 만들기

```bash
python -m domain.seed                 # inspection.db
python -m domain.seed --db /tmp/x.db  # 다른 경로로
```

PostgreSQL은 스키마와 뷰를 컨테이너가 만들고, 행만 따로 채운다.

```bash
docker compose -f deploy/compose.yaml up -d
python -m domain.seed --target postgres
```
