"""WebSocket 대화 세션.

브라우저가 항상 마이크를 열어 두고, 말이 시작되고 끝나는 지점을 VAD 로 잡아
`start` / `end` 를 보낸다. 서버는 그 사이에 도착한 PCM 을 모아 한 "발화"로 처리한다.

    [VAD 감지] start ─ 오디오 프레임 … ─ [무음 700ms] end
                         │                        │
                         └ partial 자막            └ 언어 확정 → 전사 → 용어 보정
                                                     → translation_delta*
                                                     → turn_done → 기록 저장

interpreter 와 다른 점이 둘 있다.

* 턴 슬롯이 채널별로 있다. 마이크 2개 모드에서는 기사와 탑승객이 동시에 말할 수
  있고, 그때 한쪽을 버리면 대화가 끊긴다.
* 마이크 1개 모드에서는 화자를 언어로 가른다. 판별이 끝나야 어느 쪽 말인지 알 수
  있으므로, 발화 도중 처음 판별에 성공한 시점에 `turn_speaker` 를 따로 내려보내
  화면이 말풍선을 제자리로 옮기게 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .config import (
    BYTES_PER_SEC,
    MIC_SINGLE,
    SIDE_BY_CODE,
    SIDE_PATIENT,
    SIDE_SHARED,
    SIDE_STAFF,
    STAFF_LANG,
    Settings,
)
from .dialect import Dialect
from .glossary import Glossary
from .storage import SessionStore, turn_entry
from .stt import SttEngine
from .translate import Translator, strip_wrapping_quotes

log = logging.getLogger(__name__)

# 오디오 프레임 머리말: [채널 코드][패딩]. 패딩 덕분에 PCM16 본문이 2바이트 경계에
# 정렬된 채로 남는다.
HEADER_BYTES = 2


@dataclass
class Turn:
    id: int
    side: str
    started_at: float
    pcm: bytearray = field(default_factory=bytearray)
    ended_at: Optional[float] = None
    truncated: bool = False
    # 화자와 언어. 마이크 2개 모드에서는 시작할 때 정해지고, 1개 모드에서는
    # 언어 판별에 성공한 시점에 채워진다.
    speaker: Optional[str] = None
    src: Optional[str] = None
    dst: Optional[str] = None
    lid: Optional[dict] = None
    partial_task: Optional[asyncio.Task] = None
    last_partial_at: float = 0.0

    @property
    def resolved(self) -> bool:
        return self.src is not None


class ConsultSession:
    def __init__(
        self,
        ws: WebSocket,
        *,
        stt: SttEngine,
        translator: Translator,
        glossary: Glossary,
        dialect: Dialect,
        store: SessionStore,
        settings: Settings,
        meta: dict,
    ) -> None:
        self._ws = ws
        self._stt = stt
        self._translator = translator
        self._glossary = glossary
        self._dialect = dialect
        self._store = store
        self._s = settings

        self._session_id = str(meta["id"])
        self._patient_lang = str(meta["patient_lang"])
        self._mic_mode = str(meta.get("mic_mode") or settings.mic_mode)
        self._candidates = (STAFF_LANG, self._patient_lang)

        self._send_lock = asyncio.Lock()
        self._slots: dict[str, Turn] = {}
        self._pipelines: set[asyncio.Task] = set()
        self._turn_seq = 0
        self._recorded = 0
        # 방향별로 직전 대화를 따로 기억한다. 섞으면 모델이 출력 언어를 헷갈린다.
        self._history: dict[str, list[tuple[str, str]]] = {}
        self._max_bytes = int(self._s.max_utterance_sec * BYTES_PER_SEC)

    # ------------------------------------------------------------------ 루프
    async def run(self) -> None:
        await self._send(
            {
                "type": "ready",
                "session": self._session_id,
                "patient_lang": self._patient_lang,
                "mic_mode": self._mic_mode,
                "sample_rate": 16000,
                "encoding": "pcm_s16le",
                "channels": 1,
                "stt_model": self._stt.model_name,
                "mt_model": self._s.vllm_model,
                "glossary_terms": len(self._glossary),
            }
        )
        try:
            while True:
                message = await self._ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                payload = message.get("bytes")
                if payload is not None:
                    await self._on_audio(payload)
                    continue
                text = message.get("text")
                if text is not None:
                    await self._on_text(text)
        except WebSocketDisconnect:
            pass
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        for turn in list(self._slots.values()):
            self._cancel_partial(turn)
        self._slots.clear()
        for task in list(self._pipelines):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._pipelines.clear()

    # -------------------------------------------------------------- 수신 처리
    async def _on_text(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._send({"type": "error", "message": "invalid_json"})
            return

        kind = data.get("type")
        if kind == "start":
            await self._start_turn(data)
        elif kind == "end":
            await self._end_turn(self._side_of(data))
        elif kind == "cancel":
            await self._cancel_turn(self._side_of(data))
        elif kind == "reset":
            self._history.clear()
            await self._send({"type": "reset"})
        elif kind == "ping":
            await self._send({"type": "pong", "t": data.get("t")})
        else:
            await self._send({"type": "error", "message": f"unknown_type:{kind}"})

    def _side_of(self, data: dict) -> str:
        side = str(data.get("side") or "").lower()
        if side in (SIDE_STAFF, SIDE_PATIENT, SIDE_SHARED):
            return side
        return SIDE_SHARED

    async def _on_audio(self, frame: bytes) -> None:
        if len(frame) <= HEADER_BYTES:
            return
        side = SIDE_BY_CODE.get(frame[0], SIDE_SHARED)
        turn = self._slots.get(side)
        if turn is None or turn.ended_at is not None:
            return

        if len(turn.pcm) >= self._max_bytes:
            if not turn.truncated:
                turn.truncated = True
                await self._send_safe(
                    {
                        "type": "warning",
                        "turn": turn.id,
                        "code": "max_utterance_reached",
                        "message": f"최대 {int(self._s.max_utterance_sec)}초까지만 인식합니다.",
                    }
                )
                # 브라우저 VAD 가 멈춘 경우를 대비한 안전판. 여기서 끊지 않으면
                # 메모리가 계속 늘고 발화가 영영 끝나지 않는다.
                await self._end_turn(side)
            return

        turn.pcm.extend(frame[HEADER_BYTES:])
        await self._maybe_partial(turn)

    # -------------------------------------------------------------------- 턴
    async def _start_turn(self, data: dict) -> None:
        side = self._side_of(data)
        # 같은 채널에서 이전 발화가 아직 열려 있으면 닫고 넘긴다. VAD 가 end 를
        # 놓쳤을 때 새 발화가 앞 발화에 섞이지 않게 한다.
        existing = self._slots.get(side)
        if existing is not None and existing.ended_at is None:
            await self._end_turn(side)

        self._turn_seq += 1
        turn = Turn(id=self._turn_seq, side=side, started_at=time.perf_counter())
        turn.last_partial_at = turn.started_at

        if side in (SIDE_STAFF, SIDE_PATIENT):
            # 마이크 2개 모드는 채널이 곧 화자다. 판별할 것이 없다.
            self._assign(turn, SIDE_STAFF if side == SIDE_STAFF else SIDE_PATIENT)

        self._slots[side] = turn
        await self._send(
            {
                "type": "turn_start",
                "turn": turn.id,
                "side": side,
                "speaker": turn.speaker,
                "src": turn.src,
                "dst": turn.dst,
            }
        )

    def _assign(self, turn: Turn, speaker: str) -> None:
        turn.speaker = speaker
        if speaker == SIDE_STAFF:
            turn.src, turn.dst = STAFF_LANG, self._patient_lang
        else:
            turn.src, turn.dst = self._patient_lang, STAFF_LANG

    async def _end_turn(self, side: str) -> None:
        turn = self._slots.get(side)
        if turn is None or turn.ended_at is not None:
            return
        turn.ended_at = time.perf_counter()
        self._slots.pop(side, None)
        self._cancel_partial(turn)

        task = asyncio.create_task(self._run_pipeline(turn))
        self._pipelines.add(task)
        task.add_done_callback(self._pipelines.discard)

    async def _cancel_turn(self, side: str) -> None:
        turn = self._slots.pop(side, None)
        if turn is None:
            return
        turn.ended_at = time.perf_counter()
        self._cancel_partial(turn)
        await self._send_safe({"type": "cancelled", "turn": turn.id})

    @staticmethod
    def _cancel_partial(turn: Turn) -> None:
        if turn.partial_task is not None and not turn.partial_task.done():
            turn.partial_task.cancel()
        turn.partial_task = None

    # ------------------------------------------------------------ STT 프롬프트
    def _stt_prompt(self, lang: str) -> str:
        """whisper 에 미리 들려줄 한 줄. 용어집 뒤에 사투리 예시를 붙인다.

        사투리는 기사(한국어) 쪽에만 붙인다. 탑승객 언어 전사에 넣으면 엉뚱한
        한국어 토큰이 섞일 뿐이다.
        """
        prompt = self._glossary.initial_prompt(lang, self._s.glossary_prompt_terms)
        if lang == STAFF_LANG:
            hint = self._dialect.prompt(self._s.dialect_prompt_words)
            if hint:
                prompt = f"{prompt} {hint}" if prompt else hint
        return prompt

    # -------------------------------------------------------------- 중간 자막
    async def _maybe_partial(self, turn: Turn) -> None:
        interval = self._s.partial_interval_sec
        if interval <= 0:
            return
        if turn.partial_task is not None and not turn.partial_task.done():
            return
        now = time.perf_counter()
        if now - turn.last_partial_at < interval:
            return
        turn.last_partial_at = now
        turn.partial_task = asyncio.create_task(self._run_partial(turn, bytes(turn.pcm)))

    async def _run_partial(self, turn: Turn, pcm: bytes) -> None:
        try:
            if not turn.resolved:
                # 화자를 모르는 채로는 자막을 어느 쪽에 붙일지 정할 수 없다.
                # 먼저 언어를 확정하고, 그 결과를 화면에 알린다.
                if not await self._resolve_language(turn, pcm, announce=True):
                    return

            result = await self._stt.transcribe(
                pcm,
                turn.src,
                final=False,
                initial_prompt=self._stt_prompt(turn.src or STAFF_LANG),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("중간 전사 실패(무시)", exc_info=True)
            return

        if turn.ended_at is not None:
            # 늦게 도착한 중간 결과가 확정 자막을 덮어쓰지 않게 버린다.
            return
        text = (result.get("text") or "").strip()
        if not text:
            return
        await self._send_safe(
            {
                "type": "partial",
                "turn": turn.id,
                "speaker": turn.speaker,
                "lang": turn.src,
                "text": text,
            }
        )

    # -------------------------------------------------------------- 언어 판별
    async def _resolve_language(self, turn: Turn, pcm: bytes, *, announce: bool) -> bool:
        """중간 자막을 어느 쪽에 어느 언어로 띄울지 임시로 정한다.

        여기서 내리는 결정은 잠정적이다. 발화가 끝나면 `_run_pipeline` 이 후보 언어로
        각각 전사해 보고 점수로 화자를 확정하며, 그 결과가 이 추정을 덮는다. 판별이
        애매해도 확률 1위를 그대로 쓴다. 예전에는 직전 화자의 반대편으로 넘겼지만,
        그렇게 뒤집힌 발화에 엉뚱한 언어가 강제되면 whisper 가 상용구를 지어냈다.
        """
        if turn.resolved:
            return True

        detection = await self._stt.detect_language(pcm, self._candidates)
        if detection is None:
            return False

        language = str(detection["language"])
        detection["provisional"] = True
        if detection["margin"] < self._s.lid_margin:
            # 확정 단계에서 뒤집힐 가능성이 높다는 표시. 판단 자체는 바꾸지 않는다.
            detection["low_margin"] = True

        self._assign(turn, SIDE_STAFF if language == STAFF_LANG else SIDE_PATIENT)
        turn.lid = detection
        log.info(
            "턴 %s 화자 추정: %s (%s, p=%s, margin=%s%s)",
            turn.id,
            turn.speaker,
            language,
            detection.get("probability"),
            detection.get("margin"),
            ", 판별 애매" if detection.get("low_margin") else "",
        )
        if announce:
            await self._send_safe(
                {
                    "type": "turn_speaker",
                    "turn": turn.id,
                    "speaker": turn.speaker,
                    "src": turn.src,
                    "dst": turn.dst,
                    "detection": detection,
                }
            )
        return True

    # ------------------------------------------------------------ 파이프라인
    async def _run_pipeline(self, turn: Turn) -> None:
        t_end = turn.ended_at or time.perf_counter()
        pcm = bytes(turn.pcm)
        metrics: dict = {"audio_sec": round(len(pcm) / BYTES_PER_SEC, 2)}
        try:
            if self._mic_mode == MIC_SINGLE:
                # 채널이 화자를 알려주지 않는 모드다. 언어 판별만 믿고 한쪽 언어를
                # 강제하면, 틀렸을 때 whisper 가 상용구를 지어내 그것이 그대로 번역돼
                # 올라간다. 후보 언어로 각각 전사해 보고 점수가 높은 쪽을 택한다.
                stt_result = await self._stt.transcribe_best(
                    pcm,
                    self._candidates,
                    prompts={lang: self._stt_prompt(lang) for lang in self._candidates},
                )
                chosen = str(stt_result.get("language") or STAFF_LANG)
                self._assign(turn, SIDE_STAFF if chosen == STAFF_LANG else SIDE_PATIENT)
                metrics["stt_scores"] = stt_result.get("scores")
                metrics["stt_score_margin"] = stt_result.get("score_margin")
                src, dst = turn.src or STAFF_LANG, turn.dst or self._patient_lang
                log.info(
                    "턴 %s 화자 확정: %s (%s, 점수 %s, 차 %s)",
                    turn.id,
                    turn.speaker,
                    chosen,
                    stt_result.get("scores"),
                    stt_result.get("score_margin"),
                )
            else:
                # 마이크 2개 모드는 채널이 곧 화자라 전사할 언어가 이미 정해져 있다.
                src = turn.src or STAFF_LANG
                dst = turn.dst or self._patient_lang
                stt_result = await self._stt.transcribe(
                    pcm,
                    src,
                    final=True,
                    initial_prompt=self._stt_prompt(src),
                )
            metrics["stt_ms"] = stt_result.get("ms")
            source_text = self._glossary.canonicalize(
                (stt_result.get("text") or "").strip(), src
            )

            if not source_text:
                await self._send_safe({"type": "empty", "turn": turn.id})
                return

            await self._send(
                {
                    "type": "transcript",
                    "turn": turn.id,
                    "speaker": turn.speaker,
                    "src": src,
                    "dst": dst,
                    "text": source_text,
                    "stt_ms": stt_result.get("ms"),
                }
            )

            terms = self._glossary.match(source_text, src, dst)
            if terms:
                metrics["terms"] = [f"{a}->{b}" for a, b in terms]

            # 사투리 뜻풀이는 한국어 발화에만 붙인다. 탑승객 언어에는 해당이 없다.
            notes = (
                self._dialect.notes(source_text, self._s.dialect_max_notes)
                if src == STAFF_LANG
                else []
            )
            if notes:
                metrics["dialect"] = len(notes)

            translation = await self._stream_translation(
                turn, source_text, src, dst, terms, notes, metrics, t_end
            )

            await self._send(
                {
                    "type": "translation_done",
                    "turn": turn.id,
                    "lang": dst,
                    "text": translation,
                }
            )
            self._remember(src, dst, source_text, translation)

            metrics["total_ms"] = int((time.perf_counter() - t_end) * 1000)
            await self._record(turn, src, dst, source_text, translation, metrics)
            await self._send({"type": "turn_done", "turn": turn.id, "metrics": metrics})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("턴 %s 처리 실패", turn.id)
            await self._send_safe(
                {"type": "error", "turn": turn.id, "message": f"{type(exc).__name__}: {exc}"}
            )

    async def _stream_translation(
        self,
        turn: Turn,
        source_text: str,
        src: str,
        dst: str,
        terms: list[tuple[str, str]],
        dialect_notes: list[str],
        metrics: dict,
        t_end: float,
    ) -> str:
        parts: list[str] = []
        first = True
        async for delta in self._translator.stream(
            source_text,
            src,
            dst,
            self._history_for(src, dst),
            terms,
            dialect_enabled=bool(self._dialect),
            dialect_notes=dialect_notes,
        ):
            if first:
                first = False
                metrics["mt_first_token_ms"] = int((time.perf_counter() - t_end) * 1000)
            parts.append(delta)
            await self._send({"type": "translation_delta", "turn": turn.id, "text": delta})
        metrics["mt_ms"] = int((time.perf_counter() - t_end) * 1000)
        return strip_wrapping_quotes("".join(parts))

    # ------------------------------------------------------------------ 기록
    async def _record(
        self,
        turn: Turn,
        src: str,
        dst: str,
        original: str,
        translated: str,
        metrics: dict,
    ) -> None:
        self._recorded += 1
        entry = turn_entry(
            seq=self._recorded,
            speaker=turn.speaker or SIDE_STAFF,
            src=src,
            dst=dst,
            original=original,
            translated=translated,
            metrics=metrics,
        )
        try:
            await self._store.append_turn(self._session_id, entry)
        except Exception:  # noqa: BLE001
            # 기록이 실패해도 대화는 계속돼야 한다.
            log.exception("대화기록 저장 실패: %s", self._session_id)

    # ------------------------------------------------------------------ 이력
    def _history_for(self, src: str, dst: str) -> list[tuple[str, str]]:
        return self._history.get(f"{src}>{dst}", [])

    def _remember(self, src: str, dst: str, source_text: str, translation: str) -> None:
        if not source_text or not translation:
            return
        key = f"{src}>{dst}"
        bucket = self._history.setdefault(key, [])
        bucket.append((source_text, translation))
        del bucket[: -self._s.history_turns]

    # ------------------------------------------------------------------ 송신
    async def _send(self, payload: dict) -> None:
        async with self._send_lock:
            await self._ws.send_json(payload)

    async def _send_safe(self, payload: dict) -> None:
        with contextlib.suppress(Exception):
            await self._send(payload)
