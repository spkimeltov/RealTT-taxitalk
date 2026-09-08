"""성형외과·피부과 전문용어 사전.

범용 STT·번역 모델을 그대로 쓰면서 이 분야 용어만 정확히 처리하기 위한 장치다.
같은 사전을 세 군데에 쓴다.

1. `initial_prompt()` — whisper 에 용어 목록을 미리 들려줘 인식을 그쪽으로 기울인다.
2. `match()` — 원문에 실제로 나온 용어만 골라 번역 프롬프트에 대응표로 붙인다.
3. `canonicalize()` — `울세라` 처럼 굳어진 오인식을 확정 표기로 되돌린다.

용어를 늘릴 때는 `data/glossary/terms.json` 만 고치면 된다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .config import LATIN_SCRIPT_LANGS

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "glossary" / "terms.json"

# 라틴 문자 언어는 단어 경계를 봐야 한다. `pore` 가 `important` 안에서 잡히면 곤란하다.
# 한국어·중국어·일본어는 띄어쓰기가 경계 노릇을 못 하므로 부분 문자열로 찾는다.
_LATIN_LANGS = LATIN_SCRIPT_LANGS

# whisper initial_prompt 앞에 붙일 문맥 한 줄. 목록만 나열하는 것보다 인식이 안정적이다.
# 용어집이 채워진 언어만 있으면 된다. 나머지는 영어 문장으로 대체한다.
_PROMPT_LEAD = {
    "ko": "성형외과·피부과 대화입니다. 다음 용어가 자주 나옵니다:",
    "en": "A plastic surgery and dermatology consultation. Common terms:",
    "zh": "整形外科与皮肤科咨询。常见术语：",
    "yue": "整形外科同皮膚科諮詢。常見術語：",
    "ja": "美容外科・皮膚科のカウンセリングです。よく出る用語:",
    "vi": "Tư vấn phẫu thuật thẩm mỹ và da liễu. Các thuật ngữ thường gặp:",
    "ru": "Консультация по пластической хирургии и дерматологии. Частые термины:",
    "th": "การให้คำปรึกษาด้านศัลยกรรมตกแต่งและผิวหนัง คำศัพท์ที่พบบ่อย:",
    "mn": "Гоо сайхны мэс засал, арьсны эмчилгээний зөвлөгөө. Түгээмэл нэр томьёо:",
    "uz": "Plastik jarrohlik va dermatologiya maslahati. Keng tarqalgan atamalar:",
    "ar": "استشارة في جراحة التجميل والأمراض الجلدية. المصطلحات الشائعة:",
    "es": "Consulta de cirugía plástica y dermatología. Términos frecuentes:",
    "id": "Konsultasi bedah plastik dan dermatologi. Istilah yang sering muncul:",
}


@dataclass(frozen=True)
class Term:
    id: str
    priority: int
    forms: dict[str, str]
    alias: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def form(self, lang: str) -> Optional[str]:
        return self.forms.get(lang)


class Glossary:
    def __init__(self, terms: Iterable[Term], source: str = "") -> None:
        self._terms: tuple[Term, ...] = tuple(terms)
        self.source = source
        self._matchers: dict[str, tuple[Optional[re.Pattern], dict[str, Term]]] = {}
        self._canon: dict[str, tuple[tuple[re.Pattern, str], ...]] = {}
        self._prompts: dict[tuple[str, int], str] = {}

    # ------------------------------------------------------------------ 로딩
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Glossary":
        target = Path(path) if path else DEFAULT_PATH
        if not target.is_file():
            log.warning("용어집을 찾지 못했습니다: %s (전문용어 보정 없이 동작)", target)
            return cls((), source=str(target))

        raw = json.loads(target.read_text(encoding="utf-8"))
        terms: list[Term] = []
        for entry in raw.get("terms") or []:
            forms = {
                code: value.strip()
                for code, value in (entry.get("forms") or {}).items()
                if isinstance(value, str) and value.strip()
            }
            if not forms:
                continue
            alias = {
                code: tuple(v.strip() for v in values if isinstance(v, str) and v.strip())
                for code, values in (entry.get("alias") or {}).items()
            }
            terms.append(
                Term(
                    id=str(entry.get("id") or forms.get("en") or forms.get("ko")),
                    priority=int(entry.get("priority") or 0),
                    forms=forms,
                    alias={code: values for code, values in alias.items() if values},
                )
            )
        log.info("용어집 %s개 로드: %s", len(terms), target)
        return cls(terms, source=str(target))

    def __len__(self) -> int:
        return len(self._terms)

    @property
    def languages(self) -> tuple[str, ...]:
        codes: set[str] = set()
        for term in self._terms:
            codes.update(term.forms)
        return tuple(sorted(codes))

    # -------------------------------------------------------------- 내부 색인
    @staticmethod
    def _fold(text: str, lang: str) -> str:
        return text.lower() if lang in _LATIN_LANGS else text

    def _matcher(self, lang: str) -> tuple[Optional[re.Pattern], dict[str, Term]]:
        cached = self._matchers.get(lang)
        if cached is not None:
            return cached

        index: dict[str, Term] = {}
        for term in self._terms:
            candidates = [term.forms.get(lang), *term.alias.get(lang, ())]
            for candidate in candidates:
                if candidate:
                    index.setdefault(self._fold(candidate, lang), term)

        pattern: Optional[re.Pattern] = None
        if index:
            # 긴 표기를 먼저 놓아야 `국소마취` 가 `마취` 에 먹히지 않는다.
            needles = sorted(index, key=len, reverse=True)
            body = "|".join(re.escape(n) for n in needles)
            if lang in _LATIN_LANGS:
                pattern = re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)
            else:
                pattern = re.compile(body)

        self._matchers[lang] = (pattern, index)
        return pattern, index

    def _canon_rules(self, lang: str) -> tuple[tuple[re.Pattern, str], ...]:
        cached = self._canon.get(lang)
        if cached is not None:
            return cached

        rules: list[tuple[re.Pattern, str]] = []
        for term in self._terms:
            canonical = term.forms.get(lang)
            aliases = [a for a in term.alias.get(lang, ()) if a and a != canonical]
            if not canonical or not aliases:
                continue
            body = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
            if lang in _LATIN_LANGS:
                rules.append((re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE), canonical))
            else:
                rules.append((re.compile(body), canonical))

        result = tuple(rules)
        self._canon[lang] = result
        return result

    # -------------------------------------------------------------------- API
    def initial_prompt(self, lang: str, limit: int = 32) -> str:
        """whisper 에 들려줄 용어 목록.

        `initial_prompt` 는 224 토큰까지만 반영되므로 우선순위 높은 용어부터 자른다.
        """
        if not self._terms or limit <= 0:
            return ""
        key = (lang, limit)
        cached = self._prompts.get(key)
        if cached is not None:
            return cached

        ranked = sorted(self._terms, key=lambda t: (-t.priority, t.id))
        words = [term.forms[lang] for term in ranked if term.forms.get(lang)][:limit]
        prompt = ""
        if words:
            lead = _PROMPT_LEAD.get(lang, _PROMPT_LEAD["en"])
            prompt = f"{lead} " + ", ".join(words) + "."
        self._prompts[key] = prompt
        return prompt

    def match(self, text: str, src: str, dst: str) -> list[tuple[str, str]]:
        """원문에 나온 용어만 `(원문 표기, 대상 언어 표기)` 로 돌려준다.

        전체 사전을 프롬프트에 넣으면 토큰만 먹고 모델이 엉뚱한 용어를 끌어다 쓴다.
        실제로 나온 것만 붙인다.
        """
        if not text or not self._terms:
            return []
        pattern, index = self._matcher(src)
        if pattern is None:
            return []

        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for hit in pattern.finditer(text):
            term = index.get(self._fold(hit.group(0), src))
            if term is None or term.id in seen:
                continue
            target = term.forms.get(dst)
            if not target:
                continue
            seen.add(term.id)
            found.append((term.forms.get(src) or hit.group(0), target))
        return found

    def canonicalize(self, text: str, lang: str) -> str:
        """굳어진 오인식 표기를 확정 표기로 되돌린다."""
        if not text:
            return text
        for pattern, canonical in self._canon_rules(lang):
            # 치환문에 역슬래시가 섞여도 이스케이프로 해석되지 않도록 람다로 넘긴다.
            text = pattern.sub(lambda _m, value=canonical: value, text)
        return text
