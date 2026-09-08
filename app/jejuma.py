"""사투리 → 표준어 변환 (JEJUMA-002 게이트웨이).

기사가 사투리로 말하면 번역 모델이 뜻을 놓친다. 그래서 받아쓴 한국어를 먼저
표준어로 옮기고, 그 표준어를 번역 입력으로 쓴다. 화면과 대화기록에는 기사가 실제로
한 말(사투리 원문)이 그대로 남는다.

    받아쓰기 "어데 가노?"  →  게이트웨이  →  "어디 가니?"  →  번역
       └ 화면·기록에 남는 것                    └ 번역 모델이 받는 것

게이트웨이는 `region` 을 받아 방언별 프롬프트를 만들어 vLLM(JEJUMA-002)에 넘긴다.
`task` 는 여럿이지만 여기서는 `dialect_to_standard` 하나만 쓴다.
`detect_dialect_and_convert` 는 지역을 모델이 알아서 고르게 하는데, 기사가 어느
지역 사람인지는 설정에서 이미 아는 값이라 굳이 모델의 추측을 끼울 이유가 없다.

**변환은 실패해도 되는 단계다.** 이 왕복이 번역 앞에 끼어 있으므로 게이트웨이가
느리거나 죽으면 통역 전체가 멈춘다. 그래서 못 믿을 결과는 모두 `None` 으로 접고,
호출한 쪽이 사투리 원문으로 번역을 진행하게 한다. 8B 모델이라 라벨을 붙이거나
설명을 늘어놓는 일이 있어서, 오류가 없더라도 결과를 한 번 걸러야 한다.
"""

from __future__ import annotations

import logging
import re

import httpx

from .config import Settings
from .translate import strip_wrapping_quotes

log = logging.getLogger(__name__)

# 모델이 답 앞에 붙이는 라벨. `표준어: 어디 가니?` 처럼 나오는 경우를 벗긴다.
_LABEL = re.compile(
    r"^\s*(?:표준어(?:로)?|표준|standard(?:\s+korean)?)\s*(?:번역)?\s*[:：]\s*",
    re.IGNORECASE,
)
_HANGUL = re.compile(r"[가-힣]")

# 요금·시간·인원처럼 틀리면 안 되는 값. 변환 모델이 사투리가 아닌 낱말까지 건드리는
# 일이 있어서(`광안리까지 이만원입니다` → `이만큼입니다`) 원문에 있던 수량 표현이
# 결과에서 사라지면 변환을 통째로 버린다. 사투리를 못 고치고 넘어가는 편이 요금을
# 틀리게 말하는 것보다 낫다. `시간` 을 `시` 보다 앞에 두어야 통째로 잡힌다.
_QUANTITY = re.compile(
    r"\d[\d,]*"
    r"|[일이삼사오육칠팔구십백천만억]+\s*"
    r"(?:원|분|시간|시|초|명|개|번|층|킬로|미터|퍼센트|프로|년|월|일)"
)

# 이보다 짧은 입력은 변환해 봐야 얻을 것이 없다. `네` `예` 같은 대답이다.
MIN_CHARS = 2


def _lost_quantities(source: str, result: str) -> list[str]:
    """원문에 있었는데 변환 결과에서 사라진 수량 표현.

    띄어쓰기는 무시한다. `이십분` 을 `이십 분` 으로 띄우는 것은 옳은 변환이지
    값이 바뀐 것이 아니다. 반대로 한글 수를 아라비아 숫자로 바꾸는 변환(`이십분`
    → `20분`)도 여기서 걸리는데, 그래도 원문을 쓰는 쪽이 안전하다.
    """
    packed = re.sub(r"\s+", "", result)
    lost = []
    for hit in _QUANTITY.findall(source):
        token = re.sub(r"\s+", "", hit)
        if token and token not in packed:
            lost.append(token)
    return lost


class Standardizer:
    """게이트웨이 클라이언트. 주소나 키가 비어 있으면 아무것도 하지 않는다."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.jejuma_timeout_sec, connect=2.0)
        )

    @property
    def available(self) -> bool:
        return bool(self._s.jejuma_base_url and self._s.jejuma_api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def to_standard(self, text: str, region: str) -> str | None:
        """사투리를 표준어로. 못 믿을 결과면 `None` 을 돌려준다.

        `None` 은 오류가 아니라 "이번에는 원문을 쓰라"는 뜻이다. 부르는 쪽에서
        예외를 잡을 필요가 없도록 여기서 전부 삼킨다.
        """
        text = (text or "").strip()
        if not self.available or not region:
            return None
        if len(text) < MIN_CHARS or not _HANGUL.search(text):
            return None

        try:
            response = await self._client.post(
                f"{self._s.jejuma_base_url}/api/dialect",
                headers={"Authorization": f"Bearer {self._s.jejuma_api_key}"},
                json={
                    "task": "dialect_to_standard",
                    "region": region,
                    "text": text,
                    "stream": False,
                    "temperature": self._s.jejuma_temperature,
                    "max_tokens": self._s.jejuma_max_tokens,
                },
            )
        except httpx.HTTPError as exc:
            log.warning("표준어 변환 실패(원문으로 진행): %s", exc)
            return None

        if response.status_code >= 400:
            log.warning(
                "표준어 변환 %s(원문으로 진행): %s",
                response.status_code,
                response.text[:200],
            )
            return None

        try:
            result = str((response.json() or {}).get("result") or "")
        except ValueError:
            log.warning("표준어 변환 응답을 읽지 못했습니다(원문으로 진행)")
            return None

        return self._clean(result, text, region)

    @staticmethod
    def _clean(result: str, source: str, region: str) -> str | None:
        """모델 출력에서 표준어 한 문장만 남긴다. 아니면 `None`."""
        # 여러 줄로 답하면 첫 줄만 쓴다. 뒤따라 붙는 것은 대개 해설이다.
        line = result.strip().splitlines()[0] if result.strip() else ""
        line = strip_wrapping_quotes(_LABEL.sub("", line).strip())
        if not line:
            return None

        if not _HANGUL.search(line):
            # 한글이 없으면 변환이 아니라 영어 해설이 돌아온 것이다.
            log.warning("표준어 변환 결과에 한글이 없어 버립니다: %r", line[:80])
            return None

        # 짧은 발화에서 모델이 같은 말을 되풀이하며 무너지는 경우가 있다. 표준어는
        # 사투리와 길이가 크게 다르지 않으므로 길이로 걸러낸다.
        if len(line) > max(40, len(source) * 3):
            log.warning("표준어 변환 결과가 지나치게 길어 버립니다: %r", line[:80])
            return None

        missing = _lost_quantities(source, line)
        if missing:
            log.warning(
                "표준어 변환이 수량 표현을 바꿔 버립니다(%s 사라짐): %r → %r",
                ", ".join(missing),
                source,
                line,
            )
            return None

        log.info("표준어 변환(%s): %r → %r", region, source, line)
        return line
