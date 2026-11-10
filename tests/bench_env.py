"""측정 환경 지문.

실측치를 적을 때 환경을 같이 안 적으면 숫자가 거짓말을 한다.

같은 모델(Qwen2.5-1.5B)에 같은 프롬프트를 주고 장치만 바꾸면 호출당 21.2초와 0.9초다.
24배 차이가 모델이 아니라 torch 설치에서 나온다.
앞의 숫자만 적으면 읽는 사람은 "이 모델은 느리다"를 배우는데, 그건 설치의 성질이다.

그래서 벤치는 무엇을 쟀는지 말하기 전에 어디서 쟀는지부터 말한다.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from datetime import datetime


def _gpu_name() -> str:
    """CUDA를 실제로 쓸 수 있는지와 장치 이름. torch가 없으면 그렇다고 말한다."""
    try:
        import torch
    except ImportError:
        return "torch 없음"
    if not torch.cuda.is_available():
        return f"CPU 전용 (torch {torch.__version__})"
    props = torch.cuda.get_device_properties(0)
    vram = props.total_memory / 1024**3
    return f"{props.name} · VRAM {vram:.1f} GiB · torch {torch.__version__}"


def _cpu_name() -> str:
    """모델명까지 읽는다. `platform.processor()`는 윈도우에서 계열만 준다."""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["wmic", "cpu", "get", "name"],
                capture_output=True, text=True, timeout=5,
            ).stdout.splitlines()
            names = [line.strip() for line in out if line.strip() and "Name" not in line]
            if names:
                return names[0]
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.processor() or platform.machine()


def describe(llm=None) -> str:
    """벤치 머리말 한 덩어리. 이 문자열을 그대로 문서에 붙일 수 있어야 한다.

    Args:
        llm: 잰 모델. 여러 모델을 훑는 벤치는 None을 주고 기계만 적는다.
    """
    lines = [f"측정 시각: {datetime.now().isoformat(timespec='seconds')}"]
    if llm is not None:
        device = getattr(llm, "_device", None)
        where = "외부 API" if device is None else f"로컬 · {device}"
        lines += [
            f"모델: {getattr(llm, 'model', None) or getattr(llm, 'model_id', '?')}",
            f"백엔드: {type(llm).__name__} ({where})",
        ]
    lines += [
        f"OS: {platform.system()} {platform.release()}",
        f"CPU: {_cpu_name()}",
        f"GPU: {_gpu_name()}",
        f"Python: {platform.python_version()}",
    ]
    return "\n".join(lines)
