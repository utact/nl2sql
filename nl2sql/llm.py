"""LLM 어댑터.

모델은 인터페이스 하나 뒤의 변수다.
파이프라인은 `complete_json(system, user, schema) -> dict` 하나만 요구한다.
그 경계만 지키면 백엔드 교체가 환경변수 한 줄이 된다.

- `HuggingFaceLLM`: 로컬 경로. 기본값이다.
- `NvidiaLLM`: API 경로. NVIDIA NIM(OpenAI 호환)의 호스팅 오픈 모델.
- `StubLLM`: 테스트·데모 경로. 핸들러가 정해진 응답을 돌려준다.

오프라인 모드가 기본으로 켜져 있어 API 경로는 선택 자체가 차단된다.
자세한 이유는 README를 보라.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Protocol


class LLM(Protocol):
    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        """system/user 프롬프트로 schema를 따르는 JSON 객체 하나를 받는다."""
        ...


def offline_mode() -> bool:
    """보안(오프라인) 모드 여부. 기본값이 켜짐이다.

    켜져 있으면 API 백엔드는 선택 자체가 차단되고, 허깅페이스 모델도 로컬 캐시에서만 적재된다.
    외부로 내보내려면 `NL2SQL_OFFLINE=0`을 명시해야 한다.

    Returns:
        오프라인 모드면 True. `NL2SQL_OFFLINE`이 0|false|no 일 때만 False.
    """
    return os.environ.get("NL2SQL_OFFLINE", "1").lower() not in ("0", "false", "no")


DEFAULT_HF_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict:
    """모델 출력에서 첫 번째 JSON 객체를 꺼낸다 (코드펜스/앞뒤 잡담 허용)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = _JSON_FENCE.search(text)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("{")
    if start == -1:
        raise ValueError(f"JSON 객체를 찾지 못했습니다: {text[:200]!r}")
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"JSON 객체가 닫히지 않았습니다: {text[:200]!r}")


def _prompted_json(generate, system: str, user: str, schema: dict) -> dict:
    """구조적 강제가 없는 백엔드의 공통 경로 — 스키마 프롬프트 + 추출 + 1회 재시도.

    Args:
        generate: messages(list[dict]) -> str 텍스트 생성 함수.
        system/user: 프롬프트.
        schema: 응답이 따라야 하는 JSON 스키마. 프롬프트에 삽입된다.

    Returns:
        추출된 JSON 객체.

    Raises:
        RuntimeError: 재시도 후에도 유효한 JSON을 얻지 못했을 때.
    """
    system_with_schema = (
        f"{system}\n\n"
        "Respond with a single JSON object and nothing else — no prose, "
        "no markdown fences. The object MUST conform to this JSON schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    messages = [
        {"role": "system", "content": system_with_schema},
        {"role": "user", "content": user},
    ]
    last_error: Exception | None = None
    for _ in range(2):
        text = generate(messages)
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            messages = messages[:2] + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": f"Invalid JSON ({e}). Output ONLY the corrected JSON object.",
                },
            ]
    raise RuntimeError(f"모델이 유효한 JSON을 반환하지 못했습니다: {last_error}")


# 이 기본값은 벤치마크로 고른 것이 아니라 동작을 확인한 값이다.
# 후보를 제대로 비교하려면 NL2SQL_TEST_LLM=1로 골든셋(tests/test_layer3_routing.py)을 돌린다.
DEFAULT_NVIDIA_MODEL = "openai/gpt-oss-120b"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


class NvidiaLLM:
    """NVIDIA NIM(호스팅, OpenAI 호환 API) 구현. NVIDIA_API_KEY를 사용한다.

    로컬 GPU 없이 대형 오픈 모델을 쓰는 경로다.
    structured outputs가 없어 허깅페이스와 같은 _prompted_json을 쓴다.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = 2048,
    ):
        """클라이언트를 만든다.

        Args:
            model: NIM 모델 ID. 기본값은 NL2SQL_NVIDIA_MODEL 환경변수.
                호스팅 모델은 예고 없이 EOL 된다 (410 Gone).
                그 실패가 사용자에게 어떻게 나가는지는 NL2SQL._backend_failure를 보라.
            api_key: NVIDIA API 키. 기본값: NVIDIA_API_KEY 환경변수.
            timeout: HTTP 타임아웃(초).
            max_tokens: 응답 생성 상한.

        Raises:
            ValueError: API 키가 없을 때.
        """
        import httpx

        self.model = model or os.environ.get("NL2SQL_NVIDIA_MODEL", DEFAULT_NVIDIA_MODEL)
        key = api_key or os.environ.get("NVIDIA_API_KEY")
        if not key:
            raise ValueError("NVIDIA_API_KEY가 설정되어 있지 않습니다 (.env 참고).")
        self._max_tokens = max_tokens
        self._client = httpx.Client(
            base_url=NVIDIA_BASE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )

    def _generate(self, messages: list[dict]) -> str:
        response = self._client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": self._max_tokens,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        return _prompted_json(self._generate, system, user, schema)


class HuggingFaceLLM:
    """허깅페이스 로컬 모델 구현.

    구조적 강제가 없어 스키마를 프롬프트에 넣고 출력에서 JSON을 추출한다.
    실패하면 오류를 알려주며 1회 재시도한다.
    기본 모델(Qwen2.5-1.5B-Instruct)은 CPU 에서도 돈다.
    품질은 모델 크기를 따라가므로 여유가 있으면 NL2SQL_HF_MODEL로 더 큰 모델을 지정한다.
    """

    def __init__(
        self,
        model_id: str | None = None,
        max_new_tokens: int = 512,
        device: str | None = None,
        offline: bool | None = None,
    ):
        """모델을 로드한다.

        Args:
            model_id: 허깅페이스 모델 ID 또는 로컬 경로.
                기본값: NL2SQL_HF_MODEL 환경변수 → Qwen/Qwen2.5-1.5B-Instruct.
            max_new_tokens: 응답 생성 상한.
            device: "cuda"/"cpu". 기본은 자동 감지.
            offline: True 면 로컬 캐시에서만 로드하고 네트워크에 나가지 않는다.
                기본값은 NL2SQL_OFFLINE 환경변수를 따른다.
        """
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.model_id = model_id or os.environ.get("NL2SQL_HF_MODEL", DEFAULT_HF_MODEL)
        self._max_new_tokens = max_new_tokens
        self._device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        local_only = offline_mode() if offline is None else offline
        dtype = torch.bfloat16 if self._device == "cpu" else "auto"
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_id, local_files_only=local_only
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_id, dtype=dtype, local_files_only=local_only
        )
        self._model.to(self._device)
        self._model.eval()

    def _generate(self, messages: list[dict]) -> str:
        import torch

        inputs = self._tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True
        ).to(self._device)
        with torch.no_grad():
            output = self._model.generate(
                **inputs,
                max_new_tokens=self._max_new_tokens,
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        prompt_len = inputs["input_ids"].shape[1]
        return self._tokenizer.decode(output[0][prompt_len:], skip_special_tokens=True)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        return _prompted_json(self._generate, system, user, schema)


class StubLLM:
    """테스트용: (system, user) 를 받아 dict를 돌려주는 핸들러를 감싼다."""

    def __init__(self, handler: Callable[[str, str], dict]):
        self._handler = handler

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        return self._handler(system, user)


def resolve_llm(backend: str | None = None, **kwargs) -> LLM:
    """환경에 맞는 LLM 백엔드를 고른다.

    기본은 로컬 오픈소스 모델이다. 오프라인 모드가 켜져 있으면 API 백엔드는 선택이 차단된다.

    우선순위는 인자 > NL2SQL_LLM 환경변수 > 자동이다.
    자동은 오프라인이면 huggingface, 아니면 NVIDIA_API_KEY가 있을 때 nvidia를 고른다.

    Args:
        backend: "huggingface" | "nvidia". 미지정 시 자동 선택.
        **kwargs: 선택한 백엔드 생성자에 그대로 전달.

    Returns:
        LLM 프로토콜을 구현한 인스턴스.

    Raises:
        ValueError: 알 수 없는 백엔드이거나, 오프라인에서 API 백엔드를 지정한 경우.
    """
    backend = backend or os.environ.get("NL2SQL_LLM")
    if backend is None:
        if not offline_mode() and os.environ.get("NVIDIA_API_KEY"):
            backend = "nvidia"
        else:
            backend = "huggingface"
    if backend == "nvidia":
        if offline_mode():
            raise ValueError(
                "오프라인 모드에서는 API 백엔드(nvidia)를 쓸 수 없습니다. "
                "로컬 모델을 쓰거나 NL2SQL_OFFLINE=0을 명시하세요."
            )
        return NvidiaLLM(**kwargs)
    if backend == "huggingface":
        if "model" in kwargs:  # 백엔드 공통 kwarg 이름을 HF 생성자 이름으로 번역
            kwargs["model_id"] = kwargs.pop("model")
        return HuggingFaceLLM(**kwargs)
    raise ValueError(f"알 수 없는 LLM 백엔드: {backend!r} (huggingface | nvidia)")
