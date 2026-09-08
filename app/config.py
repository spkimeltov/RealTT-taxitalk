"""TAXI-TALK 런타임 설정과 언어 표기.

interpreter 서비스의 `Settings.from_env()` 패턴을 따르되 TTS 항목을 모두 걷어내고,
듀얼 화면 · 마이크 모드 · 대화기록 항목을 더했다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# 브라우저가 올려보내는 오디오 규격. faster-whisper 입력과 같게 맞춘다.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
BYTES_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH

# 기사는 항상 한국어다. 대화 시작 시 고르는 것은 탑승객 언어 하나뿐이다.
STAFF_LANG = "ko"

SIDE_STAFF = "staff"
SIDE_PATIENT = "patient"
# 마이크 1개 모드에서 올라오는 채널. 화자는 언어 판별로 정한다.
SIDE_SHARED = "shared"

# 오디오 프레임 앞에 붙는 2바이트 머리말의 첫 바이트. WebSocket 하나로 마이크 두
# 개를 실어 나르려면 프레임마다 어느 채널인지 표시해야 한다. 두 번째 바이트는
# 패딩이며, 이 덕분에 PCM16 본문이 2바이트 경계에 정렬된 채로 남는다.
SIDE_BY_CODE = {0: SIDE_SHARED, 1: SIDE_STAFF, 2: SIDE_PATIENT}
CODE_BY_SIDE = {side: code for code, side in SIDE_BY_CODE.items()}

# 마이크 1개는 공용 채널로 올라와 화자를 언어로 가른다. 2개는 채널이 곧 화자다.
MIC_SINGLE = "single"
MIC_DUAL = "dual"
MIC_MODES = (MIC_SINGLE, MIC_DUAL)
SCREEN_LAYOUTS = ("single", "dual")

# --------------------------------------------------------------- 지역 사투리
# JEJUMA-002 게이트웨이가 받는 region 코드와 화면에 뜨는 이름. 코드는 게이트웨이의
# Region 리터럴과 한 글자도 다르면 400 이 돌아온다(`jeonla`, `jeolla` 가 아니다).
# 여기 적은 순서가 곧 설정창 선택 상자의 순서다.
DIALECT_REGIONS: dict[str, str] = {
    "gyeongsang": "경상",
    "jeonla": "전라",
    "chungcheong": "충청",
    "gangwon": "강원",
    "jeju": "제주",
}
DIALECT_REGION_CODES = tuple(DIALECT_REGIONS)

# 번역 프롬프트에 적을 영문 지역명. 표준어 변환이 실패해 사투리 원문을 그대로
# 번역할 때, 어느 지역 말인지 알려 주는 문단에 들어간다. 게이트웨이 코드(`jeonla`)와
# 영어에서 통용되는 표기(`Jeolla`)가 달라 따로 둔다.
DIALECT_REGION_NAMES: dict[str, str] = {
    "gyeongsang": "Gyeongsang",
    "jeonla": "Jeolla",
    "chungcheong": "Chungcheong",
    "gangwon": "Gangwon",
    "jeju": "Jeju",
}

# --------------------------------------------------------------- 언어 카탈로그
# faster-whisper large-v3 가 인식하는 99개 가운데, 인식률과 번역 품질이 함께 받쳐
# 주는 34개만 남겼다. 이 표가 곧 서비스가 다룰 수 있는 언어의 상한이다. 뺀 언어는
# 받아쓰기 자체가 안 되는 것도 있지만 대부분은 대화에 쓸 만한 문장이 나오지 않아
# 내린 것이라, 되살릴 때는 아래 등급 기준을 다시 따져 보고 넣는다.
#
#     코드: (영문명, 원어 표기, 한국어 표기, 등급)
#
# 영문명은 번역 프롬프트에, 원어 표기는 탑승객이 읽을 자리에, 한국어 표기는 기사가
# 읽을 자리에 쓴다. 등급은 언어별 학습량 차이를 화면에 그대로 드러내려고 붙였다. 목록에 있다고
# 다 같은 품질이 나오지는 않는다.
#
# 판단 기준은 STT 와 번역 **둘 다** 다. 광둥어·몽골어·우즈베크어처럼 받아쓰기는
# 되는데 번역이 흔들리는 언어가 있어서, 한쪽만 보고 매기면 등급이 과대평가된다.
#
#     1 권장  국내 의료관광 수요가 많고 인식·번역이 모두 안정적이다.
#     2 지원  대화에 쓸 만하다. 다만 어휘나 문체가 어색해지는 경우가 있다.
#     3 실험  학습량이 적어 오인식이 잦다. 다른 수단이 없을 때만 쓴다.
TIER_RECOMMENDED = 1
TIER_SUPPORTED = 2
TIER_EXPERIMENTAL = 3

LANGUAGES: dict[str, tuple[str, str, str, int]] = {
    # --- 기사 ---
    "ko": ("Korean", "한국어", "한국어", TIER_RECOMMENDED),
    # --- 권장 ---
    "en": ("English", "English", "영어", TIER_RECOMMENDED),
    "zh": ("Chinese", "中文", "중국어", TIER_RECOMMENDED),
    "ja": ("Japanese", "日本語", "일본어", TIER_RECOMMENDED),
    "vi": ("Vietnamese", "Tiếng Việt", "베트남어", TIER_RECOMMENDED),
    "ru": ("Russian", "Русский", "러시아어", TIER_RECOMMENDED),
    "th": ("Thai", "ไทย", "태국어", TIER_RECOMMENDED),
    "ar": ("Arabic", "العربية", "아랍어", TIER_RECOMMENDED),
    "es": ("Spanish", "Español", "스페인어", TIER_RECOMMENDED),
    "id": ("Indonesian", "Indonesia", "인도네시아어", TIER_RECOMMENDED),
    # --- 지원 ---
    "cs": ("Czech", "Čeština", "체코어", TIER_SUPPORTED),
    "da": ("Danish", "Dansk", "덴마크어", TIER_SUPPORTED),
    "de": ("German", "Deutsch", "독일어", TIER_SUPPORTED),
    "el": ("Greek", "Ελληνικά", "그리스어", TIER_SUPPORTED),
    "fa": ("Persian", "فارسی", "페르시아어", TIER_SUPPORTED),
    "fi": ("Finnish", "Suomi", "핀란드어", TIER_SUPPORTED),
    "fr": ("French", "Français", "프랑스어", TIER_SUPPORTED),
    "hi": ("Hindi", "हिन्दी", "힌디어", TIER_SUPPORTED),
    "it": ("Italian", "Italiano", "이탈리아어", TIER_SUPPORTED),
    "kk": ("Kazakh", "Қазақша", "카자흐어", TIER_SUPPORTED),
    "mn": ("Mongolian", "Монгол", "몽골어", TIER_SUPPORTED),
    "ms": ("Malay", "Melayu", "말레이어", TIER_SUPPORTED),
    "nl": ("Dutch", "Nederlands", "네덜란드어", TIER_SUPPORTED),
    "pl": ("Polish", "Polski", "폴란드어", TIER_SUPPORTED),
    "pt": ("Portuguese", "Português", "포르투갈어", TIER_SUPPORTED),
    "ro": ("Romanian", "Română", "루마니아어", TIER_SUPPORTED),
    "sv": ("Swedish", "Svenska", "스웨덴어", TIER_SUPPORTED),
    "tl": ("Tagalog", "Tagalog", "타갈로그어", TIER_SUPPORTED),
    "tr": ("Turkish", "Türkçe", "튀르키예어", TIER_SUPPORTED),
    "uk": ("Ukrainian", "Українська", "우크라이나어", TIER_SUPPORTED),
    "uz": ("Uzbek", "Oʻzbekcha", "우즈베크어", TIER_SUPPORTED),
    "yue": ("Cantonese", "廣東話", "광둥어", TIER_SUPPORTED),
    # --- 실험 ---
    # 인식·번역 모두 불안하지만 국내 의료관광 수요가 있어 남겨 둔 언어다.
    "km": ("Khmer", "ភាសាខ្មែរ", "크메르어", TIER_EXPERIMENTAL),
    "my": ("Myanmar", "မြန်မာ", "미얀마어", TIER_EXPERIMENTAL),
    "ne": ("Nepali", "नेपाली", "네팔어", TIER_EXPERIMENTAL),
}

# 오른쪽에서 왼쪽으로 쓰는 언어. 탑승객 화면의 글 방향을 뒤집는다.
RTL_LANGS = frozenset({"ar", "fa"})

# 아래 세 표는 카탈로그에서 뽑아 쓴다. 언어를 늘릴 때는 `LANGUAGES` 만 고치면 된다.
LANG_NAMES = {code: entry[0] for code, entry in LANGUAGES.items()}
LANG_LABELS = {code: entry[1] for code, entry in LANGUAGES.items()}
LANG_LABELS_KO = {code: entry[2] for code, entry in LANGUAGES.items()}
LANG_TIERS = {code: entry[3] for code, entry in LANGUAGES.items()}

# 라틴 문자로 적는 언어. 용어집이 단어 경계를 봐야 할지 판단하는 데 쓴다.
LATIN_SCRIPT_LANGS = frozenset(
    {
        "cs", "da", "de", "en", "es", "fi", "fr", "id", "it", "ms", "nl", "pl",
        "pt", "ro", "sv", "tl", "tr", "uz", "vi",
    }
)

# 시작 화면에 카드로 띄울 기본 언어 15개. `PATIENT_LANGUAGES` 로 덮어쓴다.
# 여기 적은 순서가 곧 카드가 놓이는 순서다. 탑승객이 늘 같은 자리에서 자기 언어를
# 찾도록 대화 이력에 따라 자리를 바꾸지 않으므로, 자주 오는 언어를 앞에 둔다.
# 한 줄에 일곱 장씩 놓이므로 7 · 7 · 1 로 떨어진다.
DEFAULT_PATIENT_LANGUAGES = (
    "en", "ja", "zh", "yue", "th", "uz", "mn",
    "vi", "ru", "id", "ms", "ar", "fr", "de", "es",
)

# 탑승객 화면은 탑승객 언어로만 채운다. 등록되지 않은 언어는 영어로 보여준다.
UI_TEXT = {
    "ko": {
        "staff": "택시기사",
        "patient": "탑승객",
        "listening": "듣는 중",
        "paused": "일시정지",
        "connecting": "연결 중",
        "offline": "연결 끊김",
        "waiting": "통역을 준비하고 있습니다",
        "select_hint": "사용하실 언어를 선택해 주세요",
        "ended": "대화가 종료되었습니다",
        "thanks": "이용해 주셔서 감사합니다",
    },
    "en": {
        "staff": "Driver",
        "patient": "Passenger",
        "listening": "Listening",
        "paused": "Paused",
        "connecting": "Connecting",
        "offline": "Disconnected",
        "waiting": "Preparing your interpretation",
        "select_hint": "Please select your language",
        "ended": "The conversation has ended",
        "thanks": "Thank you for riding with us",
    },
    "zh": {
        "staff": "司机",
        "patient": "乘客",
        "listening": "正在聆听",
        "paused": "已暂停",
        "connecting": "连接中",
        "offline": "连接已断开",
        "waiting": "正在准备翻译",
        "select_hint": "请选择您使用的语言",
        "ended": "对话已结束",
        "thanks": "感谢您的乘坐",
    },
    "ja": {
        "staff": "運転手",
        "patient": "お客さま",
        "listening": "認識中",
        "paused": "一時停止",
        "connecting": "接続中",
        "offline": "接続が切れました",
        "waiting": "通訳の準備をしています",
        "select_hint": "ご利用の言語をお選びください",
        "ended": "会話が終了しました",
        "thanks": "ご乗車ありがとうございました",
    },
    "vi": {
        "staff": "Tài xế",
        "patient": "Hành khách",
        "listening": "Đang nghe",
        "paused": "Tạm dừng",
        "connecting": "Đang kết nối",
        "offline": "Mất kết nối",
        "waiting": "Đang chuẩn bị phiên dịch",
        "select_hint": "Vui lòng chọn ngôn ngữ của bạn",
        "ended": "Cuộc trò chuyện đã kết thúc",
        "thanks": "Cảm ơn quý khách đã đi xe",
    },
    "ru": {
        "staff": "Водитель",
        "patient": "Пассажир",
        "listening": "Слушаю",
        "paused": "Пауза",
        "connecting": "Подключение",
        "offline": "Соединение потеряно",
        "waiting": "Готовим перевод",
        "select_hint": "Выберите ваш язык",
        "ended": "Разговор завершён",
        "thanks": "Спасибо за поездку",
    },
    "yue": {
        "staff": "司機",
        "patient": "乘客",
        "listening": "聆聽中",
        "paused": "已暫停",
        "connecting": "連接中",
        "offline": "連接已中斷",
        "waiting": "正在準備翻譯",
        "select_hint": "請揀你使用嘅語言",
        "ended": "對話已結束",
        "thanks": "多謝你嘅乘坐",
    },
    "th": {
        "staff": "คนขับรถ",
        "patient": "ผู้โดยสาร",
        "listening": "กำลังฟัง",
        "paused": "หยุดชั่วคราว",
        "connecting": "กำลังเชื่อมต่อ",
        "offline": "การเชื่อมต่อขาดหาย",
        "waiting": "กำลังเตรียมการแปล",
        "select_hint": "กรุณาเลือกภาษาที่ท่านใช้",
        "ended": "การสนทนาสิ้นสุดแล้ว",
        "thanks": "ขอบคุณที่ใช้บริการ",
    },
    "mn": {
        "staff": "Жолооч",
        "patient": "Зорчигч",
        "listening": "Сонсож байна",
        "paused": "Түр зогсоов",
        "connecting": "Холбогдож байна",
        "offline": "Холболт тасарлаа",
        "waiting": "Орчуулгад бэлтгэж байна",
        "select_hint": "Хэрэглэх хэлээ сонгоно уу",
        "ended": "Харилцан яриа дууслаа",
        "thanks": "Аялсанд баярлалаа",
    },
    "uz": {
        "staff": "Haydovchi",
        "patient": "Yoʻlovchi",
        "listening": "Tinglanmoqda",
        "paused": "Toʻxtatildi",
        "connecting": "Ulanmoqda",
        "offline": "Aloqa uzildi",
        "waiting": "Tarjima tayyorlanmoqda",
        "select_hint": "Iltimos, tilingizni tanlang",
        "ended": "Suhbat yakunlandi",
        "thanks": "Safaringiz uchun rahmat",
    },
    "ar": {
        "staff": "السائق",
        "patient": "الراكب",
        "listening": "جارٍ الاستماع",
        "paused": "متوقف مؤقتًا",
        "connecting": "جارٍ الاتصال",
        "offline": "انقطع الاتصال",
        "waiting": "نقوم بتجهيز الترجمة",
        "select_hint": "يرجى اختيار لغتك",
        "ended": "انتهت المحادثة",
        "thanks": "شكرًا لرحلتكم معنا",
    },
    "es": {
        "staff": "Conductor",
        "patient": "Pasajero",
        "listening": "Escuchando",
        "paused": "En pausa",
        "connecting": "Conectando",
        "offline": "Conexión perdida",
        "waiting": "Preparando la traducción",
        "select_hint": "Seleccione su idioma",
        "ended": "La conversación ha finalizado",
        "thanks": "Gracias por viajar con nosotros",
    },
    "id": {
        "staff": "Pengemudi",
        "patient": "Penumpang",
        "listening": "Mendengarkan",
        "paused": "Dijeda",
        "connecting": "Menghubungkan",
        "offline": "Koneksi terputus",
        "waiting": "Menyiapkan penerjemahan",
        "select_hint": "Silakan pilih bahasa Anda",
        "ended": "Percakapan telah selesai",
        "thanks": "Terima kasih telah menggunakan layanan kami",
    },
    "ms": {
        "staff": "Pemandu",
        "patient": "Penumpang",
        "listening": "Sedang mendengar",
        "paused": "Dijeda",
        "connecting": "Menyambung",
        "offline": "Sambungan terputus",
        "waiting": "Sedang menyediakan terjemahan",
        "select_hint": "Sila pilih bahasa anda",
        "ended": "Perbualan telah tamat",
        "thanks": "Terima kasih kerana menggunakan perkhidmatan kami",
    },
    "tl": {
        "staff": "Drayber",
        "patient": "Pasahero",
        "listening": "Nakikinig",
        "paused": "Naka-pause",
        "connecting": "Kumokonekta",
        "offline": "Nawalan ng koneksyon",
        "waiting": "Inihahanda ang pagsasalin",
        "select_hint": "Pakipili ang inyong wika",
        "ended": "Tapos na ang pag-uusap",
        "thanks": "Salamat sa pagsakay",
    },
    "fr": {
        "staff": "Chauffeur",
        "patient": "Passager",
        "listening": "Écoute en cours",
        "paused": "En pause",
        "connecting": "Connexion en cours",
        "offline": "Connexion perdue",
        "waiting": "Préparation de la traduction",
        "select_hint": "Veuillez choisir votre langue",
        "ended": "La conversation est terminée",
        "thanks": "Merci d’avoir voyagé avec nous",
    },
    "de": {
        "staff": "Fahrer",
        "patient": "Fahrgast",
        "listening": "Hört zu",
        "paused": "Pausiert",
        "connecting": "Verbindung wird hergestellt",
        "offline": "Verbindung getrennt",
        "waiting": "Die Übersetzung wird vorbereitet",
        "select_hint": "Bitte wählen Sie Ihre Sprache",
        "ended": "Das Gespräch ist beendet",
        "thanks": "Danke für Ihre Fahrt",
    },
    "pt": {
        "staff": "Motorista",
        "patient": "Passageiro",
        "listening": "Ouvindo",
        "paused": "Pausado",
        "connecting": "Conectando",
        "offline": "Conexão perdida",
        "waiting": "Preparando a tradução",
        "select_hint": "Selecione seu idioma",
        "ended": "A conversa foi encerrada",
        "thanks": "Obrigado pela viagem",
    },
    "it": {
        "staff": "Autista",
        "patient": "Passeggero",
        "listening": "In ascolto",
        "paused": "In pausa",
        "connecting": "Connessione in corso",
        "offline": "Connessione persa",
        "waiting": "Stiamo preparando la traduzione",
        "select_hint": "Selezioni la sua lingua",
        "ended": "La conversazione è terminata",
        "thanks": "Grazie per il viaggio",
    },
    "pl": {
        "staff": "Kierowca",
        "patient": "Pasażer",
        "listening": "Słucham",
        "paused": "Wstrzymano",
        "connecting": "Łączenie",
        "offline": "Utracono połączenie",
        "waiting": "Przygotowujemy tłumaczenie",
        "select_hint": "Proszę wybrać swój język",
        "ended": "Rozmowa zakończona",
        "thanks": "Dziękujemy za podróż",
    },
    "uk": {
        "staff": "Водій",
        "patient": "Пасажир",
        "listening": "Слухаю",
        "paused": "Пауза",
        "connecting": "Підключення",
        "offline": "З’єднання втрачено",
        "waiting": "Готуємо переклад",
        "select_hint": "Оберіть вашу мову",
        "ended": "Розмову завершено",
        "thanks": "Дякуємо за поїздку",
    },
    "kk": {
        "staff": "Жүргізуші",
        "patient": "Жолаушы",
        "listening": "Тыңдалуда",
        "paused": "Кідіртілді",
        "connecting": "Қосылуда",
        "offline": "Байланыс үзілді",
        "waiting": "Аудармаға дайындалудамыз",
        "select_hint": "Тіліңізді таңдаңыз",
        "ended": "Сөйлесу аяқталды",
        "thanks": "Сапарыңыз үшін рақмет",
    },
    "tr": {
        "staff": "Şoför",
        "patient": "Yolcu",
        "listening": "Dinleniyor",
        "paused": "Duraklatıldı",
        "connecting": "Bağlanıyor",
        "offline": "Bağlantı kesildi",
        "waiting": "Çeviri hazırlanıyor",
        "select_hint": "Lütfen dilinizi seçin",
        "ended": "Görüşme sona erdi",
        "thanks": "Yolculuğunuz için teşekkürler",
    },
    "hi": {
        "staff": "चालक",
        "patient": "यात्री",
        "listening": "सुन रहे हैं",
        "paused": "रोका गया",
        "connecting": "कनेक्ट हो रहा है",
        "offline": "कनेक्शन टूट गया",
        "waiting": "अनुवाद तैयार किया जा रहा है",
        "select_hint": "कृपया अपनी भाषा चुनें",
        "ended": "बातचीत समाप्त हो गई",
        "thanks": "यात्रा के लिए धन्यवाद",
    },
    "ne": {
        "staff": "चालक",
        "patient": "यात्रु",
        "listening": "सुन्दै",
        "paused": "रोकिएको",
        "connecting": "जडान हुँदै",
        "offline": "जडान टुट्यो",
        "waiting": "अनुवादको तयारी हुँदैछ",
        "select_hint": "कृपया आफ्नो भाषा छान्नुहोस्",
        "ended": "कुराकानी समाप्त भयो",
        "thanks": "यात्राको लागि धन्यवाद",
    },
    "km": {
        "staff": "អ្នកបើកបរ",
        "patient": "អ្នកដំណើរ",
        "listening": "កំពុងស្តាប់",
        "paused": "បានផ្អាក",
        "connecting": "កំពុងតភ្ជាប់",
        "offline": "ការតភ្ជាប់ត្រូវបានផ្តាច់",
        "waiting": "កំពុងរៀបចំការបកប្រែ",
        "select_hint": "សូមជ្រើសរើសភាសារបស់អ្នក",
        "ended": "ការសន្ទនាបានបញ្ចប់",
        "thanks": "អរគុណសម្រាប់ការធ្វើដំណើរ",
    },
    "my": {
        "staff": "ကားမောင်းသူ",
        "patient": "ခရီးသည်",
        "listening": "နားထောင်နေသည်",
        "paused": "ခေတ္တရပ်ထားသည်",
        "connecting": "ချိတ်ဆက်နေသည်",
        "offline": "ချိတ်ဆက်မှု ပြတ်တောက်သွားသည်",
        "waiting": "ဘာသာပြန်ကို ပြင်ဆင်နေပါသည်",
        "select_hint": "သင်အသုံးပြုမည့် ဘာသာစကားကို ရွေးချယ်ပါ",
        "ended": "စကားဝိုင်း ပြီးဆုံးပါပြီ",
        "thanks": "စီးနင်းသည့်အတွက် ကျေးဇူးတင်ပါသည်",
    },
}


def _s(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(_s(key) or default)
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(_s(key) or default)
    except ValueError:
        return default


def _b(key: str, default: bool) -> bool:
    raw = _s(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _choice(key: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _s(key, default).lower()
    return value if value in allowed else default


def _floats(key: str, default: tuple[float, ...]) -> tuple[float, ...]:
    """쉼표로 나열한 실수를 읽는다. 하나라도 숫자가 아니면 기본값을 쓴다."""
    raw = _s(key)
    if not raw:
        return default
    try:
        values = tuple(float(x.strip()) for x in raw.split(",") if x.strip())
    except ValueError:
        log.warning("%s 를 읽지 못해 기본값을 씁니다: %s", key, raw)
        return default
    return values or default


def _langs(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """쉼표로 나열한 언어 코드를 읽는다. `all` 이면 카탈로그 전체.

    카탈로그에 없는 코드는 STT 가 받지 못하므로 조용히 버린다. 오타 하나로 서비스가
    기동하지 못하는 편보다는, 남은 목록으로 뜨고 로그를 남기는 편이 낫다.
    """
    tokens = [x.strip().lower() for x in _s(key).split(",") if x.strip()]
    if not tokens:
        return default
    if "all" in tokens:
        return tuple(code for code in LANGUAGES if code != STAFF_LANG)

    codes, unknown = [], []
    for token in tokens:
        if token in LANGUAGES and token != STAFF_LANG:
            codes.append(token)
        else:
            unknown.append(token)
    if unknown:
        log.warning("%s 의 알 수 없는 언어 코드를 건너뜁니다: %s", key, ", ".join(unknown))
    return tuple(dict.fromkeys(codes)) or default


@dataclass(frozen=True)
class Settings:
    # --- STT (컨테이너 내장, GPU 0) ---
    whisper_model: str = "large-v3"
    # 읽기 전용으로 마운트한 기존 HF 캐시. 여기서 찾으면 재다운로드하지 않는다.
    whisper_cache_dirs: tuple[str, ...] = ()
    whisper_device: str = "cuda"
    whisper_device_index: int = 0
    whisper_compute_type: str = "float16"
    stt_workers: int = 2
    stt_beam_size: int = 5
    stt_beam_size_partial: int = 1
    # 확정 전사의 온도 후퇴 단계. 값이 하나면 후퇴가 없다.
    # whisper 는 디코딩 결과가 자기 검사(반복 비율, 평균 로그확률)를 통과하지 못하면
    # 다음 온도로 다시 디코딩한다. 단계가 하나뿐이면 물러설 곳이 없어서, 빔 탐색이
    # 같은 말을 되풀이하며 무너진 결과("다, 다, 다, …")를 그대로 내보낸다.
    # 실패한 구간에서만 추가 디코딩이 붙으므로 정상 발화의 지연은 그대로다.
    stt_temperatures: tuple[float, ...] = (0.0, 0.2, 0.4)
    # 전사 평균 로그확률이 이보다 낮으면 발화를 버린다. 언어를 무엇으로 읽어도
    # 그럴듯하지 않았다는 뜻이고, 그런 결과는 대개 지어낸 문장이다.
    stt_min_logprob: float = -1.0
    # 발화 중 중간 자막 주기(초). 0 이면 중간 자막을 만들지 않는다.
    partial_interval_sec: float = 1.2
    min_utterance_sec: float = 0.4
    max_utterance_sec: float = 60.0

    # --- 언어 판별 (마이크 1개 모드에서만) ---
    # 두 후보의 확률 차가 이보다 작으면 판별을 믿지 않고 직전 화자의 반대편으로 본다.
    lid_margin: float = 0.15
    # 이만큼 쌓이기 전에는 판별하지 않는다. 짧은 조각은 오판이 잦다.
    lid_min_sec: float = 0.8

    # --- VAD (브라우저가 실행한다. 서버는 값만 내려보낸다) ---
    vad_rms_threshold: float = 0.012
    vad_silence_ms: int = 700
    vad_min_speech_ms: int = 250
    # 말이 시작되기 직전 구간도 함께 보내 첫 음절이 잘리지 않게 한다.
    vad_prespeech_ms: int = 300

    # --- 번역 (vLLM) ---
    vllm_base_url: str = "http://vllm-gpt-oss-120b:8000/v1"
    vllm_model: str = "qwen3-coder-next"
    vllm_api_key: str = "EMPTY"
    vllm_timeout_sec: float = 60.0
    vllm_temperature: float = 0.2
    vllm_max_tokens: int = 512
    history_turns: int = 3

    # --- 방언 (한국어 화자 쪽에만 적용) ---
    # 사투리는 두 경로로 다룬다. 화면에서 켜고 끄는 것은 아래 `dialect_*` 쪽이다.
    #
    # 1. **표준어 변환** — JEJUMA-002 게이트웨이에 발화를 보내 표준어로 옮기고, 그
    #    표준어를 번역 입력으로 쓴다. 사투리 원문은 기사 화면과 대화기록에 남는다.
    # 2. **뜻풀이 주석** — 1이 실패하거나 꺼져 있을 때의 경로다. 원문을 고치지 않고
    #    `data/dialect/<지역>.json` 의 뜻풀이만 번역 프롬프트에 붙인다.
    #
    # 어느 경로든 whisper `initial_prompt` 에는 사투리 표기를 들려준다. 받아쓰기가
    # 표준어로 새면 변환할 사투리가 애초에 남지 않는다.
    ko_dialect: str = "off"
    dialect_path: str = ""
    # whisper initial_prompt 에 섞을 사투리 예시 수. 용어집 프롬프트와 224 토큰을
    # 나눠 쓰므로 넉넉히 잡으면 용어가 밀린다.
    dialect_prompt_words: int = 8
    # 번역 프롬프트에 붙일 뜻풀이 수의 상한.
    dialect_max_notes: int = 8
    # 화면에서 지역을 고르지 않았을 때 쓰는 기본 지역.
    dialect_region: str = "gyeongsang"
    # 설정창 체크박스의 초기 상태. 기사가 한 번 바꾸면 그 브라우저에 저장된다.
    dialect_default_on: bool = False

    # --- 표준어 변환 (JEJUMA-002 게이트웨이) ---
    # 개발 중에는 사내망 `192.168.0.208`, 배포 서버에서는 공인 `106.254.227.226` 이다.
    # 주소나 키가 비어 있으면 변환 경로 자체를 끄고 설정창에서 체크박스를 감춘다.
    jejuma_base_url: str = "http://192.168.0.208:48125"
    jejuma_api_key: str = ""
    # 번역 앞에 끼어드는 왕복이라 넉넉히 잡으면 통역 전체가 늦어진다. 넘기면 원문
    # 그대로 번역하고 뜻풀이 경로로 되돌아간다.
    jejuma_timeout_sec: float = 3.0
    jejuma_temperature: float = 0.1
    jejuma_max_tokens: int = 256

    # --- 전문용어 ---
    glossary_path: str = ""
    # whisper initial_prompt 는 224 토큰까지만 반영된다. 지명은 한 개가 길어서 24개면
    # 한도에 가깝다. 넣을 용어 수를 제한한다.
    glossary_prompt_terms: int = 24

    # --- 대화기록 ---
    session_dir: str = "/srv/taxitalk/data/sessions"
    session_list_limit: int = 50

    # --- 화면 / 입력 ---
    mic_mode: str = "single"
    screen_layout: str = "single"

    # --- 서비스 ---
    base_path: str = ""
    app_api_key: str = ""
    clinic_name: str = ""
    # 시작 화면에 카드로 뜨는 언어. 탑승객이 고를 수 있는 것은 이 카드뿐이다.
    patient_languages: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_PATIENT_LANGUAGES
    )
    # 카드에 올릴 수 있는 언어의 범위. 기본값은 카탈로그 전부다.
    enabled_languages: tuple[str, ...] = field(
        default_factory=lambda: tuple(c for c in LANGUAGES if c != STAFF_LANG)
    )

    @classmethod
    def from_env(cls) -> "Settings":
        caches = _s("WHISPER_CACHE_DIRS")
        enabled = _langs("ENABLED_LANGUAGES", tuple(c for c in LANGUAGES if c != STAFF_LANG))
        # 카드는 고를 수 있는 언어 안에서만 뜬다. 둘이 어긋나면 카드가 눌리지 않는다.
        featured = tuple(c for c in _langs("PATIENT_LANGUAGES", DEFAULT_PATIENT_LANGUAGES) if c in enabled)
        return cls(
            whisper_model=_s("WHISPER_MODEL", "large-v3"),
            whisper_cache_dirs=tuple(x.strip() for x in caches.split(",") if x.strip()),
            whisper_device=_s("WHISPER_DEVICE", "cuda"),
            whisper_device_index=_i("WHISPER_DEVICE_INDEX", 0),
            whisper_compute_type=_s("WHISPER_COMPUTE_TYPE", "float16"),
            stt_workers=_i("STT_WORKERS", 2),
            stt_beam_size=_i("STT_BEAM_SIZE", 5),
            stt_beam_size_partial=_i("STT_BEAM_SIZE_PARTIAL", 1),
            stt_temperatures=_floats("STT_TEMPERATURES", (0.0, 0.2, 0.4)),
            stt_min_logprob=_f("STT_MIN_LOGPROB", -1.0),
            partial_interval_sec=_f("PARTIAL_INTERVAL_SEC", 1.2),
            min_utterance_sec=_f("MIN_UTTERANCE_SEC", 0.4),
            max_utterance_sec=_f("MAX_UTTERANCE_SEC", 60.0),
            lid_margin=_f("LID_MARGIN", 0.15),
            lid_min_sec=_f("LID_MIN_SEC", 0.8),
            vad_rms_threshold=_f("VAD_RMS_THRESHOLD", 0.012),
            vad_silence_ms=_i("VAD_SILENCE_MS", 700),
            vad_min_speech_ms=_i("VAD_MIN_SPEECH_MS", 250),
            vad_prespeech_ms=_i("VAD_PRESPEECH_MS", 300),
            vllm_base_url=_s("VLLM_BASE_URL", "http://vllm-gpt-oss-120b:8000/v1").rstrip("/"),
            vllm_model=_s("VLLM_MODEL", "qwen3-coder-next"),
            vllm_api_key=_s("VLLM_API_KEY", "EMPTY"),
            vllm_timeout_sec=_f("VLLM_TIMEOUT_SEC", 60.0),
            vllm_temperature=_f("VLLM_TEMPERATURE", 0.2),
            vllm_max_tokens=_i("VLLM_MAX_TOKENS", 512),
            history_turns=_i("HISTORY_TURNS", 3),
            ko_dialect=_s("KO_DIALECT", "off").lower(),
            dialect_path=_s("DIALECT_PATH"),
            dialect_prompt_words=_i("DIALECT_PROMPT_WORDS", 8),
            dialect_max_notes=_i("DIALECT_MAX_NOTES", 8),
            dialect_region=_choice("DIALECT_REGION", "gyeongsang", DIALECT_REGION_CODES),
            dialect_default_on=_b("DIALECT_DEFAULT_ON", False),
            jejuma_base_url=_s("JEJUMA_BASE_URL", "http://192.168.0.208:48125").rstrip("/"),
            jejuma_api_key=_s("JEJUMA_API_KEY"),
            jejuma_timeout_sec=_f("JEJUMA_TIMEOUT_SEC", 3.0),
            jejuma_temperature=_f("JEJUMA_TEMPERATURE", 0.1),
            jejuma_max_tokens=_i("JEJUMA_MAX_TOKENS", 256),
            glossary_path=_s("GLOSSARY_PATH"),
            glossary_prompt_terms=_i("GLOSSARY_PROMPT_TERMS", 24),
            session_dir=_s("SESSION_DIR", "/srv/taxitalk/data/sessions"),
            session_list_limit=_i("SESSION_LIST_LIMIT", 50),
            mic_mode=_choice("MIC_MODE", "single", MIC_MODES),
            screen_layout=_choice("SCREEN_LAYOUT", "single", SCREEN_LAYOUTS),
            base_path=_s("BASE_PATH").rstrip("/"),
            app_api_key=_s("APP_API_KEY"),
            clinic_name=_s("CLINIC_NAME"),
            patient_languages=featured or DEFAULT_PATIENT_LANGUAGES,
            enabled_languages=enabled,
        )


def lang_name(code: str) -> str:
    """번역 프롬프트에 넣을 영문 언어명."""
    return LANG_NAMES.get((code or "").lower(), code or "English")


def lang_label(code: str) -> str:
    """원어 표기(탑승객이 읽을 자리용)."""
    code = (code or "").lower()
    return LANG_LABELS.get(code, code.upper())


def lang_label_ko(code: str) -> str:
    """한국어 표기(기사가 읽을 자리용)."""
    code = (code or "").lower()
    return LANG_LABELS_KO.get(code, code.upper())


def lang_tier(code: str) -> int:
    """인식 품질 등급. 모르는 코드는 실험으로 본다."""
    return LANG_TIERS.get((code or "").lower(), TIER_EXPERIMENTAL)


def is_rtl(code: str) -> bool:
    return (code or "").lower() in RTL_LANGS


def ui_text(code: str) -> dict:
    """해당 언어의 화면 문구. 없으면 영어로 대체한다."""
    return UI_TEXT.get((code or "").lower()) or UI_TEXT["en"]


def dialect_region_label(code: str) -> str:
    """지역 사투리의 한국어 표기. 모르는 코드는 그대로 돌려준다."""
    code = (code or "").lower()
    return DIALECT_REGIONS.get(code, code)


def dialect_region_entries() -> list[dict]:
    """설정창 선택 상자를 그리는 데 필요한 값. 적어 둔 순서를 지킨다."""
    return [{"code": code, "label": label} for code, label in DIALECT_REGIONS.items()]


def lang_entry(code: str) -> dict:
    """화면에 언어 하나를 그리는 데 필요한 값 전부."""
    code = (code or "").lower()
    return {
        "code": code,
        "label": lang_label(code),
        "label_ko": lang_label_ko(code),
        "name": lang_name(code),
        "tier": lang_tier(code),
        "rtl": is_rtl(code),
    }
