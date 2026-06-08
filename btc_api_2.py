from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import sqlite3
import time
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

DB_PATH = '/root/trades.db'

_candle_cache = {'data': None, 'ts': 0}
CACHE_TTL = 60


def fetch_ohlc():
    now = time.time()
    if _candle_cache['data'] is not None and now - _candle_cache['ts'] < CACHE_TTL:
        return _candle_cache['data']

    try:
        # Primary: Binance.US 1-min OHLC 100 candles (matches PS: bhe("1m", 100))
        r = requests.get(
            'https://api.binance.us/api/v3/klines?symbol=BTCUSD&interval=1m&limit=100',
            timeout=10
        )
        if r.status_code == 200:
            k = r.json()
            data = {
                'closes': [float(x[4]) for x in k],
                'opens':  [float(x[1]) for x in k],
                'highs':  [float(x[2]) for x in k],
                'lows':   [float(x[3]) for x in k],
            }
            _candle_cache['data'] = data
            _candle_cache['ts']   = now
            return data
    except Exception:
        pass

    # Fallback: CoinGecko price-only
    r = requests.get(
        'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart'
        '?vs_currency=usd&days=1',
        timeout=10
    )
    closes = [p[1] for p in r.json()['prices']]
    data = {'closes': closes, 'opens': closes, 'highs': closes, 'lows': closes}
    _candle_cache['data'] = data
    _candle_cache['ts']   = now
    return data


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route('/scan')
def scan():
    try:
        ohlc   = fetch_ohlc()
        closes = ohlc['closes']
        opens  = ohlc['opens']
        highs  = ohlc['highs']
        lows   = ohlc['lows']
        kalshi = None
        try:
            kr = requests.get(
                'https://api.elections.kalshi.com/trade-api/v2/markets'
                '?series_ticker=KXBTC15M&limit=10&status=open',
                timeout=5
            )
            kd = kr.json()
            markets = kd.get('markets', [])
            if not markets:
                kr2 = requests.get(
                    'https://api.elections.kalshi.com/trade-api/v2/markets'
                    '?series_ticker=KXBTC&limit=10&status=open',
                    timeout=5
                )
                markets = kr2.json().get('markets', [])
            if markets:
                markets.sort(key=lambda m: m.get('close_time') or '')
                active = markets[0]
                yp = float(
                    active.get('yes_ask_dollars') or
                    active.get('last_price_dollars') or 0.5
                )
                if yp > 1:
                    yp = yp / 100
                kalshi = {
                    'yes_price': yp,
                    'threshold': active.get('floor_strike') or active.get('cap_strike'),
                    'ticker': active.get('ticker'),
                    'expiry': active.get('close_time'),
                }
        except Exception as ke:
            kalshi = {'error': str(ke)}
        return jsonify({'closes': closes, 'opens': opens, 'highs': highs, 'lows': lows, 'kalshi': kalshi})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/trades')
def trades():
    try:
        strategy = request.args.get('strategy', 'all')
        limit = min(int(request.args.get('limit', 200)), 500)
        conn = get_db()
        if strategy == 'all':
            rows = conn.execute(
                'SELECT * FROM trades ORDER BY id DESC LIMIT ?', (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM trades WHERE strategy=? ORDER BY id DESC LIMIT ?',
                (strategy, limit)
            ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stats')
def stats():
    try:
        conn = get_db()
        result = {}
        for strat in ('conservative', 'moderate', 'aggressive'):
            rows = conn.execute(
                "SELECT outcome, pnl FROM trades WHERE strategy=?", (strat,)
            ).fetchall()
            trades_all  = [r for r in rows]
            trades_done = [r for r in rows if r['outcome'] in ('WIN', 'LOSS')]
            trades_fired = [r for r in rows if r['outcome'] in ('WIN', 'LOSS', 'PENDING')]
            wins  = sum(1 for r in trades_done if r['outcome'] == 'WIN')
            total = len(trades_done)
            pnl   = sum(r['pnl'] for r in trades_done if r['pnl'] is not None)
            result[strat] = {
                'total_logged': len(trades_all),
                'fired': len(trades_fired),
                'resolved': total,
                'wins': wins,
                'losses': total - wins,
                'win_rate': round(wins / total, 3) if total > 0 else None,
                'pnl': round(pnl, 2),
                'pending': sum(1 for r in rows if r['outcome'] == 'PENDING'),
            }
        conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
        conn.close()
        db_ok = True
    except Exception:
        count = 0
        db_ok = False
    return jsonify({
        'status': 'ok',
        'db': db_ok,
        'trade_count': count,
        'time_utc': datetime.now(timezone.utc).isoformat(),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
