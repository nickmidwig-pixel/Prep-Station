(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const SR = 48000;
  const RATES = { '23.976': [24000, 1001], '24': [24, 1], '25': [25, 1], '29.97': [30000, 1001], '30': [30, 1],
    '47.952': [48000, 1001], '48': [48, 1], '50': [50, 1], '59.94': [60000, 1001], '60': [60, 1] };
  const COL = {
    cam: '#6E9CC4', aud: '#93B58A', fix: '#E2A23B', bad: '#E0614A', off: '#5E666E', led: '#FF3B30', man: '#F2EFE6',
    paper: '#E6E2D8', muted: '#8E969E', line: '#363E46', well: '#15181B', lane: '#1B1F23', label: '#20252A',
  };
  // cameras get their own hue each (reference first); sound is coloured by what the mic is
  const CAM_HUES = ['#6E9CC4', '#5FB3AE', '#9B8BD1', '#7FB0E8', '#C48BB8', '#8FA3B8', '#6FA88F', '#B39C7A'];
  const ROLE_COL = { boom: '#D9B95B', lav: '#93B58A', plant: '#8FC1A9', multi: '#C99A6B', mix: '#9A938A', tc: '#6F7780', empty: '#5E666E' };
  const ROLE_LABEL = { boom: 'boom', lav: 'lav', plant: 'plant', multi: 'multitrack', mix: 'mix', tc: 'timecode', cam: 'camera' };
  const STEP_MAP = { 'Building aligned ISOs': 'Auto-Align Post', 'Checking alignment': 'Auto-Align Post' };

  const S = { project: null, settings: {}, running: false, scanning: false, sel: null, view: null, done: null, day: null, checks: [],
    expanded: new Set(), collapsed: new Set(), ph: null, playing: null };
  const api = () => (window.pywebview && window.pywebview.api) || null;
  const APP = () => (S.app && S.app.name) || 'Prep Station';
  const call = async (name, ...args) => { const a = api(); if (!a || !a[name]) return null; try { return await a[name](...args); } catch (e) { console.error(e); return null; } };

  // ------------------------------------------------------------------ timecode
  function fpsPair() { return RATES[(S.project && S.project.fps) || '23.976'] || RATES['23.976']; }
  function samplesToTc(s, withFrames = true) {
    if (s == null) return '--:--:--:--';
    const [n, d] = fpsPair();
    const nb = Math.round(n / d);
    let fr = Math.round(s * n / (d * SR));
    fr = ((fr % (nb * 86400)) + nb * 86400) % (nb * 86400);
    const f = fr % nb, sec = Math.floor(fr / nb) % 60, m = Math.floor(fr / (nb * 60)) % 60, h = Math.floor(fr / (nb * 3600)) % 24;
    const p = (x) => String(x).padStart(2, '0');
    return withFrames ? `${p(h)}:${p(m)}:${p(sec)}:${p(f)}` : `${p(h)}:${p(m)}:${p(sec)}`;
  }
  function dur(s) {
    const t = Math.round(s / SR), h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, x = t % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`;
  }
  function setSlate(s, what) {
    $('tcLit').textContent = samplesToTc(s);
    $('tcWhat').textContent = what || '';
  }

  // ------------------------------------------------------------------ rows
  const days = () => (S.project && S.project.days) || [];
  const multiDay = () => days().length > 1;
  const inDay = (c) => !multiDay() || !S.day || c.day === S.day;
  const dayClips = () => (S.project ? S.project.clips.filter(inDay) : []);
  function rolls() {
    if (!S.project) return [];
    const ref = S.settings.reference && S.settings.reference.roll;
    const present = new Set(dayClips().map((c) => c.roll));
    const rs = S.project.rolls.filter((r) => present.has(r.name));
    rs.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'camera' ? -1 : 1;
      if (a.kind === 'camera') return (b.name === ref) - (a.name === ref) || a.name.localeCompare(b.name);
      return (a.track_no ?? 999) - (b.track_no ?? 999) || a.name.localeCompare(b.name);
    });
    return rs;
  }
  const clipsOf = (roll) => dayClips().filter((c) => c.roll === roll);
  // lanes: one per camera body (A CAM, B CAM...) and one per sound track folder; multitrack lanes open into tracks
  const lanesAll = () => (S.project && S.project.lanes) || [];
  const laneOf = (c) => lanesAll().find((l) => l.key === c.lane);
  const laneClips = (key) => dayClips().filter((c) => c.lane === key);
  function laneColor(ln) {
    if (!ln) return COL.aud;
    if (ln.kind === 'camera') return CAM_HUES[Math.max(0, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.indexOf(ln.letter)) % CAM_HUES.length];
    return ROLE_COL[ln.role] || COL.aud;
  }
  function laneRows() {
    const present = new Set(dayClips().map((c) => c.lane));
    const out = [];
    for (const ln of lanesAll()) {
      if (!present.has(ln.key)) continue;
      const multi = ln.kind === 'audio' && ln.channels > 1;
      const open = multi && S.expanded.has(ln.key);
      out.push({ type: 'lane', lane: ln, key: ln.key, multi, open });
      if (open) (ln.tracks || []).forEach((t, i) => out.push({ type: 'track', lane: ln, key: `${ln.key}#${i}`, i, t }));
    }
    return out;
  }
  const refLane = () => {
    const r = S.settings.reference && S.settings.reference.roll;
    return r ? lanesAll().find((l) => l.rolls.includes(r)) : null;
  };
  // what the run will align to: a camera mic, or (no usable camera sound) the recorder's own boom / lav
  const hasCamRef = () => !!(S.project && rolls().some((r) => r.kind === 'camera' && dayClips().some((c) => c.roll === r.name && c.has_audio)));
  function effMode() {
    const m = S.settings.mode || 'auto';
    if (m === 'audio') return 'audio';
    return hasCamRef() ? 'camera' : 'audio';
  }
  function rollColor(name) {
    const r = S.project.rolls.find((x) => x.name === name);
    if (!r) return COL.aud;
    if (r.kind === 'camera') {
      const cams = rolls().filter((x) => x.kind === 'camera').map((x) => x.name);
      const i = Math.max(0, cams.indexOf(name));
      return CAM_HUES[i % CAM_HUES.length];
    }
    return ROLE_COL[r.role] || COL.aud;
  }
  const moved = (c) => c.flags.includes('sync_corrected') || c.flags.includes('sync_found') || c.flags.includes('sync_manual');
  const failed = (c) => c.flags.includes('sync_failed') || ['sample_rate'].some((f) => c.flags.includes(f));
  function clipColor(c) {
    if (!c.include) return COL.off;
    return laneColor(laneOf(c));
  }
  function overlay(c) {
    if (!c.include) return null;
    if (c.slowmo) return 'slow';
    if (failed(c)) return 'bad';
    if (c.flags.includes('sync_manual')) return 'man';
    if (moved(c)) return 'fix';
    return null;
  }

  // ------------------------------------------------------------------ status text
  function status(c) {
    const f = new Set(c.flags);
    const s = c.sync || {};
    if (f.has('sync_manual') && c.manual) {
      const how = { typed: 'Timecode typed in by you', nudged: 'Nudged by you', waveform: 'Placed by waveform (you picked it)' }[c.manual.how] || 'Set by you';
      return { t: `${how}${c.manual.note ? ' · ' + c.manual.note : ''}`, k: 'fix', old: c.orig_tc };
    }
    if (f.has('short_silent')) return { t: 'Short accidental clip, left out', k: '' };
    if (f.has('sync_failed')) return { t: 'Couldn’t be placed by waveform, so it’s left out. Check it by hand.', k: 'bad' };
    if (f.has('sample_rate')) return { t: 'Not 48 kHz, left out of alignment and polywav', k: 'bad' };
    if (f.has('sync_found')) return { t: `No timecode, placed by waveform (match ${s.score ?? '?'} against ${s.against || '?'})`, k: 'fix' };
    if (f.has('sync_corrected')) {
      const sec = (c.start - (c.orig_start ?? c.start)) / SR;
      return { t: `Timecode was off by ${sec > 0 ? '+' : '−'}${Math.abs(sec).toFixed(3)} s, fixed by waveform`, k: 'fix', old: c.orig_tc };
    }
    if (f.has('no_tc')) return { t: 'No timecode. Sync places it by waveform.', k: 'fix' };
    if (f.has('zero_tc')) return { t: 'Timecode starts at 00:00:00:00, probably a missed jam. Sync checks it.', k: 'fix' };
    if (f.has('tc_far')) return { t: 'Timecode is hours away from the rest. Sync checks it.', k: 'fix' };
    if (f.has('tc_overlap')) return { t: 'Timecode overlaps another clip on this card. Sync checks it.', k: 'fix' };
    if (s.note && s.offset != null) return { t: `${(s.offset / 48).toFixed(0)} ms from the camera, Auto-Align takes care of it`, k: '' };
    const extra = [];
    if (c.ltc && c.tc_source && c.tc_source.startsWith('LTC')) extra.push(`Timecode read from the LTC on ch ${c.ltc.channel + 1}`);
    if (c.chapter_of && c.tc_source && c.tc_source.startsWith('follows')) extra.push(`Chapter ${c.chapter}, follows the one before`);
    if (c.converted) extra.push(`Converted ${c.converted} (original untouched)`);
    if (f.has('raw') && c.audio_from) extra.push(`RAW: audio and timecode from ${c.audio_from}`);
    else if (f.has('raw')) extra.push('RAW: synced from its audio and timecode');
    if (c.opatom) extra.push('P2: audio from the AUDIO folder');
    if (f.has('fps_mismatch')) extra.push(`Different frame rate (${c.fps})`);
    if (f.has('no_audio')) extra.push('No audio');
    if (f.has('ubits')) extra.push('User bits differ from the rest of the day');
    return { t: extra.join('. '), k: '' };
  }

  // ------------------------------------------------------------------ timeline canvas
  const cv = $('tl');
  const ctx = cv.getContext('2d');
  const LABEL_W = 150, RULER_H = 26;
  let hoverX = null;

  function span() {
    const cl = dayClips().filter((c) => c.start != null);
    if (!cl.length) return null;
    let a = Infinity, b = -Infinity;
    for (const c of cl) { a = Math.min(a, c.start, c.orig_start ?? c.start); b = Math.max(b, c.start + c.length); }
    return [a, b];
  }
  function fit() {
    const sp = span();
    if (!sp) { S.view = null; return; }
    const pad = Math.max((sp[1] - sp[0]) * 0.03, SR * 10);
    S.view = [sp[0] - pad, sp[1] + pad];
  }
  function layout() {
    const r = cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(r.width * dpr) || cv.height !== Math.round(r.height * dpr)) {
      cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const rows = laneRows();
    const base = rows.map((x) => (x.type === 'lane' ? 34 : 22));
    const want = Math.round(RULER_H + 8 + base.reduce((a, b) => a + b, 0));
    const box = cv.parentElement;
    const maxH = Math.round(window.innerHeight * 0.5);
    const h = Math.max(120, Math.min(want, maxH));
    if (Math.abs(box.getBoundingClientRect().height - h) > 1) { box.style.height = `${h}px`; return layout(); }
    const k = Math.min(1, (r.height - RULER_H - 6) / Math.max(1, base.reduce((a, b) => a + b, 0)));
    let y = RULER_H + 3;
    for (let n = 0; n < rows.length; n++) { rows[n].y = y; rows[n].h = Math.max(14, base[n] * k); y += rows[n].h; }
    return { W: r.width, H: r.height, rows };
  }
  const xOf = (s, L) => LABEL_W + (s - S.view[0]) / (S.view[1] - S.view[0]) * (L.W - LABEL_W);
  const sOf = (x, L) => S.view[0] + (x - LABEL_W) / (L.W - LABEL_W) * (S.view[1] - S.view[0]);

  function niceStep(pxPerSec) {
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800];
    for (const s of steps) if (s * pxPerSec >= 90) return s;
    return 21600;
  }

  function draw() {
    if (!S.project || !S.project.clips.length) return;
    if (!S.view) fit();
    if (!S.view) return;
    const L = layout();
    ctx.clearRect(0, 0, L.W, L.H);
    ctx.fillStyle = COL.well; ctx.fillRect(0, 0, L.W, L.H);
    // ruler: ticks on whole timecode seconds/minutes (TC seconds aren't real seconds at 23.976)
    const [rn, rd] = fpsPair();
    const nb = Math.round(rn / rd);
    const spf = SR * rd / rn;
    const pxs = (L.W - LABEL_W) / ((S.view[1] - S.view[0]) / SR);
    const step = niceStep(pxs);
    const stepF = step * nb;
    const f0 = Math.ceil(S.view[0] / spf / stepF) * stepF;
    ctx.font = '500 11px Barlow, sans-serif'; ctx.textBaseline = 'middle';
    for (let fr = f0; fr * spf < S.view[1]; fr += stepF) {
      const s = fr * spf;
      const x = xOf(s, L);
      ctx.fillStyle = 'rgba(255,255,255,0.05)'; ctx.fillRect(Math.round(x), RULER_H, 1, L.H - RULER_H);
      ctx.fillStyle = COL.muted; ctx.fillText(samplesToTc(s, false), x + 4, RULER_H / 2);
      ctx.fillStyle = COL.line; ctx.fillRect(Math.round(x), RULER_H - 7, 1, 7);
    }
    ctx.fillStyle = COL.line; ctx.fillRect(0, RULER_H - 1, L.W, 1);
    // rows
    const rl = refLane();
    L.rows.forEach((row, n) => {
      const { y, h: rh } = row;
      const ln = row.lane;
      const isTrack = row.type === 'track';
      ctx.fillStyle = isTrack ? 'rgba(21,24,27,0.9)' : (n % 2 ? COL.lane : 'rgba(27,31,35,0.55)');
      ctx.fillRect(LABEL_W, y, L.W - LABEL_W, rh - 2);
      // label column
      ctx.fillStyle = isTrack ? '#1A1E22' : COL.label; ctx.fillRect(0, y, LABEL_W - 1, rh - 2);
      const col = isTrack ? (ROLE_COL[row.t.role] || COL.aud) : laneColor(ln);
      ctx.fillStyle = col; ctx.fillRect(isTrack ? 14 : 0, y, 3, rh - 2);
      const x0l = isTrack ? 24 : (row.multi ? 24 : 12);
      if (row.multi) {
        ctx.fillStyle = COL.muted; ctx.font = '600 10px Barlow, sans-serif';
        ctx.fillText(row.open ? '▾' : '▸', 10, y + rh / 2 - (rh >= 28 ? 7 : 1));
      }
      const name0 = isTrack ? row.t.name : ln.label;
      const name = name0.length > 17 ? name0.slice(0, 16) + '…' : name0;
      const isRef = !isTrack && rl && rl.key === ln.key && effMode() !== 'audio';
      const sub = isTrack ? (ROLE_LABEL[row.t.role] || row.t.role || '') : (isRef ? `reference · ${ln.sub}` : ln.sub);
      ctx.fillStyle = COL.paper; ctx.font = isTrack ? '500 11px Barlow, sans-serif' : '600 12px Barlow, sans-serif';
      if (rh >= 28) {
        ctx.fillText(name, x0l, y + rh / 2 - 7);
        ctx.font = '400 11px Barlow, sans-serif'; ctx.fillStyle = isRef ? COL.led : COL.muted;
        ctx.fillText(sub.length > 24 ? sub.slice(0, 23) + '…' : sub, x0l, y + rh / 2 + 7);
      } else {
        ctx.fillText(name, x0l, y + rh / 2 - 1);
        if (isTrack && sub) { const tw = ctx.measureText(name).width; ctx.fillStyle = COL.muted; ctx.font = '400 10px Barlow, sans-serif'; ctx.fillText(sub, x0l + tw + 6, y + rh / 2 - 1); }
        if (isRef) { const tw = ctx.measureText(name).width; ctx.fillStyle = COL.led; ctx.beginPath(); ctx.arc(x0l + tw + 8, y + rh / 2 - 1, 3, 0, Math.PI * 2); ctx.fill(); }
      }
      // clips
      for (const c of laneClips(ln.key)) {
        if (c.start == null) continue;
        const h = rh - (isTrack ? 6 : 10), yy = y + (isTrack ? 2 : 4);
        if (!isTrack && moved(c) && c.orig_start != null) {
          const gx0 = xOf(c.orig_start, L), gx1 = xOf(c.orig_start + c.length, L);
          ctx.setLineDash([3, 3]); ctx.strokeStyle = 'rgba(226,162,59,0.55)'; ctx.lineWidth = 1;
          ctx.strokeRect(Math.max(gx0, LABEL_W) + 0.5, yy + 0.5, Math.max(1, gx1 - Math.max(gx0, LABEL_W)), h - 1);
          ctx.setLineDash([]);
        }
        let x0 = xOf(c.start, L), x1 = xOf(c.start + c.length, L);
        if (x1 < LABEL_W || x0 > L.W) continue;
        x0 = Math.max(x0, LABEL_W);
        const hasTrack = !isTrack || row.i < (c.channels || 1);
        if (!hasTrack) continue;
        ctx.fillStyle = isTrack ? (c.include ? (ROLE_COL[row.t.role] || COL.aud) : COL.off) : clipColor(c);
        ctx.globalAlpha = c.include ? (isTrack ? 0.75 : 0.9) : 0.35;
        roundRect(x0, yy, Math.max(2, x1 - x0), h, 2); ctx.fill();
        ctx.globalAlpha = 1;
        const ov = isTrack ? null : overlay(c);
        if (ov === 'slow') {
          ctx.save(); roundRect(x0, yy, Math.max(2, x1 - x0), h, 2); ctx.clip();
          ctx.fillStyle = 'rgba(21,24,27,.55)'; ctx.fillRect(x0, yy, x1 - x0, h);
          ctx.strokeStyle = 'rgba(242,239,230,.45)'; ctx.lineWidth = 2;
          for (let sx = x0 - h; sx < x1; sx += 6) { ctx.beginPath(); ctx.moveTo(sx, yy + h); ctx.lineTo(sx + h, yy); ctx.stroke(); }
          ctx.restore();
        }
        if (ov === 'bad') {
          ctx.save(); roundRect(x0, yy, Math.max(2, x1 - x0), h, 2); ctx.clip();
          ctx.strokeStyle = 'rgba(224,97,74,.85)'; ctx.lineWidth = 3;
          for (let sx = x0 - h; sx < x1; sx += 8) { ctx.beginPath(); ctx.moveTo(sx, yy + h); ctx.lineTo(sx + h, yy); ctx.stroke(); }
          ctx.restore();
        }
        if (ov && ov !== 'slow') {
          ctx.strokeStyle = ov === 'bad' ? COL.bad : ov === 'man' ? COL.man : COL.fix; ctx.lineWidth = 2;
          roundRect(x0 + 1, yy + 1, Math.max(2, x1 - x0) - 2, h - 2, 2); ctx.stroke();
        }
        const label = isTrack ? row.t.name : c.name;
        if (x1 - x0 > 60 && h >= 12) {
          ctx.save(); ctx.beginPath(); ctx.rect(x0 + 5, yy, x1 - x0 - 10, h); ctx.clip();
          ctx.fillStyle = '#15181B'; ctx.font = `500 ${isTrack ? 10 : 11}px Barlow, sans-serif`;
          ctx.fillText(label, x0 + 6, yy + h / 2 + 0.5); ctx.restore();
        }
        if (S.sel === c.id) { ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; roundRect(x0 - 1, yy - 1, Math.max(2, x1 - x0) + 2, h + 2, 3); ctx.stroke(); }
      }
      if (!isTrack) {
        const unplaced = laneClips(ln.key).filter((c) => c.start == null).length;
        if (unplaced) {
          ctx.fillStyle = COL.fix; ctx.font = '500 11px Barlow, sans-serif';
          ctx.fillText(`${unplaced} without timecode`, LABEL_W + 8, y + rh / 2);
        }
      }
    });
    // hover line and playhead
    if (hoverX != null && hoverX > LABEL_W) {
      ctx.fillStyle = 'rgba(242,239,230,0.35)'; ctx.fillRect(Math.round(hoverX), RULER_H, 1, L.H - RULER_H);
    }
    if (S.ph != null) {
      const px = xOf(S.ph, L);
      if (px >= LABEL_W && px <= L.W) {
        ctx.fillStyle = COL.led; ctx.fillRect(Math.round(px) - 0.5, 0, 2, L.H);
        ctx.beginPath(); ctx.moveTo(px - 5, 0); ctx.lineTo(px + 6, 0); ctx.lineTo(px + 0.5, 7); ctx.closePath(); ctx.fill();
      }
    }
  }
  function roundRect(x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  function rowAt(my, L) { return L.rows.find((r) => my >= r.y && my < r.y + r.h) || null; }
  function hit(mx, my) {
    const L = layout();
    const row = rowAt(my, L);
    if (!row || mx < LABEL_W) return null;
    const s = sOf(mx, L);
    const tol = (S.view[1] - S.view[0]) / (L.W - LABEL_W) * 3;
    return laneClips(row.lane.key).find((c) => c.start != null && s >= c.start - tol && s <= c.start + c.length + tol) || null;
  }

  cv.addEventListener('mousemove', (e) => {
    if (!S.view) return;
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    hoverX = mx;
    const L = layout();
    if (mx > LABEL_W) setSlate(sOf(mx, L), 'under cursor');
    const c = hit(mx, my);
    const tip = $('tip');
    if (c) {
      const st = status(c);
      tip.innerHTML = `<b>${esc(c.name)}</b><br><span class="t">${c.tc} → ${samplesToTc(c.start + c.length)} (${c.dur})</span>` +
        (st.t ? `<br>${esc(st.t)}` : '');
      tip.hidden = false;
      const tx = Math.min(mx + 14, r.width - 370), ty = Math.min(my + 14, r.height - 70);
      tip.style.left = `${Math.max(4, tx)}px`; tip.style.top = `${Math.max(4, ty)}px`;
    } else tip.hidden = true;
    draw();
  });
  cv.addEventListener('mouseleave', () => { hoverX = null; $('tip').hidden = true; slateIdle(); draw(); });
  cv.addEventListener('click', (e) => {
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const L = layout();
    if (mx < LABEL_W) {                       // lane labels: open / close a multitrack lane
      const row = rowAt(my, L);
      if (row && row.type === 'lane' && row.multi) {
        if (S.expanded.has(row.key)) S.expanded.delete(row.key); else S.expanded.add(row.key);
        draw();
      }
      return;
    }
    const c = my > RULER_H ? hit(mx, my) : null;
    setPlayhead(sOf(mx, L));
    if (my > RULER_H) S.sel = c ? c.id : null;
    renderTable(); renderInspector(); draw();
    if (c) { const row = document.querySelector(`tr[data-id="${c.id}"]`); if (row) row.scrollIntoView({ block: 'nearest' }); }
  });
  cv.addEventListener('dblclick', (e) => {
    const r = cv.getBoundingClientRect();
    const c = hit(e.clientX - r.left, e.clientY - r.top);
    if (c) { const pad = c.length * 0.15; S.view = [c.start - pad, c.start + c.length + pad]; draw(); }
  });
  cv.addEventListener('wheel', (e) => {
    if (!S.view) return;
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    const L = layout();
    const w = S.view[1] - S.view[0];
    if (e.ctrlKey || e.metaKey || Math.abs(e.deltaY) > Math.abs(e.deltaX) && e.altKey) {
      const at = sOf(e.clientX - r.left, L);
      const k = Math.exp(e.deltaY * 0.01);
      const nw = Math.min(Math.max(w * k, SR * 5), SR * 86400);
      const f = (at - S.view[0]) / w;
      S.view = [at - f * nw, at - f * nw + nw];
    } else {
      const d = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) / (L.W - LABEL_W) * w;
      S.view = [S.view[0] + d, S.view[1] + d];
    }
    draw();
  }, { passive: false });
  window.addEventListener('resize', draw);
  function zoom(k) {
    if (!S.view) return;
    const w = S.view[1] - S.view[0];
    const at = S.ph != null && S.ph > S.view[0] && S.ph < S.view[1] ? S.ph : (S.view[0] + S.view[1]) / 2;
    const nw = Math.min(Math.max(w * k, SR * 5), SR * 86400);
    const f = (at - S.view[0]) / w;
    S.view = [at - f * nw, at - f * nw + nw];
    draw();
  }

  // ------------------------------------------------------------------ playhead, sound and picture
  let frameT = null;
  function setPlayhead(s) {
    S.ph = s;
    $('phTc').textContent = samplesToTc(s);
    if (S.playing) stopPlay();
    clearTimeout(frameT);
    frameT = setTimeout(() => showFrame(s), 120);
  }
  async function showFrame(s) {
    const r = await call('frame', Math.round(s), S.sel);
    const img = $('frame');
    if (r && r.ok) { img.src = r.img; img.classList.add('on'); $('frameNote').textContent = r.label || ''; }
    else { img.classList.remove('on'); $('frameNote').textContent = (r && r.why) || 'No picture here'; }
  }
  async function togglePlay() {
    if (S.playing) { stopPlay(); return; }
    if (S.ph == null) { const sp = span(); if (!sp) return; S.ph = sp[0]; }
    const r = await call('play', Math.round(S.ph), S.sel);
    if (!r || !r.ok) { $('phWhat').textContent = (r && r.why) || 'Nothing to play here.'; return; }
    S.ph = r.from;
    S.playing = { t0: performance.now(), from: r.from, dur: r.dur, lastFrame: 0 };
    $('playBtn').textContent = '■ Stop'; $('playBtn').classList.add('playing');
    $('phWhat').textContent = `Playing ${r.what}`;
    requestAnimationFrame(tick);
  }
  function tick(now) {
    const p = S.playing;
    if (!p) return;
    const el = (performance.now() - p.t0) / 1000;
    if (el >= p.dur) { stopPlay(true); return; }
    S.ph = p.from + el * SR;
    $('phTc').textContent = samplesToTc(S.ph);
    if (S.view && S.ph > S.view[1] - (S.view[1] - S.view[0]) * 0.05) {      // page along
      const w = S.view[1] - S.view[0]; S.view = [S.ph - w * 0.1, S.ph + w * 0.9];
    }
    if (el - p.lastFrame >= 1) { p.lastFrame = el; showFrame(S.ph); }
    draw();
    requestAnimationFrame(tick);
  }
  function stopPlay(ended) {
    if (!S.playing) return;
    S.playing = null;
    if (!ended) call('stop_play');
    $('playBtn').textContent = '▶ Play'; $('playBtn').classList.remove('playing');
    $('phWhat').textContent = ended ? 'End of the preview.' : 'Stopped.';
    showFrame(S.ph); draw();
  }
  $('playBtn').addEventListener('click', togglePlay);

  function slateIdle() {
    const sp = S.project && span();
    if (S.sel) {
      const c = S.project.clips.find((x) => x.id === S.sel);
      if (c && c.start != null) return setSlate(c.start, `${c.name.slice(0, 18)} starts`);
    }
    setSlate(sp ? sp[0] : null, sp ? 'day starts' : 'no clips yet');
  }

  // ------------------------------------------------------------------ table
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
  const SLOW_LABEL = (c) => {
    const k = c.slowmo && c.slowmo.kind;
    return k === 'timebase' ? `Slow motion · ${c.fps} fps` : k === 'in_camera' ? `Slow motion · shot ${c.capture_fps}` :
      k === 'likely' ? 'Slow motion?' : k === 'rate' ? `${c.fps} fps · own timeline` : 'Slow motion (you)';
  };
  function chips(c) {
    const f = new Set(c.flags), out = [];
    const s = c.sync || {};
    if (c.slowmo) {
      out.push(['slow', SLOW_LABEL(c)]);
      if (f.has('raw')) out.push(['info', 'RAW']);
      return out;
    }
    if (f.has('sync_failed')) out.push(['bad', 'Not placed']);
    if (f.has('sync_manual')) out.push(['man', c.manual && c.manual.how === 'waveform' ? 'Picked by you' : 'Set by you']);
    else if (f.has('sync_corrected')) out.push(['fix', `Fixed ${((c.start - c.orig_start) / SR >= 0 ? '+' : '−')}${Math.abs((c.start - c.orig_start) / SR).toFixed(3)} s`]);
    else if (f.has('sync_found')) out.push(['fix', 'Placed by waveform']);
    else if (f.has('no_tc')) out.push(['fix', 'No timecode']);
    if (f.has('zero_tc') && !moved(c)) out.push(['fix', 'TC 00:00']);
    if (f.has('tc_far') && !moved(c)) out.push(['fix', 'TC far off']);
    if (f.has('tc_overlap') && !moved(c)) out.push(['fix', 'TC jump']);
    if (f.has('duplicate')) out.push(['bad', `Duplicate`]);
    if (f.has('short_silent')) out.push(['', 'Accidental, left out']);
    if (c.ltc) out.push(['info', `LTC ch ${c.ltc.channel + 1}`]);
    if (f.has('raw')) out.push(['info', 'RAW']);
    if (c.converted) out.push(['info', c.converted.replace(' → 48 kHz WAV', '→WAV').replace(' → 48 kHz', '→48k')]);
    if (c.chapter_of && c.chapter) out.push(['info', `Chapter ${c.chapter}`]);
    if (c.after_midnight) out.push(['info', 'After midnight']);
    if (c.opatom) out.push(['info', 'P2']);
    if (f.has('no_audio')) out.push(['', 'No audio']);
    if (f.has('ubits')) out.push(['', 'Other user bits']);
    if (s.note && s.offset != null && !moved(c)) out.push(['', `${(s.offset / 48).toFixed(0)} ms off, Auto-Align fixes it`]);
    return out;
  }
  function tracksCell(c) {
    if (c.kind === 'video') return c.channels ? `<span class="trk cam">${c.channels} ch</span>` : '<span class="trk">no audio</span>';
    const roles = c.roles || [];
    const names = c.track_names || {};
    return roles.map((r, i) => `<span class="trk ${r}" title="${esc(r)}">${esc(names[i + 1] || names[String(i + 1)] || (roles.length === 1 ? r : 'ch' + (i + 1)))}</span>`).join('');
  }
  function renderTable() {
    const tb = $('clipRows');
    if (!S.project) { tb.innerHTML = ''; return; }
    const html = [];
    const byStart = (a, b) => (a.start ?? 1e18) - (b.start ?? 1e18) || a.name.localeCompare(b.name);
    for (const ln of lanesAll()) {
      const cl = laneClips(ln.key).sort(byStart);
      if (!cl.length) continue;
      const closed = S.collapsed.has(ln.key);
      const inc = cl.filter((c) => c.include).length;
      html.push(`<tr class="grp" data-grp="${esc(ln.key)}"><td colspan="6"><span class="tw">${closed ? '▸' : '▾'}</span>`
        + `<i class="sw" style="background:${laneColor(ln)}"></i><b>${esc(ln.label)}</b>${esc(ln.sub || '')}`
        + `<span class="cnt">${inc === cl.length ? `${cl.length} clip${cl.length === 1 ? '' : 's'}` : `${inc} of ${cl.length} used`}</span></td></tr>`);
      if (closed) continue;
      for (const c of cl) {
        const st = status(c);
        const cls = [c.include ? '' : 'off', S.sel === c.id ? 'sel' : ''].join(' ');
        const tc = c.tc ? (st.old && st.old !== c.tc ? `<span class="old">${st.old}</span>${c.tc}` : c.tc) : '—';
        const ch = chips(c);
        const chipHtml = ch.map(([kk, t]) => `<span class="chip s ${kk}">${esc(t)}</span>`).join('');
        const sub2 = ln.kind === 'camera' && ln.rolls.length > 1 ? `<span class="sub2">${esc(c.roll)}</span>` : '';
        html.push(`<tr class="${cls}" data-id="${c.id}">
          <td class="c-inc"><input type="checkbox" ${c.include ? 'checked' : ''} data-inc="${c.id}" aria-label="Use ${esc(c.name)}"></td>
          <td class="file" title="${esc(c.path)}">${esc(c.name)}${sub2}</td>
          <td class="num">${tc}</td><td class="num">${c.dur}</td>
          <td class="tracks">${tracksCell(c)}</td>
          <td class="status">${chipHtml || `<span class="txt">${esc(st.t)}</span>`}</td></tr>`);
      }
    }
    tb.innerHTML = html.join('');
  }
  function tableOrder() {
    const out = [];
    for (const ln of lanesAll()) {
      if (S.collapsed.has(ln.key)) continue;
      out.push(...laneClips(ln.key).sort((a, b) => (a.start ?? 1e18) - (b.start ?? 1e18) || a.name.localeCompare(b.name)));
    }
    return out;
  }
  $('clipRows').addEventListener('change', async (e) => {
    const id = e.target.dataset.inc;
    if (!id) return;
    const st = await call('set_include', +id, e.target.checked);
    if (st) applyState(st);
  });
  $('clipRows').addEventListener('click', (e) => {
    const tr = e.target.closest('tr');
    if (!tr || e.target.matches('input')) return;
    if (tr.dataset.grp) {
      const k = tr.dataset.grp;
      if (S.collapsed.has(k)) S.collapsed.delete(k); else S.collapsed.add(k);
      renderTable(); return;
    }
    { const c0 = S.project.clips.find((x) => x.id === +tr.dataset.id); if (c0 && c0.start != null) setPlayhead(c0.start); }
    S.sel = +tr.dataset.id; renderTable(); renderInspector(); slateIdle(); draw();
  });

  // ------------------------------------------------------------------ selected clip: timecode by hand + waveform
  const FIND = {};   // cid -> last waveform search result
  let finding = null;
  const selClip = () => (S.project && S.sel != null ? S.project.clips.find((x) => x.id === S.sel) : null);
  function showClipMsg(m) {
    const el = $('iMsg');
    el.textContent = m && m.text ? m.text : '';
    el.className = 'insp-msg' + (m && !m.ok ? ' err' : '');
  }
  function signedSec(s) { return `${s >= 0 ? '+' : '−'}${Math.abs(s).toFixed(3)} s`; }
  function renderInspector() {
    const c = selClip();
    $('insp').hidden = !c;
    if (!c) return;
    const busy = S.running || S.scanning;
    $('iName').textContent = c.name;
    const sub = [c.roll, c.camera, c.kind === 'video' ? (c.codec || '') : `${c.channels} ch`, c.dur].filter(Boolean);
    $('iSub').textContent = sub.join(' · ');
    const src = c.tc_source ? ` <span class="why">(${esc(c.tc_source)})</span>` : '';
    $('iFileTc').innerHTML = c.orig_tc ? `${c.orig_tc}${src}` : '<span class="why">No timecode in the file</span>';
    let now = c.tc || '—';
    if (c.start != null && c.orig_start != null && c.start !== c.orig_start) now += ` <span class="fix">${signedSec((c.start - c.orig_start) / SR)}</span>`;
    const st = status(c);
    if (st.t && moved(c)) now += ` <span class="why">${esc(st.t)}</span>`;
    $('iNowTc').innerHTML = now;
    const inp = $('iTcIn');
    if (document.activeElement !== inp || inp.dataset.cid !== String(c.id)) { inp.value = c.tc || ''; inp.dataset.cid = c.id; inp.classList.remove('err'); }
    // against: every other roll with something placed
    const prev = $('iAgainst').value;
    const others = S.project.rolls.filter((r) => r.name !== c.roll);
    $('iAgainst').innerHTML = '<option value="">Everything (best match)</option>' +
      others.map((r) => `<option value="${esc(r.name)}">${esc(r.name)}${r.camera ? ' · ' + esc(r.camera) : ''}</option>`).join('');
    if ([...$('iAgainst').options].some((o) => o.value === prev)) $('iAgainst').value = prev;
    const noAudio = c.has_audio === false;
    $('iFind').disabled = busy || noAudio || finding === c.id || !others.length;
    $('iAgainst').disabled = busy || noAudio;
    ['iSet', 'iReset', 'iStamp'].forEach((id) => { $(id).disabled = busy; });
    document.querySelectorAll('[data-nudge]').forEach((b) => { b.disabled = busy || c.start == null; });
    $('iReset').hidden = !c.manual;
    const isVid = c.kind === 'video';
    $('iSlowRow').hidden = !isVid;
    $('iSlowRow').classList.toggle('on', !!c.slowmo);
    if (isVid) {
      $('iSlowText').textContent = c.slowmo ? `${c.slowmo.why} Kept off the sync timeline; its sound isn’t used.`
        : 'On the sync timeline.';
      $('iSlow').textContent = c.slowmo ? 'Not slow motion' : 'Mark as slow motion';
      $('iSlow').disabled = busy;
    }
    if (c.slowmo) { $('iFind').disabled = true; $('iAgainst').disabled = true; }
    $('iStamp').disabled = busy || c.start == null || !S.settings.out_dir;
    $('iStamp').hidden = c.kind === 'video';
    $('iProg').hidden = finding !== c.id;
    let note = '';
    if (c.slowmo) note = 'Slow motion isn’t synced to sound, so there’s nothing to find.';
    else if (noAudio) note = 'This clip has no audio to match, so give it a timecode by hand.';
    else if (!others.length) note = 'Add another camera or recorder to match it against.';
    else if (!FIND[c.id]) note = c.orig_start == null ? 'No timecode in the file: Find searches the whole day for where its sound matches.' :
      'Find checks the whole day, including near its own timecode, and shows how sure each match is.';
    const r = FIND[c.id];
    let html = '';
    if (r && r.error) note = r.error;
    else if (r) {
      const row = (x, near) => `<li class="${near ? 'near' : ''}">
          <span class="tc">${x.tc}</span>
          <span class="chip ${x.confidence}">${x.confidence}</span>
          <span class="why">${near ? 'near its own TC · ' : ''}${x.shift_s != null ? signedSec(x.shift_s) + ' · ' : ''}match ${x.score} vs ${esc(x.rolls.join(', '))}${x.fine ? ' · fine ' + x.fine.cc : ''}</span>
          <button class="btn small" data-use="${x.start}" data-note="${esc(`match ${x.score} vs ${x.rolls.join(', ')}`)}" ${busy ? 'disabled' : ''}>Use</button></li>`;
      html = (r.candidates || []).map((x) => row(x, false)).join('');
      if (r.near && !(r.candidates || []).some((x) => Math.abs(x.start - r.near.start) < SR * 0.05)) html += row(r.near, true);
      if (!html) note = 'No match found. Try against a different roll, or type the timecode.';
      else if ((r.candidates || [])[0] && r.candidates[0].confidence === 'weak') note = 'Only weak matches. Listen before you trust one, or type the timecode.';
      else note = '';
    }
    $('iCands').innerHTML = html;
    $('iNote').textContent = note;
  }
  async function setTc() {
    const c = selClip(); if (!c) return;
    const typed = $('iTcIn').value;
    const st = await call('clip_set_tc', c.id, typed);
    const bad = st && st.clip_msg && !st.clip_msg.ok;
    if (!bad) $('iTcIn').blur();
    applyState(st);
    if (bad) { const el = $('iTcIn'); el.value = typed; el.classList.add('err'); el.focus(); el.select(); }
  }
  $('iSet').addEventListener('click', setTc);
  $('iTcIn').addEventListener('keydown', (e) => { if (e.key === 'Enter') setTc(); if (e.key === 'Escape') { e.target.blur(); renderInspector(); } });
  $('iTcIn').addEventListener('input', (e) => e.target.classList.remove('err'));
  document.querySelectorAll('[data-nudge]').forEach((b) => b.addEventListener('click', async () => {
    const c = selClip(); if (!c) return;
    const [n, d] = fpsPair(); const sec = Math.round(n / d);
    const v = b.dataset.nudge; const fr = v === 'sec' ? sec : v === '-sec' ? -sec : +v;
    applyState(await call('clip_nudge', c.id, fr));
  }));
  $('iReset').addEventListener('click', async () => { const c = selClip(); if (c) applyState(await call('clip_reset', c.id)); });
  $('iStamp').addEventListener('click', async () => { const c = selClip(); if (c) applyState(await call('clip_stamp', c.id)); });
  $('iClose').addEventListener('click', () => { S.sel = null; renderTable(); renderInspector(); draw(); });
  $('iFind').addEventListener('click', async () => {
    const c = selClip(); if (!c) return;
    finding = c.id; delete FIND[c.id];
    $('iProgFill').style.width = '0'; $('iProgText').textContent = 'Listening…';
    renderInspector();
    const ok = await call('clip_find', c.id, $('iAgainst').value || null);
    if (!ok) { finding = null; renderInspector(); }
  });
  $('iCands').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-use]'); const c = selClip();
    if (!b || !c) return;
    applyState(await call('clip_accept', c.id, +b.dataset.use, b.dataset.note || ''));
  });

  // ------------------------------------------------------------------ panel
  function renderDays() {
    const el = $('days');
    const ds = days();
    el.hidden = ds.length < 2;
    if (ds.length < 2) { S.day = null; el.innerHTML = ''; return; }
    if (S.day && !ds.some((d) => d.key === S.day)) S.day = null;
    if (!S.day) S.day = ds[0].key;
    el.innerHTML = ds.map((d) => `<button role="tab" data-day="${esc(d.key)}" aria-selected="${d.key === S.day}">` +
      `<b>${esc(d.label)}</b>${d.n} file${d.n === 1 ? '' : 's'} · ${d.rolls.length} roll${d.rolls.length === 1 ? '' : 's'}</button>`).join('');
  }
  $('days').addEventListener('click', (e) => {
    const b = e.target.closest('[data-day]'); if (!b) return;
    S.day = b.dataset.day; S.sel = null; fit();
    renderDays(); renderPanel(); renderTable(); renderInspector(); slateIdle(); draw();
  });

  // everything that needs a person, in one list: click a name to jump to the clip
  function attention() {
    const P = S.project; if (!P) return [];
    const out = [];
    for (const c of dayClips()) {
      const f = new Set(c.flags);
      if (c.slowmo) {
        if (c.include && c.slowmo.kind === 'likely') out.push(['warn', c, 'looks like slow motion: no sound, and its timecode runs into the next clip. It goes on the slow-motion timeline. If it isn’t, select it and click “Not slow motion”.']);
        continue;
      }
      if (f.has('sync_failed')) out.push(['bad', c, 'couldn’t be placed by waveform. Type its timecode or use Find.']);
      else if (c.start == null && c.include) {
        if (S.settings.waveform_check) out.push(['warn', c, 'has no timecode. The waveform check places it when you sync, or select it and use Find now.']);
        else out.push(['bad', c, 'has no timecode. Turn on “Check timecode by waveform”, or select it and use Find.']);
      }
      else if (f.has('duplicate')) out.push(['warn', c, `is the same recording as ${c.duplicate_of || 'another file'}, so it’s left out.`]);
      else if (f.has('sample_rate') && !c.converted) out.push(['bad', c, 'isn’t 48 kHz.']);
      else if (c.kind === 'video' && c.has_audio === false && c.include && !f.has('raw')) out.push(['warn', c, 'has no sound to check its timecode against.']);
      else if (f.has('raw') && !c.channels) out.push(['warn', c, 'is RAW with no proxy beside it, so there’s nothing to read. Put the proxy next to it.']);
    }
    if (multiDay()) {
      const guessed = dayClips().filter((c) => c.date_note);
      if (guessed.length === 1) out.push(['warn', guessed[0], `${guessed[0].date_note}. Check it’s on the right day.`]);
      else if (guessed.length) out.push(['warn', { name: `${guessed.length} files` }, 'had no recording date, so they were put on this day by their timecode. Check they belong here.']);
    }
    for (const u of (P.unsupported || [])) out.push(['warn', { name: (u.path || u).split('/').pop() }, u.why || 'can’t be read.']);
    for (const e of (P.errors || [])) out.push(['bad', { name: String(e[0] || '').split('/').pop() }, `couldn’t be read: ${e[1] || ''}`]);
    return out;
  }
  function renderAttention() {
    const a = attention();
    const el = $('attn');
    el.hidden = !a.length;
    if (!a.length) return;
    const bad = a.filter((x) => x[0] === 'bad').length;
    el.classList.toggle('only-warn', !bad);
    $('attnSum').innerHTML = bad ? `<b>${bad} need${bad === 1 ? 's' : ''} you</b>${a.length - bad ? `and ${a.length - bad} to know about` : ''}`
      : `<b>${a.length} to know about</b>`;
    $('attnList').innerHTML = a.slice(0, 80).map(([k, c, t]) =>
      `<li><i class="k ${k}"></i><span>${c.id != null ? `<button data-jump="${c.id}">${esc(c.name)}</button>` : `<b>${esc(c.name)}</b>`} <span class="w">${esc(t)}</span></span></li>`).join('');
    if (bad && !el.dataset.seen) { el.open = true; el.dataset.seen = '1'; }
  }
  $('attnList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-jump]'); if (!b) return;
    selectClip(+b.dataset.jump, true);
  });
  function selectClip(id, zoom) {
    const c = S.project && S.project.clips.find((x) => x.id === id);
    if (!c) return;
    S.sel = id;
    if (zoom && c.start != null) { const L = c.length || SR * 10, pad = Math.max(L * 0.6, SR * 20); S.view = [c.start - pad, c.start + L + pad]; }
    renderTable(); renderInspector(); slateIdle(); draw();
    const row = document.querySelector(`tr[data-id="${id}"]`); if (row) row.scrollIntoView({ block: 'nearest' });
  }

  function renderMode() {
    const m = S.settings.mode || 'auto', eff = effMode();
    document.querySelectorAll('#modeSeg [data-mode]').forEach((b) => {
      b.setAttribute('aria-checked', String(b.dataset.mode === m));
      b.disabled = S.running || (b.dataset.mode === 'camera' && !hasCamRef());
    });
    const P = S.project;
    let hint = '';
    if (!P || !P.clips.length) hint = 'Auto picks a camera mic when there is one, otherwise the boom.';
    else if (eff === 'camera') {
      const r = (S.settings.reference || {}).roll;
      hint = m === 'auto' ? `Camera sound found, so everything aligns to ${r || 'the camera'}.` : `Everything aligns to ${r || 'the camera'}.`;
    } else {
      const roles = new Set(dayClips().filter((c) => c.kind === 'audio' && c.include).flatMap((c) => c.roles || []));
      const ref = roles.has('boom') ? 'the boom' : roles.has('plant') ? 'the plant mic' : 'the main lav';
      hint = (m === 'audio' ? 'Audio only: ' : 'No camera sound, so audio only: ') +
        `each take aligns to ${ref}, falling back to the next mic when it drops out. Mix and timecode tracks pass through untouched.`;
    }
    $('modeHint').textContent = hint;
    $('refRow').hidden = eff !== 'camera';
    $('refNote').hidden = eff !== 'camera';
  }
  document.querySelectorAll('#modeSeg [data-mode]').forEach((b) => b.addEventListener('click', async () => {
    if (b.disabled) return;
    applyState(await call('set_setting', 'mode', b.dataset.mode));
  }));

  // preflight: refreshed a moment after anything changes
  let checkT = null;
  function queueChecks() {
    clearTimeout(checkT);
    checkT = setTimeout(async () => {
      if (!S.project || !S.project.clips.length || S.running) { $('checks').hidden = true; return; }
      const r = await call('preflight');
      S.checks = Array.isArray(r) ? r : [];
      renderChecks();
    }, 400);
  }
  function renderChecks() {
    const ck = S.checks.filter((c) => c.status !== 'skip' || c.id === 'space');
    const el = $('checks');
    el.hidden = !ck.length || S.running || !!S.done;
    const order = { fail: 0, warn: 1, skip: 2, ok: 3 };
    $('checkList').innerHTML = ck.slice().sort((a, b) => order[a.status] - order[b.status]).map((c) =>
      `<li><i class="${esc(c.status)}"></i><b>${esc(c.label)}</b>${c.detail ? `<span>${esc(c.detail)}</span>` : ''}</li>`).join('');
  }

  function renderFps() {
    const P = S.project, fi = P && P.fps_info;
    $('fpsBlock').hidden = !P || !P.clips.length;
    if (!fi) return;
    const auto = fi.status !== 'set';
    $('fpsSel').innerHTML = [`<option value="auto" ${auto ? 'selected' : ''}>Auto (${esc(auto ? fi.label : '23.976')})</option>`]
      .concat(fi.choices.map((c) => `<option value="${esc(c)}" ${!auto && c === fi.label ? 'selected' : ''}>${esc(c)} fps</option>`)).join('');
    $('fpsSel').disabled = S.running;
    const stEl = $('fpsState');
    stEl.className = 'fps-state st-' + fi.status;
    stEl.textContent = fi.status === 'ask' ? 'Check this' : fi.status === 'set' ? 'Set by you' : 'Checked';
    $('fpsAsk').hidden = fi.status !== 'ask';
    if (fi.status === 'ask') {
      $('fpsAskText').innerHTML = `<b>This day might not be 23.976.</b> ${esc(fi.reason)} Which is it?`;
      $('fpsUse').textContent = `Use ${fi.suggest}`;
    }
    $('fpsWhy').textContent = fi.status === 'ask' ? 'What the files say' : fi.reason;
    const kinds = { camera: 'Camera', sound: 'Sound', report: 'Sound report' };
    $('fpsList').innerHTML = fi.evidence.length ? fi.evidence.map((e) =>
      `<li class="${e.votes ? '' : 'no'}">${kinds[e.kind]} ${esc(e.name)}: <span class="r">${esc(e.label)}</span>${e.note ? ` <span class="n">(${esc(e.note)})</span>` : ''}</li>`).join('')
      : '<li>No frame rate in any file. Using 23.976.</li>';
  }
  $('fpsSel').addEventListener('change', async (e) => applyState(await call('set_fps', e.target.value)));
  $('fpsUse').addEventListener('click', async () => S.project && applyState(await call('set_fps', S.project.fps_info.suggest)));
  $('fpsKeep').addEventListener('click', async () => applyState(await call('set_fps', '23.976')));
  const fpsAsk = () => !!(S.project && S.project.fps_info && S.project.fps_info.status === 'ask');

  function renderPanel() {
    const P = S.project, st = S.settings;
    const has = P && P.clips.length;
    $('empty').hidden = !!has;
    $('loaded').hidden = !has;
    const dn = multiDay() && S.day && S.dayNames ? S.dayNames[S.day] : null;
    if (document.activeElement !== $('jobName')) $('jobName').value = (dn ? dn.job_name : st.job_name) || '';
    $('outDirText').textContent = (dn ? dn.out_dir : st.out_dir) || 'Choose where results go';
    $('outDir').title = dn ? 'Each day gets its own folder. Click to change where they go.' : 'Change the output folder';
    $('tcFps').textContent = P ? `${P.fps}${P.df ? ' DF' : ''} fps` : '';
    // reference
    const cams = P ? rolls().filter((r) => r.kind === 'camera') : [];
    const ref = st.reference || {};
    const auto = !ref.roll || ref.auto;
    $('refRoll').innerHTML = cams.length ? `<option value="auto" ${auto ? 'selected' : ''}>Best onboard mic (listen)</option>` + cams.map((r) => `<option value="${esc(r.name)}" ${!auto && r.name === ref.roll ? 'selected' : ''}>${esc(r.name)}${r.camera ? ' (' + esc(r.camera) + ')' : ''}</option>`).join('')
      : '<option>No camera yet</option>';
    $('refRoll').disabled = !cams.length;
    const nch = P ? Math.max(1, ...P.clips.filter((c) => c.roll === ref.roll).map((c) => c.channels || 1)) : 1;
    $('refCh').innerHTML = Array.from({ length: Math.max(nch, 1) }, (_, i) => `<option value="${i}" ${i === (ref.channel || 0) ? 'selected' : ''}>Ch ${i + 1}</option>`).join('');
    $('refCh').disabled = !cams.length;
    $('refCh').hidden = auto;
    $('refNote').textContent = auto ? (S.refPick ? `Picked ${S.refPick.roll} ch ${S.refPick.channel + 1}: ${S.refPick.why}.`
      : 'When you sync, every camera channel is compared with the recorder’s mics. The onboard mic that matches best wins. A channel that’s the mixer’s feed, timecode or silence is never used.')
      : 'Chosen by you.';
    renderMode();
    renderFps();
    // toggles
    document.querySelectorAll('[data-set]').forEach((el) => {
      const k = el.dataset.set; const v = st[k];
      if (el.type === 'checkbox') el.checked = el.dataset.on ? v === el.dataset.on : !!v;
      else if (v != null) el.value = v;
      const li = el.closest('.steps > li');
      if (li && el.type === 'checkbox' && li.querySelector('input') === el) li.classList.toggle('off', !el.checked);
    });
    const subs = document.querySelector('.steps .subopts');
    if (subs) subs.classList.toggle('dim', st.aap_mode !== 'resolve');     // aligned ISOs need Auto-Align
    // summary line
    if (has) {
      const cl = dayClips();
      const cams_n = cl.filter((c) => c.kind === 'video').length, aud_n = cl.length - cams_n;
      const parts = [];
      if (cams_n) parts.push(`${cams_n} camera clip${cams_n === 1 ? '' : 's'}`);
      if (aud_n) parts.push(`${aud_n} sound file${aud_n === 1 ? '' : 's'}`);
      $('sumLine').textContent = parts.join(', ') || 'Nothing on this day';
      const sp = span();
      const notes = [];
      if (sp) notes.push(`${samplesToTc(sp[0], false)} to ${samplesToTc(sp[1], false)}`);
      const fixed = cl.filter(moved).length;
      if (fixed) notes.push(`${fixed} timecode${fixed === 1 ? '' : 's'} fixed`);
      const slow = cl.filter((c) => c.slowmo && c.include).length;
      if (slow) notes.push(`${slow} slow motion / other rate, on their own timeline`);
      $('sumSub').textContent = notes.join(' · ');
    }
    renderAttention();
    $('viewer').hidden = !has;
    document.querySelector('#viewer .screen').hidden = !lanesAll().some((l) => l.kind === 'camera');
    const gb = $('goBtn');
    const ds = days();
    const dayLabel = multiDay() && S.day ? (ds.find((d) => d.key === S.day) || {}).label : '';
    gb.disabled = !has || S.scanning || (!S.running && fpsAsk());
    gb.textContent = S.running ? 'Stop' : fpsAsk() ? 'Confirm the frame rate first' : (dayLabel ? `Sync ${dayLabel}` : (S.done ? 'Sync the day again' : 'Sync the day'));
    gb.classList.toggle('stop', S.running);
    const ga = $('goAll');
    ga.hidden = !multiDay() || S.running;
    ga.textContent = `Sync all ${ds.length} days, one folder each`;
    ga.disabled = S.scanning || fpsAsk();
    document.body.classList.toggle('running', S.running);
    document.querySelectorAll('[data-set], #refRoll, #refCh, #jobName, #outDir, #addMore, #clearAll').forEach((el) => { el.disabled = S.running || (el.id === 'refRoll' && !cams.length); });
  }

  function applyState(st) {
    if (!st) return;
    if (st.clip_msg !== undefined) showClipMsg(st.clip_msg);
    const hadClips = S.project && S.project.clips.length;
    S.project = st.project; S.settings = st.settings || {}; S.running = !!st.running; S.scanning = !!st.scanning;
    S.dayNames = st.day_names || {};
    if (st.app) { S.app = st.app; document.title = st.app.name; $('appName').textContent = st.app.name; $('appVer').textContent = st.app.version ? ` ${st.app.version}` : ''; }
    renderDays();
    if (!hadClips || !S.view) fit();
    renderPanel(); renderTable(); renderInspector(); slateIdle(); draw();
    queueChecks();
  }

  // inputs
  document.querySelectorAll('[data-set]').forEach((el) => el.addEventListener('change', async () => {
    const k = el.dataset.set;
    const v = el.type === 'checkbox' ? (el.dataset.on ? (el.checked ? el.dataset.on : el.dataset.off) : el.checked) : el.value;
    applyState(await call('set_setting', k, v));
  }));
  $('jobName').addEventListener('change', async (e) => applyState(await call('set_setting', 'job_name', e.target.value)));
  $('refRoll').addEventListener('change', async (e) => { S.refPick = null; applyState(await call('set_reference', e.target.value, 0)); });
  $('refCh').addEventListener('change', async (e) => applyState(await call('set_reference', $('refRoll').value, +e.target.value)));
  $('outDir').addEventListener('click', async () => applyState(await call('choose_output')));
  $('chooseFolders').addEventListener('click', () => call('choose_folders'));
  $('chooseFiles').addEventListener('click', () => call('choose_files'));
  $('addMore').addEventListener('click', () => call('choose_folders'));
  $('fitDay').addEventListener('click', () => { fit(); draw(); });
  $('clearAll').addEventListener('click', async () => { S.done = null; resetRun(); applyState(await call('clear')); });
  async function go(which) {
    if (S.running) { await call('cancel'); return; }
    if (S.scanning || !S.project || !S.project.clips.length) return;
    resetRun();
    const ok = await call('run', which);
    if (ok) { S.running = true; S.done = null; $('checks').hidden = true; renderPanel(); $('progress').hidden = false; }
    else { $('msg').className = 'msg err'; $('msg').textContent = 'Nothing to sync on that day. Tick at least one clip.'; }
  }
  $('goBtn').addEventListener('click', () => go(S.running ? null : (multiDay() ? S.day : null)));
  $('goAll').addEventListener('click', () => go('all'));
  $('showOut').addEventListener('click', () => S.done && call('open_path', S.done.out));
  $('openReport').addEventListener('click', () => S.done && call('open_path', S.done.report));
  $('openReadme').addEventListener('click', () => S.done && call('open_path', `${S.done.out}/READ ME.txt`));
  $('aapContinue').addEventListener('click', () => call('aap_continue'));
  $('aapPress').addEventListener('click', () => { $('aapLead').textContent = 'Opening the Auto-Align Post panel and pressing Render…'; call('aap_press_again'); });
  $('copyDir').addEventListener('click', () => { call('copy_text', $('aapDir').dataset.full || $('aapDir').textContent); $('copyDir').textContent = 'Copied'; setTimeout(() => { $('copyDir').textContent = 'Copy'; }, 1500); });

  // drag highlight (the drop itself is handled natively so we get real paths)
  let dragDepth = 0;
  document.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth++; document.body.classList.add('dragging'); });
  document.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) document.body.classList.remove('dragging'); });
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', (e) => { e.preventDefault(); dragDepth = 0; document.body.classList.remove('dragging'); });

  // ------------------------------------------------------------------ run state
  function resetSteps() { document.querySelectorAll('#steps li').forEach((li) => li.classList.remove('active', 'done')); }
  function resetRun() {
    resetSteps();
    $('review').hidden = true; $('camFix').hidden = true;
    $('progFill').style.width = '0'; $('progText').textContent = '';
    $('doneRow').hidden = true; $('aapCard').hidden = true;
    $('msg').textContent = ''; $('msg').className = 'msg';
  }
  function markStep(name) {
    const target = STEP_MAP[name] || name;
    let seen = false;
    const lis = [...document.querySelectorAll('#steps li')];
    const idx = lis.findIndex((li) => li.dataset.step === target);
    if (idx < 0) return;
    lis.forEach((li, i) => {
      li.classList.toggle('done', i < idx);
      li.classList.toggle('active', i === idx);
    });
    void seen;
  }
  function log(line) {
    const el = $('log');
    el.textContent += line + '\n';
    if (el.textContent.length > 60000) el.textContent = el.textContent.slice(-50000);
    el.scrollTop = el.scrollHeight;
  }

  const handlers = {
    state: (d) => applyState(d),
    project: (d) => { S.project = d; renderPanel(); renderTable(); renderInspector(); draw(); },
    scan: (d) => {
      S.scanning = !!d.active;
      $('scanBar').hidden = !d.active;
      if (d.active) {
        $('scanFill').style.width = d.total ? `${(100 * d.done / d.total).toFixed(1)}%` : '8%';
        $('scanText').textContent = d.total ? `Reading ${d.done} of ${d.total}: ${d.name}` : 'Looking for media…';
      }
      renderPanel();
    },
    log: (line) => log(line),
    step: (name) => { markStep(name); $('progText').textContent = name; $('progFill').style.width = '0'; },
    progress: (d) => { $('progFill').style.width = `${Math.min(100, 100 * (d.frac || 0)).toFixed(1)}%`; if (d.msg) $('progText').textContent = d.msg; },
    aap_wait: (d) => {
      $('aapCard').hidden = false;
      $('aapDir').textContent = d.render_dir.split('/').slice(-2).join('/');
      $('aapDir').title = d.render_dir;
      $('aapDir').dataset.full = d.render_dir;
      $('aapRef').textContent = d.ref_track || 'Audio 1';
      $('aapSrc').textContent = d.sources || '';
      $('aapSet').hidden = !!d.error;
      $('aapPress').hidden = !!d.error || S.platform !== 'darwin' && false;
      if (d.error) {
        $('aapTitle').textContent = 'Resolve didn’t respond';
        $('aapLead').textContent = `${d.error} You can open Resolve and run Sync the day again, or continue without alignment.`;
        $('aapContinue').textContent = 'Continue without alignment';
      } else if (d.pressed) {
        $('aapTitle').textContent = 'Auto-Align Post is rendering';
        $('aapLead').textContent = `Resolve project ${d.project} is open and Auto-Align is rendering. ${APP()} picks up each file as it lands.`;
        $('aapContinue').textContent = 'Continue with what’s rendered';
      } else {
        $('aapTitle').textContent = 'Press Render in Auto-Align Post';
        $('aapLead').textContent = `Resolve project ${d.project || ''} is ready. In Resolve, open Workspace › Workflow Integrations › Auto-Align Post, check these settings, then press Render. ${APP()} picks up the files as they appear.`;
        $('aapContinue').textContent = 'Continue with what’s rendered';
      }
      handlers.aap_status({ done: 0, want: d.want || 0 });
    },
    aap_status: (d) => {
      $('aapFill').style.width = d.want ? `${(100 * d.done / d.want).toFixed(1)}%` : '0';
      $('aapCount').textContent = `${d.done} of ${d.want} pieces rendered`;
    },
    aap_pressed: (d) => {
      $('aapLead').textContent = d.ok ? 'Render pressed. Auto-Align is working; files are picked up as they land.'
        : 'Couldn’t find the Render button. Press it in the Auto-Align Post panel, files are picked up as they land.';
    },
    aap_done: () => { $('aapCard').hidden = true; },
    clip_progress: (d) => {
      $('iProgFill').style.width = `${Math.min(100, 100 * (d.frac || 0)).toFixed(1)}%`;
      if (d.msg) $('iProgText').textContent = d.msg;
    },
    clip_find: (d) => { FIND[d.cid] = d; finding = null; if (d.trace) log(d.trace); renderInspector(); },
    done: (d) => {
      S.running = false; S.done = d;
      document.querySelectorAll('#steps li').forEach((li) => { li.classList.remove('active'); if (!li.classList.contains('off')) li.classList.add('done'); });
      $('progFill').style.width = '100%'; $('progText').textContent = 'Finished';
      $('doneRow').hidden = false;
      const all = d.all || [d];
      const cf = all.flatMap((x) => x.camera_fixes || []);
      $('camFix').hidden = !cf.length;
      if (cf.length) {
        $('camLead').textContent = `The timelines already place ${cf.length === 1 ? 'this clip' : 'these clips'} at the fixed timecode. Want copies that carry it too? `
          + 'Each copy is the original file byte for byte with only the timecode number changed: same codec, same log, no re-encode. The originals stay as they are.';
        $('camList').innerHTML = cf.map((x) => `<li><b>${esc(x.roll)} · ${esc(x.name)}</b><span class="tc">${x.new}</span></li>`).join('');
      }
      $('msg').className = 'msg ok';
      $('msg').textContent = all.length > 1 ? `${all.length} days synced, each in its own folder next to ${d.out.split('/').slice(-1)[0]}.` : `Everything is in ${d.out}`;
      renderPanel();
    },
    review_tc: (d) => {
      const fx = d.fixes || [];
      $('review').hidden = false;
      $('revLead').textContent = `${fx.length} file${fx.length === 1 ? '' : 's'} ${fx.length === 1 ? 'is' : 'are'} off from ${fx.length === 1 ? 'its' : 'their'} own timecode. `
        + 'Nothing has been written yet. Click any of them in the timeline to look closer.';
      $('revList').innerHTML = fx.map((x) => `<li><b>${esc(x.roll)} · ${esc(x.name)}</b>
          <span class="tc">${x.old ? `<s>${x.old}</s>` : '<s>no TC</s>'}${x.new}</span>
          <span class="w">${x.shift_s != null ? signedSec(x.shift_s) + ' · ' : ''}${esc(x.how)}${x.against ? ' against ' + esc(x.against) : ''}${x.score != null ? ` (match ${x.score})` : ''}</span></li>`).join('');
      $('progText').textContent = 'Waiting for you: confirm the timecode fixes';
      $('review').scrollIntoView({ block: 'nearest' });
    },
    ref_pick: (d) => { S.refPick = d; renderPanel(); },
    cameras_done: (d) => {
      S.running = false; renderPanel();
      $('camFix').hidden = true;
      $('msg').className = 'msg ok';
      $('msg').textContent = d.made && d.made.length ? `${d.made.length} corrected camera cop${d.made.length === 1 ? 'y' : 'ies'} in CAMERA_TC_CORRECTED. The XML now points at them.` : 'No camera copies written.';
    },
    day: (d) => { log(`══ Day ${d.i} of ${d.n}: ${d.label}`); $('progText').textContent = `Day ${d.i} of ${d.n}: ${d.label}`; resetSteps(); },
    notice: (d) => { $('msg').className = 'msg' + (d.ok === false ? ' err' : ''); $('msg').textContent = d.text || ''; },
    stopped: () => { S.running = false; $('msg').textContent = 'Stopped. Nothing you already have was changed.'; renderPanel(); },
    error: (d) => { S.running = false; $('msg').className = 'msg err'; $('msg').textContent = d.message; renderPanel(); log(d.trace || ''); },
  };

  async function decide(choice) { $('review').hidden = true; await call('review_tc', choice); }
  $('revWrite').addEventListener('click', () => decide('write'));
  $('revSkip').addEventListener('click', () => decide('skip'));
  $('revStop').addEventListener('click', () => decide('stop'));
  $('camWrite').addEventListener('click', async () => {
    const ok = await call('rewrite_cameras');
    if (ok) { $('camFix').hidden = true; S.running = true; renderPanel(); $('progress').hidden = false; }
  });
  $('camNo').addEventListener('click', () => { $('camFix').hidden = true; });
  $('iSlow').addEventListener('click', async () => {
    const c = selClip(); if (!c) return;
    applyState(await call('clip_slowmo', c.id, !c.slowmo));
  });
  window.MS = { event: (kind, data) => { const h = handlers[kind]; if (h) h(data); } };

  // ------------------------------------------------------------------ keyboard
  document.addEventListener('keydown', async (e) => {
    const typing = e.target.matches('input[type=text], textarea, select');
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === 'o') { e.preventDefault(); if (!S.running) call('choose_folders'); return; }
    if (mod && e.key === 'Enter') { e.preventDefault(); if (!S.running) go(multiDay() ? S.day : null); return; }
    if (typing) { if (e.key === 'Escape') e.target.blur(); return; }
    if (e.key === 'Escape') { if (S.playing) { stopPlay(); return; } if (S.sel != null) { S.sel = null; renderTable(); renderInspector(); slateIdle(); draw(); } return; }
    if (!S.project || !S.project.clips.length) return;
    if (e.key === '+' || e.key === '=') { e.preventDefault(); zoom(0.5); return; }
    if (e.key === '-' || e.key === '_') { e.preventDefault(); zoom(2); return; }
    if (e.key === ' ') { e.preventDefault(); togglePlay(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const list = tableOrder();
      if (!list.length) return;
      let i = list.findIndex((c) => c.id === S.sel);
      i = i < 0 ? 0 : Math.min(list.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)));
      selectClip(list[i].id, false);
      return;
    }
    const c = selClip();
    if (!c || S.running) return;
    if (e.key === 'Backspace' || e.key === 'Delete') {
      e.preventDefault();
      applyState(await call('set_include', c.id, !c.include));
    }
  });

  async function boot() {
    const st = await call('get_state');
    if (st) { S.platform = st.platform; applyState(st); }
    else { renderPanel(); slateIdle(); }
  }
  if (api()) boot(); else window.addEventListener('pywebviewready', boot);
  slateIdle();
})();
