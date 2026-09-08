#!/usr/bin/env python3
"""종단간 검증.

시험용 발화를 합성해 브라우저 마이크처럼 실시간 속도로 흘려보내고,
`발화 종료 → 화면에 번역문이 다 찍히기까지` 걸리는 시간을 잰다. 목표는 1초 이내다.

같이 확인하는 것:
  * 마이크 2개 모드에서 채널이 화자로 그대로 고정되는가
  * 마이크 1개 모드에서 한국어/외국어 판별이 화자를 맞게 고르는가
  * 용어집이 STT 결과와 번역문에 반영되는가
  * 대화기록이 저장되고 TXT/JSON/HTML 로 내려받아지는가

컨테이너 안에서 실행한다. 발화 합성에만 기존 tts-api 를 쓰므로 키가 필요하다.

    KEY=$(sudo docker exec interpreter-web printenv TTS_API_KEY)
    sudo docker cp deploy/e2e_test.py taxitalk-web:/tmp/e2e_test.py
    sudo docker exec -e TTS_API_KEY="$KEY" taxitalk-web python3 -W ignore /tmp/e2e_test.py

환경변수
    E2E_BASE=http://127.0.0.1:8000    HTTP 진입점 (nginx 경유 검증 시 교체)
    E2E_WS=ws://127.0.0.1:8000        WebSocket 진입점
    E2E_MODE=single|dual|both
    E2E_PATIENT_LANG=en    탑승객 언어. 아래 SPEECH 에 예문이 있는 언어만 검사한다.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import ssl
import time
import wave

import httpx
import numpy as np
import websockets

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8000").rstrip("/")
WS_BASE = os.environ.get("E2E_WS", BASE.replace("http", "ws", 1)).rstrip("/")
TTS_BASE = os.environ.get("TTS_BASE_URL", "https://host.docker.internal:8084/api/tts")
TTS_KEY = os.environ.get("TTS_API_KEY", "")
TTS_VERIFY = (os.environ.get("TTS_VERIFY_SSL") or "0").lower() in {"1", "true", "yes", "on"}
MODE = (os.environ.get("E2E_MODE") or "both").lower()
PATIENT_LANG = os.environ.get("E2E_PATIENT_LANG", "en")

TARGET_RATE = 16000
CHUNK_MS = 64
SIDE_CODE = {"shared": 0, "staff": 1, "patient": 2}

# 전문용어가 들어간 실제 대화 문장. 괄호 안은 결과에서 찾아볼 표기다.
SPEECH = {
    "ko": (
        "안녕하세요. 해운대해수욕장 먼저 보시고 광안대교 야경까지 도시겠습니다. "
        "감천문화마을을 경유지로 넣으면 소요시간은 한 시간 정도 늘어납니다.",
        ["해운대해수욕장", "광안대교", "감천문화마을", "소요시간"],
    ),
    "en": (
        "Can we go to Jagalchi Market first and then Gamcheon Culture Village? "
        "I also want to know the base fare and whether card payment is possible.",
        ["Jagalchi Market", "Gamcheon Culture Village", "base fare", "card payment"],
    ),
}

# tts-api 는 정해진 속성 태그만 받는다(`GET /api/tts/attributes` 참고).
TTS_INSTRUCT = {
    "ko": "female, young adult, korean accent",
    "en": "female, young adult, american accent",
    "zh": "female, young adult, chinese accent",
    "ja": "female, young adult, japanese accent",
}


def tls_context(url: str):
    """nginx 를 경유할 때는 인증서 호스트명이 맞지 않으므로 검증을 끈다."""
    if not url.startswith("wss://"):
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def make_speech(text: str, lang: str) -> bytes:
    response = httpx.post(
        f"{TTS_BASE}/synthesize",
        json={
            "text": text,
            "language": lang,
            "instruct": TTS_INSTRUCT.get(lang, TTS_INSTRUCT["en"]),
            "num_step": 16,
            "format": "wav",
            "download": False,
        },
        headers={"Authorization": f"Bearer {TTS_KEY}"},
        timeout=240.0,
        verify=TTS_VERIFY,
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"TTS {response.status_code}: {response.text[:500]}"
        )
    return response.content


def to_pcm16_16k(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())

    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    else:
        raise SystemExit(f"지원하지 않는 sample width: {width}")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)

    if rate != TARGET_RATE:
        length = int(round(len(samples) * TARGET_RATE / rate))
        samples = np.interp(
            np.linspace(0, len(samples) - 1, length, dtype=np.float64),
            np.arange(len(samples), dtype=np.float64),
            samples,
        ).astype(np.float32)

    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class TurnResult:
    def __init__(self) -> None:
        self.speaker = ""
        self.detection: dict | None = None
        self.transcript = ""
        self.translation = ""
        self.partials = 0
        self.transcript_ms: int | None = None
        self.first_token_ms: int | None = None
        self.done_ms: int | None = None
        self.metrics: dict = {}
        self.failed = ""


async def collect(ws, turn_id_holder: dict, t_end_holder: dict) -> TurnResult:
    """한 발화가 끝날 때까지 서버 메시지를 모은다."""
    result = TurnResult()

    def elapsed() -> int:
        return int((time.perf_counter() - t_end_holder["t"]) * 1000)

    while True:
        message = await asyncio.wait_for(ws.recv(), timeout=180)
        if isinstance(message, bytes):
            continue
        event = json.loads(message)
        kind = event.get("type")

        if kind == "turn_start":
            turn_id_holder["id"] = event["turn"]
        elif kind == "turn_speaker":
            result.speaker = event["speaker"]
            result.detection = event.get("detection")
        elif kind == "partial":
            result.partials += 1
        elif kind == "transcript":
            result.speaker = event["speaker"]
            result.transcript = event["text"]
            result.transcript_ms = elapsed()
        elif kind == "translation_delta":
            if result.first_token_ms is None:
                result.first_token_ms = elapsed()
        elif kind == "translation_done":
            result.translation = event["text"]
        elif kind == "turn_done":
            result.done_ms = elapsed()
            result.metrics = event.get("metrics") or {}
            return result
        elif kind in {"empty", "error", "cancelled"}:
            result.failed = f"{kind}: {event.get('message', '')}"
            return result


async def run_turn(ws, side: str, pcm: bytes) -> TurnResult:
    chunk = int(TARGET_RATE * 2 * CHUNK_MS / 1000)
    code = SIDE_CODE[side]
    header = bytes([code, 0])

    turn_id_holder: dict = {}
    t_end_holder = {"t": time.perf_counter()}
    reader = asyncio.create_task(collect(ws, turn_id_holder, t_end_holder))

    await ws.send(json.dumps({"type": "start", "side": side}))
    for offset in range(0, len(pcm), chunk):
        if reader.done():
            break
        await ws.send(header + pcm[offset : offset + chunk])
        # 브라우저 VAD 가 내보내는 속도 그대로 흘려보낸다.
        await asyncio.sleep(CHUNK_MS / 1000)

    t_end_holder["t"] = time.perf_counter()
    await ws.send(json.dumps({"type": "end", "side": side}))
    return await reader


async def run_mode(client: httpx.AsyncClient, mic_mode: str, clips: dict) -> list[dict]:
    print(f"\n{'=' * 72}")
    print(f"  마이크 {'2개 (역할 고정)' if mic_mode == 'dual' else '1개 (언어 판별)'}")
    print("=" * 72)

    response = await client.post(
        f"{BASE}/api/sessions", json={"patient_lang": PATIENT_LANG, "mic_mode": mic_mode}
    )
    response.raise_for_status()
    session = response.json()
    print(f"세션 {session['id']}")

    url = f"{WS_BASE}/ws/session?session={session['id']}"
    rows: list[dict] = []

    async with websockets.connect(url, max_size=None, ssl=tls_context(url)) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready

        for expected_speaker, lang in (("staff", "ko"), ("patient", PATIENT_LANG)):
            if lang not in clips:
                continue
            pcm, keywords = clips[lang]
            side = expected_speaker if mic_mode == "dual" else "shared"
            seconds = len(pcm) / (TARGET_RATE * 2)

            print(f"\n-- {expected_speaker} / {lang} · 발화 {seconds:.1f}s --")
            result = await run_turn(ws, side, pcm)

            if result.failed:
                print(f"  실패: {result.failed}")
                rows.append({"case": f"{mic_mode}/{lang}", "ok": False, "note": result.failed})
                continue

            hits = [word for word in keywords if word.lower() in result.transcript.lower()]
            speaker_ok = result.speaker == expected_speaker
            print(f"  화자     {result.speaker} {'OK' if speaker_ok else '오판 ← ' + expected_speaker}")
            if result.detection:
                print(f"  판별     {json.dumps(result.detection, ensure_ascii=False)}")
            print(f"  중간자막 {result.partials}회")
            print(f"  원문     {result.transcript}")
            print(f"  번역     {result.translation}")
            print(f"  용어     {len(hits)}/{len(keywords)} 일치 {hits}")
            print(
                f"  지연     전사 +{result.transcript_ms}ms · 첫 토큰 "
                f"+{result.first_token_ms}ms · 완료 +{result.done_ms}ms"
            )
            print(f"  metrics  {json.dumps(result.metrics, ensure_ascii=False)}")

            rows.append(
                {
                    "case": f"{mic_mode}/{lang}",
                    "speaker_ok": speaker_ok,
                    "terms": f"{len(hits)}/{len(keywords)}",
                    "done_ms": result.done_ms,
                    "ok": speaker_ok and bool(result.translation),
                }
            )

    closed = (await client.post(f"{BASE}/api/sessions/{session['id']}/close")).json()
    print(f"\n기록 {closed['turns']}건 저장")
    for fmt in ("txt", "json", "html"):
        export = await client.get(f"{BASE}/api/sessions/{session['id']}/export?format={fmt}")
        state = "OK" if export.status_code == 200 else f"실패 {export.status_code}"
        print(f"  export {fmt:4} {len(export.content):>7,}B  {state}")

    return rows


async def main() -> None:
    if not TTS_KEY:
        raise SystemExit(
            "TTS_API_KEY 가 없습니다. 시험 발화 합성에만 씁니다.\n"
            '  KEY=$(sudo docker exec interpreter-web printenv TTS_API_KEY)\n'
            '  sudo docker exec -e TTS_API_KEY="$KEY" taxitalk-web python3 -W ignore /tmp/e2e_test.py'
        )

    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        health = (await client.get(f"{BASE}/health")).json()
        print(f"health ok={health['ok']} stt={health['stt']['model']} "
              f"용어 {health['glossary']['terms']}개")
        if not health["ok"]:
            raise SystemExit(f"STT 준비 안 됨: {health['stt'].get('error')}")

        print("\n시험 발화 합성 중…")
        clips: dict = {}
        for lang in ("ko", PATIENT_LANG):
            if lang not in SPEECH:
                print(f"  {lang}: 예문이 없어 건너뜁니다.")
                continue
            text, keywords = SPEECH[lang]
            pcm = to_pcm16_16k(make_speech(text, lang))
            clips[lang] = (pcm, keywords)
            print(f"  {lang}: {len(pcm) / (TARGET_RATE * 2):.1f}s")

        modes = {"single": ["single"], "dual": ["dual"]}.get(MODE, ["dual", "single"])
        rows: list[dict] = []
        for mic_mode in modes:
            rows += await run_mode(client, mic_mode, clips)

    print(f"\n{'=' * 72}")
    print(f"  {'케이스':<16} {'화자':<6} {'용어':<8} {'완료(ms)':>10}")
    print("-" * 72)
    for row in rows:
        speaker = "OK" if row.get("speaker_ok") else "오판"
        print(
            f"  {row['case']:<16} {speaker:<6} {str(row.get('terms', '-')):<8} "
            f"{str(row.get('done_ms', '-')):>10}"
        )
    slow = [r for r in rows if isinstance(r.get("done_ms"), int) and r["done_ms"] > 1000]
    print("-" * 72)
    print(f"  전체 {len(rows)}건 · 1초 초과 {len(slow)}건 · 실패 {sum(1 for r in rows if not r['ok'])}건")


if __name__ == "__main__":
    asyncio.run(main())
