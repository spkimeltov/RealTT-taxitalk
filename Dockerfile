# 기존 STT 컨테이너 이미지를 베이스로 쓴다. faster-whisper 1.2.1 + ctranslate2 4.7.2 +
# CUDA/cuDNN 조합이 이 서버에서 이미 검증됐고, fastapi / uvicorn / httpx / numpy 도
# 모두 들어 있어 추가 설치가 없다.
FROM stt-api-stt-admin-api:latest

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/root/.cache/huggingface

# 베이스 이미지의 /app(STT 관리 API)과 섞이지 않게 별도 경로에 올린다.
WORKDIR /srv/taxitalk
COPY app ./app
COPY static ./static

# 대화기록이 쌓이는 곳. compose 에서 호스트 디렉터리를 덮어씌운다.
RUN mkdir -p /srv/taxitalk/data/sessions

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "60"]
