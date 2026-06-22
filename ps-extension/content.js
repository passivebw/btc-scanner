(function () {
  'use strict';

  const GIST_URL   = 'https://gist.githubusercontent.com/passivebw/8060a0bd99f0b8b3cbf2125ab7446b84/raw/config.json';
  const LS_KEY     = 'ps_data';
  const MAX_POINTS = 1000;
  const INTERVAL   = 60_000;   // ms between collections
  const FREEZE_MS  = 5 * 60_000; // reload if upPct unchanged for 5 min

  let tunnelUrl    = null;
  let lastPoint    = null;
  let lastChangeAt = Date.now();

  // ── Gist config ─────────────────────────────────────────────────────────────
  async function loadTunnel() {
    try {
      const r = await fetch(`${GIST_URL}?_t=${Date.now()}`, { cache: 'no-store' });
      const c = await r.json();
      if (c.api_base) tunnelUrl = c.api_base;
    } catch (e) {
      console.warn('[PS-ext] Gist fetch failed:', e.message);
    }
  }

  // ── DOM helpers ──────────────────────────────────────────────────────────────
  function clickText(selector, text) {
    for (const el of document.querySelectorAll(selector)) {
      if (el.textContent.trim().toLowerCase().includes(text.toLowerCase())) {
        el.click();
        return true;
      }
    }
    return false;
  }

  function clickCryptoTab() {
    return (
      clickText('button, [role="tab"], nav a, li a', 'crypto') ||
      clickText('[class*="tab"], [class*="Tab"], [class*="nav"]', 'crypto')
    );
  }

  function clickExpand() {
    for (const btn of document.querySelectorAll('button')) {
      const t = btn.textContent.trim();
      if (t === '▶' || t.includes('▶') ||
          /breakdown|score detail|expand/i.test(t)) {
        btn.click();
        return true;
      }
    }
    return false;
  }

  // ── Data extraction ──────────────────────────────────────────────────────────
  function num(s) {
    if (s == null) return null;
    const n = parseFloat(s.replace(/,/g, ''));
    return isNaN(n) ? null : n;
  }

  function extractData() {
    const text = document.body.innerText;

    function get(re) {
      const m = text.match(re);
      return m ? m[1] : null;
    }

    const btcPrice = num(get(/BTC PRICE\s*\$([\d,\.]+)/i));
    const upPct    = num(get(/↑\s*([\d\.]+)%/));
    if (btcPrice == null || upPct == null) return null;

    // Signal — most specific first
    let signal = 'UNKNOWN';
    if      (/TOO CLOSE/i.test(text))            signal = 'TOO_CLOSE';
    else if (/NO TRADE/i.test(text))             signal = 'NO_TRADE';
    else if (/YES ABOVE|BUY YES/i.test(text))    signal = 'YES_ABOVE';
    else if (/NO BELOW|BUY NO/i.test(text))      signal = 'NO_BELOW';
    else if (/\bABOVE\b/i.test(text))            signal = 'YES_ABOVE';
    else if (/\bBELOW\b/i.test(text))            signal = 'NO_BELOW';

    const kalshiRaw = num(get(/Kalshi:\s*(\d+)%/));

    return {
      timestamp_utc: new Date().toISOString(),
      source:        'browser',
      btc_price:     btcPrice,
      up_pct:        upPct,
      ma5:           num(get(/MA\(5\)\s*\$([\d,\.]+)/)),
      ema21:         num(get(/EMA\(21\)\s*\$([\d,\.]+)/)),
      rsi9:          num(get(/RSI\(9\)\s*([\d\.]+)/)),
      macd_val:      num(get(/MACD\n([-\d\.]+)/)),
      price_gap:     num(get(/Price Gap\n([-\d\.]+)/)),
      ma_struct:     num(get(/MA Structure\n([-\d\.]+)/)),
      rsi_score:     num(get(/RSI\n([-\d\.]+)/)),
      sr_score:      num(get(/Support\/Resistance\n([-\d\.]+)/)),
      mom_score:     num(get(/Momentum\n([-\d\.]+)/)),
      total_raw:     num(get(/Total:\s*([-\d\.]+)/)),
      kalshi_yes:    kalshiRaw != null ? kalshiRaw / 100 : null,
      mins_left:     num(get(/(\d+)m left/i)),
      signal,
    };
  }

  // ── Storage ──────────────────────────────────────────────────────────────────
  function saveLocal(pt) {
    try {
      const arr = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
      arr.unshift(pt);
      if (arr.length > MAX_POINTS) arr.length = MAX_POINTS;
      localStorage.setItem(LS_KEY, JSON.stringify(arr));
    } catch (e) {
      console.warn('[PS-ext] localStorage error:', e.message);
    }
  }

  async function postServer(pt) {
    if (!tunnelUrl) await loadTunnel();
    if (!tunnelUrl) return;
    try {
      await fetch(`${tunnelUrl}/ps-log?_t=${Date.now()}`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(pt),
      });
    } catch (e) {
      console.warn('[PS-ext] POST failed:', e.message);
    }
  }

  // ── Changed enough to bother saving? ─────────────────────────────────────────
  function isNew(pt) {
    if (!lastPoint) return true;
    return (
      Math.abs(pt.up_pct    - lastPoint.up_pct)    > 0.5 ||
      Math.abs(pt.btc_price - lastPoint.btc_price) > 10
    );
  }

  // ── Main tick ────────────────────────────────────────────────────────────────
  async function tick() {
    const pt = extractData();
    if (!pt) {
      console.log('[PS-ext] No data yet — page may still be loading');
      return;
    }

    // Freeze detection: same upPct for 5+ min → hard reload
    if (lastPoint && pt.up_pct === lastPoint.up_pct) {
      if (Date.now() - lastChangeAt > FREEZE_MS) {
        console.warn('[PS-ext] Page frozen 5 min — reloading');
        location.reload();
        return;
      }
    } else {
      lastChangeAt = Date.now();
    }

    if (!isNew(pt)) {
      console.log(`[PS-ext] No meaningful change — BTC $${pt.btc_price} UP ${pt.up_pct}%`);
      lastPoint = pt;
      return;
    }

    lastPoint = pt;
    saveLocal(pt);
    await postServer(pt);
    console.log(`[PS-ext] Saved — BTC $${pt.btc_price} UP ${pt.up_pct}% signal=${pt.signal} total=${pt.total_raw}`);
  }

  // ── Init ─────────────────────────────────────────────────────────────────────
  async function init() {
    console.log('[PS-ext] Starting');
    await loadTunnel();

    // Click Crypto tab; retry once after 2s if DOM not ready yet
    if (!clickCryptoTab()) {
      setTimeout(() => {
        clickCryptoTab();
        setTimeout(clickExpand, 1500);
      }, 2000);
    } else {
      setTimeout(clickExpand, 1500);
    }

    // First collection after 4s, then every 60s
    setTimeout(() => {
      tick();
      setInterval(tick, INTERVAL);
    }, 4000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
