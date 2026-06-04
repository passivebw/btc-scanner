from flask import Flask, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

@app.route('/scan')
def scan():
    try:
        r = requests.get('https://api.binance.us/api/v3/klines?symbol=BTCUSD&interval=1m&limit=60')
        k = r.json()
        closes = [float(x[4]) for x in k]
        highs = [float(x[2]) for x in k]
        lows = [float(x[3]) for x in k]
        kr = requests.get('https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTCD&limit=5')
        kd = kr.json()
        markets = kd.get('markets', [])
        active = next((m for m in markets if m.get('status') == 'active'), None)
        kalshi = None
        if active:
            yp = active.get('last_price') or active.get('yes_ask') or 0.5
            yp = yp/100 if yp > 1 else yp
            kalshi = {
                'yes_price': yp,
                'threshold': active.get('floor_strike') or active.get('cap_strike'),
                'ticker': active.get('ticker'),
                'expiry': active.get('close_time')
            }
        return jsonify({'closes': closes, 'highs': highs, 'lows': lows, 'kalshi': kalshi})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
