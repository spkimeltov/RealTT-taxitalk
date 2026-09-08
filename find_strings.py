#!/usr/bin/env python3
"""관광투어 텍스트를 찾는 스크립트"""

import re
from pathlib import Path

# 찾을 텍스트
TARGET_TEXT = "의료"

# 대상 파일 확장자
TARGET_EXTENSIONS = {".py", ".html", ".css", ".md", ".yml", ".ps1", ".conf"}


def should_process_file(file_path: Path) -> bool:
    """파일이 처리 대상인지 확인"""
    return file_path.suffix.lower() in TARGET_EXTENSIONS


def find_in_file(file_path: Path) -> list:
    """파일 내 텍스트를 찾아서 줄 번호와 내용을 반환"""
    try:
        # 파일 읽기 (UTF-8 인코딩 시도)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        
        # 찾은 결과 저장
        results = []
        
        # 모든 줄을 순회하며 찾기
        for line_num, line in enumerate(lines, 1):
            if TARGET_TEXT in line:
                results.append((line_num, line.strip()))
        
        return results
    except Exception as e:
        print(f"파일 처리 중 오류 발생: {file_path} - {e}")
        return []


def main():
    """메인 함수"""
    # 현재 디렉토리 기준으로 모든 파일 탐색
    root_dir = Path(__file__).parent
    
    # 찾은 파일 목록
    found_files = []
    
    # 모든 파일 순회
    for file_path in root_dir.rglob("*"):
        # 디렉토리 건너뛰기
        if file_path.is_dir():
            continue
        
        # 처리 대상 파일인지 확인
        if not should_process_file(file_path):
            continue
        
        # 파일 처리
        results = find_in_file(file_path)
        if results:
            found_files.append((file_path, results))
    
    # 결과 출력
    if found_files:
        print(f"\"{TARGET_TEXT}\" 텍스트를 찾은 파일:")
        for file_path, results in found_files:
            print(f"\n파일: {file_path}")
            for line_num, line in results:
                print(f"  줄 {line_num}: {line}")
    else:
        print(f"\"{TARGET_TEXT}\" 텍스트를 찾을 수 없습니다.")


if __name__ == "__main__":
    main()
