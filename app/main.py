"""TAXI-TALK 서비스 엔트리포인트."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import (
    MIC_MODES,
    STAFF_LANG,
    UI_TEXT,
    Settings,
    lang_entry,
    ui_text,
)
from .export import FORMATS, render
from .glossary import Glossary
from .session import ConsultSession
from .storage import SessionNotFound, SessionStore
from .stt import SttEngine
from .translate import Translator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("taxitalk")

SETTINGS = Settings.from_env()
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def _load_models(app: FastAPI) -> None:
    """모델 로드를 백그라운드로 돌려 컨테이너가 즉시 응답할 수 있게 한다."""
    try:
        await asyncio.to_thread(app.state.stt.load)
        await asyncio.to_thread(app.state.stt.warmup)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        app.state.load_error = f"{type(exc).__name__}: {exc}"
        log.exception("STT 모델 로드 실패")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = SETTINGS
    app.state.stt = SttEngine(SETTINGS)
    app.state.translator = Translator(SETTINGS)
    app.state.glossary = Glossary.load(SETTINGS.glossary_path or None)
    app.state.store = SessionStore(SETTINGS.session_dir)
    app.state.store.ensure_root()
    app.state.load_error = None
    app.state.loader = asyncio.create_task(_load_models(app))
    log.info(
        "기동: stt=%s mt=%s@%s 용어 %s개 기록=%s",
        SETTINGS.whisper_model,
        SETTINGS.vllm_model,
        SETTINGS.vllm_base_url,
        len(app.state.glossary),
        SETTINGS.session_dir,
    )
    try:
        yield
    finally:
        app.state.loader.cancel()
        with_suppressed = (asyncio.CancelledError, Exception)
        try:
            await app.state.loader
        except with_suppressed:
            pass
        await app.state.translator.aclose()
        app.state.stt.close()


app = FastAPI(
    title="TAXI-TALK",
    version=__version__,
    root_path=SETTINGS.base_path,
    lifespan=lifespan,
)


def _check_key(key: str | None) -> None:
    if SETTINGS.app_api_key and key != SETTINGS.app_api_key:
        raise HTTPException(status_code=401, detail="invalid_key")


# ---------------------------------------------------------------------- 상태
@app.get("/health")
async def health() -> dict:
    return {
        "ok": app.state.stt.ready,
        "version": __version__,
        "stt": {
            "ready": app.state.stt.ready,
            "model": SETTINGS.whisper_model,
            "device": f"{SETTINGS.whisper_device}:{SETTINGS.whisper_device_index}",
            "compute_type": SETTINGS.whisper_compute_type,
            "error": app.state.load_error,
        },
        "mt": {"model": SETTINGS.vllm_model, "base_url": SETTINGS.vllm_base_url},
        "glossary": {"terms": len(app.state.glossary), "source": app.state.glossary.source},
        "sessions": {"dir": SETTINGS.session_dir},
    }


@app.get("/api/config")
async def config() -> dict:
    return {
        "staff_lang": STAFF_LANG,
        # 서버가 다룰 수 있는 언어 전부. 이 중 `featured` 만 시작 화면에 카드로
        # 뜨고, 고객이 고를 수 있는 것도 그 카드뿐이다.
        "languages": [lang_entry(code) for code in SETTINGS.enabled_languages],
        "featured": list(SETTINGS.patient_languages),
        # 고객 화면 문구는 번역이 준비된 언어만 내려보낸다. 없는 언어는 화면이
        # `fallback_ui`(영어)로 대체한다. 카탈로그를 다 실어 보내면 대부분이 같은
        # 영어 문구의 사본이라 낭비다.
        "ui": {code: text for code, text in UI_TEXT.items() if code != STAFF_LANG},
        "fallback_ui": ui_text("en"),
        "staff_ui": ui_text(STAFF_LANG),
        "mic_mode": SETTINGS.mic_mode,
        "screen_layout": SETTINGS.screen_layout,
        "clinic_name": SETTINGS.clinic_name,
        "auth_required": bool(SETTINGS.app_api_key),
        "sample_rate": 16000,
        "vad": {
            "rms_threshold": SETTINGS.vad_rms_threshold,
            "silence_ms": SETTINGS.vad_silence_ms,
            "min_speech_ms": SETTINGS.vad_min_speech_ms,
            "prespeech_ms": SETTINGS.vad_prespeech_ms,
            "max_utterance_sec": SETTINGS.max_utterance_sec,
        },
        "models": {"stt": SETTINGS.whisper_model, "mt": SETTINGS.vllm_model},
        "export_formats": list(FORMATS),
    }


# -------------------------------------------------------------------- 대화기록
@app.post("/api/sessions", status_code=201)
async def create_session(
    payload: dict = Body(default_factory=dict),
    key: str | None = Query(default=None),
) -> dict:
    _check_key(key)
    patient_lang = str(payload.get("patient_lang") or "").lower()
    # 카드로 뜨는 언어(`patient_languages`)가 아니라 고를 수 있는 전체를 기준으로
    # 본다. 카드는 자주 쓰는 언어를 앞에 꺼내 둔 것일 뿐이다.
    if patient_lang not in SETTINGS.enabled_languages:
        raise HTTPException(status_code=400, detail="unsupported_patient_lang")

    mic_mode = str(payload.get("mic_mode") or SETTINGS.mic_mode).lower()
    if mic_mode not in MIC_MODES:
        mic_mode = SETTINGS.mic_mode

    return await app.state.store.create(
        patient_lang,
        staff_lang=STAFF_LANG,
        mic_mode=mic_mode,
        clinic=SETTINGS.clinic_name,
    )


@app.get("/api/sessions")
async def list_sessions(key: str | None = Query(default=None)) -> dict:
    _check_key(key)
    return {"sessions": await app.state.store.list_recent(SETTINGS.session_list_limit)}


@app.get("/api/sessions/{session_id}")
async def read_session(session_id: str, key: str | None = Query(default=None)) -> dict:
    _check_key(key)
    try:
        return await app.state.store.load(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session_not_found") from None


@app.post("/api/sessions/{session_id}/close")
async def close_session(session_id: str, key: str | None = Query(default=None)) -> dict:
    _check_key(key)
    try:
        return await app.state.store.close(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session_not_found") from None


@app.get("/api/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    format: str = Query(default="txt"),
    key: str | None = Query(default=None),
) -> Response:
    _check_key(key)
    if format.lower() not in FORMATS:
        raise HTTPException(status_code=400, detail="unsupported_format")
    try:
        data = await app.state.store.load(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session_not_found") from None

    body, media_type, filename = render(format, data["meta"], data["turns"])
    return Response(
        content=body,
        media_type=media_type,
        headers={
            # 파일명에 한글은 없지만, 표준 형식을 지켜 브라우저가 그대로 저장하게 한다.
            "Content-Disposition": f"attachment; filename=\"{filename}\"; "
            f"filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
        },
    )


# ------------------------------------------------------------------ WebSocket
@app.websocket("/ws/session")
async def session_ws(ws: WebSocket) -> None:
    if SETTINGS.app_api_key and ws.query_params.get("key") != SETTINGS.app_api_key:
        await ws.close(code=1008, reason="invalid_key")
        return

    session_id = ws.query_params.get("session") or ""
    try:
        meta = await ws.app.state.store.meta(session_id)
    except SessionNotFound:
        await ws.close(code=1008, reason="session_not_found")
        return

    await ws.accept()
    stt: SttEngine = ws.app.state.stt
    if not stt.ready:
        await ws.send_json(
            {
                "type": "error",
                "code": "stt_not_ready",
                "message": ws.app.state.load_error or "STT 모델을 로드하는 중입니다.",
            }
        )
        await ws.close(code=1013)
        return

    session = ConsultSession(
        ws,
        stt=stt,
        translator=ws.app.state.translator,
        glossary=ws.app.state.glossary,
        store=ws.app.state.store,
        settings=ws.app.state.settings,
        meta=meta,
    )
    try:
        await session.run()
    except Exception:  # noqa: BLE001
        log.exception("세션 종료(예외): %s", session_id)


@app.middleware("http")
async def no_store_static(request, call_next):
    """정적 파일을 매번 검사하게 한다.

    배포 후에도 브라우저가 예전 app.js 를 계속 쓰는 일을 막는다. 파일이 몇 개뿐이라
    재검사 비용은 무시할 수 있다.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
    return response


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
