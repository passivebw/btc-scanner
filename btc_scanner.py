#!/usr/bin/env python3
"""BTC Kalshi paper trading scanner — runs every 20s, logs to SQLite"""

import time
import sqlite3
import requests
import math
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/root/scanner.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

DB_PATH = '/root/trades.db'
SCAN_INTERVAL = 20
STAKE = 5
STRATS = {'conservative': 0.30, 'moderate': 0.20, 'aggressive': 0.12}
# PS $he(minutesRemaining, gapPct) — exact port from bundle
def calc_time_mult(mins, gap_pct):
    if   mins <= 2:  r = 1.8
    elif mins <= 5:  r = 1.5
    elif mins <= 10: r = 1.2
    elif mins <= 15: r = 1.0
    else:            r = 0.85
    if   gap_pct < 0.05:           r *= 0.6   # too close to threshold
    elif gap_pct > 0.5 and mins <= 5: r *= 1.3  # large gap, final minutes
    return r


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contract_key TEXT NOT NULL,
        strategy TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL,
        signal TEXT NOT NULL,
        direction TEXT,
        entry_price REAL,
        win_prob REAL,
        edge REAL,
        btc_price REAL,
        threshold REAL,
        expiry TEXT,
        ticker TEXT,
        contracts INTEGER,
        stake REAL,
        max_gain REAL,
        max_loss REAL,
        ev REAL,
        outcome TEXT DEFAULT 'PENDING',
        pnl REAL,
        final_btc REAL,
        UNIQUE(contract_key, strategy)
    )''')
    conn.commit()
    conn.close()
    log.info('DB initialized at %s', DB_PATH)


def calc_ema(d, p):
    k = 2 / (p + 1)
    e = sum(d[:p]) / p
    for i in range(p, len(d)):
        e = d[i] * k + e * (1 - k)
    return e


def calc_rsi(c, p):
    diffs = [c[i] - c[i-1] for i in range(len(c)-p, len(c))]
    ag = sum(d for d in diffs if d > 0) / p
    al = sum(-d for d in diffs if d < 0) / p
    return 100 if al == 0 else 100 - (100 / (1 + ag / al))


def calc_macd(c):
    # MACD(5,13,6) matching PS: bhe("1m",100) full candle array
    history = []
    for i in range(13, len(c) + 1):
        sub = c[:i]
        history.append(calc_ema(sub, 5) - calc_ema(sub, 13))
    val = calc_ema(c, 5) - calc_ema(c, 13)
    sig = calc_ema(history, 6) if len(history) >= 6 else val
    return val, sig


def calc_mom(closes, opens):
    # Mhe: weighted average of last 10 candle directions (close>=open), recent = highest weight
    pairs = list(zip(closes[-10:], opens[-10:]))
    dirs  = [1 if c >= o else -1 for c, o in pairs]
    dirs.reverse()
    total  = sum(d * (i + 1) for i, d in enumerate(dirs))
    weight = sum(range(1, len(dirs) + 1))
    return (total / weight) * 0.08 if weight else 0


def calc_sr(highs, lows, price, thresh):
    # khe + Ohe: $50 grid levels with rejection counts
    if len(highs) < 20:
        return 0
    GRID  = 50
    PROX  = 0.003
    counts = {}
    for h in highs:
        lvl = round(h / GRID) * GRID
        counts[lvl] = counts.get(lvl, 0) + 1
    for l in lows:
        lvl = round(l / GRID) * GRID
        counts[lvl] = counts.get(lvl, 0) + 1
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


def fetch_ohlc_binance():
    r = requests.get(
        'https://data-api.binance.vision/api/v3/klines'
        '?symbol=BTCUSDT&interval=1m&limit=100',
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


def fetch_ohlc_kraken():
    r = requests.get(
        'https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1',
        timeout=10
    )
    d = r.json()
    if d['error']:
        raise Exception(f'Kraken: {d["error"]}')
    ohlc = [v for k, v in d['result'].items() if k != 'last'][0][-100:]
    return (
        [float(x[4]) for x in ohlc],
        [float(x[1]) for x in ohlc],
        [float(x[2]) for x in ohlc],
        [float(x[3]) for x in ohlc],
    )


def fetch_data():
    try:
        closes, opens, highs, lows = fetch_ohlc_binance()
    except Exception as e:
        log.warning('Binance.vision unavailable (%s), falling back to Kraken', e)
        try:
            closes, opens, highs, lows = fetch_ohlc_kraken()
        except Exception as e2:
            log.warning('Kraken unavailable (%s), falling back to CoinGecko', e2)
            r = requests.get(
                'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart'
                '?vs_currency=usd&days=1', timeout=10
            )
            closes = [p[1] for p in r.json()['prices']]
            opens = closes; highs = closes; lows = closes

    kalshi = None
    try:
        kr = requests.get(
            'https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&limit=10&status=open',
            timeout=5
        )
        markets = kr.json().get('markets', [])
        if not markets:
            kr2 = requests.get(
                'https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC&limit=10&status=open',
                timeout=5
            )
            markets = kr2.json().get('markets', [])
        if markets:
            now_dt = datetime.now(timezone.utc)
            def _mins(m):
                ct = m.get('close_time')
                if not ct: return 999
                try:
                    exp = datetime.fromisoformat(ct.replace('Z', '+00:00'))
                    return (exp - now_dt).total_seconds() / 60
                except Exception:
                    return 999
            markets = [m for m in markets if 0 < _mins(m) <= 20]
            if not markets:
                log.info('Kalshi: no KXBTC15M markets within 20min — skipping')
            else:
                current_price = closes[-1] if closes else None
                def _mk(m):
                    thresh = m.get('floor_strike') or m.get('cap_strike') or 0
                    dist = abs(thresh - current_price) if current_price else 0
                    return (m.get('close_time') or '', dist)
                markets.sort(key=_mk)
                active = markets[0]
                yp = float(active.get('yes_ask_dollars') or active.get('last_price_dollars') or 0.5)
                if yp > 1:
                    yp = yp / 100
                kalshi = {
                    'yes_price': yp,
                    'threshold': active.get('floor_strike') or active.get('cap_strike'),
                    'ticker': active.get('ticker'),
                    'expiry': active.get('close_time'),
                }
    except Exception as e:
        log.warning('Kalshi fetch failed: %s', e)

    return closes, opens, highs, lows, kalshi


def score_indicators(closes, opens, highs, lows, kalshi):
    price = closes[-1]
    ma5   = sum(closes[-5:]) / 5
    ema21 = calc_ema(closes, 21)
    rsi9  = calc_rsi(closes, 9)
    macd_val, macd_sig = calc_macd(closes)

    thresh = (kalshi['threshold'] if kalshi and kalshi['threshold'] else price)

    # mins_left needed for price_gap and rsi_score — compute first
    mins_left = 7
    if kalshi and kalshi.get('expiry'):
        try:
            expiry_dt = datetime.fromisoformat(kalshi['expiry'].replace('Z', '+00:00'))
            mins_left = max(1, round((expiry_dt - datetime.now(timezone.utc)).total_seconds() / 60))
        except Exception:
            pass

    # Price Gap (Che): time-dependent normalization
    pg_norm   = 0.00015 * mins_left * price
    price_gap = max(-1, min(1, (price - thresh) / pg_norm)) * 0.25 if pg_norm > 0 else 0

    # MA Structure (Nhe): 4-component including threshold relationship
    _i = 0
    _i += 0.25 if price > ma5   else -0.25
    _i += 0.25 if price > ema21 else -0.25
    _i += 0.25 if ma5   > ema21 else -0.25
    if price > thresh:
        if ma5 < thresh and ema21 < thresh: _i += 0.25
        elif ma5 > thresh or ema21 > thresh: _i -= 0.15
    else:
        if ma5 > thresh and ema21 > thresh: _i -= 0.25
        elif ma5 < price and ema21 < price: _i += 0.15
    ma_struct = _i * 0.20

    # RSI Score (jhe): continuous curve
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

    # MACD score (Phe)
    _hist = macd_val - macd_sig
    _n    = 0.5 if macd_val > macd_sig else -0.5
    _norm = price * 5e-4
    _mag  = min(abs(_hist) / _norm, 1.0) if _norm > 0 else 0
    _n   += _mag * 0.5 if _hist > 0 else -_mag * 0.5
    macd_score = _n * 0.13

    # S/R (khe + Ohe) and Momentum (Mhe)
    sr        = calc_sr(highs, lows, price, thresh)
    mom_score = calc_mom(closes, opens)

    total = price_gap + ma_struct + rsi_score + macd_score + sr + mom_score

    gap_pct = abs(price - thresh) / price * 100
    mult = calc_time_mult(mins_left, gap_pct)
    time_adj = total * mult
    model_up_prob = max(0.03, min(0.97, 0.50 + time_adj * 0.45))

    return {
        'price': price, 'ma5': ma5, 'ema21': ema21, 'rsi9': rsi9, 'macd': macd_val,
        'sr': sr, 'price_gap': price_gap, 'ma_struct': ma_struct,
        'rsi_score': rsi_score, 'macd_score': macd_score, 'mom_score': mom_score,
        'total': total, 'model_up_prob': model_up_prob, 'mins_left': mins_left,
    }


def build_signal(sc, kalshi, min_edge):
    yes_price = kalshi['yes_price'] if kalshi else 0.5
    edge = sc['model_up_prob'] - yes_price
    ef   = yes_price if edge > 0 else 1 - yes_price
    mg   = 1 - ef
    ml   = ef
    wp   = sc['model_up_prob'] if edge > 0 else 1 - sc['model_up_prob']
    ev   = wp * mg - (1 - wp) * ml
    ae   = abs(edge)
    mins = sc['mins_left']

    if mins <= 2:
        signal = 'TOO_CLOSE'
    elif ae < min_edge:
        signal = 'NO_TRADE'
    else:
        signal = 'TRADE'

    return {
        'signal': signal,
        'direction': 'UP' if edge > 0 else 'DOWN',
        'edge': edge, 'ef': ef, 'mg': mg, 'ml': ml, 'wp': wp, 'ev': ev,
    }


def auto_log(sc, kalshi):
    if not kalshi or not kalshi.get('ticker') or not kalshi.get('expiry'):
        return
    contract_key = f"{kalshi['ticker']}_{kalshi['expiry']}"
    mins = sc['mins_left']

    conn = sqlite3.connect(DB_PATH)
    try:
        for strat, min_edge in STRATS.items():
            if conn.execute('SELECT 1 FROM trades WHERE contract_key=? AND strategy=?',
                            (contract_key, strat)).fetchone():
                continue
            sig = build_signal(sc, kalshi, min_edge)
            n_contracts = math.floor(STAKE / sig['ef']) if sig['signal'] == 'TRADE' else 0
            outcome = 'PENDING' if sig['signal'] == 'TRADE' else 'SKIP'
            now_utc = datetime.now(timezone.utc).isoformat()

            conn.execute('''INSERT OR IGNORE INTO trades
                (contract_key, strategy, timestamp_utc, signal, direction, entry_price,
                 win_prob, edge, btc_price, threshold, expiry, ticker,
                 contracts, stake, max_gain, max_loss, ev, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (contract_key, strat, now_utc, sig['signal'], sig['direction'], sig['ef'],
                 sig['wp'], sig['edge'], sc['price'], kalshi.get('threshold'),
                 kalshi['expiry'], kalshi['ticker'],
                 n_contracts, n_contracts * sig['ef'], n_contracts * sig['mg'],
                 n_contracts * sig['ml'], sig['ev'], outcome))

            log.info('Logged %s: %s %s | edge=%.3f | BTC=%.2f | mins=%d',
                     strat, sig['signal'], sig.get('direction', ''),
                     sig['edge'], sc['price'], mins)
        conn.commit()
    finally:
        conn.close()


def resolve_expired():
    now_utc = datetime.now(timezone.utc)
    conn = sqlite3.connect(DB_PATH)
    try:
        pending = conn.execute(
            "SELECT id, expiry, direction, threshold, max_gain, max_loss "
            "FROM trades WHERE outcome='PENDING'"
        ).fetchall()

        expired = [row for row in pending
                   if row[1] and datetime.fromisoformat(
                       row[1].replace('Z', '+00:00')) < now_utc]
        if not expired:
            return

        r = requests.get(
            'https://api.binance.us/api/v3/ticker/price?symbol=BTCUSD',
            timeout=10
        )
        final_price = float(r.json()['price'])

        for trade_id, expiry, direction, threshold, max_gain, max_loss in expired:
            if not threshold:
                continue
            above = final_price > threshold
            win   = (direction == 'UP' and above) or (direction == 'DOWN' and not above)
            outcome = 'WIN' if win else 'LOSS'
            pnl = max_gain if win else -(max_loss or 0)
            conn.execute(
                'UPDATE trades SET outcome=?, pnl=?, final_btc=? WHERE id=?',
                (outcome, pnl, final_price, trade_id)
            )
            log.info('Resolved #%d: %s @ $%.2f (thresh=$%.2f)',
                     trade_id, outcome, final_price, threshold)

        conn.commit()
    except Exception as e:
        log.error('Resolve error: %s', e)
    finally:
        conn.close()


def main():
    log.info('BTC Scanner starting...')
    init_db()
    last_resolve = 0

    while True:
        try:
            closes, opens, highs, lows, kalshi = fetch_data()
            sc = score_indicators(closes, opens, highs, lows, kalshi)
            log.info('Scan: BTC=$%.2f modelUP=%.3f mins=%d',
                     sc['price'], sc['model_up_prob'], sc['mins_left'])
            auto_log(sc, kalshi)
            if time.time() - last_resolve > 60:
                resolve_expired()
                last_resolve = time.time()
        except Exception as e:
            log.error('Scan error: %s', e)
        time.sleep(SCAN_INTERVAL)


if __name__ == '__main__':
    main()
