"""
Публичная ссылка на калькулятор, пока включён компьютер.

    python share.py                 перебрать каналы, взять первый рабочий
    python share.py --via serveo     конкретный канал
    python share.py --list           проверить, какие вообще доступны

Белый IP и настройка роутера не нужны: туннель соединяется наружу сам и
работает за NAT провайдера. Пока скрипт не запущен, снаружи не видно ничего —
сервер слушает только 127.0.0.1.

Каналов несколько не от хорошей жизни. Cloudflare-туннель на украинских
провайдерах часто не поднимается: DNS резолвится, интернет есть, а TCP до
api.trycloudflare.com не проходит — домен режут из-за фишинга. Поэтому по
умолчанию скрипт пробует по очереди, пока какой-нибудь не отдаст ссылку.

Что важно понимать про эту ссылку:

* Она даёт полный доступ: чужой человек может грузить лоты и запускать разбор
  фотографий. Разбор идёт с твоей подписки Claude, окно Chromium при загрузке
  лота открывается на твоём экране.
* Наружу уходит ГОСТЕВОЙ ключ: он открывает калькулятор, загрузку лотов и
  разбор фото, но не журнал сделок — там цены покупки и прибыль.
* Ключ уходит дальше, чем его дают. Смена — удалить `.guest-key` и
  перезапустить сервер; все старые ссылки сразу умрут.
"""
import re, sys, socket, shutil, pathlib, argparse, threading, subprocess

ROOT = pathlib.Path(__file__).parent
KEY_FILE = ROOT / ".guest-key"   # наружу раздаём гостевой, не свой
PORT = 8731
# Сколько ждать ссылку, прежде чем считать канал негодным. Провайдер может
# принять соединение и замолчать — serveo, например, так отвечает на слишком
# частые перезапуски: пишет «too many tunnel starts» и висит. Без таймера
# перебор на таком канале застревает навсегда.
FIND_TIMEOUT = 40

# Ключи ssh чужих серверов не проверяем и в known_hosts не пишем: адреса
# одноразовые, а любой интерактивный вопрос повесит скрипт намертво.
SSH_Q = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=30"]

PROVIDERS = {
    "serveo": {
        "probe": ("serveo.net", 22),
        "need":  "ssh",
        "cmd":   lambda: ["ssh", *SSH_Q, f"-R80:127.0.0.1:{PORT}", "serveo.net"],
        # выдаёт адрес на serveousercontent.com, а не на serveo.net
        "url":   re.compile(r"https://[\w.-]+\.(?:serveousercontent\.com|serveo\.net)"),
        "note":  "гостю один раз покажет страницу-предупреждение",
    },
    "pinggy": {
        "probe": ("a.pinggy.io", 443),
        "need":  "ssh",
        # -tt: баннер со ссылкой приходит только на псевдотерминал
        "cmd":   lambda: ["ssh", "-tt", "-p", "443", *SSH_Q,
                          f"-R0:127.0.0.1:{PORT}", "a.pinggy.io"],
        "url":   re.compile(r"https://[\w-]+\.(?:[\w-]+\.)*pinggy\.link"),
        "note":  "бесплатная сессия живёт около часа, потом перезапусти",
    },
    "cloudflare": {
        "probe": ("api.trycloudflare.com", 443),
        "need":  "cloudflared",
        "cmd":   lambda: ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{PORT}",
                          "--no-autoupdate"],
        # адрес туннеля — слова через дефис; рядом в выводе мелькает служебный
        # api.trycloudflare.com, поэтому дефис в имени обязателен
        "url":   re.compile(r"https://[\w]+(?:-[\w]+)+\.trycloudflare\.com"),
        "note":  "у украинских провайдеров часто заблокирован",
    },
    "localtunnel": {
        "probe": ("localtunnel.me", 443),
        "need":  "npx",
        "cmd":   lambda: ["npx", "--yes", "localtunnel", "--port", str(PORT)],
        "url":   re.compile(r"https://[\w-]+\.loca\.lt"),
        "note":  "гостю покажет страницу-предупреждение перед входом",
    },
}
ORDER = ["serveo", "pinggy", "cloudflare", "localtunnel"]


def reachable(host, port, timeout=6):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def ready(name):
    """Канал годится, если есть чем его поднять и до него доходит TCP."""
    p = PROVIDERS[name]
    if not shutil.which(p["need"]):
        return False, f"нет {p['need']}"
    if not reachable(*p["probe"]):
        return False, f"{p['probe'][0]} недоступен — режет провайдер"
    return True, "доступен"


def server_alive(key):
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/?k={key}", timeout=5).read(1)
        return True
    except Exception:
        return False


def argv(cmd):
    """Windows не умеет запускать .cmd напрямую — а npx именно такой."""
    exe = shutil.which(cmd[0]) or cmd[0]
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *cmd[1:]]
    return [exe, *cmd[1:]]


def run(name, key):
    """Поднимает канал и ждёт от него ссылку. True, если ссылка была."""
    p = PROVIDERS[name]
    print(f"→ {name}: поднимаю…", flush=True)
    proc = subprocess.Popen(argv(p["cmd"]()), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            stdin=subprocess.DEVNULL)
    state = {"shown": False}

    def giveup():
        if not state["shown"]:
            print(f"   {name}: за {FIND_TIMEOUT} с ссылки не дал", flush=True)
            try: proc.terminate()
            except Exception: pass

    timer = threading.Timer(FIND_TIMEOUT, giveup)
    timer.daemon = True
    timer.start()

    try:
        for line in proc.stdout:
            m = p["url"].search(line)
            if m and not state["shown"]:
                state["shown"] = True
                timer.cancel()
                print("─" * 72)
                print("  Ссылка для других людей:")
                print(f"  {m.group(0)}/?k={key}")
                print("─" * 72)
                print(f"  канал {name} — {p['note']}")
                print("  Работает, пока открыто это окно и включён компьютер.")
                print("  Разбор фотографий у них идёт с ТВОЕЙ подписки Claude,")
                print("  окно Chromium при загрузке лота откроется на ТВОЁМ экране.")
                print("  Закрыть доступ — Ctrl+C. Сменить ключ — удали .guest-key")
                print("  и перезапусти сервер.")
                print("─" * 72, flush=True)
            elif not state["shown"] and line.strip():
                print("   " + line.rstrip()[:150], flush=True)
        proc.wait()
    except KeyboardInterrupt:
        print("\nтуннель закрыт, снаружи больше не видно")
        proc.terminate()
        return True
    finally:
        timer.cancel()
    if not state["shown"]:
        print(f"   {name} отвалился, не дав ссылку\n", flush=True)
    return state["shown"]


def main():
    ap = argparse.ArgumentParser(description="Публичная ссылка на калькулятор")
    ap.add_argument("--via", choices=list(PROVIDERS), help="конкретный канал")
    ap.add_argument("--list", action="store_true", help="проверить доступность каналов")
    a = ap.parse_args()

    if a.list:
        for n in ORDER:
            ok, why = ready(n)
            print(f"  {n:<12} {'да ' if ok else 'нет'}  {why}")
        return

    if not KEY_FILE.exists():
        sys.exit("Нет .guest-key — сначала запусти сервер: python serve.py")
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not server_alive(key):
        sys.exit(f"Локальный сервер не отвечает на {PORT}. Запусти start.bat.")

    for name in ([a.via] if a.via else ORDER):
        ok, why = ready(name)
        if not ok:
            print(f"→ {name}: пропускаю — {why}", flush=True)
            continue
        if run(name, key):
            return
    sys.exit("Ни один канал не поднялся. Посмотри `python share.py --list`.")


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    main()
