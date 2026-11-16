// 도우미 답의 마크다운 → HTML (사이드 패널 전용, 외부 라이브러리 없이 — 확장 화면은 외부 스크립트를 못 불러온다).
// 답에는 페이지 내용이 섞일 수 있으므로 **모든 글자를 먼저 HTML 이스케이프**하고, 그 위에 정해진 태그만 만든다.
// 링크는 http(s) 만, 새 탭으로. 지원: 문단·줄바꿈·제목(#~###)·굵게·기울임·인라인 코드·코드 블록·목록(-,*,•,1.)·인용·표·구분선.

function mdEscape(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function mdInline(text) {
  // 인라인 코드는 먼저 떼어 두었다가 마지막에 되돌린다 (안의 * 가 굵게로 바뀌지 않게)
  const codes = [];
  let s = mdEscape(text).replace(/`([^`]+)`/g, (_m, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  return s.replace(/\u0000(\d+)\u0000/g, (_m, i) => `<code>${codes[Number(i)]}</code>`);
}

function mdTableRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function renderMarkdown(src) {
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l || "");
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {                                    // 코드 블록
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${mdEscape(buf.join("\n"))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out.push(`<h${Math.min(h[1].length + 2, 6)}>${mdInline(h[2])}</h${Math.min(h[1].length + 2, 6)}>`); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (line.includes("|") && isTableSep(lines[i + 1])) {           // 표
      const head = mdTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(mdTableRow(lines[i++]));
      out.push(`<div class="table"><table><thead><tr>${head.map((c) => `<th>${mdInline(c)}</th>`).join("")}</tr></thead><tbody>`
        + rows.map((r) => `<tr>${head.map((_c, k) => `<td>${mdInline(r[k] || "")}</td>`).join("")}</tr>`).join("")
        + "</tbody></table></div>");
      continue;
    }
    if (/^\s*>/.test(line)) {                                       // 인용
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`);
      continue;
    }
    const li = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/;
    if (li.test(line)) {                                             // 목록 (들여쓰기 한 단계까지)
      const ordered = /\d/.test(line.match(li)[2]);
      const items = [];
      while (i < lines.length && (li.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        const m = lines[i].match(li);
        if (m && (m[1].length < 2 || !items.length)) items.push({ text: m[3], sub: [] });
        else if (m) items[items.length - 1].sub.push(m[3]);
        else items[items.length - 1].text += " " + lines[i].trim();
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>` + items.map((it) => `<li>${mdInline(it.text)}`
        + (it.sub.length ? `<ul>${it.sub.map((t) => `<li>${mdInline(t)}</li>`).join("")}</ul>` : "") + "</li>").join("") + `</${tag}>`);
      continue;
    }
    const buf = [];                                                  // 문단 (줄바꿈 유지)
    while (i < lines.length && lines[i].trim() && !/^\s*```/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])
           && !li.test(lines[i]) && !/^\s*>/.test(lines[i]) && !(lines[i].includes("|") && isTableSep(lines[i + 1]))) {
      buf.push(mdInline(lines[i++]));
    }
    out.push(`<p>${buf.join("<br>")}</p>`);
  }
  return out.join("");
}
