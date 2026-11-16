// 손눈서브 백그라운드 (MV3 서비스 워커)
// 로컬 서버(sonnun serve)에 WebSocket 으로 붙어서, 서버가 보내는 명령 {id, op, args} 을 실행하고 {id, ok, result|error} 로 답한다.
// 판단은 하지 않는다 — 보기(눈)·조작(손)만. 사이트별 권한(끔·눈길·손길)과 전체 멈춤은 여기서 실제 동작 직전에 검사한다
// (명령을 보내는 쪽이 여럿이어도: 로컬 LLM·Claude Code·import, 마지막 관문은 하나).

importScripts("shared.js");         // DEFAULTS · siteRule · levelForHost (패널·설정 화면과 같은 것)

const RANK = { off: 0, eye: 1, hand: 2 };
const LEVEL_NAME = { off: "끔", eye: "눈길(보기만)", hand: "손길(보기+조작)" };
// 동작별로 필요한 권한. list_tabs 는 탭 목록이라 따로 (끔 사이트는 가려서 보여 준다).
const NEED = {
  get_page: "eye", find: "eye", read_text: "eye", get_selection: "eye", screenshot: "eye", wait_for: "eye",
  navigate: "hand", click: "hand", type_text: "hand", scroll: "hand", go_back: "hand", switch_tab: "eye",
};

let ws = null;
let state = "closed";          // closed | connecting | open
let lastError = "";
let retryTimer = null;

class OpError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

async function settings() {
  return chrome.storage.local.get(DEFAULTS);
}

// ------------------------------------------------------------------ 서버 연결
async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(retryTimer);
  const { server } = await settings();
  const url = server.replace(/\/+$/, "").replace(/^http/, "ws") + "/ws/extension";
  let sock;
  try {
    sock = new WebSocket(url);
  } catch (e) {
    lastError = `서버 주소가 잘못됨: ${url}`;
    setState("closed");
    return;
  }
  ws = sock;
  setState("connecting");
  sock.onopen = () => {
    lastError = "";
    sock.send(JSON.stringify({ type: "hello", version: chrome.runtime.getManifest().version, ua: navigator.userAgent }));
    pushSettings();
    setState("open");
  };
  sock.onerror = () => { lastError = `서버에 연결할 수 없음 (${server}) — sonnun serve 가 떠 있는지 확인`; };
  sock.onclose = () => {
    if (ws === sock) ws = null;
    setState("closed");
    retryTimer = setTimeout(connect, 3000);
  };
  sock.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (!msg || !msg.id) return;                       // pong 등
    const reply = await handle(msg.op, msg.args || {});
    if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify({ id: msg.id, ...reply }));
  };
}

function setState(s) {
  state = s;
  chrome.runtime.sendMessage({ type: "bridge", state, lastError }).catch(() => {});   // 패널이 닫혀 있으면 받을 곳 없음
}

// 서비스 워커는 30초 넘게 조용하면 잠든다 — 열려 있는 동안은 20초마다 ping, 잠들었으면 알람이 깨워 다시 붙는다.
setInterval(() => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" })); }, 20000);
chrome.alarms.create("sonnun-keep", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "sonnun-keep") connect(); });
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});
chrome.runtime.onStartup.addListener(connect);
chrome.storage.onChanged.addListener((changes) => {
  if (changes.server) { if (ws) ws.close(); else connect(); }
  if (changes.trusted || changes.paused) pushSettings();
  if (changes.trusted && !changes.trusted.newValue) detachAll();
});

// 서버(상태 화면·demo)가 알 수 있게 설정 중 동작에 영향 주는 것만 보낸다
async function pushSettings() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const { trusted, paused } = await settings();
  ws.send(JSON.stringify({ type: "settings", trusted, paused }));
}
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === "bridge?") { reply({ state, lastError }); return; }
  if (msg && msg.type === "reconnect") { if (ws) ws.close(); else connect(); reply({ ok: true }); }
  if (msg && msg.type === "requests?") { reply({ requests: [...requests.values()].map((r) => r.req) }); return; }
  if (msg && msg.type === "answer") { finishRequest(msg.id, { ok: !!msg.ok, note: msg.note || "" }); reply({ ok: true }); }
});

// ------------------------------------------------------------------ 사용자에게 묻기 (승인 · 사람에게 넘기기)
// 패널에 카드로 띄우고 답(또는 시간 초과)을 기다린다. 툴바 아이콘에 "!" 를 달아 알린다.
const requests = new Map();      // id -> { req, resolve, timer }

function askPanel(req, timeoutMs) {
  const id = crypto.randomUUID();
  return new Promise((resolve) => {
    const timer = setTimeout(() => finishRequest(id, { ok: false, timeout: true }), timeoutMs);
    requests.set(id, { req: { ...req, id, at: Date.now(), timeout_ms: timeoutMs }, resolve, timer });
    announceRequests();
  });
}

function finishRequest(id, answer) {
  const r = requests.get(id);
  if (!r) return;
  clearTimeout(r.timer);
  requests.delete(id);
  r.resolve(answer);
  announceRequests();
}

function announceRequests() {
  chrome.action.setBadgeText({ text: requests.size ? "!" : "" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color: "#c62828" }).catch(() => {});
  chrome.runtime.sendMessage({ type: "requests", requests: [...requests.values()].map((r) => r.req) }).catch(() => {});
}

// 되돌리기 어려운 동작: 이름에 확인 단어가 있으면 사용자 승인 (지침이 아니라 여기서 강제)
async function guard(tab, what, labels, url, extra = "") {
  const { confirmOn, confirmWords, approvalTimeoutSec } = await settings();
  const names = labels.filter(Boolean);
  const words = String(confirmWords || "").split(/[,\n]/).map((w) => w.trim()).filter(Boolean);
  const hit = [...new Set(names.flatMap((l) => words.filter((w) => l.includes(w))))];
  if (!extra && (!confirmOn || !hit.length)) return;
  const why = [hit.length ? `확인 단어: ${hit.join(", ")}` : "", extra].filter(Boolean).join(" · ");
  const ans = await askPanel({ kind: "approve", title: `${what} 승인`, detail: `"${names.join(" / ") || "(이름 없음)"}" — ${hostOf(url || tab.url)}`, why },
    Number(approvalTimeoutSec || 120) * 1000);
  if (!ans.ok) {
    throw new OpError("denied", ans.timeout
      ? `사용자 승인이 없어 하지 않음 (${approvalTimeoutSec}초 기다림) — 사용자에게 손눈서브 패널에서 허용해 달라고 알리세요`
      : "사용자가 거부함 — 하지 않음");
  }
}

// ------------------------------------------------------------------ 알림창(alert·confirm·prompt) · 새 탭
async function registerDialogScript() {
  try { await chrome.scripting.unregisterContentScripts({ ids: ["sonnun-dialogs"] }); } catch { /* 처음 */ }
  try {
    await chrome.scripting.registerContentScripts([{ id: "sonnun-dialogs", js: ["dialogs.js"], matches: ["<all_urls>"],
      runAt: "document_start", world: "MAIN", allFrames: true, persistAcrossSessions: true }]);
  } catch (e) { console.warn("dialogs.js 등록 실패", e); }
}
chrome.runtime.onInstalled.addListener(registerDialogScript);

// 동작 직전: 그 문서에 10초 동안 알림창을 가로채라고 표시 (이미 열려 있던 탭은 dialogs.js 를 지금 넣는다)
async function armDialogs(tab, frameId, accept, promptText) {
  const target = { tabId: tab.id, frameIds: [frameId || 0] };
  await chrome.scripting.executeScript({ target, files: ["dialogs.js"], world: "MAIN" }).catch(() => {});
  await chrome.scripting.executeScript({ target, args: [!!accept, promptText ?? ""], func: (acc, txt) => {
    const d = document.documentElement.dataset;
    d.sonnunUntil = String(Date.now() + 10000);
    d.sonnunAccept = acc ? "1" : "0";
    d.sonnunPromptText = txt;
    d.sonnunDialogs = "[]";
  } }).catch(() => {});
}

async function readDialogs(tab, frameId) {
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id, frameIds: [frameId || 0] }, func: () => {
      const d = document.documentElement.dataset;
      const v = d.sonnunDialogs || "[]";
      d.sonnunDialogs = "[]";
      d.sonnunUntil = "0";
      return v;
    } });
    return JSON.parse((r && r.result) || "[]");
  } catch { return []; }          // 페이지가 넘어갔으면 기록도 사라짐
}

const createdTabs = [];          // { id, opener, at }
chrome.tabs.onCreated.addListener((t) => {
  createdTabs.push({ id: t.id, opener: t.openerTabId, at: Date.now() });
  if (createdTabs.length > 30) createdTabs.shift();
});

async function newTabsSince(tabId, since) {
  const out = [];
  for (const c of createdTabs) {
    if (c.at < since || c.opener !== tabId) continue;
    const t = (await settle(c.id, { first: 0, timeout: 8000 })) || (await chrome.tabs.get(c.id).catch(() => null));
    if (t) out.push({ tab_id: t.id, title: t.title || "", url: t.url || t.pendingUrl || "", active: t.active });
  }
  return out;
}
connect();

// ------------------------------------------------------------------ 권한
function scriptable(url) { return /^(https?|file):/.test(url || ""); }

async function levelFor(url) {
  const { perms, defaultLevel } = await settings();
  return levelForHost(hostOf(url), perms, defaultLevel).level;
}

async function checkLevel(tab, need) {
  const lv = await levelFor(tab.url);
  if (RANK[lv] < RANK[need]) {
    throw new OpError("permission",
      `${hostOf(tab.url)} 은(는) ${LEVEL_NAME[lv]} 권한이라 ${need === "hand" ? "조작할" : "볼"} 수 없음 — 사용자가 손눈서브 패널에서 바꿀 수 있음`);
  }
}

function requirePage(tab) {
  if (!scriptable(tab.url)) {
    throw new OpError("blocked_page", `이 탭(${tab.url || "빈 탭"})은 브라우저가 확장 접근을 막아 둔 페이지라 볼 수 없음 — navigate 로 다른 주소를 여세요`);
  }
}

async function targetTab(args) {
  if (args.tab_id !== undefined && args.tab_id !== null && args.tab_id !== "") {
    try { return await chrome.tabs.get(Number(args.tab_id)); } catch {
      throw new OpError("not_found", `탭 ${args.tab_id} 이(가) 없음 — list_tabs 로 확인`);
    }
  }
  // 사용자가 마지막으로 본 "일반" 창의 지금 탭 (팝업·확장 창은 제외)
  try {
    const win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    const [t] = await chrome.tabs.query({ active: true, windowId: win.id });
    if (t) return t;
  } catch { /* 일반 창이 없음 */ }
  const [t2] = await chrome.tabs.query({ active: true, windowType: "normal" });
  if (t2) return t2;
  throw new OpError("not_found", "열린 탭이 없음");
}

// ------------------------------------------------------------------ 명령 실행
async function handle(op, args) {
  try {
    const { paused } = await settings();
    if (paused) throw new OpError("paused", "사용자가 손눈서브를 멈춰 둠 — 패널에서 다시 켜야 동작함");
    const fn = OPS[op];
    if (!fn) throw new OpError("bad_op", `모르는 동작: ${op}`);
    return { ok: true, result: await fn(args) };
  } catch (e) {
    return { ok: false, code: e.code || "error", error: e.message || String(e) };
  }
}

// 프레임 하나(또는 전부)에서 함수를 실행. frameIds 가 없으면 맨 위 문서만.
async function inFrames(tab, func, args = [], { frameIds = null, allFrames = false } = {}) {
  const target = { tabId: tab.id };
  if (allFrames) target.allFrames = true;
  else if (frameIds) target.frameIds = frameIds;
  try {
    return await chrome.scripting.executeScript({ target, func, args });
  } catch (e) {
    throw new OpError("blocked_page", `페이지에서 실행할 수 없음: ${e.message || e}`);
  }
}

async function inPage(tab, func, args = [], frameId = 0) {
  const [res] = await inFrames(tab, func, args, { frameIds: [frameId] });
  const r = res && res.result;
  if (r && r.__error) throw new OpError(r.__error, r.message);
  return r;
}

// 이 탭의 보이는 프레임들 (맨 위 먼저). iframe 은 다른 사이트일 수 있어 프레임마다 권한을 따로 본다.
const MAX_FRAMES = 10;
async function listFrames(tab) {
  const res = await inFrames(tab, () => ({ url: location.href, title: document.title, w: innerWidth, h: innerHeight }),
    [], { allFrames: true });
  const out = res.filter((r) => r.result && (r.frameId === 0 || (r.result.w > 0 && r.result.h > 0)))
    .map((r) => ({ frame_id: r.frameId, ...r.result }));
  out.sort((a, b) => a.frame_id - b.frame_id);
  return out.slice(0, MAX_FRAMES);
}

async function frameLevelOk(url, need) {
  if (!scriptable(url)) return url === "about:blank" || url === "about:srcdoc";   // 내용을 부모가 채운 빈 프레임
  return RANK[await levelFor(url)] >= RANK[need];
}

// ref 가 있는 프레임 찾기 (ref 는 브라우저 전체에서 유일하므로 한 곳에만 있다)
async function locateRef(tab, ref) {
  const res = await inFrames(tab, findRefInFrame, [ref], { allFrames: true });
  const hit = res.find((r) => r.result && r.result.found);
  if (!hit) throw new OpError("not_found", `ref ${ref} 를 찾을 수 없음 — 페이지가 바뀌었을 수 있으니 get_page 를 다시 부르세요`);
  return { frameId: hit.frameId, url: hit.result.url, label: hit.result.label, form_submit: hit.result.form_submit };
}

async function checkFrameHand(tab, loc) {
  const url = loc.frameId === 0 || !scriptable(loc.url) ? tab.url : loc.url;
  if (!(await frameLevelOk(url, "hand"))) {
    throw new OpError("permission", `${hostOf(url)} 은(는) 조작 권한(손길)이 아님 — 사용자가 손눈서브 패널에서 바꿀 수 있음`);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 탭이 로딩을 끝낼 때까지 (최대 timeout). 클릭처럼 이동할지 모르는 동작 뒤에는 잠깐 기다린 뒤 확인한다.
async function settle(tabId, { first = 300, timeout = 15000 } = {}) {
  await sleep(first);
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    let t;
    try { t = await chrome.tabs.get(tabId); } catch { return null; }
    if (t.status === "complete") return t;
    await sleep(200);
  }
  return chrome.tabs.get(tabId).catch(() => null);
}

function tabInfo(t) {
  return { tab_id: t.id, title: t.title || "", url: t.url || "" };
}


// ------------------------------------------------------------------ 진짜 입력 (CDP, chrome.debugger) — 설정에서 켤 때만
// 브라우저 자체 입력(isTrusted=true)으로 누르고 치며, 닫힌 shadow DOM 안도 본다. 붙어 있는 동안 Chrome 에 "디버깅 중" 띠가 뜬다
// → 20초 안 쓰면 뗀다. 다른 출처 iframe(별도 프로세스)은 이 경로로 닿지 않아 가짜 이벤트로 처리한다.
const attached = new Map();                       // tabId -> 떼기 타이머

async function cdp(tabId, method, params = {}) {
  if (!attached.has(tabId)) {
    try { await chrome.debugger.attach({ tabId }, "1.3"); } catch (e) {
      if (!String(e.message || e).includes("already attached")) {
        throw new OpError("cdp", `진짜 입력(CDP)을 붙이지 못함: ${e.message || e}`);
      }
    }
  }
  clearTimeout(attached.get(tabId));
  attached.set(tabId, setTimeout(() => { attached.delete(tabId); chrome.debugger.detach({ tabId }).catch(() => {}); }, 20000));
  try {
    return await chrome.debugger.sendCommand({ tabId }, method, params);
  } catch (e) {
    throw new OpError("cdp", `CDP ${method} 실패: ${e.message || e}`);
  }
}
chrome.debugger.onDetach.addListener((src) => {
  if (src.tabId !== undefined) { clearTimeout(attached.get(src.tabId)); attached.delete(src.tabId); }
});
function detachAll() {
  for (const [tabId, t] of attached) { clearTimeout(t); chrome.debugger.detach({ tabId }).catch(() => {}); }
  attached.clear();
}

const attrOf = (n, name) => {
  const a = n.attributes || [];
  for (let i = 0; i < a.length; i += 2) if (a[i] === name) return a[i + 1];
  return undefined;
};
function* walkNodes(n, closed = false) {
  yield [n, closed];
  for (const c of n.children || []) yield* walkNodes(c, closed);
  for (const r of n.shadowRoots || []) yield* walkNodes(r, closed || r.shadowRootType === "closed");
  if (n.contentDocument) yield* walkNodes(n.contentDocument, closed);
}
const textOf = (n) => {
  if (n.nodeType === 3) return n.nodeValue || "";
  if (["STYLE", "SCRIPT"].includes(n.nodeName)) return "";
  return [...(n.children || []), ...(n.shadowRoots || [])].map(textOf).join("");
};
const INTERACTIVE = new Set(["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA", "SUMMARY"]);
const ROLES = new Set(["button", "link", "tab", "checkbox", "menuitem", "option", "switch"]);

async function cdpTree(tabId) {
  return (await cdp(tabId, "DOM.getDocument", { depth: -1, pierce: true })).root;
}

// 닫힌 shadow DOM 안의 누를 수 있는 요소 (페이지 코드로는 못 보는 것만)
async function closedElements(tab, nextRef, max, query) {
  const out = [];
  let next = nextRef;
  for (const [n, closed] of walkNodes(await cdpTree(tab.id))) {
    if (!closed || n.nodeType !== 1) continue;
    if (!INTERACTIVE.has(n.nodeName) && !ROLES.has(attrOf(n, "role"))) continue;
    if (n.nodeName === "INPUT" && attrOf(n, "type") === "hidden") continue;
    const label = (attrOf(n, "aria-label") || textOf(n) || attrOf(n, "placeholder") || "").replace(/\s+/g, " ").trim().slice(0, 60);
    if (query && !label.toLowerCase().includes(query.toLowerCase())) continue;
    try { await cdp(tab.id, "DOM.getContentQuads", { nodeId: n.nodeId }); } catch { continue; }   // 화면에 안 그려짐
    let ref = attrOf(n, "data-sonnun-ref");
    if (!ref) {
      ref = String(next++);
      await cdp(tab.id, "DOM.setAttributeValue", { nodeId: n.nodeId, name: "data-sonnun-ref", value: ref });
    }
    const tag = n.nodeName.toLowerCase();
    out.push({ ref, kind: attrOf(n, "role") || (tag === "input" ? `input:${attrOf(n, "type") || "text"}` : tag), label, shadow: "closed" });
    if (out.length >= max) break;
  }
  return { elements: out, next };
}

async function cdpNode(tabId, ref) {
  for (const [n] of walkNodes(await cdpTree(tabId))) {
    if (n.nodeType === 1 && attrOf(n, "data-sonnun-ref") === ref) return n;
  }
  return null;
}

async function cdpCenter(tabId, nodeId) {
  await cdp(tabId, "DOM.scrollIntoViewIfNeeded", { nodeId }).catch(() => {});
  const { quads } = await cdp(tabId, "DOM.getContentQuads", { nodeId });
  if (!quads || !quads.length) throw new OpError("not_found", "요소가 화면에 그려져 있지 않음");
  const q = quads[0];
  return { x: (q[0] + q[2] + q[4] + q[6]) / 4, y: (q[1] + q[3] + q[5] + q[7]) / 4 };
}

async function trustedClick(tab, node) {
  const { x, y } = await cdpCenter(tab.id, node.nodeId);
  await cdp(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await cdp(tab.id, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await cdp(tab.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
  return { clicked: attrOf(node, "data-sonnun-ref"), label: textOf(node).replace(/\s+/g, " ").trim().slice(0, 60), method: "cdp" };
}

const keyInfo = (ch) => {
  if (/^[0-9]$/.test(ch)) return { code: "Digit" + ch, vk: ch.charCodeAt(0) };
  if (/^[a-zA-Z]$/.test(ch)) return { code: "Key" + ch.toUpperCase(), vk: ch.toUpperCase().charCodeAt(0) };
  if (ch === " ") return { code: "Space", vk: 32 };
  return null;
};

async function trustedType(tab, node, text, submit) {
  if (attrOf(node, "type") === "password") throw new OpError("refused", "비밀번호 칸은 손눈서브가 입력하지 않음 — 사용자가 직접 입력해야 함");
  const { x, y } = await cdpCenter(tab.id, node.nodeId);
  await cdp(tab.id, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await cdp(tab.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
  // 지우기: 전체 선택 + Backspace
  await cdp(tab.id, "Input.dispatchKeyEvent", { type: "rawKeyDown", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: 2, commands: ["selectAll"] });
  await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: 2 });
  await cdp(tab.id, "Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
  await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
  for (const ch of text) {
    const k = keyInfo(ch);
    if (k) {        // 영문·숫자·공백은 키를 친다 (keydown 으로만 받는 칸도 받게)
      await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyDown", key: ch, code: k.code, windowsVirtualKeyCode: k.vk, text: ch, unmodifiedText: ch });
      await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", key: ch, code: k.code, windowsVirtualKeyCode: k.vk });
    } else {        // 한글 등은 입력기처럼 글자를 넣는다
      await cdp(tab.id, "Input.insertText", { text: ch });
    }
  }
  if (submit) {
    await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
    await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  }
  return { typed: attrOf(node, "data-sonnun-ref"), length: text.length, submitted: !!submit, method: "cdp" };
}

// ref -> 어디서 어떻게 누를지. 진짜 입력이 켜져 있으면 CDP 로 닿는 곳(맨 위·같은 프로세스 프레임·닫힌 shadow)은 CDP,
// 안 닿는 곳(다른 출처 iframe)은 가짜 이벤트. 꺼져 있으면 지금처럼 페이지 안에서.
async function resolveRef(tab, ref) {
  const { trusted } = await settings();
  let loc = null;
  try { loc = await locateRef(tab, ref); } catch (e) { if (!trusted) throw e; }
  if (loc) await checkFrameHand(tab, loc);
  if (trusted) {
    const node = await cdpNode(tab.id, ref);
    if (node) {
      const label = (attrOf(node, "aria-label") || textOf(node)).replace(/\s+/g, " ").trim().slice(0, 60);
      return { node, loc: loc || { frameId: 0, url: tab.url, label, form_submit: "" } };
    }
    if (!loc) throw new OpError("not_found", `ref ${ref} 를 찾을 수 없음 — get_page 를 다시 부르세요`);
  }
  return { node: null, loc };
}

// 모든 보이는 프레임에서 요소 모으기 (get_page·find 공용). ref 번호는 브라우저 전체에서 겹치지 않게 이어서 매긴다 --
// 페이지마다 1부터 매기면, 이동 전 페이지의 ref 로 누를 때 "없음"이 아니라 새 페이지의 다른 요소를 눌러 버린다.
async function collect(tab, { max, topChars, frameChars, query }) {
  let { nextRef = 1 } = await chrome.storage.session.get("nextRef");
  let budget = max;
  let top = null;
  const elements = [];
  const frames = [];
  let total = 0;
  for (const f of await listFrames(tab)) {
    const isTop = f.frame_id === 0;
    if (!isTop && !(await frameLevelOk(f.url, "eye"))) {
      frames.push({ frame_id: f.frame_id, url: "", title: "", hidden: `${hostOf(f.url)} 은(는) 끔이라 가림` });
      continue;
    }
    const [res] = await inFrames(tab, pageSnapshot, [Math.max(budget, 0), isTop ? topChars : frameChars, nextRef, query],
      { frameIds: [f.frame_id] });
    const snap = res && res.result;
    if (!snap) continue;
    nextRef = snap.next;
    budget -= snap.elements.length;
    total += snap.elements_total;
    for (const e of snap.elements) elements.push(isTop ? e : { ...e, frame_id: f.frame_id });
    if (isTop) top = snap;
    else if (!query) frames.push({ frame_id: f.frame_id, url: snap.url, title: snap.title, text: snap.text, text_length: snap.text_length });
  }
  if ((await settings()).trusted && budget > 0) {      // 진짜 입력(CDP) 켜짐: 닫힌 shadow DOM 안도
    const c = await closedElements(tab, nextRef, budget, query);
    nextRef = c.next;
    elements.push(...c.elements);
    total += c.elements.length;
  }
  await chrome.storage.session.set({ nextRef });
  if (!top) throw new OpError("blocked_page", "페이지 내용을 읽지 못함");
  const { next, elements: _e, elements_total: _t, ...rest } = top;
  return { ...rest, elements, elements_total: total, frames, offscreen: (top.offscreen || []).filter(Boolean) };
}

const OPS = {
  // 서버 안쪽용 (에이전트 도구 아님): 대화 턴을 시작할 때 작업 탭을 정하려고 사용자가 보는 탭 번호만
  async active_tab() {
    const tab = await targetTab({});
    return { tab_id: tab.id };
  },

  async list_tabs() {
    const tabs = [...await chrome.tabs.query({ windowType: "normal" }), ...await chrome.tabs.query({ windowType: "popup" })]
      .filter((t) => !String(t.url || "").startsWith(`chrome-extension://${chrome.runtime.id}/`));   // 손눈서브 자기 화면 제외
    const out = [];
    for (const t of tabs.slice(0, 40)) {
      const lv = scriptable(t.url) ? await levelFor(t.url) : "hand";
      out.push(lv === "off"
        ? { tab_id: t.id, title: "(끔으로 가려진 사이트)", url: "", active: t.active, level: lv }
        : { ...tabInfo(t), active: t.active, level: lv });
    }
    return { tabs: out, total: tabs.length };
  },

  async get_page(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, NEED.get_page);
    const r = await collect(tab, { max: Number(args.max_elements) || 40, topChars: 1200, frameChars: 400, query: "" });
    return { tab_id: tab.id, ...r };
  },

  // 이름으로 찾기: 화면 근처가 아니어도 페이지 전체(프레임·열린 shadow DOM 포함)에서
  async find(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, NEED.find);
    const q = String(args.text || "").trim();
    if (!q) throw new OpError("bad_args", "text 가 비었음");
    const r = await collect(tab, { max: 10, topChars: 0, frameChars: 0, query: q });
    return { tab_id: tab.id, url: r.url, query: q, elements: r.elements, matched: r.elements_total };
  },

  async read_text(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, NEED.read_text);
    const start = Math.max(0, Number(args.start) || 0);
    const length = Math.min(4000, Math.max(100, Number(args.length) || 2000));
    const frameId = Number(args.frame_id) || 0;
    if (frameId) {
      const f = (await listFrames(tab)).find((x) => x.frame_id === frameId);
      if (!f) throw new OpError("not_found", `프레임 ${frameId} 이(가) 없음 — get_page 의 frames 확인`);
      if (!(await frameLevelOk(f.url, "eye"))) throw new OpError("permission", `${hostOf(f.url)} 은(는) 끔 — 읽을 수 없음`);
    }
    return { tab_id: tab.id, frame_id: frameId || undefined, ...(await inPage(tab, pageText, [start, length], frameId)) };
  },

  async get_selection(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, NEED.get_selection);
    const text = await inPage(tab, () => String(window.getSelection() || ""));
    return { tab_id: tab.id, url: tab.url, text: text.slice(0, 4000), length: text.length };
  },

  async navigate(args) {
    let url = String(args.url || "").trim();
    if (!url) throw new OpError("bad_args", "url 이 비었음");
    if (!/^[a-z]+:/i.test(url)) url = "https://" + url;
    if (!/^(https?|file):/.test(url)) throw new OpError("bad_args", `http(s) 주소만 열 수 있음: ${url}`);
    const tab = await targetTab(args);
    if (scriptable(tab.url)) await checkLevel(tab, "hand");          // 지금 페이지에서 떠나는 것도 조작
    if ((await levelFor(url)) === "off") throw new OpError("permission", `${hostOf(url)} 은(는) 끔 — 열 수 없음`);
    await chrome.tabs.update(tab.id, { url });
    const t = await settle(tab.id, { first: 400 });
    return { navigated: true, ...(t ? tabInfo(t) : { tab_id: tab.id, url }), loading: t ? t.status !== "complete" : true };
  },

  async click(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "hand");
    const ref = String(args.ref ?? "");
    const { node, loc } = await resolveRef(tab, ref);
    const accept = !!args.accept_dialog;
    await guard(tab, "누르기", [loc.label], loc.url, accept ? "확인창이 뜨면 '확인'으로 답함" : "");
    const since = Date.now();
    await armDialogs(tab, loc.frameId, accept, args.dialog_text);
    const r = node ? await trustedClick(tab, node) : await inPage(tab, pageAct, ["click", ref, "", false], loc.frameId);
    const dialogs = await readDialogs(tab, loc.frameId);
    const t = await settle(tab.id, { first: 500, timeout: 10000 });
    const newTabs = await newTabsSince(tab.id, since);
    return { ...r, tab_id: tab.id, frame_id: loc.frameId || undefined, url: t ? t.url : tab.url,
             page_changed: !!t && t.url !== tab.url, dialogs: dialogs.length ? dialogs : undefined,
             new_tabs: newTabs.length ? newTabs : undefined };
  },

  async type_text(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "hand");
    const ref = String(args.ref ?? "");
    const { node, loc } = await resolveRef(tab, ref);
    const text = String(args.text ?? "");
    if (args.submit) await guard(tab, "입력 후 제출", [loc.form_submit], loc.url);
    await armDialogs(tab, loc.frameId, false, "");
    // select·날짜·시간·색상 칸은 브라우저 기본 위젯(칸이 나뉨·팝업)이라 키로 치면 안 들어간다 -> 페이지 안에서 값으로 넣는다
    const widget = node && (node.nodeName === "SELECT" ||
      ["date", "time", "datetime-local", "month", "week", "color", "range"].includes(attrOf(node, "type")));
    const r = node && !widget
      ? await trustedType(tab, node, text, !!args.submit)
      : await inPage(tab, pageAct, ["type", ref, text, !!args.submit], loc.frameId);
    const dialogs = await readDialogs(tab, loc.frameId);
    const t = args.submit ? await settle(tab.id, { first: 500, timeout: 10000 }) : null;
    return { ...r, label: loc.label, tab_id: tab.id, frame_id: loc.frameId || undefined, url: t ? t.url : tab.url,
             dialogs: dialogs.length ? dialogs : undefined };
  },

  async scroll(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "hand");
    const dir = args.direction === "up" ? -1 : 1;
    return { tab_id: tab.id, ...(await inPage(tab, (d) => {
      window.scrollBy(0, d * Math.round(innerHeight * 0.8));
      return { y: Math.round(scrollY), height: document.documentElement.scrollHeight, view: innerHeight };
    }, [dir])) };
  },

  async go_back(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "hand");
    await chrome.tabs.goBack(tab.id).catch(() => { throw new OpError("not_found", "뒤로 갈 페이지가 없음"); });
    const t = await settle(tab.id, { first: 400 });
    return t ? tabInfo(t) : { tab_id: tab.id };
  },

  // 화면 캡처 (보이는 부분만). 서버의 look 도구가 로컬 비전 모델에 넘긴다. 너비 1024 로 줄여 이미지 토큰을 아낀다.
  async screenshot(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "eye");
    if (!tab.active) throw new OpError("bad_args", "보이는 탭만 찍을 수 있음 — switch_tab 으로 먼저 앞에 띄우세요");
    const off = [];
    for (const f of await listFrames(tab)) {
      if (f.frame_id && !(await frameLevelOk(f.url, "eye"))) off.push(hostOf(f.url));
    }
    if (off.length) throw new OpError("permission", `화면 안에 끔으로 둔 사이트(${off.join(", ")})의 프레임이 있어 캡처하지 않음`);
    const shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 80 });
    const bmp = await createImageBitmap(await (await fetch(shot)).blob());
    const scale = Math.min(1, 1024 / bmp.width);
    const w = Math.round(bmp.width * scale), h = Math.round(bmp.height * scale);
    const canvas = new OffscreenCanvas(w, h);
    canvas.getContext("2d").drawImage(bmp, 0, 0, w, h);
    const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.75 });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return { tab_id: tab.id, url: tab.url, title: tab.title, width: w, height: h, bytes: bytes.length,
             image: "data:image/jpeg;base64," + btoa(bin) };
  },

  // 글자가 나타나거나(또는 사라질) 때까지 기다리기 — 주소는 그대로인 채 내용만 늦게 바뀌는 페이지(SPA)용
  async wait_for(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "eye");
    const text = String(args.text || "").trim();
    if (!text) throw new OpError("bad_args", "text 가 비었음");
    const gone = !!args.gone;
    const limit = Math.min(30, Math.max(1, Number(args.timeout_sec) || 10)) * 1000;
    const t0 = Date.now();
    for (;;) {
      let found = false;
      const res = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: pageHasText, args: [text] })
        .catch(() => []);
      for (const r of res) {
        if (r.result && r.result.has && (r.frameId === 0 || (await frameLevelOk(r.result.url, "eye")))) { found = true; break; }
      }
      if (found !== gone) return { met: true, text, gone, waited_ms: Date.now() - t0 };
      if (Date.now() - t0 > limit) return { met: false, text, gone, waited_ms: Date.now() - t0 };
      await sleep(300);
    }
  },

  // 특수 키 (Enter·Esc·Tab·방향키 등). ref 가 있으면 그 요소에, 없으면 지금 초점에.
  async press_key(args) {
    const key = KEYS[args.key] ? args.key : null;
    if (!key) throw new OpError("bad_args", `key 는 ${Object.keys(KEYS).join(", ")} 중 하나`);
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "hand");
    const ref = args.ref === undefined || args.ref === null || args.ref === "" ? "" : String(args.ref);
    let node = null, loc = { frameId: 0, url: tab.url, label: "", form_submit: "" };
    if (ref) ({ node, loc } = await resolveRef(tab, ref));
    if (key === "Enter") await guard(tab, "Enter", [loc.form_submit], loc.url);
    await armDialogs(tab, loc.frameId, false, "");
    const since = Date.now();
    let r;
    if ((await settings()).trusted && (node || !ref)) {
      if (node) await cdp(tab.id, "DOM.focus", { nodeId: node.nodeId }).catch(() => {});
      const k = KEYS[key];
      await cdp(tab.id, "Input.dispatchKeyEvent", { type: k.text ? "keyDown" : "rawKeyDown", key, code: k.code, windowsVirtualKeyCode: k.vk, text: k.text });
      await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", key, code: k.code, windowsVirtualKeyCode: k.vk });
      r = { pressed: key, method: "cdp" };
    } else {
      r = await inPage(tab, pageKey, [ref, key, KEYS[key]], loc.frameId);
    }
    const dialogs = await readDialogs(tab, loc.frameId);
    const t = await settle(tab.id, { first: 300, timeout: 10000 });
    const newTabs = await newTabsSince(tab.id, since);
    return { ...r, tab_id: tab.id, url: t ? t.url : tab.url, page_changed: !!t && t.url !== tab.url,
             dialogs: dialogs.length ? dialogs : undefined, new_tabs: newTabs.length ? newTabs : undefined };
  },

  // 마우스 올리기 (올려야 펼쳐지는 메뉴). 진짜 입력이 꺼져 있으면 JS 로 여는 메뉴만 열린다 (CSS :hover 는 안 됨).
  async hover(args) {
    const tab = await targetTab(args);
    requirePage(tab);
    await checkLevel(tab, "hand");
    const ref = String(args.ref ?? "");
    const { node, loc } = await resolveRef(tab, ref);
    if (node) {
      const { x, y } = await cdpCenter(tab.id, node.nodeId);
      await cdp(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
      return { hovered: ref, label: loc.label, method: "cdp", tab_id: tab.id };
    }
    return { ...(await inPage(tab, pageHover, [ref], loc.frameId)), label: loc.label, tab_id: tab.id };
  },

  // 사람에게 넘기기: CAPTCHA·로그인·인증처럼 사용자가 직접 해야 할 때. 패널에서 "다 했어요"를 누를 때까지 기다린다.
  async ask_user(args) {
    const message = String(args.message || "").trim();
    if (!message) throw new OpError("bad_args", "message 가 비었음");
    const sec = Math.min(600, Math.max(10, Number(args.timeout_sec) || 300));
    const ans = await askPanel({ kind: "ask", title: "에이전트 요청", detail: message }, sec * 1000);
    return { done: !!ans.ok, timeout: !!ans.timeout, note: ans.note || "" };
  },

  async switch_tab(args) {
    if (args.tab_id === undefined || args.tab_id === null) throw new OpError("bad_args", "tab_id 가 필요함");
    const tab = await targetTab(args);
    if (scriptable(tab.url)) await checkLevel(tab, "eye");
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
    return { switched: true, ...tabInfo(tab) };
  },
};

// ------------------------------------------------------------------ 페이지 안에서 실행되는 함수들
// executeScript 로 주입되므로 바깥 변수를 쓰지 않는 독립 함수여야 한다 (그래서 shadow DOM 탐색이 함수마다 들어 있다).
// 실패는 {__error, message} 로 돌려준다. 열린(open) shadow DOM 안까지 본다. 닫힌(closed) 것은 페이지 코드로 들어갈 수 없다.

function pageSnapshot(maxEls, textChars, start, query) {
  const SEL = "a[href], button, input, select, textarea, summary, [role=button], [role=link], [role=tab], " +
    "[role=checkbox], [role=menuitem], [role=option], [role=switch], [contenteditable=''], [contenteditable=true], [onclick]";
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && Number(s.opacity) !== 0;
  };
  const near = (el) => { const r = el.getBoundingClientRect(); return r.bottom > -innerHeight * 0.5 && r.top < innerHeight * 1.5; };
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const label = (el) => {
    const root = el.getRootNode();
    const byId = el.id && root.querySelector && root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    const wrap = ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) && el.closest("label");   // <label><input> 동의</label>
    const img = el.querySelector && el.querySelector("img[alt]");
    return clean(el.getAttribute("aria-label") || (byId && byId.innerText) || (wrap && wrap.innerText) || el.innerText ||
      (["checkbox", "radio"].includes(el.type) ? "" : el.value) ||
      el.getAttribute("placeholder") || el.title || (img && img.alt) || el.name || "").slice(0, 60);
  };
  // 문서 순서대로, 열린 shadow root 안까지
  const found = [];
  const shadowTexts = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.matches(SEL)) found.push(el);
      if (el.shadowRoot) {
        shadowTexts.push([...el.shadowRoot.children].map((c) => c.innerText || "").join("\n"));
        walk(el.shadowRoot);
      }
    }
  };
  walk(document);
  const inClickable = (el) => !!(el.parentElement && el.parentElement.closest("a[href], button, [role=button]"));
  let next = start;
  const elements = [];
  const offscreen = [];
  let total = 0;
  for (const el of found) {
    if (el.disabled || el.type === "hidden" || !visible(el)) continue;
    if (inClickable(el)) continue;  // 버튼 안의 버튼
    total++;
    if (query) {                                    // find: 화면 위치와 상관없이 이름에 query 가 든 것만
      if (!label(el).toLowerCase().includes(query.toLowerCase())) { total--; continue; }
      if (elements.length >= maxEls) continue;
    } else if (elements.length >= maxEls || !near(el)) {
      if (offscreen.length < 40) offscreen.push(label(el).slice(0, 30));   // 이름만 (ref 없이) -> 모델이 find 로 찾게
      continue;
    }
    if (!el.dataset.sonnunRef) el.dataset.sonnunRef = String(next++);
    const tag = el.tagName.toLowerCase();
    const item = {
      ref: el.dataset.sonnunRef,
      kind: el.getAttribute("role") || (tag === "input" ? `input:${el.type || "text"}` : tag),
      label: label(el),
    };
    if (el.getRootNode() instanceof ShadowRoot) item.shadow = true;
    if (tag === "a" && !item.label) item.href = String(el.getAttribute("href")).slice(0, 80);
    if (["input", "textarea", "select"].includes(tag) && el.type !== "password" && !["checkbox", "radio"].includes(el.type)) {
      item.value = String(el.value || "").slice(0, 40);
    }
    if (tag === "select") item.options = [...el.options].slice(0, 10).map((o) => clean(o.text)).join(" / ");
    if (el.type === "checkbox" || el.type === "radio") item.checked = el.checked;
    elements.push(item);
  }
  let text = String(document.body ? document.body.innerText : "");
  const extra = shadowTexts.filter((t) => t.trim());
  if (extra.length) text += "\n" + extra.join("\n");     // innerText 는 shadow DOM 글을 포함하지 않는다
  text = text.replace(/\n{3,}/g, "\n\n");
  return {
    next, title: document.title, url: location.href, elements, elements_total: total, offscreen,
    text: text.slice(0, textChars), text_length: text.length,
    scroll: { y: Math.round(scrollY), height: document.documentElement.scrollHeight, view: innerHeight },
  };
}

function pageText(start, length) {
  let text = String(document.body ? document.body.innerText : "");
  const extra = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) { extra.push([...el.shadowRoot.children].map((c) => c.innerText || "").join("\n")); walk(el.shadowRoot); }
    }
  };
  walk(document);
  if (extra.some((t) => t.trim())) text += "\n" + extra.filter((t) => t.trim()).join("\n");
  text = text.replace(/\n{3,}/g, "\n\n");
  return { url: location.href, start, text: text.slice(start, start + length), text_length: text.length,
           more: start + length < text.length };
}

function findRefInFrame(ref) {
  const sel = `[data-sonnun-ref="${CSS.escape(ref)}"]`;
  const find = (root) => {
    const hit = root.querySelector(sel);
    if (hit) return hit;
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) { const h = find(el.shadowRoot); if (h) return h; }
    }
    return null;
  };
  const el = find(document);
  if (!el) return { found: false, url: location.href };
  const clean = (v) => String(v || "").replace(/\s+/g, " ").trim().slice(0, 60);
  const root = el.getRootNode();
  const byId = el.id && root.querySelector && root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
  const label = clean(el.getAttribute("aria-label") || (byId && byId.innerText) || el.innerText || el.value || el.getAttribute("placeholder") || el.title);
  const form = el.form || (el.closest && el.closest("form"));
  const sub = form && form.querySelector("button[type=submit], button:not([type]), input[type=submit]");
  return { found: true, url: location.href, label, form_submit: sub ? clean(sub.innerText || sub.value) : "" };
}

function pageAct(action, ref, text, submit) {
  const sel = `[data-sonnun-ref="${CSS.escape(ref)}"]`;
  const find = (root) => {
    const hit = root.querySelector(sel);
    if (hit) return hit;
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) { const h = find(el.shadowRoot); if (h) return h; }
    }
    return null;
  };
  const el = find(document);
  if (!el) return { __error: "not_found", message: `ref ${ref} 를 찾을 수 없음 — get_page 를 다시 부르세요` };
  const name = String(el.innerText || el.value || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().slice(0, 60);

  if (action === "click") {
    el.scrollIntoView({ block: "center", inline: "center" });
    const o = { bubbles: true, cancelable: true, composed: true, view: window };
    el.dispatchEvent(new PointerEvent("pointerdown", o));
    el.dispatchEvent(new MouseEvent("mousedown", o));
    if (el.focus) el.focus();
    el.dispatchEvent(new PointerEvent("pointerup", o));
    el.dispatchEvent(new MouseEvent("mouseup", o));
    el.click();
    return { clicked: ref, label: name };
  }

  // type
  let method = "direct";
  if (el.type === "password") return { __error: "refused", message: "비밀번호 칸은 손눈서브가 입력하지 않음 — 사용자가 직접 입력해야 함" };
  el.scrollIntoView({ block: "center" });
  el.focus();
  if (el.isContentEditable) {
    document.execCommand("selectAll", false);
    document.execCommand("insertText", false, text);
  } else if (el.tagName === "SELECT") {
    // 보이는 글자로 고르기 (값이 아니라) -- 모델은 get_page 의 options 글자를 본다
    const opt = [...el.options].find((o) => o.text.trim() === text.trim() || o.value === text);
    if (!opt) return { __error: "bad_args", message: `선택지에 "${text}" 없음 — 있는 것: ${[...el.options].map((o) => o.text.trim()).join(" / ")}` };
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set.call(el, opt.value);
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  } else if (["INPUT", "TEXTAREA"].includes(el.tagName)) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setValue = (v) => Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);   // React 등이 알아채게
    setValue(text);
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    // 칸이 통째로 넣은 값을 되돌렸으면 (keydown 으로 한 글자씩만 받는 칸) 키를 한 글자씩 친다.
    // 서식이 붙는 칸("1,234원")은 글자·숫자만 비교해 통과로 본다.
    const core = (v) => String(v).replace(/[^\p{L}\p{N}]/gu, "");
    if (text && !core(el.value).includes(core(text))) {
      method = "keys";
      const key = (type, k) => el.dispatchEvent(new KeyboardEvent(type, {
        key: k, code: /^\d$/.test(k) ? "Digit" + k : k === "Backspace" ? "Backspace" : "Key" + k.toUpperCase(),
        keyCode: k === "Backspace" ? 8 : k.toUpperCase().charCodeAt(0), bubbles: true, cancelable: true, composed: true }));
      for (let i = String(el.value).length + 2; i > 0; i--) {          // 남아 있는 값 지우기
        if (key("keydown", "Backspace")) setValue(String(el.value).slice(0, -1));
        key("keyup", "Backspace");
      }
      for (const ch of text) {
        const go = key("keydown", ch);
        key("keypress", ch);
        if (go) {                                                        // 칸이 막지 않았으면 브라우저처럼 글자를 넣는다
          setValue(String(el.value) + ch);
          el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: ch, inputType: "insertText" }));
        }
        key("keyup", ch);
      }
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (!core(el.value).includes(core(text))) {
        return { __error: "not_applied", message: `값이 들어가지 않음 (칸이 받아들이지 않음, 지금 값 "${String(el.value).slice(0, 40)}") — 진짜 입력(CDP)이 필요할 수 있음` };
      }
    }
  } else {
    return { __error: "bad_args", message: `ref ${ref} 는 입력 칸이 아님 (${el.tagName.toLowerCase()})` };
  }
  let submitted = false;
  if (submit) {
    const k = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
    const go = el.dispatchEvent(new KeyboardEvent("keydown", k));
    el.dispatchEvent(new KeyboardEvent("keypress", k));
    el.dispatchEvent(new KeyboardEvent("keyup", k));
    if (go && el.form) {
      if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
    }
    submitted = true;
  }
  return { typed: ref, length: text.length, submitted, method, value_now: el.isContentEditable ? undefined : String(el.value).slice(0, 40) };
}

const KEYS = {
  Enter: { code: "Enter", vk: 13, text: "\r" }, Escape: { code: "Escape", vk: 27 }, Tab: { code: "Tab", vk: 9 },
  ArrowDown: { code: "ArrowDown", vk: 40 }, ArrowUp: { code: "ArrowUp", vk: 38 }, ArrowLeft: { code: "ArrowLeft", vk: 37 },
  ArrowRight: { code: "ArrowRight", vk: 39 }, Backspace: { code: "Backspace", vk: 8 }, Delete: { code: "Delete", vk: 46 },
  Home: { code: "Home", vk: 36 }, End: { code: "End", vk: 35 }, PageDown: { code: "PageDown", vk: 34 },
  PageUp: { code: "PageUp", vk: 33 }, Space: { code: "Space", vk: 32, text: " " },
};

function pageHasText(text) {
  let all = String(document.body ? document.body.innerText : "");
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) { all += "\n" + [...el.shadowRoot.children].map((c) => c.innerText || "").join("\n"); walk(el.shadowRoot); }
    }
  };
  walk(document);
  return { has: all.includes(text), url: location.href };
}

function pageKey(ref, key, info) {
  const sel = ref ? `[data-sonnun-ref="${CSS.escape(ref)}"]` : null;
  const find = (root) => {
    const hit = root.querySelector(sel);
    if (hit) return hit;
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) { const h = find(el.shadowRoot); if (h) return h; }
    return null;
  };
  let el = sel ? find(document) : null;
  if (sel && !el) return { __error: "not_found", message: `ref ${ref} 를 찾을 수 없음 — get_page 를 다시 부르세요` };
  if (el && el.focus) el.focus();
  el = el || document.activeElement || document.body;
  const k = { key: key === "Space" ? " " : key, code: info.code, keyCode: info.vk, which: info.vk, bubbles: true, cancelable: true, composed: true };
  const go = el.dispatchEvent(new KeyboardEvent("keydown", k));
  if (info.text) el.dispatchEvent(new KeyboardEvent("keypress", k));
  el.dispatchEvent(new KeyboardEvent("keyup", k));
  if (go && key === "Enter" && el.form) { if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit(); }
  if (go && key === "Tab") {      // 가짜 Tab 은 브라우저가 초점을 옮기지 않는다 -> 다음 입력 가능한 요소로 직접
    const all = [...document.querySelectorAll("a[href], button, input, select, textarea, [tabindex]:not([tabindex='-1'])")]
      .filter((x) => !x.disabled && x.offsetParent !== null);
    const i = all.indexOf(el);
    if (all.length) all[(i + 1) % all.length].focus();
  }
  return { pressed: key, method: "event", focus: String((document.activeElement && (document.activeElement.getAttribute("aria-label") || document.activeElement.innerText || document.activeElement.tagName)) || "").slice(0, 40) };
}

function pageHover(ref) {
  const sel = `[data-sonnun-ref="${CSS.escape(ref)}"]`;
  const find = (root) => {
    const hit = root.querySelector(sel);
    if (hit) return hit;
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) { const h = find(el.shadowRoot); if (h) return h; }
    return null;
  };
  const el = find(document);
  if (!el) return { __error: "not_found", message: `ref ${ref} 를 찾을 수 없음 — get_page 를 다시 부르세요` };
  el.scrollIntoView({ block: "center" });
  const r = el.getBoundingClientRect();
  const o = { bubbles: true, cancelable: true, composed: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  for (const t of ["pointerover", "pointerenter", "mouseover", "mouseenter", "pointermove", "mousemove"]) {
    const E = t.startsWith("pointer") ? PointerEvent : MouseEvent;
    el.dispatchEvent(new E(t, { ...o, bubbles: !t.endsWith("enter") }));
  }
  return { hovered: ref, method: "event" };
}
