/*
  TAXI-TALK 대화 화면.

  창 하나로 화면을 다 쓰기 때문에 WebSocket 도 하나다. 마이크만 1개(언어 판별로
  화자 구분) 또는 2개(채널이 곧 화자)로 갈리고, 그 뒤 화면 갱신은 같은 경로를 탄다.
  같은 대화를 두 벌 그려 두고, 모니터가 둘이면 각자 자기 언어를, 하나로 합쳤으면
  발화마다 들을 사람의 언어를 크게 보여준다.
*/

const SCREENS = ['staff', 'patient'];
const SIDE_CODE = { shared: 0, staff: 1, patient: 2 };
const STAFF_LANG = 'ko';
const DEVICE_KEY = 'taxitalk.devices';
const MIC_MODE_KEY = 'taxitalk.micMode';

// 시작 화면에 놓을 카드 수의 상한. 이보다 많으면 한눈에 안 들어온다.
const CARD_MAX = 15;

// 서버가 매긴 인식 품질 등급(`tier`). 목록에 있다고 다 같은 품질이 나오지 않으므로,
// 오인식이 잦은 실험 등급만 카드와 토스트로 알린다.
const TIER_EXPERIMENTAL = 3;

// 고객 화면 안내 문구는 한 번에 이만큼만 띄우고 나머지는 돌려 가며 보여준다.
const HINTS_PER_PAGE = 4;
const HINT_ROTATE_MS = 4200;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function apiUrl(path) {
  const url = new URL(path, location.href);
  const key = new URLSearchParams(location.search).get('key');
  if (key) url.searchParams.set('key', key);
  return url;
}

/* 언어 코드마다 원형 국기 SVG 를 하나씩 받아 두었다. 영어는 쓰는 나라가 여럿이라
   특정 국가 깃발 대신 지구본을 쓴다. 파일이 없거나 깨졌을 때 깨진 이미지 아이콘이
   뜨지 않도록 onerror 에서 스스로를 숨긴다. */
function flagSrc(code) {
  return code === 'en'
    ? 'assets/images/earth.png'
    : `assets/images/flags/flag_${code}.svg`;
}

function flagIcon(code, className = 'flag') {
  const img = document.createElement('img');
  img.className = className;
  img.src = flagSrc(code);
  img.alt = '';
  img.decoding = 'async';
  img.addEventListener('error', () => { img.hidden = true; });
  return img;
}

function micErrorMessage(err) {
  const name = err && err.name;
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return '브라우저에서 마이크 사용이 차단되어 있습니다. 주소창의 자물쇠 아이콘에서 허용해 주세요.';
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return '선택한 마이크를 찾을 수 없습니다. 설정에서 다른 입력장치를 골라 주세요.';
  }
  if (name === 'NotReadableError') {
    return '마이크를 다른 프로그램이 사용 중입니다. 해당 프로그램을 닫고 다시 시도해 주세요.';
  }
  return `마이크를 열 수 없습니다 (${name || err}).`;
}

/* ------------------------------------------------------------------ 오디오 */
class MicRig {
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.ctx = null;
    this.channels = new Map();
    this.active = false;
  }

  get sides() {
    return Array.from(this.channels.keys());
  }

  async open(requests, vad) {
    await this.close();
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('이 브라우저에서는 마이크를 쓸 수 없습니다 (HTTPS 접속이 필요합니다).');
    }
    try {
      this.ctx = new Ctx({ sampleRate: 16000, latencyHint: 'interactive' });
    } catch (_) {
      // 16kHz 를 못 여는 브라우저도 있다. 워클릿이 알아서 리샘플링한다.
      this.ctx = new Ctx();
    }
    await this.ctx.audioWorklet.addModule(new URL('pcm-worklet.js', import.meta.url));

    for (const request of requests) {
      await this.openChannel(request.side, request.deviceId, vad);
    }
    if (this.ctx.state !== 'running') await this.ctx.resume();
  }

  async openChannel(side, deviceId, vad) {
    const audio = {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    };
    if (deviceId) audio.deviceId = { exact: deviceId };

    const stream = await navigator.mediaDevices.getUserMedia({ audio });
    const source = this.ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(this.ctx, 'pcm-vad-worklet');
    node.port.onmessage = (event) => this.onEvent(side, event.data || {});
    node.port.postMessage({ type: 'configure', ...vad });

    // 워클릿이 확실히 구동되도록 0 게인으로 destination 까지 연결해 둔다.
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    source.connect(node);
    node.connect(mute);
    mute.connect(this.ctx.destination);

    this.channels.set(side, { stream, source, node, mute });
    node.port.postMessage({ type: 'active', value: this.active });
  }

  setActive(value) {
    this.active = !!value;
    for (const channel of this.channels.values()) {
      channel.node.port.postMessage({ type: 'active', value: this.active });
    }
  }

  async close() {
    this.setActive(false);
    for (const channel of this.channels.values()) {
      channel.node.port.onmessage = null;
      try {
        channel.source.disconnect();
        channel.node.disconnect();
        channel.mute.disconnect();
      } catch (_) { /* 이미 끊긴 노드 */ }
      for (const track of channel.stream.getTracks()) track.stop();
    }
    this.channels.clear();
    if (this.ctx) {
      try {
        await this.ctx.close();
      } catch (_) { /* 이미 닫힘 */ }
      this.ctx = null;
    }
  }

  static async listInputs() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices.filter((d) => d.kind === 'audioinput');
  }
}

/* ---------------------------------------------------------------------- 앱 */
class taxitalk {
  constructor() {
    this.config = null;
    this.session = null;
    this.ws = null;
    this.rig = new MicRig((side, msg) => this.onMic(side, msg));
    this.turns = new Map();
    this.paused = false;
    this.closing = false;
    this.retry = 0;
    this.retryTimer = null;
    this.micMode = 'single';
    this.devices = { shared: '', staff: '', patient: '' };
    // code → {code, label, label_ko, name, tier, rtl, ui}
    this.languages = new Map();
    this.featured = [];
    this.hintPages = [];
    this.hintTimer = null;

    this.el = {
      body: document.body,
      toasts: $('[data-toasts]'),
      screens: {},
    };
    for (const screen of SCREENS) {
      const root = $(`[data-screen="${screen}"]`);
      this.el.screens[screen] = {
        root,
        pair: $('[data-pair]', root),
        dot: $('[data-dot]', root),
        conn: $('[data-conn]', root),
        log: $('[data-log]', root),
        micDot: $('[data-mic-dot]', root),
        micText: $('[data-mic-text]', root),
      };
    }
    this.el.langs = $('[data-langs]');
    this.el.micNote = $('[data-mic-note]');
    this.el.level = $('[data-level-bar]');
    this.el.summary = $('[data-summary]');
    this.el.hints = $('[data-hints]');
    // 종료 인사는 배치에 따라 두 화면에 나뉘어 있다. 어느 쪽이 보이든 같은 내용이
    // 들어가도록 전부 찾아 두고 한꺼번에 채운다.
    this.el.endedTitle = $$('[data-ended-title]');
    this.el.endedNote = $$('[data-ended-note]');
    this.el.sheet = $('[data-sheet]');
    this.el.sheetCard = $('[data-sheet-card]');
  }

  /* -------------------------------------------------------------- 기동 */
  async boot() {
    // 배치는 서버 설정을 따르고 `?screen=single|dual` 로 덮어쓴다. 설정을 받아오기
    // 전에 첫 그림이 잘못 깔리지 않도록 쿼리부터 반영한다.
    const forced = new URLSearchParams(location.search).get('screen');
    if (forced === 'single' || forced === 'dual') this.el.body.dataset.layout = forced;

    try {
      const response = await fetch(apiUrl('api/config'));
      if (!response.ok) throw new Error(`config ${response.status}`);
      this.config = await response.json();
    } catch (err) {
      this.toast(`설정을 불러오지 못했습니다: ${err.message}`, 'error');
      return;
    }

    if (!forced) {
      this.el.body.dataset.layout = this.config.screen_layout === 'dual' ? 'dual' : 'single';
    }
    this.micMode = localStorage.getItem(MIC_MODE_KEY) || this.config.mic_mode || 'single';
    try {
      Object.assign(this.devices, JSON.parse(localStorage.getItem(DEVICE_KEY) || '{}'));
    } catch (_) { /* 저장값이 깨졌으면 무시 */ }

    this.loadLanguages();
    this.buildLanguageCards();
    this.buildHints();
    this.bindControls();
    this.showSetup();
  }

  /* ---------------------------------------------------------------- 언어 */
  loadLanguages() {
    // 화면 문구는 번역이 준비된 언어만 내려온다. 나머지는 영어로 안내한다.
    const texts = this.config.ui || {};
    const fallback = this.config.fallback_ui || {};
    this.languages = new Map(
      (this.config.languages || []).map((lang) => [
        lang.code,
        { ...lang, ui: texts[lang.code] || fallback },
      ])
    );
    this.featured = (this.config.featured || []).filter((code) => this.languages.has(code));
  }

  langInfo(code) {
    return (code && this.languages.get(code)) || null;
  }

  // 카드 순서는 서버의 `PATIENT_LANGUAGES` 를 그대로 따른다. 고객이 늘 같은 자리에서
  // 자기 언어를 찾도록 대화 이력에 따라 자리를 바꾸지 않는다.
  cardCodes() {
    return this.featured.slice(0, CARD_MAX);
  }

  buildLanguageCards() {
    this.el.langs.replaceChildren();
    for (const code of this.cardCodes()) {
      this.el.langs.append(this.languageCard(this.langInfo(code)));
    }

    // 카드에 없는 언어도 서버는 인식하지만, 창구에서 고르는 자리는 위 카드로 고정한다.
    // 누를 수 없는 안내이므로 button 이 아니라 그냥 글로 둔다.
    const note = document.createElement('div');
    note.className = 'lang-note';
    note.append(flagIcon('en', 'flag note-flag'));
    note.insertAdjacentHTML(
      'beforeend',
      '<span class="text"><b></b><span></span></span>'
    );
    $('.text b', note).textContent = '지원 언어는 지속적으로 추가될 예정입니다.';
    $('.text span', note).textContent = '더 많은 언어를 지원하기 위해 항상 노력하겠습니다.';
    this.el.langs.append(note);
  }

  /* 국기를 위, 그 언어 표기를 가운데, 한국어 표기를 아래에 둔다. 고객이 보고 누르는
     카드지만 옆에 선 대화원도 같은 카드를 읽어야 해서 한국어를 함께 싣는다. 실험
     등급은 경고를 남겨야 하므로 등급만 작게 덧붙인다. */
  languageCard(lang) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'lang-card';
    card.dataset.code = lang.code;
    card.dataset.tier = lang.tier;
    card.append(flagIcon(lang.code, 'flag lang-flag'));

    const native = document.createElement('span');
    native.className = 'native';
    native.textContent = lang.label;
    native.lang = lang.code;
    if (lang.rtl) native.dir = 'rtl';
    card.append(native);

    const ko = document.createElement('span');
    ko.className = 'ko';
    ko.textContent = `(${lang.label_ko})`;
    card.append(ko);

    if (lang.tier === TIER_EXPERIMENTAL) {
      const tier = document.createElement('span');
      tier.className = 'tier';
      tier.textContent = '실험';
      card.append(tier);
    }

    card.addEventListener('click', () => this.startConsult(lang.code));
    return card;
  }

  /* 고객 화면 안내. 언어가 많아 한 번에 다 띄우면 읽히지 않으므로 넘겨 가며 보여준다. */
  buildHints() {
    clearInterval(this.hintTimer);
    this.hintTimer = null;

    const seen = new Set();
    const lines = [];
    for (const code of this.cardCodes()) {
      const lang = this.langInfo(code);
      // 번역이 없는 언어끼리는 같은 영어 문구가 되므로 한 번만 띄운다.
      if (!lang || seen.has(lang.ui.select_hint)) continue;
      seen.add(lang.ui.select_hint);
      lines.push(lang);
    }

    this.hintPages = [];
    for (let at = 0; at < lines.length; at += HINTS_PER_PAGE) {
      this.hintPages.push(lines.slice(at, at + HINTS_PER_PAGE));
    }
    this.hintPage = 0;
    this.renderHints();
    if (this.hintPages.length > 1) {
      this.hintTimer = setInterval(() => {
        if (this.el.body.dataset.view !== 'setup') return;
        this.hintPage = (this.hintPage + 1) % this.hintPages.length;
        this.renderHints();
      }, HINT_ROTATE_MS);
    }
  }

  renderHints() {
    this.el.hints.replaceChildren();
    for (const lang of this.hintPages[this.hintPage] || []) {
      const line = document.createElement('p');
      line.textContent = lang.ui.select_hint;
      line.lang = lang.code;
      line.dir = lang.rtl ? 'rtl' : 'ltr';
      this.el.hints.append(line);
    }
  }

  bindControls() {
    const actions = {
      settings: () => this.openSettings(),
      pause: () => this.togglePause(),
      records: () => this.openRecords(),
      change: () => this.finishConsult('setup'),
      end: () => this.finishConsult('ended'),
      new: () => this.showSetup(),
      home: () => this.goHome(),
    };
    for (const [name, handler] of Object.entries(actions)) {
      for (const button of $$(`[data-action="${name}"]`)) {
        button.addEventListener('click', handler);
      }
    }
    this.el.sheet.addEventListener('click', (event) => {
      if (event.target === this.el.sheet) this.closeSheet();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !this.el.sheet.hidden) this.closeSheet();
    });
    window.addEventListener('beforeunload', () => {
      if (this.session) navigator.sendBeacon?.(apiUrl(`api/sessions/${this.session.id}/close`));
    });
  }

  lang() {
    return this.langInfo(this.session ? this.session.patient_lang : null);
  }

  // 모니터 하나를 대화원과 고객이 같이 보는 배치. 어느 한쪽 언어를 크게 둘 수 없으니
  // 발화마다 들을 사람 쪽으로 기울여 그린다.
  merged() {
    return this.el.body.dataset.layout === 'single';
  }

  setControls(enabled) {
    for (const name of ['pause', 'records', 'change', 'end']) {
      for (const button of $$(`[data-action="${name}"]`)) button.disabled = !enabled;
    }
  }

  /* ---------------------------------------------------------- 화면 전환 */
  /* 상단 언어 표시. 각 언어 표기 왼쪽에 그 언어의 국기를 붙이고, 두 언어 사이에만
     `↔` 를 넣는다. `code` 가 없는 항목(TAXI-TALK)은 국기 없이 글자만 나온다. */
  setPair(el, parts) {
    el.replaceChildren();
    parts.forEach((part, at) => {
      if (at > 0) {
        const sep = document.createElement('span');
        sep.className = 'sep';
        sep.textContent = '↔';
        el.append(sep);
      }
      const item = document.createElement('span');
      item.className = 'pair-lang';
      if (part.code) item.append(flagIcon(part.code, 'flag pair-flag'));
      const text = document.createElement('span');
      text.textContent = part.text;
      item.append(text);
      el.append(item);
    });
  }

  // 고객 화면 전체에 언어를 걸어 브라우저가 맞는 글꼴을 고르게 하고, 아랍어처럼
  // 오른쪽에서 왼쪽으로 쓰는 언어면 글 방향까지 뒤집는다.
  setPatientLocale(lang) {
    const root = this.el.screens.patient.root;
    root.lang = lang ? lang.code : '';
    root.dir = lang && lang.rtl ? 'rtl' : 'ltr';
  }

  showSetup() {
    this.el.body.dataset.view = 'setup';
    this.setControls(false);
    this.turns.clear();
    for (const screen of SCREENS) this.el.screens[screen].log.replaceChildren();
    this.setPair(this.el.screens.staff.pair, [{ code: STAFF_LANG, text: '한국어' }]);
    this.setPair(this.el.screens.patient.pair, [{ text: 'AI TAXI-TALK' }]);
    this.setPatientLocale(null);
    this.setConn('idle', '준비', '');
    this.setMicState('idle');
    this.paused = false;
    this.updatePauseLabel();
    this.el.micNote.textContent =
      this.micMode === 'dual'
        ? '마이크 2개 모드입니다. 설정에서 대화원용·고객용 입력장치를 확인해 주세요.'
        : '공용 마이크 1개 모드입니다. 말하는 언어로 화자를 구분합니다.';
    for (const card of $$('.lang-card')) card.disabled = false;
  }

  async startConsult(patientLang) {
    for (const card of $$('.lang-card')) card.disabled = true;
    try {
      const response = await fetch(apiUrl('api/sessions'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patient_lang: patientLang, mic_mode: this.micMode }),
      });
      if (!response.ok) throw new Error(`session ${response.status}`);
      this.session = await response.json();
    } catch (err) {
      this.toast(`대화을 시작하지 못했습니다: ${err.message}`, 'error');
      for (const card of $$('.lang-card')) card.disabled = false;
      return;
    }

    const lang = this.lang();
    if (lang.tier === TIER_EXPERIMENTAL) {
      this.toast(`${lang.label_ko}는 인식 정확도가 낮습니다. 결과를 확인해 주세요.`, 'warn');
    }
    this.setPair(this.el.screens.staff.pair, [
      { code: STAFF_LANG, text: '한국어' },
      { code: lang.code, text: `${lang.label} (${lang.label_ko})` },
    ]);
    this.setPair(this.el.screens.patient.pair, [
      { code: lang.code, text: lang.label },
      { code: STAFF_LANG, text: '한국어' },
    ]);
    this.setPatientLocale(lang);
    this.el.body.dataset.view = 'consult';
    this.setControls(true);
    this.closing = false;
    this.retry = 0;

    this.connect();
    try {
      await this.openMics();
    } catch (err) {
      this.toast(micErrorMessage(err), 'error');
      this.setMicState('idle');
    }
  }

  async finishConsult(target) {
    const ending = target === 'ended';
    const ok = await this.confirm({
      title: ending ? '대화을 종료할까요?' : '언어를 바꿀까요?',
      body: ending
        ? '대화 내용이 저장되고 다운로드 버튼이 나타납니다.'
        : '지금까지의 대화가 저장되고 언어 선택 화면으로 돌아갑니다.',
      confirmText: ending ? '대화종료' : '언어변경',
      danger: ending,
    });
    if (!ok) return;

    const meta = await this.closeSession();

    if (ending) {
      // 다운로드 링크를 그리는 동안에는 세션 정보가 아직 필요하다.
      this.renderEnded(meta);
      this.el.body.dataset.view = 'ended';
      this.setControls(false);
      this.setConn('idle', '종료됨', '');
      this.session = null;
    } else {
      this.session = null;
      this.showSetup();
    }
  }

  // 마이크와 WebSocket 만 끊는다. 서버 기록을 어떻게 할지는 부르는 쪽이 정한다.
  async stopStreams() {
    this.closing = true;
    await this.rig.close();
    this.disconnect();
    this.setMicState('idle');
  }

  // 스트림을 끊고 서버에 기록을 닫는다. 종료 화면에 쓸 세션 정보를 돌려준다.
  async closeSession() {
    const session = this.session;
    await this.stopStreams();

    let meta = session;
    try {
      const response = await fetch(apiUrl(`api/sessions/${session.id}/close`), { method: 'POST' });
      if (response.ok) meta = await response.json();
    } catch (err) {
      this.toast(`기록 종료 처리에 실패했습니다: ${err.message}`, 'warn');
    }
    return meta;
  }

  /* 대화 중이든 종료 화면이든 언제나 첫 화면(언어 선택)으로 돌아간다.
     대화을 접고 다음 손님을 받는 버튼이라 종료 처리도 다운로드도 거치지 않는다.
     서버에 이미 쌓인 대화는 그대로 두고 화면만 비운다. */
  async goHome() {
    if (this.session) {
      const ok = await this.confirm({
        title: '처음 화면으로 돌아갈까요?',
        body: '진행 중인 대화을 종료 처리 없이 중단하고 화면의 대화 내용을 지웁니다.',
        confirmText: '홈으로',
        danger: true,
      });
      if (!ok) return;
      await this.stopStreams();
      this.session = null;
    }
    this.closeSheet();
    this.showSetup();
  }

  renderEnded(meta) {
    const lang = this.langInfo(meta.patient_lang);
    const ui = lang ? lang.ui : null;
    const rows = [
      ['언어', lang ? `한국어 ↔ ${lang.label} (${lang.label_ko})` : meta.patient_lang],
      ['발화', `${meta.turns ?? 0}건`],
      ['시작', (meta.started_at || '').replace('T', ' ').slice(0, 19)],
      ['종료', (meta.ended_at || '').replace('T', ' ').slice(0, 19)],
    ];
    this.el.summary.replaceChildren();
    for (const [label, value] of rows) {
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = value;
      this.el.summary.append(dt, dd);
    }
    for (const anchor of $$('[data-download]')) {
      anchor.href = apiUrl(
        `api/sessions/${meta.id}/export?format=${anchor.dataset.download}`
      ).toString();
    }
    if (ui) {
      const dir = lang && lang.rtl ? 'rtl' : 'ltr';
      const write = (nodes, text) => {
        for (const node of nodes) {
          node.textContent = text;
          node.lang = lang ? lang.code : '';
          node.dir = dir;
        }
      };
      write(this.el.endedTitle, ui.ended);
      write(this.el.endedNote, ui.thanks);
    }
  }

  /* -------------------------------------------------------------- 마이크 */
  async openMics() {
    const requests =
      this.micMode === 'dual'
        ? [
            { side: 'staff', deviceId: this.devices.staff },
            { side: 'patient', deviceId: this.devices.patient },
          ]
        : [{ side: 'shared', deviceId: this.devices.shared }];

    await this.rig.open(requests, {
      threshold: this.config.vad.rms_threshold,
      silenceMs: this.config.vad.silence_ms,
      minSpeechMs: this.config.vad.min_speech_ms,
      prespeechMs: this.config.vad.prespeech_ms,
      maxUtteranceSec: this.config.vad.max_utterance_sec,
    });
    this.paused = false;
    this.rig.setActive(true);
    this.setMicState('listening');
    this.updatePauseLabel();
  }

  togglePause() {
    if (!this.rig.channels.size) return;
    this.paused = !this.paused;
    this.rig.setActive(!this.paused);
    this.setMicState(this.paused ? 'paused' : 'listening');
    this.updatePauseLabel();
  }

  updatePauseLabel() {
    for (const button of $$('[data-action="pause"]')) {
      button.textContent = this.paused ? '인식 재개' : '일시정지';
    }
  }

  onMic(side, msg) {
    if (msg.type === 'audio') {
      this.sendAudio(side, msg.pcm);
      return;
    }
    if (msg.type === 'speech_start') {
      this.send({ type: 'start', side });
      this.setMicState('speech');
      return;
    }
    if (msg.type === 'speech_end') {
      this.send({ type: 'end', side });
      if (!this.paused) this.setMicState('listening');
      return;
    }
    if (msg.type === 'level' && this.el.level) {
      const percent = Math.min(100, Math.round(msg.rms * 600));
      this.el.level.style.width = `${percent}%`;
    }
  }

  setMicState(state) {
    const labels = {
      idle: '대기',
      listening: '듣는 중',
      speech: '인식 중',
      paused: '일시정지',
    };
    const lang = this.lang();
    const ui = lang ? lang.ui : null;
    const patientLabels = ui
      ? { idle: '', listening: ui.listening, speech: ui.listening, paused: ui.paused }
      : { idle: '', listening: '', speech: '', paused: '' };

    const staff = this.el.screens.staff;
    staff.micDot.dataset.state = state;
    staff.micText.textContent = labels[state] || '';

    const patient = this.el.screens.patient;
    patient.micDot.dataset.state = state;
    patient.micText.textContent = patientLabels[state] || '';
    if (state !== 'speech' && this.el.level) this.el.level.style.width = '0%';
  }

  /* ----------------------------------------------------------- WebSocket */
  connect() {
    this.disconnect();
    const url = apiUrl('ws/session');
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('session', this.session.id);

    this.setConn('wait', '연결 중', 'connecting');
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    ws.addEventListener('open', () => {
      this.retry = 0;
      this.setConn('on', '연결됨', '');
    });
    ws.addEventListener('message', (event) => {
      if (typeof event.data !== 'string') return;
      try {
        this.onMessage(JSON.parse(event.data));
      } catch (_) { /* 서버가 보낸 값이 아니면 무시 */ }
    });
    ws.addEventListener('close', () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.closing || !this.session) return;
      this.setConn('off', '재연결 중', 'offline');
      this.retry += 1;
      const delay = Math.min(8000, 800 * this.retry);
      this.retryTimer = setTimeout(() => this.session && this.connect(), delay);
    });
    ws.addEventListener('error', () => this.setConn('off', '연결 오류', 'offline'));
  }

  disconnect() {
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try {
        ws.close();
      } catch (_) { /* 이미 닫힘 */ }
    }
  }

  ready() {
    return this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  send(payload) {
    if (!this.ready()) return;
    this.ws.send(JSON.stringify(payload));
  }

  sendAudio(side, pcm) {
    if (!this.ready()) return;
    const frame = new Uint8Array(2 + pcm.byteLength);
    frame[0] = SIDE_CODE[side] ?? 0;
    frame.set(new Uint8Array(pcm), 2);
    this.ws.send(frame);
  }

  setConn(state, staffText, patientKey) {
    const lang = this.lang();
    const ui = lang ? lang.ui : null;
    this.el.screens.staff.dot.dataset.state = state;
    this.el.screens.staff.conn.textContent = staffText;
    this.el.screens.patient.dot.dataset.state = state;
    this.el.screens.patient.conn.textContent = ui && patientKey ? ui[patientKey] || '' : '';
  }

  /* ------------------------------------------------------------ 메시지 */
  onMessage(msg) {
    switch (msg.type) {
      case 'ready':
        this.setConn('on', '연결됨', '');
        break;
      case 'turn_start':
        this.createTurn(msg);
        break;
      case 'turn_speaker': {
        const turn = this.turns.get(msg.turn);
        if (!turn) break;
        Object.assign(turn, { speaker: msg.speaker, src: msg.src, dst: msg.dst });
        this.renderTurn(turn);
        break;
      }
      case 'partial': {
        const turn = this.turns.get(msg.turn);
        if (!turn) break;
        turn.original = msg.text;
        if (msg.speaker) turn.speaker = msg.speaker;
        if (msg.lang) turn.src = msg.lang;
        this.renderTurn(turn);
        break;
      }
      case 'transcript': {
        const turn = this.turns.get(msg.turn) || this.createTurn(msg);
        Object.assign(turn, {
          speaker: msg.speaker,
          src: msg.src,
          dst: msg.dst,
          original: msg.text,
          status: 'translating',
        });
        this.renderTurn(turn);
        break;
      }
      case 'translation_delta': {
        const turn = this.turns.get(msg.turn);
        if (!turn) break;
        turn.translated += msg.text;
        this.renderTurn(turn);
        break;
      }
      case 'translation_done': {
        const turn = this.turns.get(msg.turn);
        if (!turn) break;
        turn.translated = msg.text;
        this.renderTurn(turn);
        break;
      }
      case 'turn_done': {
        const turn = this.turns.get(msg.turn);
        if (!turn) break;
        turn.status = 'done';
        this.renderTurn(turn);
        console.debug('turn', msg.turn, msg.metrics);
        break;
      }
      case 'empty':
      case 'cancelled':
        this.dropTurn(msg.turn);
        break;
      case 'warning':
        this.toast(msg.message || '경고', 'warn');
        break;
      case 'error':
        this.toast(msg.message || '오류가 발생했습니다.', 'error');
        this.dropTurn(msg.turn);
        break;
      default:
        break;
    }
  }

  /* --------------------------------------------------------- 대화 렌더링 */
  createTurn(msg) {
    if (this.turns.has(msg.turn)) return this.turns.get(msg.turn);
    const turn = {
      id: msg.turn,
      speaker: msg.speaker || 'unknown',
      src: msg.src || null,
      dst: msg.dst || null,
      original: '',
      translated: '',
      status: 'listening',
      nodes: {},
    };

    for (const screen of SCREENS) {
      const article = document.createElement('article');
      article.className = 'bubble';
      article.innerHTML =
        '<div class="who"></div><div class="primary"></div><div class="secondary"></div>';
      this.el.screens[screen].log.append(article);
      turn.nodes[screen] = {
        root: article,
        who: $('.who', article),
        primary: $('.primary', article),
        secondary: $('.secondary', article),
      };
    }

    this.turns.set(turn.id, turn);
    this.renderTurn(turn);
    return turn;
  }

  dropTurn(id) {
    const turn = this.turns.get(id);
    if (!turn) return;
    for (const screen of SCREENS) turn.nodes[screen].root.remove();
    this.turns.delete(id);
  }

  speakerLabel(turn, screen) {
    const lang = this.lang();
    const ui = lang ? lang.ui : null;
    if (this.merged()) {
      if (turn.speaker === 'unknown') return '인식 중';
      // 두 사람이 같은 화면을 보므로 서로 자기 말로 화자를 알아볼 수 있게 나란히 적는다.
      const ko = turn.speaker === 'staff' ? '대화원' : '고객';
      const theirs = ui ? (turn.speaker === 'staff' ? ui.staff : ui.patient) : '';
      return theirs && theirs !== ko ? `${ko} · ${theirs}` : ko;
    }
    if (turn.speaker === 'unknown') return screen === 'staff' ? '인식 중' : '…';
    if (screen === 'staff') return turn.speaker === 'staff' ? '대화원' : '고객';
    if (!ui) return turn.speaker === 'staff' ? 'Staff' : 'Patient';
    return turn.speaker === 'staff' ? ui.staff : ui.patient;
  }

  // 한 발화를 (크게 볼 문장, 작게 볼 문장) 으로 나눈다. 나누는 기준은 배치마다 다르다.
  // 화면이 둘이면 각자 자기 언어가 크고, 하나로 합치면 들을 사람의 언어가 크다.
  textsFor(turn, screen) {
    if (!turn.src) return { primary: turn.original, secondary: '' };
    const korean = turn.src === STAFF_LANG ? turn.original : turn.translated;
    const foreign = turn.src === STAFF_LANG ? turn.translated : turn.original;
    if (this.merged()) {
      return turn.src === STAFF_LANG
        ? { primary: foreign, secondary: korean }
        : { primary: korean, secondary: foreign };
    }
    return screen === 'staff'
      ? { primary: korean, secondary: foreign }
      : { primary: foreign, secondary: korean };
  }

  renderTurn(turn) {
    const lang = this.lang();
    // 고객 언어 쪽 줄에만 그 언어를 걸어 준다. 화면 방향과 무관하게 한국어 줄은
    // 늘 왼쪽에서 오른쪽으로 읽혀야 한다.
    const foreign = { lang: lang ? lang.code : '', dir: lang && lang.rtl ? 'rtl' : 'ltr' };
    const korean = { lang: STAFF_LANG, dir: 'ltr' };
    const merged = this.merged();
    const staffSpoke = turn.src === STAFF_LANG;

    for (const screen of SCREENS) {
      const node = turn.nodes[screen];
      const { primary, secondary } = this.textsFor(turn, screen);
      let [top, bottom] = screen === 'staff' ? [korean, foreign] : [foreign, korean];
      if (merged) [top, bottom] = staffSpoke ? [foreign, korean] : [korean, foreign];
      node.root.dataset.speaker = turn.speaker;
      node.root.dataset.status = turn.status;
      node.who.textContent = this.speakerLabel(turn, screen);
      Object.assign(node.primary, top);
      Object.assign(node.secondary, bottom);
      if (primary) {
        node.primary.textContent = primary;
      } else if (turn.status !== 'done') {
        node.primary.innerHTML = '<span class="waiting"><i></i><i></i><i></i></span>';
      } else {
        node.primary.textContent = '';
      }
      node.secondary.textContent = secondary;
    }
    this.scrollLogs();
  }

  scrollLogs() {
    for (const screen of SCREENS) {
      const log = this.el.screens[screen].log;
      // 위로 올려 지난 대화를 보고 있으면 방해하지 않는다.
      const distance = log.scrollHeight - log.scrollTop - log.clientHeight;
      if (distance < 220) log.scrollTop = log.scrollHeight;
    }
  }

  /* ------------------------------------------------------- 확인창 / 설정 */
  openSheet(node) {
    this.el.sheetCard.replaceChildren(node);
    this.el.sheet.hidden = false;
  }

  closeSheet() {
    this.el.sheet.hidden = true;
    this.el.sheetCard.replaceChildren();
  }

  confirm({ title, body, confirmText, danger }) {
    return new Promise((resolve) => {
      const wrap = document.createElement('div');
      wrap.innerHTML = `
        <h2></h2>
        <p></p>
        <div class="sheet-actions">
          <button class="btn" type="button" data-no>취소</button>
          <button class="btn ${danger ? 'danger' : 'primary'}" type="button" data-yes></button>
        </div>`;
      $('h2', wrap).textContent = title;
      $('p', wrap).textContent = body;
      $('[data-yes]', wrap).textContent = confirmText;

      const done = (value) => {
        this.closeSheet();
        resolve(value);
      };
      $('[data-yes]', wrap).addEventListener('click', () => done(true));
      $('[data-no]', wrap).addEventListener('click', () => done(false));
      this.openSheet(wrap);
      $('[data-yes]', wrap).focus();
    });
  }

  openRecords() {
    if (!this.session) return;
    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <div class="sheet-actions">
        <button class="btn" type="button" data-close>닫기</button>
      </div>`;
    for (const anchor of $$('[data-fmt]', wrap)) {
      anchor.href = apiUrl(
        `api/sessions/${this.session.id}/export?format=${anchor.dataset.fmt}`
      ).toString();
    }
    $('[data-close]', wrap).addEventListener('click', () => this.closeSheet());
    this.openSheet(wrap);
  }

  async openSettings() {
    let inputs = [];
    try {
      inputs = await MicRig.listInputs();
    } catch (_) { /* 권한 전이면 목록이 비어 있다 */ }

    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <h2>마이크 설정</h2>
      <p>장치 이름이 비어 있으면 먼저 대화을 시작해 마이크 권한을 허용해 주세요.</p>
      <div class="field">
        <label>입력 방식</label>
        <div class="radio-row">
          <label><input type="radio" name="micmode" value="single">
            <span><b>마이크 1개</b>공용 마이크 하나로 받고 말하는 언어로 화자를 구분합니다.</span></label>
          <label><input type="radio" name="micmode" value="dual">
            <span><b>마이크 2개</b>화면별 마이크를 지정해 화자를 고정합니다. 오인식이 적습니다.</span></label>
        </div>
      </div>
      <div class="field" data-single>
        <label>공용 마이크</label>
        <select data-device="shared"></select>
      </div>
      <div class="field" data-dual hidden>
        <label>대화원 마이크</label>
        <select data-device="staff"></select>
      </div>
      <div class="field" data-dual hidden>
        <label>고객 마이크</label>
        <select data-device="patient"></select>
      </div>
      <div class="sheet-actions">
        <button class="btn" type="button" data-close>취소</button>
        <button class="btn primary" type="button" data-save>저장</button>
      </div>`;

    for (const select of $$('[data-device]', wrap)) {
      const role = select.dataset.device;
      const auto = document.createElement('option');
      auto.value = '';
      auto.textContent = '자동 선택 (기본 장치)';
      select.append(auto);
      inputs.forEach((device, index) => {
        const option = document.createElement('option');
        option.value = device.deviceId;
        option.textContent = device.label || `마이크 ${index + 1}`;
        select.append(option);
      });
      select.value = this.devices[role] || '';
    }

    const applyMode = (mode) => {
      for (const field of $$('[data-single]', wrap)) field.hidden = mode !== 'single';
      for (const field of $$('[data-dual]', wrap)) field.hidden = mode !== 'dual';
    };
    for (const radio of $$('[name="micmode"]', wrap)) {
      radio.checked = radio.value === this.micMode;
      radio.addEventListener('change', () => applyMode(radio.value));
    }
    applyMode(this.micMode);

    $('[data-close]', wrap).addEventListener('click', () => this.closeSheet());
    $('[data-save]', wrap).addEventListener('click', async () => {
      const mode = $('[name="micmode"]:checked', wrap).value;
      for (const select of $$('[data-device]', wrap)) {
        this.devices[select.dataset.device] = select.value;
      }
      const changed = mode !== this.micMode;
      this.micMode = mode;
      localStorage.setItem(MIC_MODE_KEY, mode);
      localStorage.setItem(DEVICE_KEY, JSON.stringify(this.devices));
      this.closeSheet();

      if (this.session) {
        try {
          await this.openMics();
          this.toast('마이크 설정을 적용했습니다.');
        } catch (err) {
          this.toast(micErrorMessage(err), 'error');
        }
      } else {
        this.showSetup();
        if (changed) this.toast('다음 대화부터 적용됩니다.');
      }
    });

    this.openSheet(wrap);
  }

  /* ------------------------------------------------------------ 토스트 */
  toast(message, kind = 'info') {
    const node = document.createElement('div');
    node.className = 'toast';
    node.dataset.kind = kind;
    node.textContent = message;
    this.el.toasts.append(node);
    setTimeout(() => node.remove(), kind === 'info' ? 2600 : 5200);
  }
}

const app = new taxitalk();
app.boot();

// 현장에서 마이크 없이 화면만 점검할 때 콘솔에서 서버 메시지를 흉내 낼 수 있게
// 열어 둔다. 예: taxitalk.onMessage({type:'turn_start', turn:1, speaker:'staff'})
window.taxitalk = app;
