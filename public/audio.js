/* Browser audio plumbing for the VoxDesk test line.
   Mic → 16 kHz mono PCM16 → base64; base64 PCM16 → scheduled playback. */
(function () {
  const TARGET_RATE = 16000;

  function downsample(input, fromRate, toRate) {
    if (fromRate === toRate) return input;
    const ratio = fromRate / toRate;
    const length = Math.floor(input.length / ratio);
    const out = new Float32Array(length);
    for (let i = 0; i < length; i += 1) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j += 1) sum += input[j];
      out[i] = sum / Math.max(1, end - start);
    }
    return out;
  }

  function toBase64Pcm16(samples) {
    const buffer = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < samples.length; i += 1) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(i * 2, Math.round(s * 32767), true);
    }
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  function fromBase64Pcm16(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const view = new DataView(bytes.buffer);
    const out = new Float32Array(Math.floor(bytes.length / 2));
    for (let i = 0; i < out.length; i += 1) out[i] = view.getInt16(i * 2, true) / 32768;
    return out;
  }

  class MicCapture {
    constructor(options) {
      this.onChunk = options.onChunk || function () {};
      this.onLevel = options.onLevel || function () {};
      this.stream = null;
      this.context = null;
      this.processor = null;
      this.source = null;
      this.sink = null;
    }

    static get supported() {
      return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.AudioContext);
    }

    async start() {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      this.context = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: TARGET_RATE });
      await this.context.resume();
      this.source = this.context.createMediaStreamSource(this.stream);
      this.processor = this.context.createScriptProcessor(2048, 1, 1);
      this.sink = this.context.createGain();
      this.sink.gain.value = 0;
      this.processor.onaudioprocess = (event) => {
        const channel = event.inputBuffer.getChannelData(0);
        let peak = 0;
        for (let i = 0; i < channel.length; i += 1) peak = Math.max(peak, Math.abs(channel[i]));
        this.onLevel(Math.min(1, peak * 2.4));
        const pcm = this.context.sampleRate === TARGET_RATE ? channel : downsample(channel, this.context.sampleRate, TARGET_RATE);
        this.onChunk(toBase64Pcm16(pcm));
      };
      this.source.connect(this.processor);
      this.processor.connect(this.sink);
      this.sink.connect(this.context.destination);
      return this.context.sampleRate;
    }

    stop() {
      try {
        if (this.processor) {
          this.processor.onaudioprocess = null;
          this.processor.disconnect();
        }
        if (this.source) this.source.disconnect();
        if (this.sink) this.sink.disconnect();
        if (this.context) this.context.close();
        if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
      } catch (error) {
        /* teardown is best effort */
      }
      this.stream = null;
      this.context = null;
      this.processor = null;
      this.source = null;
      this.sink = null;
      this.onLevel(0);
    }
  }

  class Player {
    constructor() {
      this.context = null;
      this.nextTime = 0;
      this.sources = new Set();
      this.playing = 0;
    }

    async resume() {
      if (!this.context) {
        this.context = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: TARGET_RATE });
      }
      if (this.context.state === 'suspended') await this.context.resume();
    }

    schedule(base64) {
      if (!this.context) return;
      const samples = fromBase64Pcm16(base64);
      if (samples.length === 0) return;
      const buffer = this.context.createBuffer(1, samples.length, TARGET_RATE);
      buffer.copyToChannel(samples, 0);
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.context.destination);
      const now = this.context.currentTime;
      // Keep a small jitter buffer; resync if the backlog grows too large.
      if (this.nextTime < now + 0.03 || this.nextTime > now + 1.6) this.nextTime = now + 0.09;
      const startAt = this.nextTime;
      this.nextTime = startAt + buffer.duration;
      source.start(startAt);
      this.sources.add(source);
      this.playing += 1;
      source.onended = () => {
        this.sources.delete(source);
        this.playing = Math.max(0, this.playing - 1);
      };
    }

    clear() {
      for (const source of this.sources) {
        try {
          source.stop();
        } catch (error) {
          /* already stopped */
        }
      }
      this.sources.clear();
      this.playing = 0;
      this.nextTime = this.context ? this.context.currentTime : 0;
    }

    stop() {
      this.clear();
      if (this.context) {
        try {
          this.context.close();
        } catch (error) {
          /* ignore */
        }
        this.context = null;
      }
    }
  }

  window.VoxAudio = { MicCapture, Player, downsample, toBase64Pcm16, fromBase64Pcm16, TARGET_RATE };
})();
