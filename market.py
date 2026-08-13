"""
Собирает цены с AUTO.RIA в локальную базу market.json.

    python market.py bmw x3 --from 2019 --to 2026

Ходит по страницам модели, по одному году за раз, и складывает статистику:
сколько объявлений, медиана, квартили. Калькулятор потом подставляет медиану
нужного года и вычитает скидку за аварийную историю.

Важно понимать, что это за числа: цены ЗАПРОШЕНЫ продавцами, а не цены сделок.
Реальная продажа обычно на 5–10% ниже. И это цены целых машин — скидку за
битую историю калькулятор вычитает отдельно.
"""
import sys, re, json, time, pathlib, argparse, statistics as st

import requests

ROOT = pathlib.Path(__file__).parent
OUT  = ROOT / "market.json"
UA   = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HEAD = {"User-Agent": UA, "Accept-Language": "uk,ru;q=0.9"}

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

        y = re.search(r'title="[^"]*?\b((?:19|20)\d{2})\b[^"]*"', c)
        year = int(y.group(1)) if y else None

        km = None
        r = re.search(r"js-race.*?([\d\s ]+)\s*тис\.\s*км", c, re.S)
        if r:
            digits = re.sub(r"\D", "", r.group(1))
            if digits:
                km = int(digits) * 1000

        vol = None
        v = re.search(r"([\d.,]+)\s*л\.", c)
        if v:
            try: vol = float(v.group(1).replace(",", "."))
            except ValueError: pass

        fuel = None
        f = re.search(r"(Бензин|Дизель|Гібрид|Електро|Газ)", c)
        if f:
            fuel = f.group(1)

        g = re.search(r'class="generation">\s*<span>([^<]+)</span>', c)

        out.append({"price": price, "year": year, "km": km, "vol": vol,
                    "fuel": fuel, "gen": (g.group(1).strip() if g else None)})
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


def summarize(rows):
    by_year = {}
    for y in sorted({r["year"] for r in rows if r["year"]}):
        ps = sorted(r["price"] for r in rows if r["year"] == y)
        kms = sorted(r["km"] for r in rows if r["year"] == y and r["km"])
        if len(ps) < 3:                 # на двух объявлениях медианы нет
            continue
        by_year[str(y)] = {
            "n": len(ps),
            "min": ps[0], "p25": pct(ps, .25), "median": pct(ps, .5),
            "p75": pct(ps, .75), "max": ps[-1],
            "km_median": pct(kms, .5) if kms else None,
        }
    return by_year


def main():
    ap = argparse.ArgumentParser(description="Собрать цены AUTO.RIA в market.json")
    ap.add_argument("brand"); ap.add_argument("model")
    ap.add_argument("--from", dest="y_from", type=int, default=2018)
    ap.add_argument("--to",   dest="y_to",   type=int, default=2026)
    ap.add_argument("--pages", type=int, default=3, help="страниц на каждый год")
    ap.add_argument("--pause", type=float, default=1.0, help="пауза между запросами, с")
    a = ap.parse_args()

    key = f"{a.brand.lower()}/{a.model.lower()}"
    print(f"→ собираю {key}, {a.y_from}–{a.y_to}")
    rows = collect(a.brand.lower(), a.model.lower(), a.y_from, a.y_to, a.pages, a.pause)
    by_year = summarize(rows)
    if not by_year:
        sys.exit("Ничего не собралось. Проверь написание марки и модели в ссылке "
                 f"https://auto.ria.com/uk/legkovie/{a.brand}/{a.model}/")

    db = {}
    if OUT.exists():
        try: db = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception: db = {}
    db.setdefault("models", {})
    db["models"][key] = {
        "brand": a.brand.upper(), "model": a.model.upper(),
        "collected": time.strftime("%Y-%m-%d"),
        "total": len(rows), "by_year": by_year,
    }
    db["note"] = ("Цены запрошены продавцами на AUTO.RIA, а не цены сделок: "
                  "реальная продажа обычно на 5–10% ниже. Это целые машины — "
                  "скидку за аварийную историю калькулятор вычитает отдельно.")
    OUT.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")

    print("─" * 54)
    print(f"  {'год':<6}{'шт':>5}{'p25':>10}{'медиана':>11}{'p75':>10}")
    for y, s in by_year.items():
        print(f"  {y:<6}{s['n']:>5}{s['p25']:>10}{s['median']:>11}{s['p75']:>10}")
    print("─" * 54)
    print(f"  записано: {OUT}  (всего {len(rows)} объявлений)")


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    main()
