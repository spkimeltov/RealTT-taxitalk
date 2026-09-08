"""vLLM 스트리밍 번역.

화면에만 표시하고 음성으로 읽지 않으므로 문장 단위로 잘라 넘길 이유가 없다.
델타를 받는 대로 그대로 흘려보내 자막이 타자 치듯 채워지게 한다.

프롬프트는 성형외과·피부과 대화에 맞췄다. 시술명·비용·기간·주의사항이 틀리면
안 되는 자리라, 원문에 나온 전문용어는 대응표로 못 박아 보낸다.
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
    def _system_prompt(self, src: str, dst: str, terms: Sequence[tuple[str, str]]) -> str:
        source, target = lang_name(src), lang_name(dst)
        lines = [
            f"You are a medical interpreter at a Korean plastic surgery and dermatology "
            f"clinic. Translate {source} speech into {target}.",
            "Rules:",
            f"- Output ONLY the {target} translation. No explanations, no notes, "
            f"no romanization, no quotation marks around the whole output, "
            f"and never repeat the source text.",
            "- Keep every number exact: prices, dosages, units, dates, durations, "
            "session counts and percentages.",
            "- Preserve the speaker's tone and level of politeness. Staff speech should "
            "stay courteous and professional; patient speech should stay natural.",
            "- Translate medical and cosmetic procedure names with the standard term used "
            "in the destination language, not a literal word-by-word rendering.",
            "- The input comes from speech recognition and may contain small errors; "
            "translate the intended meaning rather than the literal noise.",
            "- Do not give medical advice, add warnings, or soften what was said. "
            "Convey exactly what the speaker stated.",
            f"- Write natural {target} as it would be spoken in a clinic consultation.",
        ]
        if terms:
            pairs = "\n".join(f"- {src_form} -> {dst_form}" for src_form, dst_form in terms)
            lines.append(
                "Use exactly these terms for the clinic vocabulary that appears in the "
                f"input:\n{pairs}"
            )
        return "\n".join(lines)

    def _messages(
        self,
        text: str,
        src: str,
        dst: str,
        history: Sequence[tuple[str, str]],
        terms: Sequence[tuple[str, str]],
    ) -> list[dict]:
        system = self._system_prompt(src, dst, terms)
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
    ) -> AsyncIterator[str]:
        """번역 델타를 순서대로 yield 한다."""
        hist = list(history)
        term_list = list(terms)
        try:
            async for piece in self._stream_once(text, src, dst, hist, term_list):
                yield piece
            return
        except TranslateError as exc:
            if self._merge_system or "system" not in str(exc).lower():
                raise
            log.warning("system 롤 미지원으로 판단, user 메시지 병합으로 재시도: %s", exc)
            self._merge_system = True

        async for piece in self._stream_once(text, src, dst, hist, term_list):
            yield piece

    async def _stream_once(
        self,
        text: str,
        src: str,
        dst: str,
        history: Sequence[tuple[str, str]],
        terms: Sequence[tuple[str, str]],
    ) -> AsyncIterator[str]:
        url = f"{self._s.vllm_base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._s.vllm_api_key}"}
        payload = self._payload(self._messages(text, src, dst, history, terms))

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
