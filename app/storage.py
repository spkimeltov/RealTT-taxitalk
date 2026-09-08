"""대화기록 저장.

한 대화가 디렉터리 하나다. 발화가 확정될 때마다 `transcript.jsonl` 에 한 줄씩
덧붙이므로, 대화 도중 브라우저가 죽거나 컨테이너가 재시작돼도 그때까지의 대화는
남는다. 요약이나 검색 없이 append 만 하면 되는 단계라 DB 를 두지 않았다.

    data/sessions/20260806_143022_en_a1b2/
      meta.json         언어쌍 · 시작/종료 시각 · 발화 수
      transcript.jsonl  발화 한 건당 한 줄
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9), "KST")

# 경로 조작을 막기 위해 발급 형식과 정확히 일치하는 id 만 받는다.
SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[a-z]{2,3}_[0-9a-f]{4}$")

META_NAME = "meta.json"
TRANSCRIPT_NAME = "transcript.jsonl"


class SessionNotFound(LookupError):
    pass


def now_kst() -> datetime:
    return datetime.now(KST)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def directory(self, session_id: str) -> Path:
        if not SESSION_ID_RE.match(session_id or ""):
            raise SessionNotFound(session_id)
        return self._root / session_id

    # ------------------------------------------------------------------ 생성
    async def create(
        self,
        patient_lang: str,
        *,
        staff_lang: str = "ko",
        mic_mode: str = "single",
        clinic: str = "",
        dialect_on: bool = False,
        dialect_region: str = "",
    ) -> dict:
        started = now_kst()
        session_id = (
            f"{started.strftime('%Y%m%d_%H%M%S')}_{patient_lang}_{secrets.token_hex(2)}"
        )
        meta = {
            "id": session_id,
            "staff_lang": staff_lang,
            "patient_lang": patient_lang,
            "mic_mode": mic_mode,
            # WebSocket 은 세션 설정을 meta 로만 넘겨받는다. 여기 없으면 대화 화면의
            # 사투리 설정이 서버까지 닿지 않는다.
            "dialect_on": bool(dialect_on),
            "dialect_region": dialect_region,
            "clinic": clinic,
            "started_at": _iso(started),
            "ended_at": None,
            "turns": 0,
        }
        directory = self._root / session_id
        await asyncio.to_thread(self._write_new, directory, meta)
        log.info(
            "대화 시작: %s (ko ↔ %s, mic=%s, 사투리=%s)",
            session_id,
            patient_lang,
            mic_mode,
            dialect_region if dialect_on else "off",
        )
        return meta

    @staticmethod
    def _write_new(directory: Path, meta: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / META_NAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (directory / TRANSCRIPT_NAME).touch()

    # ------------------------------------------------------------------ 기록
    async def append_turn(self, session_id: str, entry: dict) -> None:
        directory = self.directory(session_id)
        if not directory.is_dir():
            raise SessionNotFound(session_id)
        line = json.dumps(entry, ensure_ascii=False)
        async with self._lock(session_id):
            await asyncio.to_thread(self._append_line, directory / TRANSCRIPT_NAME, line)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ------------------------------------------------------------------ 조회
    async def load(self, session_id: str) -> dict:
        directory = self.directory(session_id)
        if not directory.is_dir():
            raise SessionNotFound(session_id)
        return await asyncio.to_thread(self._read_all, directory)

    @staticmethod
    def _read_all(directory: Path) -> dict:
        meta = json.loads((directory / META_NAME).read_text(encoding="utf-8"))
        turns: list[dict] = []
        transcript = directory / TRANSCRIPT_NAME
        if transcript.is_file():
            for line in transcript.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("깨진 기록 한 줄 건너뜀: %s", directory.name)
        meta["turns"] = len(turns)
        return {"meta": meta, "turns": turns}

    async def meta(self, session_id: str) -> dict:
        directory = self.directory(session_id)
        if not directory.is_dir():
            raise SessionNotFound(session_id)
        return await asyncio.to_thread(
            lambda: json.loads((directory / META_NAME).read_text(encoding="utf-8"))
        )

    # ------------------------------------------------------------------ 종료
    async def close(self, session_id: str) -> dict:
        directory = self.directory(session_id)
        if not directory.is_dir():
            raise SessionNotFound(session_id)
        async with self._lock(session_id):
            meta = await asyncio.to_thread(self._finalize, directory)
        log.info("대화 종료: %s (%s건)", session_id, meta.get("turns"))
        return meta

    @staticmethod
    def _finalize(directory: Path) -> dict:
        meta_path = directory / META_NAME
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        transcript = directory / TRANSCRIPT_NAME
        count = 0
        if transcript.is_file():
            count = sum(1 for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip())
        meta["turns"] = count
        # 이미 닫힌 대화를 다시 닫아도 처음 종료 시각을 유지한다.
        if not meta.get("ended_at"):
            meta["ended_at"] = _iso(now_kst())
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    # ------------------------------------------------------------------ 목록
    async def list_recent(self, limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(self._scan, limit)

    def _scan(self, limit: int) -> list[dict]:
        if not self._root.is_dir():
            return []
        found: list[dict] = []
        for directory in self._root.iterdir():
            if not directory.is_dir() or not SESSION_ID_RE.match(directory.name):
                continue
            meta_path = directory / META_NAME
            if not meta_path.is_file():
                continue
            try:
                found.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        found.sort(key=lambda m: str(m.get("started_at") or ""), reverse=True)
        return found[: max(0, limit)]


def turn_entry(
    *,
    seq: int,
    speaker: str,
    src: str,
    dst: str,
    original: str,
    translated: str,
    standard: str = "",
    metrics: Optional[dict[str, Any]] = None,
) -> dict:
    """`transcript.jsonl` 한 줄의 형태를 한곳에서 정한다.

    `original` 은 늘 기사가 실제로 한 말이다. 사투리를 표준어로 옮겨 번역했다면
    그 표준어가 `standard` 로 따로 붙는다. 사투리를 쓰지 않은 발화에는 없는 항목이라,
    빈 값으로 모든 줄을 늘리지 않는다.
    """
    entry: dict[str, Any] = {
        "seq": seq,
        "at": _iso(now_kst()),
        "speaker": speaker,
        "src": src,
        "dst": dst,
        "original": original,
        "translated": translated,
    }
    if standard:
        entry["standard"] = standard
    if metrics:
        entry["metrics"] = metrics
    return entry
