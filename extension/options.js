// 손눈서브 설정 화면 — 사이트 권한 목록(추가·바꾸기·지우기)·기본 권한·승인·진짜 입력·서버 주소.
// 모두 chrome.storage.local 에 바로 저장하고, background.js 가 실제 동작 직전에 읽어 검사한다 (shared.js 의 같은 규칙).
// 패널에서 바꾼 것도 onChanged 로 바로 반영된다.

const LEVEL_NAME = { off: "끔", eye: "눈길", hand: "손길" };
const LEVEL_DESC = {
  off: "에이전트가 보지도 조작하지도 못합니다",
  eye: "에이전트가 볼 수만 있습니다",
  hand: "에이전트가 보고 클릭·입력할 수 있습니다",
};
const RANK = { off: 0, eye: 1, hand: 2 };

const $ = (id) => document.getElementById(id);
let cfg = { ...DEFAULTS };
let flashSite = "";

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kids) if (k) e.append(k);
  return e;
}

function seg(level, label, onPick) {
  const box = el("div", { class: "seg", role: "radiogroup", "aria-label": label });
  for (const lv of ["off", "eye", "hand"]) {
    const b = el("button", { type: "button", role: "radio", "data-level": lv, "aria-checked": String(lv === level), text: LEVEL_NAME[lv] });
    b.addEventListener("click", () => onPick(lv));
    box.append(b);
  }
  return box;
}

async function savePerms(perms) {
  cfg.perms = perms;
  await chrome.storage.local.set({ perms });
  renderRules();
}

// ------------------------------------------------------------------ 기본 권한
function renderDefault() {
  for (const b of $("defaultLevels").querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.level === cfg.defaultLevel));
  const hint = $("defaultHint");
  hint.textContent = `목록에 없는 사이트는 ${LEVEL_NAME[cfg.defaultLevel]} — ${LEVEL_DESC[cfg.defaultLevel]}.`
    + (cfg.defaultLevel === "hand" ? " 증권·은행·메일처럼 건드리면 안 되는 사이트는 아래에 끔이나 눈길로 추가하세요." : "");
  hint.classList.toggle("warn", cfg.defaultLevel === "hand");
}
$("defaultLevels").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-level]");
  if (!b) return;
  cfg.defaultLevel = b.dataset.level;
  await chrome.storage.local.set({ defaultLevel: cfg.defaultLevel });
  renderDefault();
  renderRules();
});

// ------------------------------------------------------------------ 사이트 추가
function addPreview() {
  const raw = $("addSite").value;
  const site = siteFromInput(raw);
  const msg = $("addMsg");
  msg.className = "hint";
  if (!raw.trim()) { msg.textContent = ""; return site; }
  if (!site) { msg.textContent = "사이트 주소로 알아볼 수 없습니다 (예: bank.com, https://www.bank.com/login)"; msg.classList.add("err"); return ""; }
  const now = cfg.perms[site];
  msg.textContent = site === "file"
    ? "로컬 파일(file://) 전체에 적용"
    : `${site} 과 하위 주소(www.${site}, m.${site} 등)에 적용` + (now ? ` — 지금 ${LEVEL_NAME[now]}, 추가하면 바뀝니다` : "");
  return site;
}
$("addSite").addEventListener("input", addPreview);
$("addForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (e.isComposing) return;
  const site = addPreview();
  if (!site) { $("addSite").focus(); return; }
  const level = $("addLevel").value;
  const before = cfg.perms[site];
  flashSite = site;
  $("filter").value = "";
  await savePerms({ ...cfg.perms, [site]: level });
  $("addSite").value = "";
  const msg = $("addMsg");
  msg.className = "hint saved";
  msg.textContent = before ? `${site}: ${LEVEL_NAME[before]} → ${LEVEL_NAME[level]}` : `${site} 을(를) ${LEVEL_NAME[level]}으로 추가함`;
  $("addSite").focus();
});

// ------------------------------------------------------------------ 목록
function renderRules() {
  const rules = Object.entries(cfg.perms || {})
    .sort((a, b) => RANK[a[1]] - RANK[b[1]] || a[0].localeCompare(b[0]));
  const q = $("filter").value.trim().toLowerCase();
  const shown = q ? rules.filter(([site]) => site.includes(q)) : rules;
  const n = { off: 0, eye: 0, hand: 0 };
  for (const [, lv] of rules) n[lv] = (n[lv] || 0) + 1;
  $("listTools").hidden = rules.length < 6;
  $("counts").textContent = `끔 ${n.off} · 눈길 ${n.eye} · 손길 ${n.hand}`;

  const list = $("rules");
  const empty = document.querySelector(".empty-rules");
  if (empty) empty.remove();
  if (!rules.length) {
    list.replaceChildren();
    list.after(el("div", { class: "empty-rules" },
      el("strong", { text: "아직 따로 정한 사이트가 없습니다" }),
      document.createTextNode(`모든 사이트가 기본값(${LEVEL_NAME[cfg.defaultLevel]})으로 열립니다. 위 칸에 주소를 넣어 막을 사이트를 추가하세요.`)));
    return;
  }
  if (!shown.length) {
    list.replaceChildren();
    list.after(el("div", { class: "empty-rules" }, document.createTextNode(`"${q}" 이(가) 든 사이트가 없습니다.`)));
    return;
  }
  list.replaceChildren(...shown.map(([site, level]) => {
    const parent = site === "file" ? null : siteRule(site.split(".").slice(1).join("."), cfg.perms);
    const scope = site === "file" ? "로컬 파일 전체"
      : parent ? `하위 주소 포함 · ${parent}(${LEVEL_NAME[cfg.perms[parent]]}) 안의 예외` : "하위 주소 포함";
    const del = el("button", { type: "button", class: "ghost del", text: "지우기", "aria-label": `${site} 규칙 지우기` });
    del.addEventListener("click", async () => {
      const { [site]: _gone, ...rest } = cfg.perms;
      await savePerms(rest);
      const msg = $("addMsg");
      msg.className = "hint saved";
      msg.textContent = `${site} 규칙을 지움 — 이제 ${parent ? parent + " 규칙" : "기본값"}(${LEVEL_NAME[levelForHost(site, rest, cfg.defaultLevel).level]})`;
    });
    const li = el("li", { "data-site": site, "data-level": level },
      el("div", { class: "site" }, el("strong", { text: site, title: site }), el("span", { text: scope })),
      seg(level, `${site} 권한`, (lv) => savePerms({ ...cfg.perms, [site]: lv })),
      del);
    if (site === flashSite) { li.classList.add("flash"); setTimeout(() => li.classList.remove("flash"), 900); }
    return li;
  }));
  flashSite = "";
}
$("filter").addEventListener("input", renderRules);

// ------------------------------------------------------------------ 승인
function saved(id) {
  $(id).textContent = "저장함";
  clearTimeout(saved[id]);
  saved[id] = setTimeout(() => { $(id).textContent = ""; }, 1800);
}
$("confirmOn").addEventListener("change", async (e) => {
  await chrome.storage.local.set({ confirmOn: e.target.checked });
  $("confirmWords").disabled = !e.target.checked;
  saved("confirmMsg");
});
$("confirmWords").addEventListener("change", async (e) => {
  const words = e.target.value.split(/[,，\n]/).map((w) => w.trim()).filter(Boolean).join(",");
  e.target.value = words || CONFIRM_WORDS;
  await chrome.storage.local.set({ confirmWords: e.target.value });
  saved("confirmMsg");
});
$("approvalTimeout").addEventListener("change", async (e) => {
  const sec = Math.min(600, Math.max(30, Math.round(Number(e.target.value) || DEFAULTS.approvalTimeoutSec)));
  e.target.value = sec;
  await chrome.storage.local.set({ approvalTimeoutSec: sec });
  saved("confirmMsg");
});
$("resetWords").addEventListener("click", async () => {
  $("confirmWords").value = CONFIRM_WORDS;
  await chrome.storage.local.set({ confirmWords: CONFIRM_WORDS });
  saved("confirmMsg");
});

// ------------------------------------------------------------------ 음성 입력
for (const r of document.querySelectorAll("input[name=voiceEngine]")) {
  r.addEventListener("change", async () => {
    await chrome.storage.local.set({ voiceEngine: r.value });
    cfg.voiceEngine = r.value;
    saved("voiceMsg");
    checkServer();
  });
}
for (const r of document.querySelectorAll("input[name=voiceMode]")) {
  r.addEventListener("change", async () => {
    await chrome.storage.local.set({ voiceMode: r.value });
    saved("voiceMsg");
  });
}
const MIC_STATE = { granted: "허용됨 — 패널의 🎤 를 쓸 수 있습니다", denied: "막혀 있음 — 주소창 왼쪽 아이콘(사이트 설정)에서 마이크를 허용으로 바꿔 주세요",
                    prompt: "아직 허용하지 않음" };
async function renderMicState() {
  try {
    const st = await navigator.permissions.query({ name: "microphone" });
    $("micState").textContent = MIC_STATE[st.state] || st.state;
    $("micState").className = "hint" + (st.state === "granted" ? " saved" : st.state === "denied" ? " err" : "");
    $("micAllow").hidden = st.state === "granted";
    st.onchange = renderMicState;
  } catch { $("micState").textContent = ""; }
}
$("micAllow").addEventListener("click", async () => {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    for (const tr of stream.getTracks()) tr.stop();          // 허용만 받고 바로 끈다
  } catch (e) {
    $("micState").textContent = `허용하지 못함: ${e.message || e.name}`;
    $("micState").className = "hint err";
    return;
  }
  renderMicState();
});

// ------------------------------------------------------------------ 진짜 입력
$("trusted").addEventListener("change", async (e) => {
  await chrome.storage.local.set({ trusted: e.target.checked });
});

// Whisper 를 골랐을 때만 음성 서버 상태를 보여 준다
function renderStt(stt) {
  const el_ = $("sttState");
  if (cfg.voiceEngine !== "whisper") { el_.textContent = ""; return; }
  el_.className = "hint " + (stt && stt.ok ? "saved" : "err");
  el_.textContent = !stt ? "손눈서브 서버가 꺼져 있어 음성 서버 상태를 모릅니다"
    : stt.ok ? `음성 서버 켜짐 (${stt.why})` : stt.why;
}

// ------------------------------------------------------------------ 서버
async function checkServer() {
  const s = $("serverState");
  try {
    const r = await fetch(cfg.server.replace(/\/+$/, "") + "/api/status");
    const j = await r.json();
    s.dataset.state = "ok";
    s.textContent = j.extension && j.extension.connected ? "연결됨" : "서버 켜짐 · 확장 연결 중";
    renderStt(j.stt);
  } catch {
    s.dataset.state = "down";
    s.textContent = "서버 꺼짐";
    s.title = "터미널에서 python -m sonnun serve";
    renderStt(null);
  }
}
$("server").addEventListener("change", async (e) => {
  let v = e.target.value.trim() || DEFAULTS.server;
  try { const u = new URL(v); if (!/^https?:$/.test(u.protocol)) throw 0; v = u.origin; } catch {
    $("serverMsg").className = "hint err";
    $("serverMsg").textContent = "http:// 로 시작하는 주소를 넣어 주세요 (예: http://127.0.0.1:8771)";
    return;
  }
  e.target.value = v;
  cfg.server = v;
  await chrome.storage.local.set({ server: v });
  $("serverMsg").className = "hint saved";
  saved("serverMsg");
  setTimeout(checkServer, 800);
});

// ------------------------------------------------------------------ 다른 곳(패널)에서 바꾼 것 반영
chrome.storage.onChanged.addListener((changes) => {
  for (const [k, v] of Object.entries(changes)) cfg[k] = v.newValue === undefined ? DEFAULTS[k] : v.newValue;
  if (changes.perms || changes.defaultLevel) { renderDefault(); renderRules(); }
  if (changes.trusted) $("trusted").checked = !!cfg.trusted;
  if (changes.confirmOn) $("confirmOn").checked = cfg.confirmOn !== false;
});

// ------------------------------------------------------------------ 시작
(async () => {
  cfg = await chrome.storage.local.get(DEFAULTS);
  renderDefault();
  renderRules();
  $("confirmOn").checked = cfg.confirmOn !== false;
  $("confirmWords").value = cfg.confirmWords || CONFIRM_WORDS;
  $("confirmWords").disabled = cfg.confirmOn === false;
  $("approvalTimeout").value = cfg.approvalTimeoutSec || DEFAULTS.approvalTimeoutSec;
  $("trusted").checked = !!cfg.trusted;
  $("server").value = cfg.server;
  const vm = document.querySelector(`input[name=voiceMode][value="${cfg.voiceMode || "preview"}"]`);
  if (vm) vm.checked = true;
  const ve = document.querySelector(`input[name=voiceEngine][value="${cfg.voiceEngine || "google"}"]`);
  if (ve) ve.checked = true;
  renderMicState();
  if (location.hash === "#mic") { $("mic").scrollIntoView(); $("micAllow").focus(); }
  checkServer();
  setInterval(checkServer, 5000);
})();
