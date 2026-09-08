// 마이크 입력을 16kHz mono PCM16 으로 바꾸고, 말이 시작되고 끝나는 지점을 직접 잡는다.
//
// 버튼 없이 대화하려면 "지금 말하고 있는가"를 브라우저가 판단해야 한다. 여기서는
// 64ms 조각마다 RMS 를 재고, 조용한 동안 관찰한 잡음 바닥을 기준으로 임계값을
// 끌어올린다. 대화실마다 공조기·기기 소음이 달라서 고정 임계값 하나로는 어디선
// 말이 안 잡히고 어디선 소음이 발화로 잡힌다.
//
// 메인 스레드로 보내는 메시지
//   { type: 'speech_start' }
//   { type: 'audio', pcm: ArrayBuffer }   말하는 동안에만
//   { type: 'speech_end', reason }
//   { type: 'level', rms, speaking }      미터 표시용(throttle)

const TARGET_RATE = 16000;
const CHUNK = 1024;                      // 16kHz 기준 1024 샘플 = 64ms
const CHUNK_MS = (CHUNK / TARGET_RATE) * 1000;
const LEVEL_EVERY = 4;                   // 약 256ms 마다 미터 갱신

class PcmVadWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.tail = new Float32Array(0);
    this.pos = 0;
    this.out = new Int16Array(CHUNK);
    this.n = 0;
    this.active = false;

    // VAD 설정. 서버 /api/config 값으로 덮어쓴다.
    this.threshold = 0.012;
    this.silenceMs = 700;
    this.minSpeechMs = 250;
    this.prespeechMs = 300;
    this.maxUtteranceMs = 60000;

    this.speaking = false;
    this.voicedMs = 0;
    this.silentMs = 0;
    this.spokenMs = 0;
    this.noiseFloor = 0.004;
    this.preroll = [];
    this.levelTick = 0;

    this.port.onmessage = (event) => {
      const msg = event.data || {};
      if (msg.type === 'configure') {
        if (typeof msg.threshold === 'number') this.threshold = msg.threshold;
        if (typeof msg.silenceMs === 'number') this.silenceMs = msg.silenceMs;
        if (typeof msg.minSpeechMs === 'number') this.minSpeechMs = msg.minSpeechMs;
        if (typeof msg.prespeechMs === 'number') this.prespeechMs = msg.prespeechMs;
        if (typeof msg.maxUtteranceSec === 'number') {
          this.maxUtteranceMs = msg.maxUtteranceSec * 1000;
        }
      } else if (msg.type === 'active') {
        const next = !!msg.value;
        if (this.speaking && !next) this.endSpeech('inactive');
        this.resetAll();
        this.active = next;
      }
    };
  }

  resetAll() {
    this.tail = new Float32Array(0);
    this.pos = 0;
    this.n = 0;
    this.speaking = false;
    this.voicedMs = 0;
    this.silentMs = 0;
    this.spokenMs = 0;
    this.preroll = [];
  }

  get prerollChunks() {
    return Math.max(0, Math.round(this.prespeechMs / CHUNK_MS));
  }

  // 잡음 바닥의 몇 배를 넘어야 발화로 본다. 바닥이 아주 조용한 방에서도 설정값
  // 아래로는 내려가지 않게 막는다.
  get effectiveThreshold() {
    return Math.max(this.threshold, this.noiseFloor * 2.5 + 0.0015);
  }

  send(pcm) {
    this.port.postMessage({ type: 'audio', pcm }, [pcm]);
  }

  startSpeech() {
    this.speaking = true;
    this.silentMs = 0;
    this.spokenMs = 0;
    this.port.postMessage({ type: 'speech_start' });
    // 말이 시작되기 직전 구간을 함께 보내야 첫 음절이 잘리지 않는다.
    for (const chunk of this.preroll) {
      this.send(chunk);
      this.spokenMs += CHUNK_MS;
    }
    this.preroll = [];
  }

  endSpeech(reason) {
    this.speaking = false;
    this.voicedMs = 0;
    this.silentMs = 0;
    this.spokenMs = 0;
    this.preroll = [];
    this.port.postMessage({ type: 'speech_end', reason });
  }

  // 완성된 64ms 조각 하나를 VAD 상태기계에 넣는다.
  handleChunk(frame) {
    let sum = 0;
    for (let i = 0; i < frame.length; i += 1) {
      const v = frame[i] / 32768;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / frame.length);

    this.levelTick += 1;
    if (this.levelTick >= LEVEL_EVERY) {
      this.levelTick = 0;
      this.port.postMessage({ type: 'level', rms, speaking: this.speaking });
    }

    const voiced = rms > this.effectiveThreshold;

    if (!this.speaking) {
      // 조용한 동안에만 잡음 바닥을 갱신한다. 발화 중에 갱신하면 목소리가
      // 바닥으로 흡수돼 임계값이 끝없이 올라간다.
      if (!voiced) this.noiseFloor = this.noiseFloor * 0.95 + rms * 0.05;

      const buffer = frame.buffer;
      this.preroll.push(buffer);
      while (this.preroll.length > this.prerollChunks) this.preroll.shift();

      if (voiced) {
        this.voicedMs += CHUNK_MS;
        if (this.voicedMs >= this.minSpeechMs) this.startSpeech();
      } else {
        this.voicedMs = 0;
      }
      return;
    }

    this.send(frame.buffer);
    this.spokenMs += CHUNK_MS;

    if (voiced) {
      this.silentMs = 0;
    } else {
      this.silentMs += CHUNK_MS;
      if (this.silentMs >= this.silenceMs) {
        this.endSpeech('silence');
        return;
      }
    }

    // 브라우저가 무음을 못 잡는 상황(계속 시끄러운 방)에 대비한 안전판.
    if (this.spokenMs >= this.maxUtteranceMs) this.endSpeech('max_length');
  }

  process(inputs, outputs) {
    // 출력은 무음. 일부 브라우저는 destination 까지 연결된 노드만 구동하므로
    // 노드 자체는 0 게인으로 연결해 두고 여기서 버퍼를 비운다.
    const out = outputs[0];
    if (out) {
      for (const channel of out) channel.fill(0);
    }

    const channel = inputs[0] && inputs[0][0];
    if (!channel || !this.active) return true;

    const buf = new Float32Array(this.tail.length + channel.length);
    buf.set(this.tail, 0);
    buf.set(channel, this.tail.length);

    let pos = this.pos;
    while (pos + 1 < buf.length) {
      const i = pos | 0;
      const frac = pos - i;
      let sample = buf[i] * (1 - frac) + buf[i + 1] * frac;
      if (sample > 1) sample = 1;
      else if (sample < -1) sample = -1;
      this.out[this.n] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.n += 1;
      if (this.n === CHUNK) {
        // 소유권을 넘겨야 하므로 매번 새 버퍼로 복사한다.
        const frame = this.out.slice(0, CHUNK);
        this.n = 0;
        this.handleChunk(frame);
      }
      pos += this.ratio;
    }

    const consumed = pos | 0;
    this.tail = buf.slice(consumed);
    this.pos = pos - consumed;
    return true;
  }
}

registerProcessor('pcm-vad-worklet', PcmVadWorklet);
