"""상담기록 내보내기 — TXT · JSON · 인쇄용 HTML.

PDF 는 만들지 않는다. CJK 폰트를 컨테이너에 넣어야 하고 레이아웃도 따로 잡아야
하는데, 인쇄용 HTML 을 브라우저에서 `PDF 로 저장` 하면 같은 결과를 얻는다.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Iterable

from .config import STAFF_LANG, lang_label, lang_label_ko

FORMATS = ("txt", "json", "html")

MEDIA_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


def _clock(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return ""


def _stamp(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "-"


def _duration(meta: dict) -> str:
    try:
        started = datetime.fromisoformat(str(meta.get("started_at")))
        ended = datetime.fromisoformat(str(meta.get("ended_at")))
    except (TypeError, ValueError):
        return "-"
    seconds = max(0, int((ended - started).total_seconds()))
    return f"{seconds // 60}분 {seconds % 60}초"


def _speaker_label(speaker: str, patient_lang: str) -> str:
    if speaker == "staff":
        return "상담원"
    return f"고객 ({lang_label(patient_lang)})"


def _pair_label(patient_lang: str) -> str:
    return f"한국어 ↔ {lang_label(patient_lang)} ({lang_label_ko(patient_lang)})"


def _korean_and_foreign(turn: dict) -> tuple[str, str]:
    """한 발화를 (한국어 문장, 외국어 문장) 으로 정리한다."""
    original = str(turn.get("original") or "")
    translated = str(turn.get("translated") or "")
    if str(turn.get("src") or "") == STAFF_LANG:
        return original, translated
    return translated, original


# ---------------------------------------------------------------------- TXT
def to_text(meta: dict, turns: Iterable[dict]) -> str:
    patient_lang = str(meta.get("patient_lang") or "en")
    lines = [
        "TAXI-TALK 상담 기록",
        "=" * 60,
        f"세션 : {meta.get('id')}",
        f"언어 : {_pair_label(patient_lang)}",
        f"시작 : {_stamp(meta.get('started_at'))}",
        f"종료 : {_stamp(meta.get('ended_at'))}",
        f"시간 : {_duration(meta)}",
        f"발화 : {meta.get('turns', 0)}건",
    ]
    if meta.get("clinic"):
        lines.insert(2, f"기관 : {meta['clinic']}")
    lines.append("=" * 60)
    lines.append("")

    empty = True
    for turn in turns:
        empty = False
        korean, foreign = _korean_and_foreign(turn)
        lines.append(
            f"[{turn.get('seq')}] {_clock(turn.get('at'))} "
            f"{_speaker_label(str(turn.get('speaker') or ''), patient_lang)}"
        )
        lines.append(f"  한국어 : {korean}")
        lines.append(f"  {lang_label(patient_lang)} : {foreign}")
        lines.append("")

    if empty:
        lines.append("(기록된 발화가 없습니다)")
    return "\n".join(lines)


# --------------------------------------------------------------------- JSON
def to_json(meta: dict, turns: Iterable[dict]) -> str:
    payload = {"meta": meta, "turns": list(turns)}
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------- HTML
_HTML_STYLE = """
:root { --line: #dfe5ef; --muted: #6b7a90; --accent: #1f6feb; --ink: #16202e; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px; background: #f6f8fb; color: var(--ink);
  font-family: "Pretendard", -apple-system, "Segoe UI", Roboto, "Noto Sans KR",
    "Noto Sans SC", "Noto Sans JP", sans-serif;
  line-height: 1.6;
}
.sheet { max-width: 900px; margin: 0 auto; background: #fff; border: 1px solid var(--line);
  border-radius: 14px; padding: 36px 40px; }
h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
dl.meta { display: grid; grid-template-columns: 90px 1fr 90px 1fr; gap: 8px 16px;
  margin: 0 0 28px; padding: 18px 20px; background: #f9fbff; border: 1px solid var(--line);
  border-radius: 10px; font-size: 13px; }
dl.meta dt { color: var(--muted); }
dl.meta dd { margin: 0; font-weight: 600; }
.turn { padding: 16px 0; border-top: 1px solid var(--line); }
.turn:first-of-type { border-top: 0; }
.head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px; }
.who { font-weight: 700; font-size: 13px; }
.who.staff { color: var(--accent); }
.who.patient { color: #0f9b7a; }
.time { color: var(--muted); font-size: 12px; }
.seq { margin-left: auto; color: var(--muted); font-size: 12px; }
.line { display: grid; grid-template-columns: 74px 1fr; gap: 12px; padding: 3px 0; }
.tag { color: var(--muted); font-size: 12px; padding-top: 3px; }
.text { font-size: 15px; white-space: pre-wrap; word-break: break-word; }
.empty { color: var(--muted); padding: 30px 0; text-align: center; }
@media print {
  body { background: #fff; padding: 0; }
  .sheet { border: 0; border-radius: 0; padding: 0; max-width: none; }
  .turn { break-inside: avoid; }
}
"""


def to_html(meta: dict, turns: Iterable[dict]) -> str:
    patient_lang = str(meta.get("patient_lang") or "en")
    foreign_label = html.escape(lang_label(patient_lang))
    rows: list[str] = []

    for turn in turns:
        korean, foreign = _korean_and_foreign(turn)
        speaker = str(turn.get("speaker") or "")
        rows.append(
            f"""      <article class="turn">
        <div class="head">
          <span class="who {html.escape(speaker)}">
            {html.escape(_speaker_label(speaker, patient_lang))}</span>
          <span class="time">{html.escape(_clock(turn.get('at')))}</span>
          <span class="seq">#{html.escape(str(turn.get('seq') or ''))}</span>
        </div>
        <div class="line"><span class="tag">한국어</span>
          <span class="text">{html.escape(korean)}</span></div>
        <div class="line"><span class="tag">{foreign_label}</span>
          <span class="text">{html.escape(foreign)}</span></div>
      </article>"""
        )

    body = "\n".join(rows) or '      <p class="empty">기록된 발화가 없습니다.</p>'
    clinic = html.escape(str(meta.get("clinic") or "성형외과·피부과 외국인 고객 상담"))

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>TAXI-TALK 상담 기록 {html.escape(str(meta.get('id') or ''))}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
  <div class="sheet">
    <h1>TAXI-TALK 상담 기록</h1>
    <p class="sub">{clinic}</p>
    <dl class="meta">
      <dt>세션</dt><dd>{html.escape(str(meta.get('id') or ''))}</dd>
      <dt>언어</dt><dd>{html.escape(_pair_label(patient_lang))}</dd>
      <dt>시작</dt><dd>{html.escape(_stamp(meta.get('started_at')))}</dd>
      <dt>종료</dt><dd>{html.escape(_stamp(meta.get('ended_at')))}</dd>
      <dt>시간</dt><dd>{html.escape(_duration(meta))}</dd>
      <dt>발화</dt><dd>{html.escape(str(meta.get('turns', 0)))}건</dd>
    </dl>
{body}
  </div>
</body>
</html>
"""


# --------------------------------------------------------------------- 진입점
def render(fmt: str, meta: dict, turns: Iterable[dict]) -> tuple[bytes, str, str]:
    """`(본문, media_type, 파일명)` 을 돌려준다."""
    fmt = (fmt or "txt").lower()
    if fmt not in FORMATS:
        raise ValueError(f"unsupported_format:{fmt}")

    rows = list(turns)
    if fmt == "json":
        text = to_json(meta, rows)
    elif fmt == "html":
        text = to_html(meta, rows)
    else:
        text = to_text(meta, rows)

    filename = f"taxitalk_{meta.get('id') or 'session'}.{fmt}"
    return text.encode("utf-8"), MEDIA_TYPES[fmt], filename
