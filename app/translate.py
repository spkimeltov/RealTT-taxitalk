"""vLLM 스트리밍 번역.

화면에만 표시하고 음성으로 읽지 않으므로 문장 단위로 잘라 넘길 이유가 없다.
델타를 받는 대로 그대로 흘려보내 자막이 타자 치듯 채워지게 한다.

프롬프트는 부산 관광투어 안내에 맞췄다. 지명·요금·시간이 틀리면 안 되는 자리이고
특히 지명은 모델에 맡기면 매번 다른 음차가 나오므로, 원문에 나온 용어는 대응표로
못 박아 보낸다.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Iterable, Sequence

import httpx

from .config import Settings, lang_name

log = logging.getLogger(__name__)

_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』"))


def strip_wrapping_quotes(text: str) -> str:
    """모델이 번역문 전체를 인용부호로 감싸는 경우가 있어 한 겹만 벗긴다."""
    t = text.strip()
    for open_q, close_q in _QUOTE_PAIRS:
        if len(t) >= 2 and t.startswith(open_q) and t.endswith(close_q):
            inner = t[1:-1].strip()
            if open_q not in inner and close_q not in inner:
                return inner
    return t


class TranslateError(RuntimeError):
    pass


class Translator:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.vllm_timeout_sec, connect=10.0)
        )
        # 일부 chat template 은 system 롤을 거부한다. 한 번 실패하면 지시문을 user
        # 메시지에 합쳐 보내는 방식으로 자동 전환한다.
        self._merge_system = False

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- 프롬프트
    def _system_prompt(
        self,
        src: str,
        dst: str,
        terms: Sequence[tuple[str, str]],
        dialect_enabled: bool = False,
        dialect_notes: Sequence[str] = (),
    ) -> str:
        source, target = lang_name(src), lang_name(dst)
        lines = [
            f"You are an interpreter on a sightseeing tour in Busan, South Korea, "
            f"working between a Korean tour driver-guide and a foreign traveller. "
            f"Translate {source} speech into {target}.",
            "Rules:",
            f"- Output ONLY the {target} translation. No explanations, no notes, "
            f"no romanization, no quotation marks around the whole output, "
            f"and never repeat the source text.",
            "- Keep every number exact: fares, prices, distances, times, dates, "
            "durations, headcounts and percentages.",
            "- Preserve the speaker's tone and level of politeness. The guide's speech "
            "should stay courteous and professional; the traveller's should stay natural.",
            "- Render Korean place, station, hotel, market and shop names with the "
            "established name used in the destination language. When there is no "
            "established name, use the official romanization instead of translating "
            "the name word by word.",
            "- The input comes from speech recognition and may contain small errors; "
            "translate the intended meaning rather than the literal noise.",
            "- Do not add sightseeing commentary, recommendations or warnings of your "
            "own, and do not soften what was said. Convey exactly what the speaker stated.",
            f"- Write natural {target} as it would be spoken in a taxi or on a guided tour.",
        ]
        if src == "ko" and dialect_enabled:
            # 기사·가이드는 대개 부산 사람이다. 사투리를 표준어로 옮기려 들면 그 자체가
            # 번역이 되어 뜻이 한 겹 더 멀어지므로, 알아듣고 곧장 대상 언어로 옮기게 한다.
            #
            # 방언을 껐을 때(`KO_DIALECT=off`) 이 문단이 남으면 표준어만 오는 현장에서
            # 없는 사투리를 찾으라는 지시가 매 발화에 붙는다. 스위치 하나로 세 층
            # (whisper 프롬프트 · 뜻풀이 주석 · 이 규칙)이 함께 꺼지게 묶어 둔다.
            lines.append(
                "- The Korean speaker is a local from Busan and may speak the Gyeongsang "
                "(Busan) dialect. Understand the dialect and render its meaning directly "
                f"in standard {target}. Do not restate it in standard Korean, do not "
                "translate dialect endings literally, and do not treat unfamiliar dialect "
                "words as speech recognition errors."
            )
        if terms:
            pairs = "\n".join(f"- {src_form} -> {dst_form}" for src_form, dst_form in terms)
            lines.append(
                "Use exactly these renderings for the place names and tour vocabulary "
                f"that appear in the input:\n{pairs}"
            )
        if dialect_notes:
            notes = "\n".join(f"- {note}" for note in dialect_notes)
            lines.append(
                "The input contains Gyeongsang dialect. These notes give the standard "
                f"Korean meaning of what appeared; translate accordingly:\n{notes}"
            )
        return "\n".join(lines)

    def _messages(
        self,
        text: str,
        src: str,
        dst: str,
        history: Sequence[tuple[str, str]],
        terms: Sequence[tuple[str, str]],
        dialect_enabled: bool = False,
        dialect_notes: Sequence[str] = (),
    ) -> list[dict]:
        system = self._system_prompt(src, dst, terms, dialect_enabled, dialect_notes)
        messages: list[dict] = []
        if not self._merge_system:
            messages.append({"role": "system", "content": system})

        for source_text, translated in history[-self._s.history_turns :]:
            if source_text and translated:
                messages.append({"role": "user", "content": source_text})
                messages.append({"role": "assistant", "content": translated})

        user = text
        if self._merge_system and not history:
            user = f"{system}\n\n---\n{text}"
        elif self._merge_system:
            user = f"[{lang_name(src)} → {lang_name(dst)}]\n{text}"
        messages.append({"role": "user", "content": user})
        return messages

    def _payload(self, messages: list[dict]) -> dict:
        return {
            "model": self._s.vllm_model,
            "messages": messages,
            "stream": True,
            "temperature": self._s.vllm_temperature,
            "max_tokens": self._s.vllm_max_tokens,
        }

    # ------------------------------------------------------------------ 스트림
    async def stream(
        self,
        text: str,
        src: str,
        dst: str,
        history: Iterable[tuple[str, str]] = (),
        terms: Iterable[tuple[str, str]] = (),
        dialect_enabled: bool = False,
        dialect_notes: Iterable[str] = (),
    ) -> AsyncIterator[str]:
        """번역 델타를 순서대로 yield 한다."""
        hist = list(history)
        term_list = list(terms)
        notes = list(dialect_notes)
        try:
            async for piece in self._stream_once(
                text, src, dst, hist, term_list, dialect_enabled, notes
            ):
                yield piece
            return
        except TranslateError as exc:
            if self._merge_system or "system" not in str(exc).lower():
                raise
            log.warning("system 롤 미지원으로 판단, user 메시지 병합으로 재시도: %s", exc)
            self._merge_system = True

        async for piece in self._stream_once(
            text, src, dst, hist, term_list, dialect_enabled, notes
        ):
            yield piece

    async def _stream_once(
        self,
        text: str,
        src: str,
        dst: str,
        history: Sequence[tuple[str, str]],
        terms: Sequence[tuple[str, str]],
        dialect_enabled: bool = False,
        dialect_notes: Sequence[str] = (),
    ) -> AsyncIterator[str]:
        url = f"{self._s.vllm_base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._s.vllm_api_key}"}
        payload = self._payload(
            self._messages(text, src, dst, history, terms, dialect_enabled, dialect_notes)
        )

        async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise TranslateError(f"vllm {resp.status_code}: {body[:400]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        return
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece
