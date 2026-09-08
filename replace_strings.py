#!/usr/bin/env python3
"""taxi-TALK를 TAXI-TALK로, 관광투어을 관광투어로 변경하는 스크립트"""

import re
from pathlib import Path

# 변경할 매핑
REPLACEMENTS = {
    "TAXI-TALK": "TAXI-TALK",
    "관광투어": "관광투어",
}

# 대상 파일 확장자
TARGET_EXTENSIONS = {".py", ".html", ".css", ".md", ".yml", ".ps1", ".conf"}


def should_process_file(file_path: Path) -> bool:
    """파일이 처리 대상인지 확인"""
    return file_path.suffix.lower() in TARGET_EXTENSIONS


def replace_in_file(file_path: Path) -> bool:
    """파일 내 문자열을 교체하고, 변경 여부를 반환"""
    try:
        # 파일 읽기 (UTF-8 인코딩 시도)
        content = file_path.read_text(encoding="utf-8")
        
        # 변경 전 내용 저장
        original_content = content
        
        # 모든 교체 적용
        for old, new in REPLACEMENTS.items():
            content = content.replace(old, new)
        
        # 변경된 내용이 있다면 파일 쓰기
        if content != original_content:
            file_path.write_text(content, encoding="utf-8")
            return True
        return False
    except Exception as e:
        print(f"파일 처리 중 오류 발생: {file_path} - {e}")
        return False


def main():
    """메인 함수"""
    # 현재 디렉토리 기준으로 모든 파일 탐색
    root_dir = Path(__file__).parent
    
    # 변경된 파일 목록
    changed_files = []
    
    # 모든 파일 순회
    for file_path in root_dir.rglob("*"):
        # 디렉토리 건너뛰기
        if file_path.is_dir():
            continue
        
        # 처리 대상 파일인지 확인
        if not should_process_file(file_path):
            continue
        
        # 파일 처리
        if replace_in_file(file_path):
            changed_files.append(file_path)
            print(f"변경 완료: {file_path}")
    
    # 요약
    print(f"\n총 {len(changed_files)}개 파일이 변경되었습니다.")
    if not changed_files:
        print("변경할 내용이 없습니다.")


if __name__ == "__main__":
    main()
