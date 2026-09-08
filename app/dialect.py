"""방언 대응표 — whisper 프롬프트와 뜻풀이 주석.

표준어로 학습된 STT·번역 모델을 그대로 쓰면서 사투리를 알아듣게 하려는 장치다.
방언은 두 군데에서 깨진다.

* **STT** — whisper 가 사투리를 못 알아듣고 비슷한 표준어로 고쳐 받아쓴다.
  `가입시다` 가 `가입시다(加入)` 로, `왔능교` 가 `왔는데요` 로 새는 식이다.
  `prompt()` 로 사투리 표기를 미리 들려줘 그쪽으로 기울인다.
* **번역** — 받아쓰기는 맞았는데 번역 모델이 뜻을 못 잡는다. `notes()` 로 원문에
  실제로 나온 어미·어휘의 표준어 뜻만 골라 번역 프롬프트에 주석으로 붙인다.


용어집과 달리 **원문을 고쳐 쓰지 않는다.** 방언은 어미 하나로 뜻이 갈려서
(`가노`/`가나`/`가꼬` 가 다 다르다) 기계적으로 표준어로 바꾸면 말이 뒤틀린다.
그래서 뜻만 알려 주고 번역은 모델이 문장 전체를 보고 하게 한다. 덕분에 대응이
잘못 걸려도 주석 한 줄이 군더더기로 붙을 뿐, 기사가 한 말 자체는 바뀌지 않는다.

방언을 늘릴 때는 `data/dialect/<이름>.json` 만 고치면 된다.

`notes()` 로 뜻만 알려 주는 이 방식은 이제 **폴백**이다. 평소에는 `jejuma.py` 가
발화를 표준어로 옮기고 그 표준어를 번역한다. 게이트웨이가 닿지 않을 때만 여기로
되돌아온다. 그래서 경상도 말고 네 지역의 대응표는 `prompt_lead` · `prompt_words` 만
채우고 `endings`·`words` 를 비워 두었다. 받아쓰기를 사투리 표기로 붙드는 일은 어느
경로에서도 필요하지만, 뜻풀이 정규식까지 다섯 지역 분량으로 쌓을 이유는 없다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data" / "dialect"


@dataclass(frozen=True)
class Ending:
    """어미 규칙. 걸리면 뜻풀이 한 줄을 붙인다."""

    id: str
    pattern: re.Pattern
    gloss: str


@dataclass(frozen=True)
class Word:
    """어휘 규칙. 걸린 표기와 표준어 뜻을 함께 붙인다."""

    forms: tuple[str, ...]
    standard: str


class Dialect:
    """방언 대응표. `off` 일 때는 빈 객체로 두고 호출하는 쪽을 고치지 않는다."""

    def __init__(
        self,
        *,
        name: str = "",
        label: str = "",
        prompt_lead: str = "",
        prompt_words: tuple[str, ...] = (),
        endings: tuple[Ending, ...] = (),
        words: tuple[Word, ...] = (),
        source: str = "",
    ) -> None:
        self.name = name
        self.label = label
        self.source = source
        self._prompt_lead = prompt_lead
        self._prompt_words = prompt_words
        self._endings = endings
        self._words = words
        self._word_matcher: Optional[re.Pattern] = None
        self._word_index: dict[str, Word] = {}
        self._prompts: dict[int, str] = {}
        if words:
            for word in words:
                for form in word.forms:
                    self._word_index.setdefault(form, word)
            # 긴 표기를 먼저 놓아야 `어데고` 가 `어데` 에 먹히지 않는다.
            needles = sorted(self._word_index, key=len, reverse=True)
            self._word_matcher = re.compile("|".join(re.escape(n) for n in needles))

    # ------------------------------------------------------------------ 로딩
    @classmethod
    def off(cls) -> "Dialect":
        return cls()

    @classmethod
    def load(cls, name: str, path: str | Path | None = None) -> "Dialect":
        """`name` 이 비었거나 `off` 면 빈 대응표를 돌려준다."""
        name = (name or "").strip().lower()
        if not name or name == "off":
            return cls.off()

        target = Path(path) if path else DATA_DIR / f"{name}.json"
        if not target.is_file():
            log.warning("방언 대응표를 찾지 못했습니다: %s (방언 보정 없이 동작)", target)
            return cls.off()

        raw = json.loads(target.read_text(encoding="utf-8"))

        endings: list[Ending] = []
        for entry in raw.get("endings") or []:
            pattern, gloss = entry.get("pattern"), entry.get("gloss")
            if not pattern or not gloss:
                continue
            try:
                compiled = re.compile(pattern)
            except re.error:
                # 정규식 하나가 잘못됐다고 서비스가 뜨지 못하는 편보다는, 그 규칙만
                # 버리고 로그를 남기는 편이 낫다.
                log.exception("방언 어미 정규식을 건너뜁니다: %s", entry.get("id"))
                continue
            endings.append(
                Ending(id=str(entry.get("id") or pattern), pattern=compiled, gloss=str(gloss))
            )

        words: list[Word] = []
        for entry in raw.get("words") or []:
            forms = tuple(
                f.strip()
                for f in (entry.get("forms") or [])
                if isinstance(f, str) and f.strip()
            )
            standard = str(entry.get("standard") or "").strip()
            if not forms or not standard:
                continue
            words.append(Word(forms=forms, standard=standard))

        dialect = cls(
            name=name,
            label=str(raw.get("label") or name),
            prompt_lead=str(raw.get("prompt_lead") or "").strip(),
            prompt_words=tuple(
                w.strip()
                for w in (raw.get("prompt_words") or [])
                if isinstance(w, str) and w.strip()
            ),
            endings=tuple(endings),
            words=tuple(words),
            source=str(target),
        )
        log.info(
            "방언 대응표 로드: %s (어미 %s개, 어휘 %s개) %s",
            dialect.label,
            len(endings),
            len(words),
            target,
        )
        return dialect

    def __bool__(self) -> bool:
        return bool(self._endings or self._words)

    def __len__(self) -> int:
        return len(self._endings) + len(self._words)

    @property
    def prompt_terms(self) -> int:
        """whisper 프롬프트에 들려줄 수 있는 표기 수. 뜻풀이가 없는 대응표도 이건 있다."""
        return len(self._prompt_words)

    # -------------------------------------------------------------------- API
    def prompt(self, limit: int = 8) -> str:
        """whisper initial_prompt 뒤에 붙일 사투리 예시 한 줄.

        표준어로 고쳐 받아쓰지 않도록 표기를 미리 들려주는 것이다. 용어집 프롬프트와
        합쳐 224 토큰을 넘기면 whisper 가 앞쪽부터 잘라내므로 개수를 제한한다.
        """
        if not self._prompt_lead or limit <= 0:
            return ""
        cached = self._prompts.get(limit)
        if cached is not None:
            return cached

        words = list(self._prompt_words[:limit])
        prompt = self._prompt_lead
        if words:
            prompt = f"{prompt} " + ", ".join(words) + "."
        self._prompts[limit] = prompt
        return prompt

    def notes(self, text: str, limit: int = 8) -> list[str]:
        """원문에 실제로 나온 방언만 뜻풀이로 돌려준다.

        대응표 전체를 넣으면 토큰만 먹고 모델이 엉뚱한 뜻을 끌어 쓴다. 어미를
        어휘보다 앞에 놓는다. 문장의 뜻을 가르는 쪽이 어미다.
        """
        if not text or limit <= 0:
            return []

        notes: list[str] = []
        for ending in self._endings:
            if ending.pattern.search(text):
                notes.append(ending.gloss)

        if self._word_matcher is not None:
            seen: set[str] = set()
            for hit in self._word_matcher.finditer(text):
                word = self._word_index.get(hit.group(0))
                if word is None or word.standard in seen:
                    continue
                seen.add(word.standard)
                notes.append(f"`{hit.group(0)}` 는 `{word.standard}`")

        return notes[:limit]
