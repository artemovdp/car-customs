"""
Забирает данные лота и все фото с Copart через настоящий браузер.

Обычный HTTP-запрос к Copart ловит бот-защиту Akamai (отдаёт 200 и страницу
блокировки вместо JSON). Поэтому страница открывается в Chromium под Playwright,
а запросы к API идут изнутри самой страницы — с её сессией и origin.

    python lot.py https://www.copart.com/lot/60156236/...

Кладёт результат в  lots/<номер>/  :  lot.json, page.txt, raw_*.json, фото.
"""
import sys, os, re, json, pathlib, argparse
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright не установлен.  Выполни:\n"
             "  python -m pip install playwright\n"
             "  python -m playwright install chromium")

import requests

ROOT     = pathlib.Path(__file__).parent
LOTS_DIR = ROOT / "lots"
PROFILE  = ROOT / ".browser-profile"      # сессия и cookies Akamai живут здесь

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


class BlockedError(RuntimeError):
    """Copart не отдал карточку лота."""

# ── разбор ссылки ────────────────────────────────────────────────────────
def parse_url(url: str):
    url = url.strip().strip('"').strip("'")
    m = re.search(r"copart\.com/lot/(\d+)", url, re.I)
    if m:
        return "copart", m.group(1)
    m = re.search(r"iaai\.com/(?:VehicleDetail|Vehicle)/(\d+)", url, re.I)
    if m:
        return "iaai", m.group(1)
    m = re.search(r"(\d{8,9})", url)
    if m:
        return "copart", m.group(1)
    raise ValueError(f"Не могу вытащить номер лота из ссылки:\n  {url}")

# ── парсер видимого текста страницы ──────────────────────────────────────
def after(text: str, label: str, maxlines: int = 3):
    """Значение, идущее за меткой 'Label:' — на той же или следующих строках.

    Метка должна совпасть целиком или обрываться двоеточием, иначе пункт меню
    'Locations' перехватывает поиск 'Location' и возвращает хвост 's'.
    """
    lines = [l.strip() for l in text.splitlines()]
    lab = label.lower()
    for i, l in enumerate(lines):
        low = l.lower()
        if low == lab or low.startswith(lab + ":"):
            tail = l[len(label):].lstrip(": ").strip()
            if tail:
                return tail
            out = []
            for nxt in lines[i + 1:i + 1 + maxlines]:
                if not nxt or nxt.endswith(":"):
                    break
                out.append(nxt)
            if out:
                return " ".join(out).strip()
    return None

def num(s):
    if not s:
        return None
    m = re.search(r"-?[\d][\d,\s]*\.?\d*", str(s).replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "").replace(" ", ""))
    except ValueError:
        return None

FUEL_MAP = {"gas": "petrol", "gasoline": "petrol", "petrol": "petrol",
            "diesel": "diesel", "hybrid": "hybrid", "electric": "ev",
            "plug-in hybrid": "phev", "flexible fuel": "petrol"}

def normalize(text: str, lot: str, url: str, images: list):
    title = None
    for l in text.splitlines():
        l = l.strip()
        if re.match(r"^(19|20)\d{2}\s+\S", l):
            title = l
            break

    engine_raw = after(text, "Engine type")          # "2.0L 4 Listen to engine"
    engine_l   = None
    if engine_raw:
        engine_raw = re.sub(r"\s*Listen to engine\s*$", "", engine_raw).strip()
        m = re.search(r"([\d.]+)\s*L", engine_raw, re.I)
        if m:
            engine_l = float(m.group(1))

    fuel_raw = (after(text, "Fuel") or "").strip()
    fuel     = FUEL_MAP.get(fuel_raw.lower(), fuel_raw.lower() or None)

    odo_raw = after(text, "Odometer")                # "15,108 mi Actual"
    odo_mi  = num(odo_raw)

    year = None
    ym = re.match(r"^((19|20)\d{2})", title or "")
    if ym:
        year = int(ym.group(1))

    bid = None
    bm = re.search(r"Current bid[\s\S]{0,120}?\$([\d,]+(?:\.\d+)?)", text)
    if bm:
        bid = num(bm.group(1))

    lowtext = text.lower()
    return {
        "source":            "copart",
        "lot":               lot,
        "url":               url,
        "fetched_at":        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title":             title,
        "year":              year,
        "vin":               (after(text, "VIN") or "").strip() or None,
        "engine_raw":        engine_raw,
        "engine_l":          engine_l,
        "cylinders":         num(after(text, "Cylinders")),
        "fuel_raw":          fuel_raw or None,
        "fuel":              fuel,
        "transmission":      after(text, "Transmission"),
        "drivetrain":        after(text, "Drivetrain"),
        "body_style":        after(text, "Body style"),
        "color":             after(text, "Color"),
        "odometer_mi":       odo_mi,
        "odometer_km":       round(odo_mi * 1.609344) if odo_mi else None,
        "odometer_actual":   bool(odo_raw and "actual" in odo_raw.lower()),
        "title_code":        after(text, "Title code", 4),
        "damage_primary":    after(text, "Primary damage"),
        "damage_secondary":  after(text, "Secondary damage"),
        "est_retail":        num(after(text, "Estimated retail value")),
        "current_bid":       bid,
        "has_keys":          (after(text, "Has key") or "").strip().lower().startswith("y"),
        "runs_drives":       "run and drive" in lowtext,
        "engine_starts":     "engine starts" in lowtext,
        "trans_engages":     "transmission engages" in lowtext,
        "sale_date":         after(text, "Sale date"),
        "location":          after(text, "Location"),
        "seller":            after(text, "Seller"),
        "photos":            images,
    }

# ── загрузка ─────────────────────────────────────────────────────────────
def fetch(url: str, headless: bool = False):
    source, lot = parse_url(url)
    if source == "iaai":
        print("! IAAI пока поддержан частично: заберу текст страницы и фото из DOM,\n"
              "  но поля лота придётся проверить руками.")
    out = LOTS_DIR / lot
    out.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=headless, user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="en-US", args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print(f"→ открываю лот {lot} …")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("text=Lot number", timeout=25000)
        except Exception:
            print("! карточка лота не отрисовалась за 25 с — работаю с тем, что есть")
        page.wait_for_timeout(1500)

        text = page.inner_text("body")
        (out / "page.txt").write_text(text, encoding="utf-8")

        # Бот-защита не всегда рисует внятную ошибку: чаще отдаёт пустой каркас.
        # Признак живой карточки — номер лота в тексте.
        blocked = ("Access Denied" in text
                   or "NOINDEX, NOFOLLOW" in page.content()[:2000]
                   or "Lot number" not in text)
        if blocked:
            ctx.close()
            raise BlockedError(
                "Copart не отдал карточку лота — сработала бот-защита Akamai.\n"
                + ("Безголовый режим она режет всегда. Убери --headless.\n" if headless else
                   "Открой лот руками в этом же окне, пройди проверку — cookies\n"
                   "сохранятся в профиль, и следующий запуск пройдёт.\n"))

        # запросы к API — изнутри страницы, с её сессией
        def api(path):
            try:
                return page.evaluate(
                    """async (p) => {
                        const r = await fetch(p, {headers:{'Accept':'application/json'}});
                        if (!r.ok) return null;
                        try { return await r.json(); } catch(e) { return null; }
                    }""", path)
            except Exception:
                return None

        raw_img = api(f"/public/data/lotdetails/solr/lotImages/{lot}/USA")
        raw_det = api(f"/public/data/lotdetails/solr/{lot}")
        for name, blob in (("raw_images.json", raw_img), ("raw_details.json", raw_det)):
            if blob:
                (out / name).write_text(json.dumps(blob, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

        # список фото: из API, иначе из DOM
        shots = []
        content = (((raw_img or {}).get("data") or {}).get("imagesList") or {}).get("content") or []
        for it in content:
            u = it.get("highResUrl") or it.get("fullUrl")
            if not u or not u.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            shots.append({"seq": it.get("imageSeqNumber"),
                          "label": it.get("imageLabelCode") or "IMG", "url": u})
        if not shots:
            print("! API фото не ответил — собираю картинки из DOM")
            for u in page.eval_on_selector_all(
                    "img", "els => els.map(e => e.src)"):
                if "cs.copart.com" in u or "vis.iaai.com" in u:
                    shots.append({"seq": len(shots) + 1, "label": "IMG",
                                  "url": u.replace("_thb", "_hrs")})
        ctx.close()

    # фото качаем обычным requests — CDN бот-защитой не прикрыт
    photos = []
    for i, s in enumerate(shots, 1):
        name = f"{i:02d}_{s['label']}.jpg"
        dst  = out / name
        if not dst.exists():
            try:
                r = requests.get(s["url"], timeout=40, headers={"User-Agent": UA})
                r.raise_for_status()
                dst.write_bytes(r.content)
            except Exception as e:
                print(f"  ! {name}: {e}")
                continue
        photos.append({"n": i, "label": s["label"], "file": name})
    print(f"→ фото: {len(photos)} шт.")

    data = normalize(text, lot, url, photos)
    (out / "lot.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    (LOTS_DIR / "latest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    return data, out

def main():
    ap = argparse.ArgumentParser(description="Забрать лот Copart/IAAI")
    ap.add_argument("url")
    ap.add_argument("--headless", action="store_true",
                    help="без окна браузера — Copart это почти всегда режет, "
                         "оставлено для экспериментов")
    a = ap.parse_args()

    try:
        data, out = fetch(a.url, headless=a.headless)
    except BlockedError as e:
        sys.exit(f"\n{e}")
    print("─" * 58)
    for k in ("title", "vin", "year", "engine_raw", "fuel", "odometer_km",
              "damage_primary", "damage_secondary", "title_code",
              "runs_drives", "current_bid", "est_retail", "location"):
        print(f"  {k:18} {data.get(k)}")
    print("─" * 58)
    print(f"  папка: {out}")

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
