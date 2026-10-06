/* RAT — dependency-free SVG chart renderer.
 *
 * Draws the dashboard time series without any external library or network
 * request: a mirrored added/removed bar chart (added up, removed down, one
 * shared scale) plus an optional line panel above it (commits or
 * modifications).  Hovering shows a tooltip with the bucket's numbers.
 *
 * Usage:  RATCharts.timeSeries(containerEl, points, {lineKey: 'commits'})
 */
'use strict';
window.RATCharts = (() => {
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
  const num = (n) => Math.round(n).toLocaleString('en-US');

  function compact(n) {
    n = Math.round(n);
    const abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (abs >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  /* Merge adjacent buckets until at most `max` remain (keeps large histories
     readable: e.g. 20 years of daily buckets become ~160 bars). */
  function coarsen(points, max) {
    if (points.length <= max) return points;
    const step = Math.ceil(points.length / max);
    const out = [];
    for (let i = 0; i < points.length; i += step) {
      const acc = {
        bucket: points[i].bucket, added: 0, removed: 0, growth: 0,
        churn: 0, commits: 0, modifications: 0,
      };
      for (let j = i; j < Math.min(i + step, points.length); j++) {
        const p = points[j];
        acc.added += p.added;
        acc.removed += p.removed;
        acc.churn += p.churn;
        acc.commits += p.commits || 0;
        acc.modifications += p.modifications || 0;
      }
      acc.growth = acc.added - acc.removed;
      out.push(acc);
    }
    return out;
  }

  function shortLabel(bucket, first) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(bucket)) {
      const january = bucket.slice(5, 7) === '01';
      return (first || january) ? bucket.slice(2, 7) : bucket.slice(5);
    }
    return bucket;
  }

  function timeSeries(container, rawPoints, opts = {}) {
    const lineKey = opts.lineKey || null;
    const lineLabel = opts.lineLabel || (lineKey === 'commits' ? 'Commits' : 'Modifications');
    const points = coarsen((rawPoints || []).slice(), 160);
    container.innerHTML = '';

    if (!points.length) {
      container.innerHTML = '<div class="chart-empty">No activity in the selected range.</div>';
      return;
    }

    const W = Math.max(container.clientWidth || 640, 320);
    const M = { l: 58, r: 14, t: 12, b: 30 };
    const innerW = W - M.l - M.r;
    const lineH = lineKey ? 84 : 0;
    const gap = lineKey ? 16 : 0;
    const barsH = 170;
    const H = M.t + lineH + gap + barsH + M.b;
    const barsTop = M.t + lineH + gap;
    const half = barsH / 2;
    const zeroY = barsTop + half;
    const step = innerW / points.length;
    const x = (i) => M.l + step * i + step / 2;

    const maxBar = Math.max(1, ...points.map((p) => Math.max(p.added, p.removed)));

    const bars = points.map((p, i) => {
      const cx = x(i);
      const w = Math.max(1.2, Math.min(step * 0.38, 26));
      let out = '';
      if (p.added > 0) {
        const hA = Math.max((p.added / maxBar) * half, 1);
        out += `<rect class="bar-add" x="${(cx - w - 0.5).toFixed(1)}" y="${(zeroY - hA).toFixed(1)}" width="${w.toFixed(1)}" height="${hA.toFixed(1)}" rx="1"></rect>`;
      }
      if (p.removed > 0) {
        const hR = Math.max((p.removed / maxBar) * half, 1);
        out += `<rect class="bar-del" x="${(cx + 0.5).toFixed(1)}" y="${zeroY.toFixed(1)}" width="${w.toFixed(1)}" height="${hR.toFixed(1)}" rx="1"></rect>`;
      }
      return out;
    }).join('');

    let lineSvg = '';
    if (lineKey) {
      const vals = points.map((p) => p[lineKey] || 0);
      const lineMax = Math.max(1, ...vals);
      const ly = (v) => M.t + lineH - 4 - (v / lineMax) * (lineH - 10);
      const path = vals.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${ly(v).toFixed(1)}`).join(' ');
      const base = M.t + lineH;
      lineSvg =
        `<path class="area-line" d="${path} L${x(points.length - 1).toFixed(1)},${base} L${x(0).toFixed(1)},${base} Z"></path>` +
        `<path class="line-path" d="${path}"></path>` +
        `<text class="axis" x="${M.l - 8}" y="${M.t + 9}" text-anchor="end">${compact(lineMax)}</text>` +
        `<text class="axis" x="${M.l - 8}" y="${base}" text-anchor="end">0</text>` +
        `<line class="axis-line" x1="${M.l}" y1="${base}" x2="${W - M.r}" y2="${base}"></line>`;
    }

    const tickEvery = Math.max(1, Math.ceil(points.length / 8));
    let ticks = '';
    for (let i = 0; i < points.length; i += tickEvery) {
      ticks += `<text class="axis" x="${x(i).toFixed(1)}" y="${H - 9}" text-anchor="middle">${esc(shortLabel(points[i].bucket, i === 0))}</text>`;
    }

    const svg =
      `<svg class="chart-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img">` +
      lineSvg +
      `<line class="axis-line" x1="${M.l}" y1="${barsTop}" x2="${W - M.r}" y2="${barsTop}"></line>` +
      `<line class="axis-line" x1="${M.l}" y1="${barsTop + barsH}" x2="${W - M.r}" y2="${barsTop + barsH}"></line>` +
      `<line class="axis-line zero" x1="${M.l}" y1="${zeroY}" x2="${W - M.r}" y2="${zeroY}"></line>` +
      `<text class="axis" x="${M.l - 8}" y="${barsTop + 10}" text-anchor="end">+${compact(maxBar)}</text>` +
      `<text class="axis" x="${M.l - 8}" y="${barsTop + barsH - 2}" text-anchor="end">−${compact(maxBar)}</text>` +
      `<text class="axis" x="${M.l - 8}" y="${zeroY + 4}" text-anchor="end">0</text>` +
      bars + ticks +
      `<line class="guide" x1="0" y1="${M.t}" x2="0" y2="${barsTop + barsH}" style="display:none"></line>` +
      `</svg>`;

    const legend = document.createElement('div');
    legend.className = 'chart-legend';
    legend.innerHTML =
      `<span><i style="background:var(--green)"></i>Added</span>` +
      `<span><i style="background:var(--red)"></i>Removed</span>` +
      (lineKey ? `<span><i class="line" style="background:var(--blue)"></i>${esc(lineLabel)}</span>` : '');

    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    wrap.innerHTML = svg;
    const tip = document.createElement('div');
    tip.className = 'chart-tip';
    tip.hidden = true;
    wrap.appendChild(tip);

    container.appendChild(legend);
    container.appendChild(wrap);

    const guide = wrap.querySelector('.guide');
    wrap.addEventListener('mousemove', (ev) => {
      const rect = wrap.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const i = Math.floor((mx - M.l) / step);
      if (i < 0 || i >= points.length) {
        tip.hidden = true;
        guide.style.display = 'none';
        return;
      }
      const p = points[i];
      guide.style.display = '';
      guide.setAttribute('x1', String(x(i)));
      guide.setAttribute('x2', String(x(i)));
      const rows = [`<div class="tip-title">${esc(p.bucket)}</div>`];
      if (lineKey) rows.push(`<div><span>${esc(lineLabel)}</span><b>${num(p[lineKey] || 0)}</b></div>`);
      rows.push(`<div><span>Added</span><b class="pos">+${num(p.added)}</b></div>`);
      rows.push(`<div><span>Removed</span><b class="neg">−${num(p.removed)}</b></div>`);
      rows.push(`<div><span>Net</span><b>${p.growth >= 0 ? '+' : '−'}${num(Math.abs(p.growth))}</b></div>`);
      if (lineKey !== 'modifications') {
        rows.push(`<div><span>Modifications</span><b>${num(p.modifications || 0)}</b></div>`);
      }
      tip.innerHTML = rows.join('');
      tip.hidden = false;
      const tw = tip.offsetWidth;
      let left = mx + 14;
      if (left + tw > rect.width) left = mx - tw - 14;
      tip.style.left = Math.max(0, left) + 'px';
      tip.style.top = Math.max(0, ev.clientY - rect.top - 12) + 'px';
    });
    wrap.addEventListener('mouseleave', () => {
      tip.hidden = true;
      guide.style.display = 'none';
    });

    if (container._ratResize) window.removeEventListener('resize', container._ratResize);
    container._ratResize = debounce(() => timeSeries(container, rawPoints, opts), 150);
    window.addEventListener('resize', container._ratResize);
  }

  return { timeSeries };
})();
