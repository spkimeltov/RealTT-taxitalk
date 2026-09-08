#!/usr/bin/env python3
"""dutyfree.conf 의 server 블록 안에 TAXI-TALK location 을 멱등하게 삽입한다.

48443 서버 블록은 이미 TLS 종단 + 서브패스 프록시 + WebSocket 업그레이드를 하고
있어서 그대로 재사용하면 공유기 포트포워딩을 건드릴 필요가 없다. 운영 중인 설정
파일이므로 수정 전에 타임스탬프 백업을 남긴다.

사용법: sudo python3 patch_nginx.py [--target /etc/nginx/conf.d/dutyfree.conf]
                                    [--snippet ./taxitalk-location.conf]
                                    [--remove]
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

BEGIN = "# BEGIN taxitalk"
END = "# END taxitalk"


def strip_existing(text: str) -> str:
    start = text.find(BEGIN)
    if start < 0:
        return text
    end = text.find(END, start)
    if end < 0:
        raise SystemExit(f"{BEGIN} 은 있는데 {END} 가 없습니다. 수동 확인이 필요합니다.")
    end = text.find("\n", end)
    end = len(text) if end < 0 else end + 1
    # 블록 앞의 들여쓰기까지 함께 제거한다.
    line_start = text.rfind("\n", 0, start)
    line_start = 0 if line_start < 0 else line_start + 1
    return text[:line_start] + text[end:]


def insert(text: str, snippet: str) -> str:
    close = text.rstrip().rfind("}")
    if close < 0:
        raise SystemExit("server 블록의 닫는 중괄호를 찾지 못했습니다.")
    body = snippet.rstrip("\n") + "\n"
    return text[:close] + body + text[close:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="/etc/nginx/conf.d/dutyfree.conf")
    parser.add_argument(
        "--snippet", default=str(Path(__file__).with_name("taxitalk-location.conf"))
    )
    parser.add_argument("--remove", action="store_true", help="삽입한 블록만 제거한다")
    args = parser.parse_args()

    target = Path(args.target)
    original = target.read_text(encoding="utf-8")
    cleaned = strip_existing(original)

    if args.remove:
        updated = cleaned
    else:
        snippet = Path(args.snippet).read_text(encoding="utf-8")
        updated = insert(cleaned, "\n" + snippet)

    if updated == original:
        print(f"변경 없음: {target}")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    backup = target.with_suffix(target.suffix + f".bak-taxitalk-{stamp}")
    shutil.copy2(target, backup)
    target.write_text(updated, encoding="utf-8")
    print(f"백업: {backup}")
    print(f"수정: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
