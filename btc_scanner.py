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
TIME_MULT = {1:68.2, 2:71.2, 3:63.1, 4:64.5, 5:65.0, 6:52.0, 7:50.8,
             8:49.0, 9:48.6, 10:49.2, 11:40.6, 12:39.2, 13:35.3, 14:29.8}


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
    # Wilder's Smoothed RSI — uses all candles, won't snap to 0 during downtrends
    diffs = [c[i] - c[i-1] for i in range(1, len(c))]
    ag = sum(d for d in diffs[:p] if d > 0) / p
    al = sum(-d for d in diffs[:p] if d < 0) / p
    for d in diffs[p:]:
        ag = (ag * (p - 1) + (d if d > 0 else 0)) / p
        al = (al * (p - 1) + (-d if d < 0 else 0)) / p
    return 100 if al == 0 else 100 - (100 / (1 + ag / al))


def calc_macd(c):
    return calc_ema(c, 12) - calc_ema(c, 26)


def calc_mom(c):
    r = c[-5:]
    ups = sum(1 for i in range(1, len(r)) if r[i] > r[i-1])
    return round(ups / 4 * 10)


def calc_sr(hi, lo, p):
    res = max(hi[-10:])
    sup = min(lo[-10:])
    rng = res - sup
    if not rng:
        return 0
    pos = (p - sup) / rng
    if pos > 0.8:
        return -0.17
    elif pos < 0.2:
        return 0.17
    return 0


def fetch_data():
    r = requests.get(
        'https://api.binance.us/api/v3/klines?symbol=BTCUSD&interval=1m&limit=60',
        timeout=10
    )
    k = r.json()
    closes = [float(x[4]) for x in k]
    highs  = [float(x[2]) for x in k]
    lows   = [float(x[3]) for x in k]

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
            markets.sort(key=lambda m: m.get('close_time') or '')
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

    return closes, highs, lows, kalshi


def score_indicators(closes, highs, lows, kalshi):
    price = closes[-1]
    ma5   = sum(closes[-5:]) / 5
    ema21 = calc_ema(closes, 21)
    rsi9  = calc_rsi(closes, 9)
    macd  = calc_macd(closes)
    mom   = calc_mom(closes)
    sr    = calc_sr(highs, lows, price)

    thresh = (kalshi['threshold'] if kalshi and kalshi['threshold'] else price)
    price_gap = max(-0.25, min(0.25, (price - thresh) / thresh * 10)) if thresh > 0 else 0

    if price < ma5 and price < ema21:
        ma_struct = -0.18
    elif price > ma5 and price > ema21:
        ma_struct = 0.18
    elif price < ma5:
        ma_struct = -0.09
    else:
        ma_struct = 0.09

    # RSI scoring from 454 PS data points (momentum-based, not pure mean-reversion)
    if rsi9 >= 90:
        rsi_score = -0.135
    elif rsi9 >= 80:
        rsi_score = -0.107
    elif rsi9 >= 70:
        rsi_score = -0.086
    elif rsi9 >= 60:
        rsi_score = 0.026
    elif rsi9 >= 50:
        rsi_score = 0.009
    elif rsi9 >= 40:
        rsi_score = -0.008
    elif rsi9 >= 30:
        rsi_score = -0.025
    elif rsi9 >= 20:
        rsi_score = 0.080
    else:
        rsi_score = 0.133

    macd_score = max(-0.15, min(0.15, macd / 469))
    mom_score  = (mom - 5) / 100
    total      = price_gap + ma_struct + rsi_score + macd_score + sr + mom_score

    mins_left = 7
    if kalshi and kalshi.get('expiry'):
        try:
            expiry_dt = datetime.fromisoformat(kalshi['expiry'].replace('Z', '+00:00'))
            mins_left = max(1, round((expiry_dt - datetime.now(timezone.utc)).total_seconds() / 60))
        except Exception:
            pass

    mult = TIME_MULT.get(min(14, max(1, mins_left)), 48)
    model_up_prob = max(0.01, min(0.99, 0.50 + total * mult / 100))

    return {
        'price': price, 'ma5': ma5, 'ema21': ema21, 'rsi9': rsi9, 'macd': macd,
        'momentum': mom, 'sr': sr, 'price_gap': price_gap, 'ma_struct': ma_struct,
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
            closes, highs, lows, kalshi = fetch_data()
            sc = score_indicators(closes, highs, lows, kalshi)
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
