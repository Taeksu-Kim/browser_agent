// 손눈서브 공통 — background.js(실제 검사)·사이드 패널·설정 화면이 같이 쓴다 (설정 기본값·사이트 권한 규칙을 한 곳에서만 정의).

// confirmWords: 이 말이 든 버튼·제출은 누르기 전에 패널에서 사용자 승인 (되돌리기 어려운 동작). 설정 화면에서 바꾼다.
const CONFIRM_WORDS = "결제,주문,구매,송금,이체,삭제,탈퇴,해지,보내기,전송,제출,매수,매도,확정,승인,구독";
const DEFAULTS = { server: "http://127.0.0.1:8771", defaultLevel: "hand", perms: {}, paused: false, trusted: false,
                   confirmOn: true, confirmWords: CONFIRM_WORDS, approvalTimeoutSec: 120,
                   // 음성 입력 마이크 모드: preview = 받아적은 글을 잠깐 보여 주고 보냄 · auto = 바로 보냄 · fill = 입력창에만
                   voiceMode: "preview",
                   // 음성 엔진: google = Chrome 내장(인터넷, 내 말이 Google 로) · whisper = 이 컴퓨터 안 (음성 서버 8013 필요)
                   voiceEngine: "google" };
const VOICE_PREVIEW_MS = 1500;

// ---- 사이트 권한 규칙
// 규칙 "bank.com" 은 bank.com 과 그 하위 주소(www.bank.com, m.bank.com …)에 적용된다.
// 여러 규칙이 맞으면 더 구체적인(긴) 규칙이 이긴다: company.com=눈길 + approval.company.com=끔.
// "file" 은 로컬 파일(file://) 전체를 뜻하는 특별한 이름이다.

// 주소 → 권한에 쓰는 사이트 이름 (로컬 파일은 "file")
function hostOf(url) {
  try { const u = new URL(url); return u.protocol === "file:" ? "file" : u.hostname; } catch { return ""; }
}

function siteRule(host, perms) {
  if (!host) return null;
  let best = null;
  for (const rule of Object.keys(perms || {})) {
    const hit = host === rule || (rule !== "file" && host !== "file" && host.endsWith("." + rule));
    if (hit && (!best || rule.length > best.length)) best = rule;
  }
  return best;
}

function levelForHost(host, perms, defaultLevel) {
  const rule = siteRule(host, perms);
  return { level: rule ? perms[rule] : defaultLevel, rule };
}

// 사용자가 넣은 글(주소 통째로·사이트 이름) → 규칙 이름. www. 는 뗀다 (www.bank.com 을 넣어도 bank.com 전체를 막도록).
// 알아볼 수 없으면 "".
function siteFromInput(text) {
  let s = String(text || "").trim().toLowerCase();
  if (!s) return "";
  if (s === "file" || s.startsWith("file:")) return "file";
  s = s.replace(/^\*\./, "");
  let host;
  try { host = new URL(/^[a-z][a-z0-9+.-]*:\/\//.test(s) ? s : "http://" + s).hostname; } catch { return ""; }
  host = host.replace(/^www\./, "").replace(/\.$/, "");
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)*$/.test(host) && !/^\[[0-9a-f:]+\]$/.test(host)) return "";
  return host;
}
