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
CACHE_TTL = 10  # short TTL — binance.vision has no rate limit concern at this frequency

_raw_candle_cache = {'data': None, 'ts': 0}
RAW_CACHE_TTL = 5  # 5s — raw klines proxy for browser; matches PS's live feel


def fetch_ohlc():
    now = time.time()
    if _candle_cache['data'] is not None and now - _candle_cache['ts'] < CACHE_TTL:
        return _candle_cache['data']

    # Primary: data-api.binance.vision — same BTCUSDT data as api.binance.com, no geo-block
    try:
        r = requests.get(
            'https://data-api.binance.vision/api/v3/klines'
            '?symbol=BTCUSDT&interval=1m&limit=100',
            timeout=10
        )
        r.raise_for_status()
        ohlc = r.json()
        data = {
            'closes': [float(x[4]) for x in ohlc],
            'opens':  [float(x[1]) for x in ohlc],
            'highs':  [float(x[2]) for x in ohlc],
            'lows':   [float(x[3]) for x in ohlc],
        }
        _candle_cache['data'] = data
        _candle_cache['ts']   = now
        return data
    except Exception:
        pass

    # Fallback: Kraken
    try:
        r = requests.get(
            'https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1',
            timeout=10
        )
        d = r.json()
        if not d['error']:
            ohlc = [v for k, v in d['result'].items() if k != 'last'][0][-100:]
            data = {
                'closes': [float(x[4]) for x in ohlc],
                'opens':  [float(x[1]) for x in ohlc],
                'highs':  [float(x[2]) for x in ohlc],
                'lows':   [float(x[3]) for x in ohlc],
            }
            _candle_cache['data'] = data
            _candle_cache['ts']   = now
            return data
    except Exception:
        pass

    # Last resort: CoinGecko price-only
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


@app.route('/candles')
def candles():
    """Raw kline proxy: browser → our server → data-api.binance.vision.
    Bypasses CORS block on api.binance.com. 5s cache so 15s scans get fresh data."""
    from flask import Response
    now = time.time()
    if _raw_candle_cache['data'] is not None and now - _raw_candle_cache['ts'] < RAW_CACHE_TTL:
        return Response(_raw_candle_cache['data'], content_type='application/json',
                        headers={'Cache-Control': 'no-store'})
    try:
        r = requests.get(
            'https://data-api.binance.vision/api/v3/klines'
            '?symbol=BTCUSDT&interval=1m&limit=100',
            timeout=8,
            headers={'Cache-Control': 'no-cache'},
        )
        r.raise_for_status()
        _raw_candle_cache['data'] = r.text
        _raw_candle_cache['ts'] = now
        return Response(r.text, content_type='application/json',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


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


@app.route('/ps-data')
def ps_data():
    try:
        limit = min(int(request.args.get('limit', 500)), 2000)
        source = request.args.get('source')  # optional filter: 'simulator' or 'browser'
        conn = get_db()
        if source:
            rows = conn.execute(
                'SELECT * FROM ps_data WHERE source=? ORDER BY id DESC LIMIT ?',
                (source, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM ps_data ORDER BY id DESC LIMIT ?', (limit,)
            ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ps-log', methods=['POST'])
def ps_log():
    """Accept PS readings POSTed from the user's browser (authentic Binance.com data)."""
    try:
        d = request.get_json(force=True)
        if not d:
            return jsonify({'error': 'no JSON body'}), 400
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
        conn.execute('''INSERT INTO ps_data
            (timestamp_utc,source,btc_price,threshold,ticker,mins_left,up_pct,
             kalshi_yes,edge,signal,price_gap,ma_struct,rsi_score,macd_score,
             sr_score,mom_score,total_raw,rsi9,macd_val,ma5,ema21)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            d.get('timestamp_utc', datetime.now(timezone.utc).isoformat()),
            'browser',
            d.get('btc_price') or d.get('price'),
            d.get('threshold'),
            d.get('ticker'),
            d.get('mins_left'),
            d.get('up_pct') or d.get('upPct'),
            d.get('kalshi_yes') or d.get('kalshiYes'),
            d.get('edge'),
            d.get('signal'),
            d.get('price_gap') or d.get('priceGap'),
            d.get('ma_struct') or d.get('maStructure'),
            d.get('rsi_score') or d.get('rsiScore'),
            d.get('macd_score') or d.get('macdScore'),
            d.get('sr_score') or d.get('sr'),
            d.get('mom_score') or d.get('momentumScore'),
            d.get('total_raw') or d.get('total'),
            d.get('rsi9') or d.get('rsi'),
            d.get('macd_val') or d.get('macd'),
            d.get('ma5'),
            d.get('ema21'),
        ))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
        ps_count = 0
        try:
            ps_count = conn.execute('SELECT COUNT(*) FROM ps_data').fetchone()[0]
        except Exception:
            pass
        conn.close()
        db_ok = True
    except Exception:
        count = 0
        ps_count = 0
        db_ok = False
    return jsonify({
        'status': 'ok',
        'db': db_ok,
        'trade_count': count,
        'ps_data_count': ps_count,
        'time_utc': datetime.now(timezone.utc).isoformat(),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
