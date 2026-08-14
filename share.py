"""
Публичная ссылка на калькулятор, пока включён компьютер.

    python share.py

Поднимает туннель Cloudflare к локальному серверу и печатает ссылку с ключом.
Белый IP и настройка роутера не нужны — туннель соединяется наружу сам,
поэтому работает и за NAT провайдера.

Что важно понимать про эту ссылку:

* Она даёт **полный** доступ: чужой человек может грузить лоты и запускать
  разбор фотографий. Разбор идёт с твоей подписки Claude, окно Chromium при
  загрузке лота открывается на твоём экране.
* Ключ уходит дальше, чем его дают. Смена ключа — удалить `.access-key`
  и перезапустить сервер; все старые ссылки сразу перестанут работать.
* Пока скрипт не запущен, снаружи не видно ничего: сервер слушает только
  127.0.0.1.
"""
import re, sys, shutil, pathlib, subprocess

ROOT = pathlib.Path(__file__).parent
KEY_FILE = ROOT / ".access-key"
PORT = 8731
URL_RE = re.compile(r"https://[-\w]+\.trycloudflare\.com")

INSTALL = """cloudflared не найден. Поставь одним из способов:

  winget install --id Cloudflare.cloudflared
  choco install cloudflared

или скачай exe: https://github.com/cloudflare/cloudflared/releases"""


def main():
    if not KEY_FILE.exists():
        sys.exit("Нет .access-key — сначала запусти сервер: python serve.py")
    key = KEY_FILE.read_text(encoding="utf-8").strip()

    try:
        import urllib.request
        urllib.request.urlopen(f"http://localhost:{PORT}/?k={key}", timeout=5).read(1)
    except Exception:
        sys.exit(f"Локальный сервер не отвечает на {PORT}. Запусти start.bat.")

    exe = shutil.which("cloudflared")
    if not exe:
        sys.exit(INSTALL)

    print("Поднимаю туннель, это несколько секунд…\n", flush=True)
    p = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)

    shown = False
    try:
        for line in p.stdout:
            m = URL_RE.search(line)
            if m and not shown:
                shown = True
                link = f"{m.group(0)}/?k={key}"
                print("─" * 72)
                print("  Ссылка для других людей:")
                print(f"  {link}")
                print("─" * 72)
                print("  Работает, пока открыто это окно и включён компьютер.")
                print("  Разбор фотографий у них идёт с ТВОЕЙ подписки Claude,")
                print("  окно Chromium при загрузке лота откроется на ТВОЁМ экране.")
                print("  Закрыть доступ — Ctrl+C. Сменить ключ — удали .access-key")
                print("  и перезапусти сервер.")
                print("─" * 72, flush=True)
            elif not shown and ("ERR" in line or "error" in line.lower()):
                print(line.rstrip(), flush=True)
        p.wait()
    except KeyboardInterrupt:
        print("\nтуннель закрыт, снаружи больше не видно")
        p.terminate()


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    main()
