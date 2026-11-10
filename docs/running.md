# 실행 방법

Python 3.11 이상이 필요하다. 저장소를 내려받고 합성 데이터를 생성한다.

```bash
git clone https://github.com/utact/nl2sql.git
cd nl2sql
python -m pip install -e '.[dev]'
python -m domain.seed
```

모델 없이 예제를 재현하려면 다음 명령을 실행한다. 고정된 질문과 `StubLLM`을 사용해 지표 정의 변경에 따른 결과 차이를 보여준다.

```bash
python -m demo
```

## 웹에서 질문하기

NVIDIA API를 사용하려면 추가 의존성을 설치하고 `.env.example`을 `.env`로 복사한다.

```bash
python -m pip install -e '.[api]'
```

`.env`에서 다음 항목을 설정하고 키를 입력한다. API 모드에서는 질문과 카탈로그 정보, 관측한 필터 값 등이 외부 모델에 전달된다.

```dotenv
NL2SQL_OFFLINE=0
NL2SQL_LLM=nvidia
NVIDIA_API_KEY=
```

```bash
uvicorn app.server:app --reload
```

[http://127.0.0.1:8000](http://127.0.0.1:8000)에서 질문할 수 있다.

## 로컬 모델 사용

기본값은 오프라인 모드다. `local-llm` 의존성을 설치하고, `.env`의 `NL2SQL_HF_MODEL`에 미리 내려받은 가중치 경로를 지정한다. API 모드를 사용하던 환경에서는 `NL2SQL_OFFLINE=1`, `NL2SQL_LLM=huggingface`로 변경한다.

```bash
python -m pip install -e '.[local-llm]'
uvicorn app.server:app --reload
```

## 검사

SQL 검사, 컴파일, 응답 상태와 회귀 사례는 모델 없이 검사한다. 실제 모델을 호출하는 라우팅 검사는 `NL2SQL_TEST_LLM=1`을 설정한 경우에만 실행된다.

```bash
python -m pytest -q
```
