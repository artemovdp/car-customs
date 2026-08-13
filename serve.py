"""
Локальный сервер калькулятора растаможки.

    python serve.py            → http://localhost:8731

Отдаёт страницу и два маленьких API поверх неё:
    /api/rates              курс НБУ на сегодня (USD, EUR)
    /api/lot?url=<ссылка>   данные лота Copart + скачанные фото

Всё крутится на твоём ноутбуке. Copart пускает только настоящий браузер,
поэтому /api/lot поднимает окно Chromium на несколько секунд — так и задумано.
"""
import json, sys, threading, time, urllib.parse, pathlib, subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import requests
from lot import parse_url, LOTS_DIR

ROOT = pathlib.Path(__file__).parent
PORT = 8731
LOT_TIMEOUT = 330          # секунд на один лот: с запасом на ручную проверку Akamai
LOG  = ROOT / "server.log"


def log(msg):
    """Пишет в файл, а не в консоль.

    Консоль Windows с включённым QuickEdit блокирует пишущий процесс, стоит
    случайно выделить в окне текст. Сервер тогда замирал ровно на обработчиках
    /api/ — единственных, что писали в stderr, — а статика продолжала отдаваться.
    """
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
    except Exception:
        pass

# Синхронный Playwright нельзя гонять в потоке веб-сервера: после первого же
# запуска процесс остаётся в нерабочем состоянии — статика ещё отдаётся, а
# остальные обработчики встают намертво. Поэтому браузер живёт в отдельном
# процессе (lot.py), а сервер только ждёт его и читает результат.
_browser_lock = threading.Lock()

NBU = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode={}&json"


def nbu_rates():
    out = {}
    for code in ("usd", "eur"):
        r = requests.get(NBU.format(code), timeout=20)
        r.raise_for_status()
        row = r.json()[0]
        out[code] = {"rate": row["rate"], "date": row["exchangedate"]}
    return out


def run_lot(url):
    """Отдельным процессом дёргает lot.py. Возвращает (HTTP-код, тело)."""
    try:
        _, lot = parse_url(url)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}

    log(f"загружаю лот {lot}")
    try:
        p = subprocess.run([sys.executable, str(ROOT / "lot.py"), url],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=LOT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 504, {"ok": False,
                     "error": f"Copart не ответил за {LOT_TIMEOUT} с. Проверь интернет "
                              f"и попробуй ещё раз."}

    if p.stdout:
        log(p.stdout.rstrip())

    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or "lot.py завершился с ошибкой"
        log(msg)
        return 503, {"ok": False, "error": msg}

    f = LOTS_DIR / lot / "lot.json"
    if not f.exists():
        return 500, {"ok": False, "error": f"lot.py отработал, но {f.name} не появился"}

    data = json.loads(f.read_text(encoding="utf-8"))
    data["photo_dir"] = f"/lots/{lot}"
    return 200, {"ok": True, "lot": data}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            log(fmt % args)

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/rates":
            try:
                self._json(200, {"ok": True, "rates": nbu_rates()})
            except Exception as e:
                self._json(502, {"ok": False, "error": f"НБУ не ответил: {e}"})
            return

        if parsed.path == "/api/lot":
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0].strip()
            if not url:
                self._json(400, {"ok": False, "error": "Не передана ссылка на лот"})
                return
            if not _browser_lock.acquire(blocking=False):
                self._json(429, {"ok": False,
                                 "error": "Уже загружаю другой лот — подожди пару секунд"})
                return
            try:
                self._json(*run_lot(url))
            finally:
                _browser_lock.release()
            return

        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()


def main():
    LOTS_DIR.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("─" * 58)
    print(f"  Калькулятор растаможки →  http://localhost:{PORT}")
    print(f"  Папка с лотами         →  {LOTS_DIR}")
    print("  Остановить             →  Ctrl+C")
    print("─" * 58, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
        srv.server_close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
