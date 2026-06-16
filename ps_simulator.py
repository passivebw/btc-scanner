#!/usr/bin/env python3
"""
PS Simulator — runs PS's exact formula on Kraken data every 60s, saves to ps_data.

Why not browser scraping: Binance.com is HTTP 451 on DigitalOcean IPs, so
Playwright would fall back to CoinGecko 5-min candles — wrong resolution for
comparison. Running the PS formula here on the same Kraken 1-min data as the
scanner gives a clean apples-to-apples formula diff.
"""

import time
import sqlite3
import requests
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/root/ps_sim.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

DB_PATH  = '/root/trades.db'
INTERVAL = 60

# PS $he(minutesRemaining, gapPct) — exact port from bundle
def calc_time_mult(mins, gap_pct):
    if   mins <= 2:  r = 1.8
    elif mins <= 5:  r = 1.5
    elif mins <= 10: r = 1.2
    elif mins <= 15: r = 1.0
    else:            r = 0.85
    if   gap_pct < 0.05:               r *= 0.6
    elif gap_pct > 0.5 and mins <= 5:  r *= 1.3
    return r


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS ps_data (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_utc TEXT NOT NULL,
        source        TEXT NOT NULL DEFAULT 'simulator',
        btc_price     REAL,
        threshold     REAL,
        ticker        TEXT,
        mins_left     INTEGER,
        up_pct        REAL,
        kalshi_yes    REAL,
        edge          REAL,
        signal        TEXT,
        price_gap     REAL,
        ma_struct     REAL,
        rsi_score     REAL,
        macd_score    REAL,
        sr_score      REAL,
        mom_score     REAL,
        total_raw     REAL,
        rsi9          REAL,
        macd_val      REAL,
        ma5           REAL,
        ema21         REAL
    )''')
    conn.commit()
    conn.close()
    log.info('ps_data table ready')


def save_row(data: dict):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute('''INSERT INTO ps_data
            (timestamp_utc,source,btc_price,threshold,ticker,mins_left,up_pct,
             kalshi_yes,edge,signal,price_gap,ma_struct,rsi_score,macd_score,
             sr_score,mom_score,total_raw,rsi9,macd_val,ma5,ema21)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            data.get('timestamp_utc'),
            data.get('source', 'simulator'),
            data.get('btc_price'),
            data.get('threshold'),
            data.get('ticker'),
            data.get('mins_left'),
            data.get('up_pct'),
            data.get('kalshi_yes'),
            data.get('edge'),
            data.get('signal'),
            data.get('price_gap'),
            data.get('ma_struct'),
            data.get('rsi_score'),
            data.get('macd_score'),
            data.get('sr_score'),
            data.get('mom_score'),
            data.get('total_raw'),
            data.get('rsi9'),
            data.get('macd_val'),
            data.get('ma5'),
            data.get('ema21'),
        ))
        conn.commit()
        log.info('Saved: BTC=%.2f RSI=%.1f MACD=%.2f UP%%=%.1f total=%.3f sig=%s mins=%d',
                 data.get('btc_price', 0), data.get('rsi9', 0), data.get('macd_val', 0),
                 data.get('up_pct', 50), data.get('total_raw', 0),
                 data.get('signal', '?'), data.get('mins_left', 0))
    except Exception as e:
        log.error('DB error: %s', e)
    finally:
        conn.close()


# --- Indicator math (matches PS bundle exactly) ---

def calc_ema(d, p):
    k = 2 / (p + 1)
    e = sum(d[:p]) / p
    for i in range(p, len(d)):
        e = d[i] * k + e * (1 - k)
    return e


def calc_rsi(c, p):
    # PS She(): simple RSI — last p diffs only, no Wilder smoothing
    diffs = [c[i] - c[i-1] for i in range(len(c)-p, len(c))]
    ag = sum(d for d in diffs if d > 0) / p
    al = sum(-d for d in diffs if d < 0) / p
    return 100 if al == 0 else 100 - (100 / (1 + ag / al))


def calc_macd(c):
    # PS: MACD(5,13,6) — build full signal history from bhe("1m",100) candles
    history = []
    for i in range(13, len(c) + 1):
        sub = c[:i]
        history.append(calc_ema(sub, 5) - calc_ema(sub, 13))
    val = calc_ema(c, 5) - calc_ema(c, 13)
    sig = calc_ema(history, 6) if len(history) >= 6 else val
    return val, sig


def calc_mom(closes, opens):
    # PS Mhe(): weighted average of last 10 candle directions, most recent = highest weight
    pairs = list(zip(closes[-10:], opens[-10:]))
    dirs  = [1 if c >= o else -1 for c, o in pairs]
    dirs.reverse()
    total  = sum(d * (i + 1) for i, d in enumerate(dirs))
    weight = sum(range(1, len(dirs) + 1))
    return (total / weight) * 0.08 if weight else 0


def calc_sr(highs, lows, price, thresh):
    # PS khe()+Ohe(): $50 grid levels with rejection counts
    if len(highs) < 20:
        return 0
    GRID = 50; PROX = 0.003; counts = {}
    for h in highs:
        lvl = round(h / GRID) * GRID; counts[lvl] = counts.get(lvl, 0) + 1
    for l in lows:
        lvl = round(l / GRID) * GRID; counts[lvl] = counts.get(lvl, 0) + 1
    levels     = sorted((lvl, cnt) for lvl, cnt in counts.items() if cnt >= 2)
    support    = [lvl for lvl, _ in levels if lvl < price][-5:]   # khe splits by currentPrice
    resistance = [lvl for lvl, _ in levels if lvl > price][:5]
    s = 0
    for a in resistance:
        w = min(counts[a] * 0.2, 0.8)
        if abs(thresh - a) / a < PROX:  s -= 0.6 * w
        elif thresh > a > price:         s -= 0.4 * w
        elif a > thresh:                 s -= 0.1 * w
    for a in support:
        w = min(counts[a] * 0.2, 0.8)
        if price > a > thresh:              s += 0.5 * w
        elif abs(price - a) / a < PROX:    s += 0.3 * w
    return max(-1.0, min(1.0, s)) * 0.17


# --- Data fetching ---

def fetch_ohlc():
    # End at last completed 1-minute candle (no partial candles)
    now_ms = int(time.time() * 1000)
    end_time = (now_ms // 60_000) * 60_000 - 1
    r = requests.get(
        f'https://data-api.binance.vision/api/v3/klines'
        f'?symbol=BTCUSDT&interval=1m&limit=100&endTime={end_time}',
        timeout=10
    )
    r.raise_for_status()
    ohlc = r.json()
    return (
        [float(x[4]) for x in ohlc],  # closes
        [float(x[1]) for x in ohlc],  # opens
        [float(x[2]) for x in ohlc],  # highs
        [float(x[3]) for x in ohlc],  # lows
    )


def fetch_kalshi():
    for ticker in ['KXBTC15M', 'KXBTC']:
        try:
            r = requests.get(
                f'https://api.elections.kalshi.com/trade-api/v2/markets'
                f'?series_ticker={ticker}&limit=10&status=open',
                timeout=5
            )
            markets = r.json().get('markets', [])
            if markets:
                markets.sort(key=lambda m: m.get('close_time') or '')
                active = markets[0]
                yp = float(active.get('yes_ask_dollars') or
                            active.get('last_price_dollars') or 0.5)
                if yp > 1:
                    yp /= 100
                expiry = active.get('close_time')
                mins_left = 7
                if expiry:
                    exp_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                    mins_left = max(1, round(
                        (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
                    ))
                return {
                    'threshold':  active.get('floor_strike') or active.get('cap_strike'),
                    'ticker':     active.get('ticker'),
                    'kalshi_yes': yp,
                    'mins_left':  mins_left,
                    'expiry':     expiry,
                }
        except Exception:
            continue
    return {}


# --- PS formula scoring ---

def score(closes, opens, highs, lows, kalshi):
    price = closes[-1]
    ma5   = sum(closes[-5:]) / 5
    ema21 = calc_ema(closes, 21)
    rsi9  = calc_rsi(closes, 9)
    macd_val, macd_sig = calc_macd(closes)

    thresh    = kalshi.get('threshold') or price
    mins_left = kalshi.get('mins_left', 7)

    # Price Gap (Che)
    pg_norm   = 0.00015 * mins_left * price
    price_gap = max(-1, min(1, (price - thresh) / pg_norm)) * 0.25 if pg_norm > 0 else 0

    # MA Structure (Nhe) — 4-component
    _i = 0
    _i += 0.25 if price > ma5   else -0.25
    _i += 0.25 if price > ema21 else -0.25
    _i += 0.25 if ma5   > ema21 else -0.25
    if price > thresh:
        if ma5 < thresh and ema21 < thresh:    _i += 0.25
        elif ma5 > thresh or ema21 > thresh:   _i -= 0.15
    else:
        if ma5 > thresh and ema21 > thresh:    _i -= 0.25
        elif ma5 < price and ema21 < price:    _i += 0.15
    ma_struct = _i * 0.20

    # RSI Score (jhe) — continuous curve with time dampening
    if rsi9 > 70:
        _r = -0.5 - (rsi9 - 70) / 30 * 0.5
        if mins_left <= 5: _r *= 0.5
    elif rsi9 < 30:
        _r = 0.5 + (30 - rsi9) / 30 * 0.5
        if mins_left <= 3:   _r *= 0.3
        elif mins_left <= 7: _r *= 0.6
    elif rsi9 > 50:
        _r = (rsi9 - 50) / 50 * 0.5
    else:
        _r = -((50 - rsi9) / 50 * 0.5)
    rsi_score = _r * 0.17

    # MACD Score (Phe)
    _hist = macd_val - macd_sig
    _n    = 0.5 if macd_val > macd_sig else -0.5
    _norm = price * 5e-4
    _mag  = min(abs(_hist) / _norm, 1.0) if _norm > 0 else 0
    _n   += _mag * 0.5 if _hist > 0 else -_mag * 0.5
    macd_score = _n * 0.13

    sr        = calc_sr(highs, lows, price, thresh)
    mom_score = calc_mom(closes, opens)

    total   = price_gap + ma_struct + rsi_score + macd_score + sr + mom_score
    gap_pct = abs(price - thresh) / price * 100
    mult    = calc_time_mult(mins_left, gap_pct)
    up_pct  = max(3.0, min(97.0, 50.0 + total * mult * 45))

    yes_price = kalshi.get('kalshi_yes', 0.50)
    edge = (up_pct / 100) - yes_price

    if mins_left <= 2:
        signal = 'TOO_CLOSE'
    elif abs(edge) < 0.12:
        signal = 'NO_TRADE'
    elif edge > 0:
        signal = 'YES_ABOVE'
    else:
        signal = 'NO_BELOW'

    return {
        'btc_price':  price,
        'ma5':        ma5,
        'ema21':      ema21,
        'rsi9':       rsi9,
        'macd_val':   macd_val,
        'threshold':  thresh,
        'ticker':     kalshi.get('ticker'),
        'mins_left':  mins_left,
        'kalshi_yes': yes_price,
        'up_pct':     up_pct,
        'edge':       edge,
        'signal':     signal,
        'price_gap':  price_gap,
        'ma_struct':  ma_struct,
        'rsi_score':  rsi_score,
        'macd_score': macd_score,
        'sr_score':   sr,
        'mom_score':  mom_score,
        'total_raw':  total,
    }


def main():
    log.info('PS Simulator starting')
    init_db()

    while True:
        try:
            closes, opens, highs, lows = fetch_ohlc()
            kalshi = fetch_kalshi()
            row = score(closes, opens, highs, lows, kalshi)
            row['timestamp_utc'] = datetime.now(timezone.utc).isoformat()
            row['source'] = 'simulator'
            save_row(row)
        except Exception as e:
            log.error('Scan error: %s', e)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
