# TAXI-TALK — 외국인 고객 대화 실시간 통역

성형외과·피부과에서 대화원과 외국인 고객이 마주 앉아 쓰는 통역 화면이다.
버튼을 누르지 않고 그냥 말하면 되고, 통역문은 상대가 읽을 언어로 크게 뜬다.
대화이 끝나면 대화록을 파일로 내려받는다.

접속: <https://ai.tovair.com:48443/demo/taxitalk/>

## 어떻게 동작하나

```
브라우저 마이크 (항상 청취)
  └ pcm-worklet VAD ─ 말 시작 / PCM16 / 무음 700ms 뒤 말 끝
      └ WebSocket /ws/session
          ├ 마이크 1개: 한국어 vs 고객 언어 2택 판별 → 화자 결정
          ├ 마이크 2개: 채널이 곧 화자 (판별 생략)
          └ faster-whisper large-v3 (언어 고정 전사, 용어집 프롬프트)
              └ 용어 표기 보정 → vLLM 스트리밍 번역 (용어 대응표 주입)
                  ├ 말풍선: 들을 사람의 언어 크게 · 말한 사람의 원문 작게
                  └ transcript.jsonl append → TXT / JSON / HTML 다운로드
```

TTS 는 쓰지 않는다. 대화실에서 기계 음성이 사람 말과 겹치면 대화가 끊기고,
화면을 같이 보는 자리라 굳이 읽어 줄 필요가 없다.

## 화면 배치 (1920x1080)

기본은 **한 화면**(`SCREEN_LAYOUT=single`)이다. 두 사람이 같은 화면을 보므로 어느
한쪽 언어를 크게 고정할 수 없다. 그래서 발화마다 **들을 사람의 언어**를 크게 띄우고
말한 사람의 원문을 그 아래에 작게 붙인다. 이름표는 서로 알아볼 수 있게 두 말로
나란히 적는다.

| | 고객이 말했을 때 | 대화원이 말했을 때 |
|---|---|---|
| 말풍선 | 왼쪽 · 초록 | 오른쪽 · 파랑 |
| 큰 글씨 | 한국어 (번역) | 고객 언어 (번역) |
| 작은 글씨 | 고객 언어 (원문) | 한국어 (원문) |
| 이름표 | `고객 · 患者さま` | `대화원 · カウンセラー` |

```bat
chrome.exe --kiosk --window-position=0,0 --window-size=1920,1080 ^
  --autoplay-policy=no-user-gesture-required ^
  "https://ai.tovair.com:48443/demo/taxitalk/"
```

화면 아래에 일시정지 · 기록 · 언어변경 · 대화종료 · 홈으로가 있다. `홈으로`는 종료
처리 없이 화면만 비우고 언어 선택으로 되돌린다.

### 모니터 두 대로 나눌 때 (`SCREEN_LAYOUT=dual`)

모니터 두 대에 **브라우저 창 하나**를 걸쳐 띄우고, CSS 로 좌우를 정확히 반씩
나눈다. 분할선이 물리적 베젤 위치와 겹치므로 각 모니터가 독립 화면처럼 보인다.
이때는 두 사람이 서로 다른 화면을 보므로 각자 자기 언어가 크게 온다.

| | 모니터 1 (좌) | 모니터 2 (우) |
|---|---|---|
| 대상 | 대화원 | 고객 |
| 큰 글씨 | 한국어 | 고객 언어 |
| 작은 글씨 | 고객 언어 | 한국어 |
| 조작 | 아래쪽 버튼 전부 | 없음 (상태등만) |

**F11 전체화면은 쓰지 않는다.** 브라우저 전체화면은 모니터 한 대에만 걸린다.
두 모니터를 가로로 이어 붙인 뒤 아래처럼 띄운다.

```bat
chrome.exe --kiosk --window-position=0,0 --window-size=3840,1080 ^
  --autoplay-policy=no-user-gesture-required ^
  "https://ai.tovair.com:48443/demo/taxitalk/"
```

배치는 `.env` 의 `SCREEN_LAYOUT` 으로 정하고, 주소에 `?screen=single` 이나
`?screen=dual` 을 붙이면 그 자리에서만 바꿔 볼 수 있다.

> 마이크는 HTTPS 또는 localhost 에서만 열린다. HTTP 로 접속하면 권한 요청 자체가
> 뜨지 않는다.

## 언어

STT(faster-whisper large-v3)가 인식하는 99개 가운데, 인식률과 번역 품질이 함께
받쳐 주는 **34개 언어**를 고를 수 있다. 대화원은 한국어로 고정이고, 시작 화면에서
고르는 것은 고객 언어 하나다.

- **카드** — 시작 화면에 15개를 깔아 둔다. `PATIENT_LANGUAGES` 로 정하고, 기본값은
  영어 · 일본어 · 중국어 · 광둥어 · 태국어 · 우즈베크어 · 몽골어 · 베트남어 ·
  러시아어 · 인도네시아어 · 말레이어 · 아랍어 · 프랑스어 · 독일어 · 스페인어다.
  카드마다 국기 · 그 언어 표기 · 한국어 표기를 함께 싣는다. 한 줄에 일곱 장씩
  놓여 7 · 7 · 1 로 떨어진다. 순서는 적은 대로 고정이다. 대화 이력에 따라 자리가
  바뀌면 고객이 매번 카드를 다시 훑어야 한다.
- **안내** — 카드 뒤에 `지원 언어는 지속적으로 추가될 예정입니다` 문구가 붙는다.
  누르는 자리가 아니라 글일 뿐이다. 카드에 없는 언어를 쓰려면 `PATIENT_LANGUAGES`
  에 코드를 넣어 카드로 올린다.
- **안내 띠** — 조작 버튼 바 바로 위에 안전성·개인정보 문구를 한 줄로 둔다. 언어를
  고르는 동안에만 뜨고 대화이 시작되면 접힌다. 문구는 `static/index.html` 의
  `.notice` 에 그대로 적혀 있다.

목록에 있다고 다 같은 품질이 나오지는 않는다. 언어별 학습량 차이를 그대로 등급으로
드러낸다. 판단 기준은 STT 와 번역 **둘 다** 다. 광둥어·몽골어·우즈베크어처럼 받아쓰기는
되는데 번역이 흔들리는 언어가 있어서, 한쪽만 보고 매기면 등급이 과대평가된다.

| 등급 | 언어 | 뜻 |
|---|---|---|
| 권장 | 영어 · 중국어 · 일본어 · 베트남어 · 러시아어 · 태국어 · 아랍어 · 스페인어 · 인도네시아어 | 수요가 많고 인식·번역이 모두 안정적 |
| 지원 | 광둥어 · 몽골어 · 우즈베크어 · 카자흐어 · 프랑스어 · 독일어 등 22개 | 대화에 쓸 만하나 어휘·문체가 어색해질 수 있음 |
| 실험 | 크메르어 · 미얀마어 · 네팔어 3개 | 오인식이 잦다. 고르면 경고를 띄운다 |

언어를 추가하거나 등급을 바꾸려면 `app/config.py` 의 `LANGUAGES` 표 하나만 고치면
된다. 영문명(번역 프롬프트) · 원어 표기(고객이 읽을 자리) · 한국어 표기(대화원이
읽을 자리)를 여기서 함께 관리한다. 고객용 문구(`UI_TEXT`)는 27개 언어까지 번역돼
있고, 없는 언어는 영어로 대체한다. 아랍어처럼 오른쪽에서 왼쪽으로 쓰는 언어는 그
언어로 적힌 줄만 글 방향이 자동으로 뒤집힌다.

## 마이크 1개 / 2개

설정(우측 상단 톱니)에서 고르고 브라우저에 저장된다. 기본값은 `.env` 의 `MIC_MODE`.

- **1개 (`single`)** — 공용 마이크 하나. 발화마다 한국어인지 고객 언어인지 가려
  화자를 정한다. 설치가 간단한 대신 아주 짧은 발화("네", "yes")에서 오판이 날 수
  있다. 확률 차가 `LID_MARGIN` 미만이면 직전 화자의 반대편으로 본다.
- **2개 (`dual`)** — 대화원용·고객용 입력장치를 각각 지정한다. 채널이 곧 화자라
  판별 오류가 없고, 두 사람이 겹쳐 말해도 각각 처리된다. 오디오 프레임 앞의
  2바이트 머리말로 채널을 구분해 WebSocket 하나로 실어 나른다.

## 전문용어

`app/data/glossary/terms.json` 하나로 관리한다. 항목을 추가하면 재기동 시 반영된다.

```json
{ "id": "ultherapy", "priority": 10,
  "forms": { "ko": "울쎄라", "en": "Ultherapy", "zh": "超声刀", "yue": "超聲刀",
             "ja": "ウルセラ", "vi": "Ultherapy", "ru": "Ultherapy", "th": "Ultherapy",
             "mn": "Ultherapy", "uz": "Ultherapy", "ar": "ألثيرابي",
             "es": "Ultherapy", "id": "Ultherapy" },
  "alias":  { "ko": ["울세라", "울쓰라", "울쎄라리프팅"] } }
```

용어 30개에 **13개 언어**(한국어 + 권장 9개 + 광둥어·몽골어·우즈베크어) 표기가 들어
있다. 카드에서 내린 셋도 여전히 고를 수 있으므로 용어집에는 남겨 두었다. `forms` 에 없는 언어는
대응표를 붙이지 못하고 번역 모델의 일반 지식에 맡기게 되므로, 자주 오는 언어가
따로 있다면 그 코드를 `forms` 에 추가하는 편이 가장 효과가 크다.

세 군데에 쓰인다.

1. **STT 편향** — `priority` 높은 순으로 `GLOSSARY_PROMPT_TERMS` 개를 whisper
   `initial_prompt` 에 넣어 그 어휘로 받아쓰게 한다. 이 프롬프트는 224 토큰까지만
   반영되는데, 태국어·아랍어·몽골어처럼 문자당 토큰이 많은 언어는 30개를 다 넣으면
   한도에 가까워진다. 넘치면 whisper 가 앞쪽(문맥 문장)부터 잘라내고 용어는 남긴다.
2. **번역 대응표** — 원문에 실제로 나온 용어만 골라 system 프롬프트에 붙인다.
   사전 전체를 넣으면 토큰만 먹고 모델이 엉뚱한 용어를 끌어 쓴다.
3. **표기 보정** — `alias` 를 `forms` 로 되돌린다 (`울세라` → `울쎄라`).

`alias` 는 STT 가 실제로 자주 틀리는 표기만 넣는다. 무르게 잡으면 멀쩡한 말이
바뀐다.

## 대화기록

발화가 확정될 때마다 한 줄씩 덧붙이므로 도중에 브라우저가 죽어도 남는다.

```
data/sessions/20260806_143022_en_a1b2/
  meta.json         언어쌍 · 시작/종료 시각 · 발화 수
  transcript.jsonl  {seq, at, speaker, src, dst, original, translated, metrics}
```

대화 중에도 `기록` 버튼으로 그때까지의 내용을 받을 수 있다. PDF 는 만들지 않는다.
인쇄용 HTML 을 브라우저에서 `PDF 로 저장` 하면 된다.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/health` | STT 로드 상태, 모델, 용어 수 |
| GET | `/api/config` | 언어 목록(전체·카드·화면 문구), VAD 값, 화면/마이크 기본값 |
| POST | `/api/sessions` | `{patient_lang, mic_mode}` → 세션 발급 |
| GET | `/api/sessions` | 최근 대화 목록 |
| GET | `/api/sessions/{id}` | 대화록 JSON |
| POST | `/api/sessions/{id}/close` | 종료 시각·발화 수 확정 |
| GET | `/api/sessions/{id}/export?format=txt\|json\|html` | 파일 다운로드 |
| WS | `/ws/session?session={id}` | 오디오 업로드 / 결과 수신 |

`APP_API_KEY` 를 채우면 모든 경로에 `?key=...` 가 필요하다.

### WebSocket 프로토콜

클라이언트 → 서버

| | |
|---|---|
| `{"type":"start","side":"shared\|staff\|patient"}` | 발화 시작 |
| 바이너리 `[채널 코드][0x00][PCM16LE…]` | 16kHz mono. 코드 0=shared, 1=staff, 2=patient |
| `{"type":"end","side":…}` | 발화 종료 → 파이프라인 시작 |
| `{"type":"cancel","side":…}` / `{"type":"reset"}` / `{"type":"ping"}` | |

서버 → 클라이언트

| | |
|---|---|
| `ready` | 세션 정보와 모델 이름 |
| `turn_start` | 말풍선 자리 확보 |
| `turn_speaker` | 마이크 1개 모드에서 화자가 정해진 시점 |
| `partial` | 중간 자막 (`PARTIAL_INTERVAL_SEC` 주기) |
| `transcript` | 확정 원문 |
| `translation_delta` / `translation_done` | 번역 스트리밍 |
| `turn_done` | 지연 metrics 포함 |
| `empty` / `cancelled` / `warning` / `error` | |

## 설정

`.env.example` 을 복사해 쓴다. 자주 만지는 값만 추린다.

| 키 | 기본 | 설명 |
|---|---|---|
| `PATIENT_LANGUAGES` | 권장 9개 | 시작 화면에 카드로 뜨는 언어. 비워두면 기본값 |
| `ENABLED_LANGUAGES` | `all` | 고를 수 있는 전체 언어. `all` 은 카탈로그 34개 |
| `MIC_MODE` | `single` | 마이크 기본 모드 |
| `SCREEN_LAYOUT` | `single` | `dual` 이면 3840 창을 좌우로 나눠 모니터 두 대에 건다 |
| `VAD_SILENCE_MS` | `700` | 이만큼 조용하면 발화 종료 |
| `VAD_RMS_THRESHOLD` | `0.012` | 하한선. 실제 임계값은 잡음 바닥에 맞춰 자동으로 올라간다 |
| `PARTIAL_INTERVAL_SEC` | `1.2` | 중간 자막 주기. `0` 이면 끄고 GPU 를 아낀다 |
| `LID_MARGIN` | `0.15` | 확률 차가 이보다 작으면 직전 화자의 반대편으로 추정 |
| `STT_BEAM_SIZE` | `5` | 확정 전사 품질/속도 |
| `MAX_UTTERANCE_SEC` | `60` | 서버 강제 종료 안전판 |

## 배포

```powershell
cd taxitalk
./deploy/deploy.ps1                       # 전체
./deploy/deploy.ps1 -SkipBuild -SkipNginx # 코드만 갱신 후 재시작
```

- 컨테이너 `taxitalk-web`, 호스트 `127.0.0.1:18092`, nginx `/demo/taxitalk/`
- 서버 `.env` 는 **새로 생긴 키만 이어붙이고 기존 값은 덮지 않는다.** 창구에서 맞춰
  둔 값을 배포가 되돌리지 않게 하려는 것인데, 반대로 `.env.example` 의 기본값을
  바꿔도 서버에는 반영되지 않는다. 기본값을 따르게 하려면 서버 `.env` 에서 해당
  줄을 직접 비운다(`PATIENT_LANGUAGES=`)
- 베이스 이미지 `stt-api-stt-admin-api:latest` — faster-whisper/CUDA 조합이 검증돼
  있고 fastapi·httpx·numpy 가 이미 들어 있어 추가 설치가 없다
- GPU 0 고정. GPU 1~3 은 vLLM 이 거의 포화 상태다
- 공용 STT 의 HF 캐시(`/home/aiuser/stt-api/models`)를 읽기 전용으로 마운트해
  large-v3 를 다시 내려받지 않는다

## 검증

```bash
KEY=$(sudo docker exec interpreter-web printenv TTS_API_KEY)
sudo docker cp deploy/e2e_test.py taxitalk-web:/tmp/e2e_test.py
sudo docker exec -e TTS_API_KEY="$KEY" taxitalk-web python3 -W ignore /tmp/e2e_test.py
```

시험 발화를 마이크처럼 실시간 속도로 흘려보내 화자 판별·용어 반영·지연을 잰다.
목표는 **발화 종료 후 번역문이 다 찍히기까지 1초 이내**. TTS 는 여기서 시험
음성을 만드는 데만 쓴다.

## 1단계에서 제외

AI 대화요약(2단계), 예약·CRM 연계(3단계), TTS 음성출력, 여러 대화실을 한 번에
보는 대시보드.
