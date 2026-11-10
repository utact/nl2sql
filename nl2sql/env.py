"""저장소 루트 `.env` 파일 로더 (의존성 없는 최소 구현).

흩어져 있던 실행 설정을 파일 하나에 모으기 위한 장치다.
실제 프로세스 환경변수가 항상 우선한다.
`.env`는 설정이 없을 때의 기본값이며, 셸에서 내린 값을 덮어쓰지 않는다.

형식은 `KEY=VALUE` 한 줄씩. `#`로 시작하는 줄과 빈 줄은 무시하고, 값 양끝의 따옴표는 벗긴다.
견본은 `.env.example`을 보라. `.env`는 API 키가 들어가므로 커밋하지 않는다.
"""

from __future__ import annotations

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env_file(path: str | None = None) -> dict[str, str]:
    """`.env` 파일을 읽어 os.environ에 없는 키만 채운다.

    Args:
        path: .env 파일 경로. 기본값은 저장소 루트의 `.env`.

    Returns:
        이번 호출로 새로 설정된 {키: 값}. 파일이 없으면 빈 dict.
    """
    path = path or os.path.join(_ROOT, ".env")
    loaded: dict[str, str] = {}
    if not os.path.exists(path):
        return loaded
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded[key] = value
    return loaded
