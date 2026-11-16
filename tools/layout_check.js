() => {
  // ui-design 스킬 stages/6-verify.md 의 레이아웃 자동 측정 (화면 밖 넘침 · 24px 미만 클릭 대상 · 겹친 클릭 대상)
  const W = document.documentElement.clientWidth, out = {overflow: [], small: [], overlap: []};
  const vis = e => { const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return (!e.checkVisibility || e.checkVisibility({visibilityProperty: true})) && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0; };
  const name = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + ' "' + (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 20) + '"';
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue; const r = e.getBoundingClientRect();
    if (r.right > W + 1 && getComputedStyle(e.parentElement).overflowX === 'visible') out.overflow.push(name(e));
  }
  const hits = [...document.querySelectorAll('a[href],button,input:not([type=hidden]),select,textarea,[role=button],[onclick]')].filter(vis);
  const tiny = r => r.width < 24 || r.height < 24;
  for (const e of hits) {
    const r = e.getBoundingClientRect();
    const lab = e.closest('label') || (e.id && document.querySelector(`label[for="${e.id}"]`));
    if (tiny(r) && !(lab && vis(lab) && !tiny(lab.getBoundingClientRect())) && !e.closest('p,li,td'))
      out.small.push(name(e) + ` ${Math.round(r.width)}x${Math.round(r.height)}`); }
  for (let i = 0; i < hits.length; i++) for (let j = i + 1; j < hits.length; j++) {
    const a = hits[i].getBoundingClientRect(), b = hits[j].getBoundingClientRect();
    if (hits[i].contains(hits[j]) || hits[j].contains(hits[i])) continue;
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) out.overlap.push(name(hits[i]) + ' ↔ ' + name(hits[j]));
  }
  return out;
}
