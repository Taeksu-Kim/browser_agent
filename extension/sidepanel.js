// 손눈서브 사이드 패널 — 연결 상태 · 이 사이트 권한(끔/눈길/손길) · 대화(로컬 Qwen / Claude Code) · 활동 기록
// 사이트 권한 목록과 나머지 설정은 설정 화면(options.html). 설정 기본값·권한 규칙은 shared.js.
// 대화와 기록은 로컬 서버(sonnun serve)가 주인이다. 권한·멈춤은 확장 저장소(chrome.storage)에 두고 background.js 가 검사한다.

const LEVEL_NAME = { off: "끔", eye: "눈길", hand: "손길" };
const LEVEL_HINT = {
  off: "에이전트가 이 사이트를 보지도, 조작하지도 못합니다.",
  eye: "에이전트가 이 사이트를 볼 수만 있습니다 (클릭·입력 불가).",
  hand: "에이전트가 이 사이트를 보고 클릭·입력할 수 있습니다.",
};
const BACKEND_NAME = { local: "로컬 Qwen", claude: "Claude Code" };
const TOOL_NAME = {
  list_tabs: "탭 목록 보기", get_page: "페이지 보기", find: "찾기", look: "화면 보기", wait_for: "기다리기",
  press_key: "키 누르기", hover: "마우스 올리기", ask_user: "사용자에게 요청", read_text: "글 읽기", get_selection: "선택한 글 보기",
  navigate: "주소 열기", click: "누르기", type_text: "입력하기", scroll: "스크롤", go_back: "뒤로 가기", switch_tab: "탭 바꾸기",
};
const SOURCE_NAME = { local: "로컬", claude: "Claude", api: "import" };

const $ = (id) => document.getElementById(id);
let cfg = { ...DEFAULTS };
let status = null;            // 서버 /api/status
let chats = [];
let currentChat = null;       // {id, backend, ...} 또는 null(새 대화)
let running = null;           // {chatId, controller}
let currentHost = "";
let currentTabId = null;      // 패널이 보고 있는 탭 — 보내면 이 탭이 작업 탭이 된다

// ------------------------------------------------------------------ 공통
async function api(path, opts = {}) {
  const r = await fetch(cfg.server.replace(/\/+$/, "") + path, {
    ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { const j = await r.json(); detail = j.detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kids) if (k) e.append(k);
  return e;
}

function readableUrl(url) {
  // 기록에는 사람이 읽을 주소로 (한글 검색어가 %EB%B6%80… 로 보이지 않게)
  try { return decodeURI(url).replace(/^https?:\/\//, ""); } catch { return url; }
}

function hhmm(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
}

// ------------------------------------------------------------------ 연결 상태
async function refreshStatus() {
  const conn = $("conn");
  try {
    status = await api("/api/status");
    if (status.extension.connected) {
      conn.dataset.state = "ok"; conn.textContent = "연결됨";
      conn.title = `서버 ${cfg.server} · 확장 연결됨`;
    } else {
      conn.dataset.state = "partial"; conn.textContent = "확장 연결 중";
      conn.title = "서버는 켜져 있지만 이 확장이 아직 붙지 않았습니다";
      chrome.runtime.sendMessage({ type: "reconnect" }).catch(() => {});
    }
  } catch {
    status = null;
    conn.dataset.state = "down"; conn.textContent = "서버 꺼짐";
    conn.title = `${cfg.server} 에 연결할 수 없습니다 — 터미널에서 python -m sonnun serve`;
  }
  renderComposer();
  renderWorkTab();
}

// ------------------------------------------------------------------ 에이전트가 묻는 것 (승인 · 사람에게 넘기기)
let pendingReqs = [];

function renderRequests() {
  const box = $("requests");
  box.hidden = !pendingReqs.length;
  box.replaceChildren(...pendingReqs.map((r) => {
    const approve = r.kind === "approve";
    const yes = el("button", { type: "button", class: approve ? "danger" : "primary", text: approve ? "허용" : "다 했어요" });
    const no = el("button", { type: "button", text: approve ? "거부" : "못 해요" });
    yes.addEventListener("click", () => answer(r.id, true));
    no.addEventListener("click", () => answer(r.id, false));
    const left = el("span", { class: "left", "data-deadline": String(r.at + r.timeout_ms) });
    return el("div", { class: "req", "data-id": r.id },
      el("strong", { text: r.title }), el("div", { class: "detail", text: r.detail }),
      r.why ? el("div", { class: "why", text: r.why }) : null, left, el("div", { class: "acts" }, yes, no));
  }));
  tickRequests();
}

function tickRequests() {
  for (const s of document.querySelectorAll(".req .left")) {
    const sec = Math.max(0, Math.round((Number(s.dataset.deadline) - Date.now()) / 1000));
    s.textContent = `${sec}초 안에 답하지 않으면 하지 않습니다`;
  }
}

async function answer(id, ok) {
  await chrome.runtime.sendMessage({ type: "answer", id, ok }).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "requests") { pendingReqs = msg.requests || []; renderRequests(); }
});

// ------------------------------------------------------------------ 이 사이트 권한
// 사이드 패널은 자기가 붙은 창(currentWindow)의 탭을 본다. 패널을 따로 띄울 때는 ?window=<창 id> 로 볼 창을 정한다.
const PINNED_WINDOW = Number(new URLSearchParams(location.search).get("window")) || null;

async function refreshSite() {
  const [tab] = await chrome.tabs.query(PINNED_WINDOW ? { active: true, windowId: PINNED_WINDOW } : { active: true, currentWindow: true });
  const url = tab ? tab.url || "" : "";
  currentTabId = tab ? tab.id : null;
  const ok = /^(https?|file):/.test(url);
  currentHost = ok ? hostOf(url) : "";
  $("host").textContent = ok ? currentHost : "브라우저 내부 페이지";
  $("host").title = url;
  const { level, rule } = ok ? levelForHost(currentHost, cfg.perms, cfg.defaultLevel) : { level: null, rule: null };
  for (const b of $("levels").querySelectorAll("button")) {
    b.disabled = !ok;
    b.setAttribute("aria-checked", String(b.dataset.level === level));
  }
  // 뜻은 버튼 부제와 ⓘ 설명에 있으니, 여기는 "어느 규칙으로 정해졌나"만 짧게
  $("levelHint").textContent = !ok ? "볼 수 없음" : !rule ? "기본값" : rule !== currentHost ? `${rule} 규칙` : "직접 정함";
  $("levelHint").title = ok ? LEVEL_HINT[level] : "브라우저가 막아 둔 페이지라 에이전트가 볼 수 없습니다. 주소 열기는 가능합니다.";
  renderWorkTab();
}

// 에이전트가 다른 탭에서 일하는 중이면 알려 준다 (사용자가 탭을 옮겨도 에이전트는 일을 시킨 탭에 머문다)
async function renderWorkTab() {
  const w = status && status.running && status.work_tab;
  const box = $("workTab");
  if (!w || w === currentTabId) { box.hidden = true; return; }
  const t = await chrome.tabs.get(w).catch(() => null);
  box.hidden = !t;
  if (t) box.textContent = `에이전트는 다른 탭 "${(t.title || hostOf(t.url || "")).slice(0, 40)}"에서 작업 중입니다 — 이 탭은 건드리지 않습니다.`;
}

$("levels").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-level]");
  if (!b || !currentHost) return;
  cfg.perms = { ...cfg.perms, [currentHost]: b.dataset.level };
  await chrome.storage.local.set({ perms: cfg.perms });
  refreshSite();
});

// ⓘ 권한 단계 설명 (눌러서 열고 닫기, Esc 로 닫기)
function setHelp(open) {
  $("levelHelp").hidden = !open;
  $("levelInfo").setAttribute("aria-expanded", String(open));
}
$("levelInfo").addEventListener("click", () => setHelp($("levelHelp").hidden));
$("helpOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());

$("pause").addEventListener("click", async () => {
  cfg.paused = !cfg.paused;
  await chrome.storage.local.set({ paused: cfg.paused });
  renderPause();
});

function renderPause() {
  $("pause").setAttribute("aria-pressed", String(cfg.paused));
  $("pause").textContent = cfg.paused ? "멈춤 해제" : "멈춤";
  $("pause").title = cfg.paused ? "지금은 모든 보기·조작이 막혀 있습니다" : "모든 보기·조작을 즉시 막습니다";
}

chrome.tabs.onActivated.addListener(refreshSite);
chrome.tabs.onUpdated.addListener((_id, info) => { if (info.url || info.status === "complete") refreshSite(); });
chrome.windows.onFocusChanged.addListener(refreshSite);

// ------------------------------------------------------------------ 탭 전환
function showTab(name) {
  const chat = name === "chat";
  $("tabChat").setAttribute("aria-selected", String(chat));
  $("tabLog").setAttribute("aria-selected", String(!chat));
  $("chatView").hidden = !chat;
  $("logView").hidden = chat;
  if (!chat) refreshActivity();
}
$("tabChat").addEventListener("click", () => showTab("chat"));
$("tabLog").addEventListener("click", () => showTab("log"));

// ------------------------------------------------------------------ 대화
const EXAMPLES = ["이 페이지 요약해줘", "이 페이지에서 중요한 숫자만 뽑아줘", "검색창에 날씨 검색해줘"];

async function loadChats(selectId) {
  try { chats = await api("/api/chats"); } catch { chats = []; }
  const sel = $("chatSelect");
  sel.replaceChildren(el("option", { value: "", text: "새 대화" }));
  for (const c of chats) {
    sel.append(el("option", { value: c.id, text: `${c.title || "(제목 없음)"} · ${BACKEND_NAME[c.backend] || c.backend}` }));
  }
  const id = selectId !== undefined ? selectId : (currentChat ? currentChat.id : "");
  sel.value = chats.some((c) => c.id === id) ? id : "";
  await openChat(sel.value || null);
}

function emptyState() {
  const ex = el("div", { class: "examples" });
  for (const t of EXAMPLES) {
    const b = el("button", { type: "button", text: t });
    b.addEventListener("click", () => { $("input").value = t; autoGrow(); $("input").focus(); });
    ex.append(b);
  }
  return el("div", { class: "empty" },
    el("strong", { text: "에이전트가 이 탭을 대신 보고 조작합니다" }),
    el("span", { text: "할 일을 적거나 아래 예시를 눌러 보세요." }), ex);
}

async function openChat(id) {
  currentChat = id ? chats.find((c) => c.id === id) || null : null;
  const box = $("messages");
  box.replaceChildren();
  if (!currentChat) {
    box.append(emptyState());
  } else {
    try {
      const data = await api(`/api/chats/${id}`);
      for (const m of data.messages) box.append(renderMessage(m));
    } catch (e) {
      box.append(el("div", { class: "error", text: `대화를 불러오지 못함: ${e.message}` }));
    }
    box.scrollTop = box.scrollHeight;
  }
  renderComposer();
}

// 작업 단계: 한 줄로 접어 두고(누르면 펼침), 진행 중에는 지금 단계를 보여 준다. 실패가 있으면 펼친다.
function stepsBox() {
  const d = el("details", { class: "steps" });
  const label = el("span", { class: "label" });
  const list = el("ol");
  d.append(el("summary", {}, label), list);
  d.hidden = true;
  let n = 0, fails = 0, current = null;
  const update = () => {
    d.hidden = n === 0 && !current;
    d.classList.toggle("running", !!current);
    d.classList.toggle("failed", fails > 0 && !current);
    label.textContent = current ? `작업 중 · ${current}`
      : `작업 ${n}단계` + (fails ? ` · ${fails}개 실패` : "");
  };
  return {
    node: d,
    start(name) { current = `${TOOL_NAME[name] || name}…`; const li = el("li", { class: "run", text: current }); list.append(li); this.li = li; update(); },
    done(s) {
      const li = this.li || el("li");
      if (!this.li) list.append(li);
      li.className = s.ok ? "" : "fail";
      li.textContent = s.summary || TOOL_NAME[s.name] || s.name;
      this.li = null; current = null; n += 1;
      if (!s.ok) { fails += 1; d.open = true; }
      update();
    },
    finish() { if (current) { current = null; if (this.li) { this.li.remove(); this.li = null; } } update(); },
  };
}

function renderMessage(m) {
  if (m.role === "user") return el("div", { class: "msg user", text: m.text });
  const wrap = el("div", { class: "msg assistant" });
  const steps = stepsBox();
  for (const s of m.steps || []) if (s.type === "tool_result") steps.done(s);
  wrap.append(steps.node);
  if (m.error) wrap.append(el("div", { class: "error", text: m.error }));
  if (m.text) { const body = el("div", { class: "md" }); body.innerHTML = renderMarkdown(m.text); wrap.append(body); }
  return wrap;
}

$("chatSelect").addEventListener("change", (e) => openChat(e.target.value || null));
$("newChat").addEventListener("click", () => { $("chatSelect").value = ""; openChat(null); $("input").focus(); });

// 더 보기 메뉴 (스크립트로 내려받기)
function setMenu(open) {
  $("moreMenu").hidden = !open;
  $("moreBtn").setAttribute("aria-expanded", String(open));
}
$("moreBtn").addEventListener("click", (e) => { e.stopPropagation(); setMenu($("moreMenu").hidden); });
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) setMenu(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { setMenu(false); setHelp(false); } });

$("exportChat").addEventListener("click", async () => {
  setMenu(false);
  if (!currentChat) return;
  try {
    const r = await fetch(`${cfg.server.replace(/\/+$/, "")}/api/chats/${currentChat.id}/script?format=sonnun`);
    if (!r.ok) { let d = `${r.status}`; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
    const url = URL.createObjectURL(new Blob([await r.text()], { type: "text/x-python" }));
    const a = el("a", { href: url, download: `sonnun_${currentChat.id}.py` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    $("messages").append(el("div", { class: "error", text: `스크립트로 내보내지 못함: ${e.message}` }));
  }
});

function chosenBackend() {
  return document.querySelector("input[name=backend]:checked").value;
}

function renderComposer() {
  const isNew = !currentChat;
  const backend = isNew ? chosenBackend() : currentChat.backend;
  document.querySelector(".pick").hidden = !isNew;
  for (const r of document.querySelectorAll("input[name=backend]")) {
    const b = status && status.backends[r.value];
    r.disabled = !!running || !b || !b.ok;
  }
  const note = $("sessionNote");
  note.classList.remove("warn");
  const b = status && status.backends[backend];
  const prefix = isNew ? "" : `이 대화: ${BACKEND_NAME[backend]} · `;
  if (!status) {
    note.textContent = "서버가 꺼져 있습니다 — 터미널에서 python -m sonnun serve";
    note.classList.add("warn");
  } else if (!b || !b.ok) {
    note.textContent = `${BACKEND_NAME[backend]} 사용 불가: ${b ? b.why : "알 수 없음"}`;
    note.classList.add("warn");
  } else if (backend === "claude") {
    note.textContent = prefix + "보는 페이지 내용이 Anthropic 으로 전송됩니다";
    note.classList.add("warn");
  } else {
    note.textContent = prefix + "이 컴퓨터 안에서만";
  }
  const usable = !!status && !!b && b.ok;
  $("send").hidden = !!running;
  $("stop").hidden = !running;
  $("send").disabled = !usable || !$("input").value.trim();
  $("send").dataset.usable = usable ? "1" : "0";
  if (typeof renderMic === "function") renderMic();
  $("input").disabled = !!running;
  $("newChat").disabled = !!running;
  $("moreBtn").disabled = !!running || !currentChat;
  $("chatSelect").disabled = !!running;
}
for (const r of document.querySelectorAll("input[name=backend]")) r.addEventListener("change", renderComposer);

// 입력창: 글 길이만큼 늘어난다 (최대 약 7줄)
function autoGrow() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 168) + "px";
  renderComposer();
}
$("input").addEventListener("input", autoGrow);

$("input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
  e.preventDefault();
  if (e.isComposing || e.keyCode === 229) return;          // 한글 조합 중 Enter 는 확정
  $("composer").requestSubmit();
});

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text || running) return;
  if (voiceTimer) { clearTimeout(voiceTimer); voiceTimer = null; voiceBar(""); }
  let chat = currentChat;
  try {
    if (!chat) {
      chat = await api("/api/chats", { method: "POST", body: JSON.stringify({ backend: chosenBackend() }) });
      chats.unshift(chat);
      currentChat = chat;
      // 보내는 동안에도 선택 칸이 이 대화를 가리키게 (목록 전체는 끝난 뒤 다시 불러온다)
      const opt = el("option", { value: chat.id, text: `${text.slice(0, 40)} · ${BACKEND_NAME[chat.backend] || chat.backend}` });
      $("chatSelect").firstElementChild.after(opt);
      $("chatSelect").value = chat.id;
    }
  } catch (err) {
    $("messages").append(el("div", { class: "error", text: `대화를 만들지 못함: ${err.message}` }));
    return;
  }
  $("input").value = "";
  autoGrow();
  const box = $("messages");
  if (box.querySelector(".empty")) box.replaceChildren();
  box.append(el("div", { class: "msg user", text }));
  const out = el("div", { class: "msg assistant" });
  const steps = stepsBox();
  const body = el("div", { class: "md" });
  const pending = el("div", { class: "typing", role: "status", "aria-label": "생각하는 중" }, el("i"), el("i"), el("i"));
  const meta = el("div", { class: "meta" });
  out.append(steps.node, body, pending, meta);
  box.append(out);
  box.scrollTop = box.scrollHeight;

  const controller = new AbortController();
  running = { chatId: chat.id, controller };
  renderComposer();
  setTimeout(refreshStatus, 400);           // 작업 탭 안내를 곧바로
  let raw = "";
  const showText = (t) => { raw = t; body.innerHTML = renderMarkdown(raw); };
  try {
    const r = await fetch(`${cfg.server.replace(/\/+$/, "")}/api/chats/${chat.id}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, tab_id: currentTabId }), signal: controller.signal,
    });
    if (!r.ok) {
      let d = `${r.status}`; try { d = (await r.json()).detail || d; } catch {}
      throw new Error(d);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        let ev; try { ev = JSON.parse(chunk.slice(6)); } catch { continue; }
        if (ev.type === "tool") {
          steps.start(ev.name);
        } else if (ev.type === "tool_result") {
          steps.done(ev);
        } else if (ev.type === "text") {
          pending.hidden = true;
          showText(raw + ev.text);
        } else if (ev.type === "done") {
          if (!raw && ev.text) showText(ev.text);
          if (ev.cost) meta.textContent = `Claude 비용 $${Number(ev.cost).toFixed(3)}`;
        } else if (ev.type === "error") {
          out.insertBefore(el("div", { class: "error", text: ev.message }), body);
        }
        box.scrollTop = box.scrollHeight;
      }
    }
  } catch (err) {
    if (err.name === "AbortError") meta.textContent = "중지함";
    else out.insertBefore(el("div", { class: "error", text: `보내지 못함: ${err.message}` }), body);
  } finally {
    pending.remove();
    steps.finish();
    running = null;
    await loadChats(chat.id);
    renderComposer();
    if (!$("logView").hidden) refreshActivity();
  }
});

$("stop").addEventListener("click", async () => {
  if (!running) return;
  const { chatId, controller } = running;
  $("stop").disabled = true;
  try { await api(`/api/chats/${chatId}/stop`, { method: "POST" }); } catch {}
  controller.abort();
  $("stop").disabled = false;
});

// ------------------------------------------------------------------ 음성 입력 (엔진: Google 음성 인식 / Whisper)
// 말을 멈추면 알아서 끝난다. 받아적은 글은 설정의 마이크 모드대로:
//   preview = 입력창에 넣고 잠깐(VOICE_PREVIEW_MS) 뒤 보냄 — 그사이 입력창을 건드리거나 Esc·취소면 보내지 않음
//   auto    = 바로 보냄 · fill = 입력창에만 넣음 (Enter 는 사용자가)
// Google: Chrome 내장(Web Speech API) — 내 말이 Google 로 감, 인터넷 필요. 말 끝은 Chrome 이 판단.
// Whisper: 이 컴퓨터 안 — 패널이 녹음하고 소리 크기로 말 끝(조용 VAD.endSilenceMs)을 정해 서버 /api/stt 로 (음성 서버 8013).
const VOICE_ERR = {
  "not-allowed": "마이크를 쓸 수 없습니다 — 한 번 허용이 필요합니다",
  "service-not-allowed": "마이크를 쓸 수 없습니다 — 한 번 허용이 필요합니다",
  "no-speech": "말소리가 들리지 않았습니다. 🎤 를 다시 눌러 말해 주세요",
  "audio-capture": "마이크를 찾지 못했습니다 — 연결된 마이크를 확인해 주세요",
  "network": "Google 음성 인식에 닿지 못했습니다 (인터넷 필요) — 설정에서 Whisper 로 바꾸면 이 컴퓨터 안에서 됩니다",
  "language-not-supported": "이 Chrome 에서 한국어 음성 인식을 쓸 수 없습니다",
};
const VOICE_MODE_NAME = { preview: "잠깐 보여 주고 보냄", auto: "바로 보냄", fill: "입력창에만 넣음" };
const VOICE_ENGINE_NAME = { google: "Google 음성 인식", whisper: "Whisper (이 컴퓨터)" };
// Whisper 쪽 말 끝 감지: 30ms 마다 소리 크기(RMS). 바닥 소음은 지금까지의 가장 조용한 값(천천히 올라감).
const VAD = { frameMs: 30, ratio: 3, minLevel: 0.012, startMs: 200, endSilenceMs: 800, noSpeechMs: 7000, maxMs: 30000 };
let rec = null;               // 듣는(받아적는) 중: { stop(), abort() }
let voiceTimer = null;        // preview 모드의 보내기 예약

function voiceEngine() { return cfg.voiceEngine === "whisper" ? "whisper" : "google"; }
function speechApi() { return window.SpeechRecognition || window.webkitSpeechRecognition || null; }

function voiceBar(msg, { err = false, allow = false, cancel = true, progressMs = 0 } = {}) {
  const bar = $("voiceBar");
  bar.hidden = !msg;
  bar.classList.toggle("err", err);
  $("voiceMsg").textContent = msg || "";
  $("voiceAllow").hidden = !allow;
  $("voiceCancel").hidden = !cancel;
  $("voiceCancel").textContent = err ? "닫기" : "취소";
  const p = $("voiceProgress");
  p.hidden = !progressMs;
  p.classList.remove("run");
  if (progressMs) { p.style.setProperty("--ms", `${progressMs}ms`); void p.offsetWidth; p.classList.add("run"); }
}

function renderMic() {
  const mic = $("mic");
  const listening = !!rec;
  mic.setAttribute("aria-pressed", String(listening));
  const engine = voiceEngine();
  if (engine === "google" && !speechApi()) {
    mic.disabled = true;
    mic.title = "이 브라우저에는 Google 음성 인식이 없습니다 — 설정에서 Whisper 로";
    return;
  }
  mic.disabled = !listening && (!!running || $("send").dataset.usable !== "1");
  mic.title = listening ? "그만 듣기 (말을 멈추면 자동으로 끝납니다)"
    : `말로 입력 — ${VOICE_ENGINE_NAME[engine]} · ${VOICE_MODE_NAME[cfg.voiceMode] || VOICE_MODE_NAME.preview}`;
  mic.setAttribute("aria-label", listening ? "그만 듣기" : "말로 입력");
}

function cancelVoiceSend(msg = "") {
  if (!voiceTimer) return false;
  clearTimeout(voiceTimer);
  voiceTimer = null;
  voiceBar(msg, { cancel: false });
  if (msg) setTimeout(() => { if (!voiceTimer && !rec) voiceBar(""); }, 2000);
  return true;
}

function setListening(on) {
  $("inputbox").classList.toggle("listening", on);
  renderMic();
}

function voiceFailed(code, detail = "") {
  rec = null;
  setListening(false);
  const allow = code === "not-allowed" || code === "service-not-allowed";
  voiceBar(VOICE_ERR[code] || detail || `음성 인식 오류: ${code}`, { err: true, allow });
}

// 받아적기가 끝남 → 마이크 모드대로
function voiceFinished(spoken) {
  rec = null;
  setListening(false);
  if (!spoken || !$("input").value.trim()) { voiceFailed("no-speech"); return; }
  const mode = cfg.voiceMode || "preview";
  if (mode === "auto") { voiceBar(""); $("composer").requestSubmit(); return; }
  if (mode === "fill") { voiceBar(""); $("input").focus(); return; }
  voiceBar(`${(VOICE_PREVIEW_MS / 1000).toFixed(1)}초 뒤 보냅니다 — 고치려면 입력창을 누르세요`, { progressMs: VOICE_PREVIEW_MS });
  voiceTimer = setTimeout(() => { voiceTimer = null; voiceBar(""); $("composer").requestSubmit(); }, VOICE_PREVIEW_MS);
}

function putSpoken(base, text) {
  $("input").value = [base, text.trim()].filter(Boolean).join(" ");
  autoGrow();
}

function startVoice() {
  if (rec) return;
  cancelVoiceSend();
  if (voiceEngine() === "whisper") startWhisper(); else startGoogle();
}

// ---- Google (Chrome 내장)
function startGoogle() {
  const SR = speechApi();
  if (!SR) return;
  const base = $("input").value.trim();
  let finalText = "", failed = false, cancelled = false;
  const r = new SR();
  r.lang = "ko-KR";
  r.interimResults = true;
  r.continuous = false;
  r.maxAlternatives = 1;
  r.onresult = (ev) => {
    let fin = "", interim = "";
    for (let i = 0; i < ev.results.length; i++) {
      const t = ev.results[i][0].transcript;
      if (ev.results[i].isFinal) fin += t; else interim += t;
    }
    finalText = fin.trim();
    putSpoken(base, fin + interim);
  };
  r.onerror = (ev) => {
    if (ev.error === "aborted") return;
    failed = true;
    voiceFailed(ev.error);
  };
  r.onend = () => {
    if (failed) return;
    if (cancelled) { rec = null; setListening(false); return; }
    voiceFinished(finalText);
  };
  rec = { stop: () => r.stop(), abort: () => { cancelled = true; putSpoken(base, ""); r.abort(); } };
  setListening(true);
  voiceBar("듣는 중… 말을 멈추면 자동으로 끝납니다");
  try { r.start(); } catch (e) { voiceFailed("error", `음성 인식을 시작하지 못함: ${e.message || e}`); }
}

// ---- Whisper (이 컴퓨터 안: 녹음 → 손눈서브 서버 → 음성 서버)
async function startWhisper() {
  const stt = status && status.stt;
  if (!stt || !stt.ok) {
    voiceBar(stt ? stt.why : "서버가 꺼져 있어 Whisper 를 쓸 수 없습니다", { err: true });
    return;
  }
  const base = $("input").value.trim();
  let cancelled = false, done = false;
  rec = { stop: () => finish(), abort: () => { cancelled = true; finish(); } };
  setListening(true);
  voiceBar("듣는 중… 말을 멈추면 자동으로 끝납니다 (Whisper)");
  let stream, ctx;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) {
    voiceFailed(e.name === "NotAllowedError" || e.name === "SecurityError" ? "not-allowed"
      : e.name === "NotFoundError" ? "audio-capture" : "error", `마이크를 열지 못함: ${e.message || e.name}`);
    return;
  }
  if (cancelled) { for (const t of stream.getTracks()) t.stop(); rec = null; setListening(false); voiceBar(""); return; }
  const chunks = [];
  const frameN = 16 * VAD.frameMs;
  let acc = 0, accN = 0, t = 0, floor = null, voiced = 0, silent = 0, speaking = false;
  try {
    ctx = new AudioContext({ sampleRate: 16000 });
    await ctx.audioWorklet.addModule(chrome.runtime.getURL("mic-worklet.js"));
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "sonnun-tap");
    src.connect(node);
    node.connect(ctx.destination);           // 그래프에 붙어 있어야 돈다 (내보내는 소리는 없음)
    node.port.onmessage = (e) => {
      if (done) return;
      const x = e.data;
      chunks.push(x);
      for (let i = 0; i < x.length; i++) { acc += x[i] * x[i]; accN++; }
      if (accN < frameN) return;
      const rms = Math.sqrt(acc / accN);
      acc = 0; accN = 0; t += VAD.frameMs;
      floor = floor === null ? rms : Math.min(floor * 1.002, rms);
      const loud = rms > Math.max(VAD.minLevel, floor * VAD.ratio);
      if (loud) { voiced += VAD.frameMs; silent = 0; if (voiced >= VAD.startMs && !speaking) { speaking = true; voiceBar("듣는 중… (말이 끝나면 받아적습니다)"); } }
      else { silent += VAD.frameMs; if (!speaking) voiced = 0; }
      if (speaking && silent >= VAD.endSilenceMs) finish();
      else if (!speaking && t >= VAD.noSpeechMs) { cancelled = true; finish(); voiceFailed("no-speech"); }
      else if (t >= VAD.maxMs) finish();
    };
  } catch (e) {
    for (const tr of stream.getTracks()) tr.stop();
    voiceFailed("error", `녹음을 시작하지 못함: ${e.message || e}`);
    return;
  }

  async function finish() {
    if (done) return;
    done = true;
    if (stream) for (const tr of stream.getTracks()) tr.stop();
    if (ctx) ctx.close().catch(() => {});
    if (cancelled) { if (rec) { rec = null; setListening(false); voiceBar(""); } return; }
    voiceBar("받아적는 중… (Whisper)", { cancel: false });
    const n = chunks.reduce((s, c) => s + c.length, 0);
    const pcm = new Int16Array(n);
    let o = 0;
    for (const c of chunks) for (let i = 0; i < c.length; i++) pcm[o++] = Math.max(-1, Math.min(1, c[i])) * 0x7fff;
    try {
      const r = await fetch(cfg.server.replace(/\/+$/, "") + "/api/stt", {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: pcm.buffer });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { voiceFailed("error", j.detail || `받아적기 실패 (${r.status})`); return; }
      putSpoken(base, j.text || "");
      voiceFinished((j.text || "").trim());
    } catch (e) {
      voiceFailed("error", `받아적기 실패: ${e.message || e}`);
    }
  }
}

function stopVoice(cancel = false) {
  if (!rec) return;
  if (cancel) { rec.abort(); voiceBar(""); } else rec.stop();
}

$("mic").addEventListener("click", () => (rec ? stopVoice() : startVoice()));
$("voiceCancel").addEventListener("click", () => {
  if (rec) stopVoice(true);
  else if (!cancelVoiceSend("보내지 않았습니다 — 고쳐서 Enter 로 보내세요")) voiceBar("");
});
$("voiceAllow").addEventListener("click", () => {
  // 사이드 패널에서는 Chrome 이 마이크 허용 창을 못 띄운다 → 설정 화면(탭)에서 한 번 허용받는다
  chrome.tabs.create({ url: chrome.runtime.getURL("options.html#mic") });
  voiceBar("");
});
// preview 모드: 보내기 전에 사용자가 입력창을 건드리면 보내지 않는다
for (const ev of ["pointerdown", "keydown"]) {
  $("input").addEventListener(ev, (e) => {
    if (!voiceTimer || (ev === "keydown" && e.key === "Enter")) return;
    cancelVoiceSend("보내지 않았습니다 — 고쳐서 Enter 로 보내세요");
  });
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (rec) stopVoice(true);
  else cancelVoiceSend("보내지 않았습니다 — 고쳐서 Enter 로 보내세요");
});

// ------------------------------------------------------------------ 기록
async function refreshActivity() {
  const list = $("activity");
  let rows;
  try { rows = await api("/api/activity?limit=100"); } catch (e) {
    list.replaceChildren(el("li", { class: "fail" }, el("span", { class: "what", text: "기록을 불러오지 못함 (서버 꺼짐)" })));
    return;
  }
  if (!rows.length) {
    list.replaceChildren(el("li", {}, el("span", { class: "what", text: "아직 에이전트가 이 브라우저를 쓴 기록이 없습니다." })));
    return;
  }
  list.replaceChildren(...rows.map((a) => el("li", { class: a.ok ? "" : "fail" },
    el("time", { datetime: a.ts, text: hhmm(a.ts) }),
    el("span", { class: "what", text: `${a.summary}` }),
    el("span", { class: "where", text: `${SOURCE_NAME[a.source] || a.source}${a.url ? " · " + readableUrl(a.url) : ""}`, title: a.url || "" }))));
}

// ------------------------------------------------------------------ 설정 (사이트 권한 목록·승인·진짜 입력·서버는 설정 화면에서)
$("openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());

chrome.storage.onChanged.addListener((changes) => {
  for (const [k, v] of Object.entries(changes)) cfg[k] = v.newValue;
  if (changes.perms || changes.defaultLevel) refreshSite();
  if (changes.paused) renderPause();
  if (changes.server) { refreshStatus(); loadChats(); }
  if (changes.voiceMode || changes.voiceEngine) renderMic();
});

// ------------------------------------------------------------------ 시작
(async () => {
  cfg = await chrome.storage.local.get(DEFAULTS);
  renderPause();
  try { pendingReqs = (await chrome.runtime.sendMessage({ type: "requests?" })).requests || []; } catch { pendingReqs = []; }
  renderRequests();
  setInterval(tickRequests, 1000);
  await refreshStatus();
  await refreshSite();
  await loadChats();
  setInterval(refreshStatus, 5000);
  setInterval(() => { if (!$("logView").hidden) refreshActivity(); }, 3000);
})();
