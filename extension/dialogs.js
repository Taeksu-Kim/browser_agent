// 페이지 쪽(MAIN world)에서 alert / confirm / prompt 를 감싼다.
// 에이전트가 방금 움직인 문서에서만(손눈서브가 표시한 10초 동안) 창을 띄우지 않고 기록한다 — 창이 뜨면 그 탭의 스크립트가
// 멈춰 손눈서브가 먹통이 되기 때문. 그 밖의 시간(사용자가 직접 쓰는 중)에는 원래 창 그대로.
// confirm/prompt 는 기본 "취소" — 에이전트가 accept_dialog 로 요청하고 사용자가 승인한 경우에만 "확인".
(() => {
  if (window.__sonnunDialogs) return;
  window.__sonnunDialogs = true;
  const root = () => document.documentElement;
  const armed = () => !!root() && Number(root().dataset.sonnunUntil || 0) > Date.now();
  const record = (type, message, answer) => {
    let list = [];
    try { list = JSON.parse(root().dataset.sonnunDialogs || "[]"); } catch { list = []; }
    list.push({ type, message: String(message ?? "").slice(0, 300), answer });
    root().dataset.sonnunDialogs = JSON.stringify(list.slice(-5));
  };
  const orig = { alert: window.alert, confirm: window.confirm, prompt: window.prompt };
  window.alert = function (message) {
    if (!armed()) return orig.alert.apply(this, arguments);
    record("alert", message, "확인");
  };
  window.confirm = function (message) {
    if (!armed()) return orig.confirm.apply(this, arguments);
    const ok = root().dataset.sonnunAccept === "1";
    record("confirm", message, ok ? "확인" : "취소");
    return ok;
  };
  window.prompt = function (message, def) {
    if (!armed()) return orig.prompt.apply(this, arguments);
    const ok = root().dataset.sonnunAccept === "1";
    const value = ok ? (root().dataset.sonnunPromptText ?? def ?? "") : null;
    record("prompt", message, ok ? `입력: ${value}` : "취소");
    return value;
  };
})();
