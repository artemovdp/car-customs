"""
Собирает цены с AUTO.RIA в локальную базу market.json.

    python market.py bmw x3               # собрать заново
    python market.py bmw x3 --recompute   # пересчитать из сохранённых объявлений

Ходит по страницам модели и складывает две вещи:

* `market.json`      — сводка для калькулятора: медианы по годам и шильдикам,
                       наклон цены по пробегу. Её грузит страница, поэтому
                       держим маленькой.
* `market-rows.json` — сами объявления. Страница их не читает, зато можно
                       пересчитать статистику под другим углом, не тревожа
                       AUTO.RIA заново (`--recompute`).

Важно понимать, что это за числа: цены ЗАПРОШЕНЫ продавцами, а не цены сделок.
Реальная продажа обычно на 5–10% ниже.
"""
import sys, re, json, time, pathlib, argparse

import requests

ROOT = pathlib.Path(__file__).parent
OUT  = ROOT / "market.json"
ROWS = ROOT / "market-rows.json"
UA   = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HEAD = {"User-Agent": UA, "Accept-Language": "uk,ru;q=0.9"}

MIN_N = 5          # меньше пяти объявлений — не статистика, а совпадение


# ── шильдик мотора ───────────────────────────────────────────────────────
def badge(modification):
    """«30e AT (292 к.с.) xDrive» → «30e».

    Шильдик BMW говорит о моторе больше, чем объём: 20d и 30i обе двухлитровые,
    а в цене расходятся на тысячи. M-версии выносим отдельно — это другой класс.
    """
    if not modification:
        return None
    m = re.search(r"\bM\s?(\d{2})\s?([id])\b", modification, re.I)
    if m:
        return "M" + m.group(1) + m.group(2).lower()
    m = re.search(r"\b(\d{2})\s?([iedx])\b", modification, re.I)
    return (m.group(1) + m.group(2).lower()) if m else None


def drive(modification):
    if not modification:
        return None
    low = modification.lower()
    return "xDrive" if "xdrive" in low else ("sDrive" if "sdrive" in low else None)


# ── парсер страницы выдачи ───────────────────────────────────────────────
def parse_page(html):
    out = []
    for c in re.split(r"(?=<section)", html):
        m = re.search(r'data-main-currency="USD"\s+data-main-price="(\d+)"', c)
        if not m:
            continue
        price = int(m.group(1))
        if price < 500 or price > 500_000:        # мусор и опечатки
            continue

        attr = lambda name: (re.search(rf'data-{name}="([^"]*)"', c) or [None, None])[1]
        mod  = attr("modification-name")

        y = re.search(r'title="[^"]*?\b((?:19|20)\d{2})\b[^"]*"', c)
        year = int(attr("year") or 0) or (int(y.group(1)) if y else None)

        km = None
        r = re.search(r"js-race.*?([\d\s ]+)\s*тис\.\s*км", c, re.S)
        if r:
            digits = re.sub(r"\D", "", r.group(1))
            if digits:
                km = int(digits) * 1000

        vol = None
        v = re.search(r"([\d.,]+)\s*л\.", c)
        if v:
            try: vol = float(v.group(1).replace(",", "."))
            except ValueError: pass

        f = re.search(r"(Бензин|Дизель|Гібрид|Електро|Газ)", c)

        vin = re.search(r'class="vin-code"[^>]*>\s*([A-HJ-NPR-Z0-9]{11,17})', c)
        vin = vin.group(1) if vin else None

        # AUTO.RIA сам помечает аварийные объявления — это и есть прямой
        # аналог отремонтированного пригона, без всяких коэффициентов.
        crashed = bool(re.search(r'class="state[^"]*"[^>]*>\s*Був в ДТП', c))
        dealer  = bool(re.search(r"Перевірений дилер|автосалон", c, re.I))
        desc    = re.search(r'class="descriptions-ticket">\s*<span>(.{0,300}?)<', c, re.S)
        d       = desc.group(1) if desc else ""
        # Происхождение берём по VIN, а не по словам в описании: у BMW X3
        # американская сборка — это 5UX, и это надёжнее любых «пригнана з США».
        usa     = bool(vin and vin[0] in "145") or \
                  bool(re.search(r"США|Америк|USA|Копарт|Copart|IAAI", d, re.I))

        out.append({"id": attr("id"), "price": price, "year": year, "km": km,
                    "vol": vol, "fuel": (f.group(1) if f else None),
                    "gen": attr("generation-name"), "mod": mod,
                    "badge": badge(mod), "drive": drive(mod),
                    "equip": attr("equipment-name"), "vin": vin,
                    "crashed": crashed, "dealer": dealer, "usa": usa})
    return out


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = (len(sorted_vals) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo))


def collect(brand, model, y_from, y_to, pages, pause):
    """Листает выдачу модели и раскладывает объявления по годам.

    Фильтр по году в ссылке этой страницей игнорируется, поэтому берём всё
    подряд и режем по году уже из разобранных данных.
    """
    base = f"https://auto.ria.com/uk/legkovie/{brand}/{model}/"
    rows, seen = [], 0
    for p in range(1, pages + 1):
        try:
            r = requests.get(f"{base}?page={p}", headers=HEAD, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"  ! стр.{p}: {e}")
            break
        items = parse_page(r.text)
        if not items:
            print(f"  стр.{p}: пусто — дальше объявлений нет")
            break
        seen += len(items)
        rows += [x for x in items
                 if x["year"] and y_from <= x["year"] <= y_to]
        print(f"  стр.{p}: +{len(items)} (в диапазоне всего {len(rows)})")
        time.sleep(pause)
    print(f"  просмотрено объявлений: {seen}")
    return rows


def stats(prices):
    ps = sorted(prices)
    if len(ps) < 3:                     # на двух объявлениях медианы нет
        return None
    return {"n": len(ps), "min": ps[0], "p25": pct(ps, .25),
            "median": pct(ps, .5), "p75": pct(ps, .75), "max": ps[-1]}


SLOPE_LO, SLOPE_HI = -0.006, -0.0002   # −0,6% и −0,02% цены за 1000 км
SHRINK = 30                            # «вес» общего наклона против годового


def ols(pts):
    """Наклон обычной прямой. Возвращает None, когда считать не из чего."""
    n = len(pts)
    if n < 8:
        return None
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    if den <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in pts) / den


def slope_of(cur, base):
    """Наклон в долях цены на 1000 км внутри одной выборки."""
    pts = [(r["km"] / 1000, r["price"] / base) for r in cur
           if r.get("km") and 0 < r["km"] < 500_000]
    b = ols(pts)
    return (b, len(pts)) if b is not None else (None, 0)


def pooled_slope(rows):
    """Общий наклон модели — считаем в долях от медианы своего года.

    В абсолютных долларах его считать нельзя: свежие дорогие годы перетянут
    наклон на себя. Служит опорой для годов с маленькой выборкой.
    """
    med = {}
    for y in {r["year"] for r in rows if r["year"]}:
        s = stats(r["price"] for r in rows if r["year"] == y)
        if s:
            med[y] = s["median"]
    pts = [(r["km"] / 1000, r["price"] / med[r["year"]]) for r in rows
           if r.get("km") and r["year"] in med and 0 < r["km"] < 500_000]
    b = ols(pts)
    return max(SLOPE_LO, min(SLOPE_HI, b)) if b is not None else -0.0017


def year_slope(cur, prior):
    """Наклон конкретного года, подтянутый к общему по размеру выборки.

    Внутри года пробег объясняет очень мало: R² держится в районе 0,05–0,35.
    Поэтому сырому годовому наклону верить нельзя — особенно свежим годам,
    где машин мало и разброс пробегов узкий, отчего прямая встаёт почти
    вертикально. Сжатие к общему наклону гасит эти выбросы, оставляя
    реальную разницу между «свежая быстро дешевеет» и «старая стоит на дне».
    """
    crashed = [r for r in cur if r["crashed"]]
    sub = crashed if len(crashed) >= 12 else cur
    s = stats(r["price"] for r in sub)
    if not s:
        return round(prior, 6)
    b, n = slope_of(sub, s["median"])
    if b is None:
        return round(prior, 6)
    b = max(SLOPE_LO, min(SLOPE_HI, b))
    return round((n * b + SHRINK * prior) / (n + SHRINK), 6)


TRIM_LO, TRIM_HI = 0.45, 2.5


def trim(rows):
    """Выбрасывает объявления с явно нерыночной ценой.

    В выдаче попадаются приманки вроде X3 2024 года с тысячей километров за
    $15 000 — это не рынок, а способ собрать звонки. Медиана к одиночке
    устойчива, но выборки по шильдику бывают в восемь машин, а наклон по
    пробегу считается наименьшими квадратами, которым любой выброс ломает
    прямую. Режем по границам от медианы своего года: настоящий разброс
    (20d против M50i) в них укладывается, подделки — нет.
    """
    out, dropped = [], []
    for y in {r["year"] for r in rows if r["year"]}:
        cur = [r for r in rows if r["year"] == y]
        s = stats(r["price"] for r in cur)
        if not s:
            out += cur
            continue
        lo, hi = s["median"] * TRIM_LO, s["median"] * TRIM_HI
        for r in cur:
            (out if lo <= r["price"] <= hi else dropped).append(r)
    out += [r for r in rows if not r["year"]]
    if dropped:
        print(f"  выброшено как нерыночное: {len(dropped)} — " +
              ", ".join(f"{r['year']} ${r['price']:,}" for r in sorted(
                  dropped, key=lambda r: r["price"])[:6]))
    return out


def bucket(cur):
    """Статистика по одной выборке: общая, целые, аварийные, американки."""
    all_ = stats(r["price"] for r in cur)
    if not all_:
        return None
    rec = dict(all_)
    kms = sorted(r["km"] for r in cur if r["km"])
    rec["km_median"] = pct(kms, .5) if kms else None
    for key, sel in (("clean",   lambda r: not r["crashed"]),
                     ("crashed", lambda r: r["crashed"]),
                     ("usa",     lambda r: r["usa"])):
        sub = [r for r in cur if sel(r)]
        s = stats(r["price"] for r in sub)
        if s:
            kk = sorted(r["km"] for r in sub if r["km"])
            s["km_median"] = pct(kk, .5) if kk else None
            rec[key] = s
    return rec


def summarize(rows):
    """Целые и аварийные считаются раздельно, внутри года — ещё и по шильдику.

    Мешать целые с аварийными нельзя: в выдаче X3 пометка «Був в ДТП» стоит
    примерно на двух третях объявлений, и общая медиана оказывается где-то
    посередине. Для отремонтированного пригона прямой аналог — именно
    аварийные, и никакого коэффициента к ним применять уже не нужно.

    Шильдик важен не меньше года: 20d, 30i и M40i одного года — это три
    разные машины по цене, а в общем ведре они усредняются в никуда.
    """
    prior = pooled_slope(rows)
    by_year = {}
    for y in sorted({r["year"] for r in rows if r["year"]}):
        cur = [r for r in rows if r["year"] == y]
        rec = bucket(cur)
        if not rec:
            continue
        rec["km_slope"] = year_slope(cur, prior)
        badges = {}
        for b in sorted({r["badge"] for r in cur if r["badge"]}):
            sub = [r for r in cur if r["badge"] == b]
            if len(sub) >= MIN_N:
                bb = bucket(sub)
                if bb:
                    badges[b] = bb
        if badges:
            rec["by_badge"] = badges
        by_year[str(y)] = rec
    return by_year


def load(path):
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except Exception: pass
    return {}


def save(key, brand, model, rows, collected):
    # Шильдик выводим заново на каждом сохранении: тогда правка разбора
    # чинит и уже собранные объявления, без похода на AUTO.RIA.
    for r in rows:
        r["badge"] = badge(r.get("mod"))
        r["drive"] = drive(r.get("mod"))
    kept = trim(rows)
    db = load(OUT)
    db.setdefault("models", {})
    db["models"][key] = {
        "brand": brand.upper(), "model": model.upper(),
        "collected": collected, "total": len(kept), "raw": len(rows),
        "km_slope": round(pooled_slope(kept), 6), "by_year": summarize(kept),
    }
    db["note"] = ("Цены запрошены продавцами на AUTO.RIA, а не цены сделок: "
                  "реальная продажа обычно на 5–10% ниже. Сами объявления "
                  "лежат в market-rows.json.")
    OUT.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")

    raw = load(ROWS)
    raw.setdefault("models", {})
    raw["models"][key] = {"collected": collected, "rows": rows}
    ROWS.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return db["models"][key]


def main():
    ap = argparse.ArgumentParser(description="Собрать цены AUTO.RIA в market.json")
    ap.add_argument("brand"); ap.add_argument("model")
    ap.add_argument("--from", dest="y_from", type=int, default=2018)
    ap.add_argument("--to",   dest="y_to",   type=int, default=2026)
    ap.add_argument("--pages", type=int, default=50, help="сколько страниц листать")
    ap.add_argument("--pause", type=float, default=1.0, help="пауза между запросами, с")
    ap.add_argument("--recompute", action="store_true",
                    help="пересчитать из market-rows.json, не ходя в сеть")
    a = ap.parse_args()

    key = f"{a.brand.lower()}/{a.model.lower()}"
    if a.recompute:
        saved = load(ROWS).get("models", {}).get(key)
        if not saved:
            sys.exit(f"{key} нет в market-rows.json — сначала собери без --recompute")
        rows, collected = saved["rows"], saved["collected"]
        print(f"→ пересчитываю {key} из {len(rows)} сохранённых объявлений")
    else:
        print(f"→ собираю {key}, {a.y_from}–{a.y_to}")
        rows = collect(a.brand.lower(), a.model.lower(),
                       a.y_from, a.y_to, a.pages, a.pause)
        collected = time.strftime("%Y-%m-%d")
        if not rows:
            sys.exit("Ничего не собралось. Проверь написание марки и модели в ссылке "
                     f"https://auto.ria.com/uk/legkovie/{a.brand}/{a.model}/")

    rec = save(key, a.brand, a.model, rows, collected)
    if not rec["by_year"]:
        sys.exit("Объявления есть, а статистики нет — проверь диапазон лет.")

    print(f"\n  объявлений {rec['total']} · общий наклон по пробегу "
          f"{rec['km_slope']*100:.3f}% за 1000 км")
    print(f"  {'год':>5} {'всего':>6} {'битые':>8} {'целые':>8} {'км/10т':>7}  шильдики")
    for y, r in rec["by_year"].items():
        cr = r.get("crashed", {}).get("median")
        cl = r.get("clean", {}).get("median")
        bl = " ".join(f"{b}:{v['n']}" for b, v in (r.get("by_badge") or {}).items())
        print(f"  {y:>5} {r['n']:>6} {('$%d' % cr) if cr else '—':>8} "
              f"{('$%d' % cl) if cl else '—':>8} {r['km_slope']*1000:>6.1f}%  {bl}")


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    main()
