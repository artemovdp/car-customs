"""
Локальный сервер калькулятора растаможки.

    python serve.py            → http://localhost:8731

Отдаёт страницу и два маленьких API поверх неё:
    /api/rates              курс НБУ на сегодня (USD, EUR)
    /api/lot?url=<ссылка>   данные лота Copart + скачанные фото

Всё крутится на твоём ноутбуке. Copart пускает только настоящий браузер,
поэтому /api/lot поднимает окно Chromium на несколько секунд — так и задумано.
"""
import json, sys, threading, urllib.parse, pathlib, traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import requests
from lot import fetch, BlockedError, LOTS_DIR

ROOT = pathlib.Path(__file__).parent
PORT = 8731

# Playwright не любит несколько браузеров разом — пускаем строго по одному.
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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % args))

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
                print(f"→ загружаю лот: {url}", flush=True)
                data, out = fetch(url)
                data["photo_dir"] = f"/lots/{data['lot']}"
                self._json(200, {"ok": True, "lot": data})
            except BlockedError as e:
                self._json(503, {"ok": False, "error": str(e)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
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
