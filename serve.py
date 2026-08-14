"""
Локальный сервер калькулятора растаможки.

    python serve.py            → http://localhost:8731

Отдаёт страницу и два маленьких API поверх неё:
    /api/rates              курс НБУ на сегодня (USD, EUR)
    /api/lot?url=<ссылка>   данные лота Copart + скачанные фото

Всё крутится на твоём ноутбуке. Copart пускает только настоящий браузер,
поэтому /api/lot поднимает окно Chromium на несколько секунд — так и задумано.
"""
import json, re, sys, secrets, threading, time, urllib.parse, pathlib, subprocess, webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import requests
from lot import parse_url, LOTS_DIR
from analyze import analyze
from journal import put_prediction, load as journal_load

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
_claude_lock  = threading.Lock()   # разбор фото тоже строго по одному

NBU = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode={}&json"

# ── доступ по ключу ──────────────────────────────────────────────────────
# Сервер слушает только 127.0.0.1, но через туннель (share.py) на него можно
# зайти из интернета — и снаружи запросы приходят с того же 127.0.0.1, потому
# что туннель работает на этой же машине. Значит «свой или чужой» по адресу
# не отличить, и ключ нужен всем, включая меня. Свой браузер получает его
# один раз в cookie и больше о нём не думает.
KEY_FILE = ROOT / ".access-key"


def access_key():
    if KEY_FILE.exists():
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    k = secrets.token_urlsafe(12)
    KEY_FILE.write_text(k, encoding="utf-8")
    return k


KEY = access_key()

# Наружу отдаём только то, из чего состоит калькулятор. Всё остальное в папке —
# журнал сделок с ценами покупки, рабочие заметки, лог, исходники — не отдаём
# вовсе. Список разрешённого надёжнее списка запрещённого: забыть добавить
# файл в него безопасно, забыть исключить — нет.
PUBLIC_FILES = {"/", "/index.html", "/market.json", "/bookmarklet.js", "/favicon.ico"}

# Разбор фотографий тратит лимиты подписки. Ключ уходит дальше, чем его дают,
# поэтому потолок на число разборов в час — страховка от цикла, а не от людей.
ANALYZE_PER_HOUR = 12
_analyze_log = []


def nbu_rates():
    out = {}
    for code in ("usd", "eur"):
        r = requests.get(NBU.format(code), timeout=20)
        r.raise_for_status()
        row = r.json()[0]
        out[code] = {"rate": row["rate"], "date": row["exchangedate"]}
    return out


def cached_lot(lot):
    """Отдаёт уже скачанный лот с диска, не поднимая браузер."""
    f = LOTS_DIR / lot / "lot.json"
    if not f.exists():
        return 404, {"ok": False,
                     "error": f"Лот {lot} ещё не качали — вставь ссылку целиком"}
    data = json.loads(f.read_text(encoding="utf-8"))
    data["photo_dir"] = f"/lots/{lot}"
    a = LOTS_DIR / lot / "analysis.json"
    if a.exists():
        try: data["analysis"] = json.loads(a.read_text(encoding="utf-8"))
        except Exception: pass
    return 200, {"ok": True, "lot": data, "cached": True}


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

    # ── ключ ─────────────────────────────────────────────────────────────
    def authed(self, query):
        given = urllib.parse.parse_qs(query).get("k", [""])[0]
        if given and secrets.compare_digest(given, KEY):
            self._set_cookie = True      # дальше ходит без ключа в ссылке
            return True
        m = re.search(r"(?:^|;\s*)ck=([^;]+)", self.headers.get("Cookie") or "")
        return bool(m and secrets.compare_digest(m.group(1), KEY))

    def end_headers(self):
        if getattr(self, "_set_cookie", False):
            self.send_header("Set-Cookie",
                             f"ck={KEY}; Path=/; Max-Age=2592000; SameSite=Lax")
            self._set_cookie = False
        super().end_headers()

    def deny(self):
        body = ("<!doctype html><meta charset=utf-8>"
                "<title>Нужен ключ</title>"
                "<style>body{font:15px/1.6 system-ui;max-width:34em;margin:14vh auto;"
                "padding:0 20px;color:#243}</style>"
                "<h1>Нужен ключ доступа</h1>"
                "<p>Ссылка должна заканчиваться на <code>?k=…</code>. "
                "Попроси её у того, кто дал вам эту страницу.</p>"
                ).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if not self.authed(parsed.query):
            self.deny()
            return

        if parsed.path == "/api/rates":
            try:
                self._json(200, {"ok": True, "rates": nbu_rates()})
            except Exception as e:
                self._json(502, {"ok": False, "error": f"НБУ не ответил: {e}"})
            return

        if parsed.path == "/api/analyze":
            lot = urllib.parse.parse_qs(parsed.query).get("lot", [""])[0].strip()
            if not re.fullmatch(r"\d{6,10}", lot):
                self._json(400, {"ok": False, "error": "Некорректный номер лота"})
                return
            now = time.time()
            _analyze_log[:] = [t for t in _analyze_log if now - t < 3600]
            if len(_analyze_log) >= ANALYZE_PER_HOUR:
                self._json(429, {"ok": False,
                                 "error": f"За час уже {ANALYZE_PER_HOUR} разборов — "
                                          f"это предел, чтобы не выжечь лимиты. "
                                          f"Подожди или подними ANALYZE_PER_HOUR."})
                return
            if not _claude_lock.acquire(blocking=False):
                self._json(429, {"ok": False,
                                 "error": "Разбор уже идёт — подожди, он небыстрый"})
                return
            _analyze_log.append(now)
            try:
                log(f"разбираю фото лота {lot}")
                try:
                    data = analyze(lot)
                except Exception as e:
                    log(f"разбор не вышел: {e}")
                    self._json(502, {"ok": False, "error": str(e)[:400]})
                    return
                # Разбор идёт минуты, и вкладку за это время закрывают — тогда
                # ответ уходит в оборванный сокет. Это не провал: результат уже
                # лежит в analysis.json и подставится при следующем открытии
                # лота. Раньше такое писалось в лог как «разбор не вышел».
                try:
                    self._json(200, {"ok": True, "analysis": data})
                except OSError:
                    log(f"разбор лота {lot} готов, но клиент уже отключился — "
                        f"результат в lots/{lot}/analysis.json")
            finally:
                _claude_lock.release()
            return

        if parsed.path == "/api/lot":
            q   = urllib.parse.parse_qs(parsed.query)
            url = q.get("url", [""])[0].strip()
            num = q.get("lot", [""])[0].strip()
            if num and not url:                       # уже скачанный — с диска
                if not re.fullmatch(r"\d{6,10}", num):
                    self._json(400, {"ok": False, "error": "Некорректный номер лота"})
                else:
                    self._json(*cached_lot(num))
                return
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

        if parsed.path == "/api/journal":
            try:
                self._json(200, {"ok": True, "journal": journal_load()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:300]})
            return

        # статика: только калькулятор и фотографии лотов
        if not (parsed.path in PUBLIC_FILES or parsed.path.startswith("/lots/")):
            self.send_error(404, "Not Found")
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if not self.authed(urllib.parse.urlparse(self.path).query):
            self.deny()
            return
        if urllib.parse.urlparse(self.path).path != "/api/journal":
            self._json(404, {"ok": False, "error": "нет такого адреса"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 64_000:
                raise ValueError("пустое или слишком большое тело запроса")
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            lot  = str(data.get("lot") or "").strip()
            if not re.fullmatch(r"\d{6,10}", lot):
                raise ValueError("некорректный номер лота")
            e = put_prediction(lot, data)
            log(f"журнал: записан прогноз по лоту {lot}")
            self._json(200, {"ok": True, "entry": e})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)[:300]})


def main():
    LOTS_DIR.mkdir(exist_ok=True)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        # Запуск вторым экземпляром выглядит успешным: окна нет, ошибка молчит,
        # а страницу продолжает отдавать первый — со старым кодом. Отсюда потом
        # берутся «правки не применились». Пишем в лог, он и есть единственный
        # способ это заметить.
        log(f"НЕ ЗАПУСТИЛСЯ: порт {PORT} занят ({e}). "
            f"Работает прежний сервер — останови его и запусти заново.")
        print(f"Порт {PORT} уже занят: сервер, скорее всего, уже запущен.")
        return
    log(f"старт, порт {PORT}")
    local = f"http://localhost:{PORT}/?k={KEY}"
    print("─" * 72)
    print(f"  Калькулятор растаможки →  {local}")
    print(f"  Папка с лотами         →  {LOTS_DIR}")
    print(f"  Ссылка для чужих       →  python share.py")
    print("  Остановить             →  Ctrl+C")
    print("─" * 72, flush=True)
    # Ключ нужен всем, поэтому свой браузер открываем сразу с ним: дальше
    # он живёт в cookie месяц, и про ключ можно забыть.
    try: webbrowser.open(local)
    except Exception: pass
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
