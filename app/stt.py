"""서비스 전용 faster-whisper STT 엔진과 2택 언어 판별.

서버의 공용 STT(`stt_main.py`)를 쓰지 않는 이유는 interpreter 와 같다. 언어가
`ko` 로 고정돼 있고, 동기 `transcribe()` 를 async 핸들러에서 직접 호출해 이벤트
루프를 막는다. 여기서는 스레드 풀에 오프로딩하고 언어를 요청마다 지정한다.

여기에 더해 마이크 1개 모드를 위한 언어 판별이 있다. whisper 의 전체 자동 감지는
후보가 99개라 짧은 발화에서 엉뚱한 언어로 새기 쉬우므로, 확률 분포를 받아
**한국어와 고객 언어 둘 중에서만** 고른다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from .config import SAMPLE_RATE, Settings

log = logging.getLogger(__name__)


def resolve_model(name: str, cache_dirs: tuple[str, ...]) -> str:
    """모델 이름을 이미 내려받아 둔 스냅샷 경로로 바꿔준다.

    이 서버의 공용 STT 컨테이너는 `Systran/faster-whisper-large-v3` 가중치를 이미
    갖고 있다. 그 디렉터리를 읽기 전용으로 마운트해 두면 여기서 찾아 바로 로드하므로
    같은 모델을 다시 내려받지 않는다. 캐시에 없으면 이름을 그대로 돌려줘 평소처럼
    HF 에서 받아오게 한다.
    """
    if Path(name).is_dir():
        return name

    try:
        from faster_whisper.utils import _MODELS
    except ImportError:
        _MODELS = {}
    repo = _MODELS.get(name, name)
    if "/" not in repo:
        return name

    folder = "models--" + repo.replace("/", "--")
    for root in cache_dirs:
        snapshots = Path(root) / "hub" / folder / "snapshots"
        if not snapshots.is_dir():
            continue
        for snapshot in sorted(snapshots.iterdir()):
            if (snapshot / "model.bin").exists():
                log.info("캐시된 가중치 사용: %s → %s", name, snapshot)
                return str(snapshot)
    return name


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """리틀엔디언 PCM16 바이트열을 whisper 입력용 float32 [-1, 1] 배열로 변환."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class SttEngine:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._model: Any = None
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.stt_workers), thread_name_prefix="stt"
        )

    # ------------------------------------------------------------------ 로딩
    def load(self) -> None:
        """블로킹 모델 로드. 기동 시 스레드로 감싸 호출한다."""
        from faster_whisper import WhisperModel

        s = self._s
        started = time.perf_counter()
        source = resolve_model(s.whisper_model, s.whisper_cache_dirs)
        log.info(
            "whisper 로드: model=%s source=%s device=%s:%s compute=%s workers=%s",
            s.whisper_model,
            source,
            s.whisper_device,
            s.whisper_device_index,
            s.whisper_compute_type,
            s.stt_workers,
        )
        self._model = WhisperModel(
            source,
            device=s.whisper_device,
            device_index=s.whisper_device_index,
            compute_type=s.whisper_compute_type,
            num_workers=max(1, s.stt_workers),
        )
        log.info("whisper 로드 완료 (%.1fs)", time.perf_counter() - started)

    def warmup(self) -> None:
        """첫 발화가 모델 준비 비용을 뒤집어쓰지 않게 더미 추론을 한 번 돌린다."""
        if self._model is None:
            return
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        try:
            segments, _ = self._model.transcribe(silence, language="ko", beam_size=1)
            list(segments)
            log.info("whisper 프리웜 완료")
        except Exception:
            log.warning("whisper 프리웜 실패(무시)", exc_info=True)

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def model_name(self) -> str:
        return self._s.whisper_model

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -------------------------------------------------------------- 언어 판별
    def _detect_sync(self, audio: np.ndarray, candidates: Sequence[str]) -> dict:
        detector = getattr(self._model, "detect_language", None)
        if detector is not None:
            _, _, all_probs = detector(audio)
            probs = {code: float(prob) for code, prob in (all_probs or [])}
        else:
            # 아주 오래된 faster-whisper 를 위한 폴백. 전사를 한 번 돌려 분포만 꺼낸다.
            segments, info = self._model.transcribe(audio, language=None, beam_size=1)
            list(segments)
            probs = {code: float(prob) for code, prob in (info.all_language_probs or [])}

        scored = sorted(
            ((code, probs.get(code, 0.0)) for code in candidates),
            key=lambda item: item[1],
            reverse=True,
        )
        best, best_prob = scored[0]
        runner_prob = scored[1][1] if len(scored) > 1 else 0.0
        return {
            "language": best,
            "probability": round(best_prob, 3),
            "margin": round(best_prob - runner_prob, 3),
            "probs": {code: round(prob, 3) for code, prob in scored},
        }

    async def detect_language(self, pcm: bytes, candidates: Sequence[str]) -> Optional[dict]:
        """후보 언어 중에서만 고른다. 판단할 만큼 오디오가 없으면 `None`.

        `margin` 은 1위와 2위의 확률 차다. 호출한 쪽이 이 값으로 판별을 믿을지
        직전 화자의 반대편으로 넘길지 정한다.
        """
        if self._model is None:
            raise RuntimeError("stt_not_ready")
        if not candidates:
            return None

        audio = pcm16_to_float32(pcm)
        if len(audio) / SAMPLE_RATE < self._s.lid_min_sec:
            return None

        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._pool, self._detect_sync, audio, list(candidates))
        result["ms"] = int((time.perf_counter() - started) * 1000)
        return result

    # ------------------------------------------------------------------ 전사
    def _transcribe_sync(
        self,
        audio: np.ndarray,
        language: Optional[str],
        beam_size: int,
        initial_prompt: Optional[str],
    ) -> dict:
        segments, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt or None,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            without_timestamps=True,
        )
        text = "".join(seg.text for seg in segments).strip()
        return {
            "text": text,
            "language": info.language,
            "language_probability": round(float(info.language_probability or 0.0), 3),
            "duration": round(float(info.duration or 0.0), 2),
        }

    async def transcribe(
        self,
        pcm: bytes,
        language: Optional[str] = None,
        *,
        final: bool = True,
        initial_prompt: Optional[str] = None,
    ) -> dict:
        """PCM16 버퍼를 전사한다. `language=None` 이면 whisper 자동 감지.

        중간 자막(`final=False`)은 beam=1 로 값을 싸게 뽑고, 확정 결과는 beam=5 를 쓴다.
        `initial_prompt` 로 전문용어를 넣으면 해당 어휘 쪽으로 인식이 기운다.
        """
        if self._model is None:
            raise RuntimeError("stt_not_ready")

        audio = pcm16_to_float32(pcm)
        seconds = len(audio) / SAMPLE_RATE
        if seconds < self._s.min_utterance_sec:
            return {"text": "", "language": language or "", "duration": round(seconds, 2), "ms": 0}

        beam = self._s.stt_beam_size if final else self._s.stt_beam_size_partial
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._pool, self._transcribe_sync, audio, language, beam, initial_prompt
        )
        result["ms"] = int((time.perf_counter() - started) * 1000)
        return result
