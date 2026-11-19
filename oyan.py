"""OYAN / ForestCare — орман өртін ерте анықтау жүйесі.

BME280 (I2C, 0x76) — температура, ылғалдылық, қысым
MQ-2 (DO -> GPIO17, LOW = түтін) — түтін / жанғыш газ
Веб: http://<pi>:5000/  (сайт)   http://<pi>:5000/data  (JSON)
"""
import time, csv, os, json, threading, collections
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import board, gpiod, requests
from gpiod.line import Direction, Value
from adafruit_bme280 import basic as adafruit_bme280

# ---- Settings ----
BME_ADDR = 0x76
MQ2_PIN = 17
WARMUP_SEC = int(os.environ.get('OYAN_WARMUP', 120))
INTERVAL = 5
PORT = int(os.environ.get('OYAN_PORT', 5000))
BASE_DIR = os.path.expanduser('~/oyan')
LOG_FILE = os.path.join(BASE_DIR, 'oyan_log.csv')
WEB_DIR = os.path.join(BASE_DIR, 'web')
CFG_FILE = os.path.join(BASE_DIR, 'config.json')


def load_cfg():
    try:
        with open(CFG_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


CFG = load_cfg()
FIREBASE_URL = str(CFG.get('firebase_url', '')).strip().rstrip('/')   # бұлттағы дерекқор
TG_TOKEN = str(CFG.get('tg_token', '')).strip()                        # Telegram bot token
TG_CHAT_ID = str(CFG.get('tg_chat_id', '')).strip()                    # Telegram chat id
ALERT_COOLDOWN = 300
DANGER = ('ҚАУІПТІ', 'ӨРТ ҚАУПІ!')

# ---- Shared state for the website ----
STATE = {'ok': False}
HIST = collections.deque(maxlen=180)   # 180 x 5s = 15 минут
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _headers(self, code, ctype, length=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Cache-Control', 'no-store')
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()

    def do_OPTIONS(self):
        self._headers(204, 'text/plain')

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/data':
            with LOCK:
                d = dict(STATE)
                d['history'] = list(HIST)
            body = json.dumps(d, ensure_ascii=False).encode('utf-8')
            self._headers(200, 'application/json; charset=utf-8', len(body))
            self.wfile.write(body)
        elif path == '/config.js':
            body = b'window.OYAN_CLOUD="";'
            self._headers(200, 'application/javascript; charset=utf-8', len(body))
            self.wfile.write(body)
        elif path in ('/', '/index.html'):
            try:
                with open(os.path.join(WEB_DIR, 'index.html'), 'rb') as fh:
                    body = fh.read()
                self._headers(200, 'text/html; charset=utf-8', len(body))
            except OSError:
                body = 'OYAN: web/index.html табылмады'.encode('utf-8')
                self._headers(404, 'text/plain; charset=utf-8', len(body))
            self.wfile.write(body)
        else:
            self._headers(404, 'text/plain')
            self.wfile.write(b'not found')

    def log_message(self, *args):
        pass


def start_web():
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f'Web: http://0.0.0.0:{PORT}/', flush=True)


def cloud_loop():
    """Соңғы мәндерді (5 сек сайын) және тарихты (30 сек сайын) Firebase-ке жібереді."""
    last_hist = 0
    while True:
        if FIREBASE_URL:
            try:
                with LOCK:
                    latest = dict(STATE)
                    hist = list(HIST)[::2][-90:]
                if latest.get('ok'):
                    requests.put(f'{FIREBASE_URL}/oyan/latest.json', json=latest, timeout=8)
                    if time.time() - last_hist > 30:
                        requests.put(f'{FIREBASE_URL}/oyan/history.json', json=hist, timeout=15)
                        last_hist = time.time()
            except Exception as e:
                print('Cloud error:', e, flush=True)
        time.sleep(INTERVAL)


def send_tg(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                      data={'chat_id': TG_CHAT_ID, 'text': text}, timeout=10)
    except Exception as e:
        print('Telegram error:', e, flush=True)


def risk_level(t, h, smoke):
    score = 0
    if t > 40: score += 2
    elif t > 30: score += 1
    if h < 20: score += 2
    elif h < 35: score += 1
    if smoke: score += 4
    if score >= 6: return score, 'ӨРТ ҚАУПІ!'
    if score >= 4: return score, 'ҚАУІПТІ'
    if score >= 2: return score, 'НАЗАР'
    return score, 'ҚАЛЫПТЫ'


def load_history():
    """Сайт графигі бос болмас үшін CSV-дің соңғы жолдарын жүктейміз."""
    try:
        with open(LOG_FILE, encoding='utf-8') as fh:
            rows = list(csv.reader(fh))[1:][-HIST.maxlen:]
        for r in rows:
            ts = int(datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S').timestamp())
            if time.time() - ts > 15 * 60:
                continue
            HIST.append({'ts': ts, 't': float(r[1]), 'h': float(r[2]), 'p': float(r[3]),
                         'smoke': r[4] == '1', 'score': int(r[5]), 'level': r[6]})
    except Exception:
        pass


def main():
    load_history()
    start_web()
    threading.Thread(target=cloud_loop, daemon=True).start()
    print('Cloud:', FIREBASE_URL or 'off', '| Telegram:', 'on' if TG_TOKEN and TG_CHAT_ID else 'off', flush=True)

    bme = adafruit_bme280.Adafruit_BME280_I2C(board.I2C(), address=BME_ADDR)
    mq2 = gpiod.request_lines('/dev/gpiochip0', consumer='oyan-mq2',
                              config={MQ2_PIN: gpiod.LineSettings(direction=Direction.INPUT)})

    new_file = not os.path.exists(LOG_FILE)
    f = open(LOG_FILE, 'a', newline='', encoding='utf-8')
    w = csv.writer(f)
    if new_file:
        w.writerow(['time', 'temp_C', 'humidity_%', 'pressure_hPa', 'smoke', 'score', 'level'])

    start = time.time()
    last_level, last_alert, ready = 'ҚАЛЫПТЫ', 0, False
    print(f'OYAN started. MQ-2 warm-up {WARMUP_SEC}s', flush=True)

    while True:
        try:
            t, h, p = bme.temperature, bme.humidity, bme.pressure
            smoke = mq2.get_value(MQ2_PIN) == Value.INACTIVE   # DO LOW = түтін
            score, level = risk_level(t, h, smoke)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            elapsed = time.time() - start
            warm = elapsed < WARMUP_SEC

            rec = {'ts': int(time.time()), 't': round(t, 2), 'h': round(h, 2), 'p': round(p, 2),
                   'smoke': smoke, 'score': score, 'level': level}
            with LOCK:
                STATE.clear()
                STATE.update(rec)
                STATE.update(ok=True, warm=warm, warm_left=max(0, int(WARMUP_SEC - elapsed)),
                             interval=INTERVAL)
                HIST.append(rec)

            print(f'{now} | {t:.1f}C | {h:.1f}% | {p:.1f} hPa | smoke: {"YES" if smoke else "no"} | '
                  f'{level}{" (warm-up)" if warm else ""}', flush=True)
            w.writerow([now, f'{t:.2f}', f'{h:.2f}', f'{p:.2f}', int(smoke), score, level])
            f.flush()

            if not warm:
                if not ready:
                    ready = True
                    send_tg('🟢 OYAN іске қосылды')
                if level in DANGER and (level != last_level or time.time() - last_alert > ALERT_COOLDOWN):
                    icon = '🔴' if level == 'ӨРТ ҚАУПІ!' else '🟠'
                    send_tg(f'{icon} OYAN: {level}\n🌡 {t:.1f}°C  💧 {h:.1f}%\n'
                            f'💨 Түтін: {"БАР" if smoke else "жоқ"}\n🕒 {now}')
                    last_alert = time.time()
                elif level not in DANGER and last_level in DANGER:
                    send_tg(f'✅ OYAN: жағдай қалпына келді ({level})')
                last_level = level
        except Exception as e:
            print('Error:', e, flush=True)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
