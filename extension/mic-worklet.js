// 마이크 소리를 128샘플씩 패널로 넘긴다 (Whisper 엔진의 녹음·말 끝 감지는 sidepanel.js 에서).
class SonnunTap extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("sonnun-tap", SonnunTap);
