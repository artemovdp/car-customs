"""
Журнал сделок: что я насчитал против того, что вышло на самом деле.

    python journal.py list                       что накопилось
    python journal.py auction 96271225 --sold 16500
    python journal.py fact 96271225 --bought 15000 --repair 8100 --sold 39000 --days 45
    python journal.py report                     насколько врут прогнозы
    python journal.py check                      подтянуть цены ухода с Copart

Смысл один: без записи фактов все коэффициенты калькулятора — скидка от
медианы, вилки в смете, потолок ставки — остаются моими догадками, и поправить
их нечем. Достаточно записывать даже те лоты, которые не покупал: цена ухода
с торгов сама по себе говорит, насколько потолок близок к рынку.

Прогноз кладёт сюда сам калькулятор (кнопка «Записать в журнал»), факты
дописываются этими командами.
"""
import sys, json, time, pathlib, argparse, subprocess, re

ROOT = pathlib.Path(__file__).parent
DB   = ROOT / "journal.json"

# что можно вписать командой fact: ключ → подсказка
FACTS = {
    "bought":  "за сколько купил на аукционе",
    "fees":    "сборы аукциона по инвойсу",
    "freight": "фрахт по инвойсу",
    "customs": "растаможка фактическая",
    "repair":  "ремонт фактический",
    "extra":   "прочее: сертификация, регистрация, посредник",
    "sold":    "за сколько продал",
    "days":    "сколько дней искал покупателя",
}


def load():
    if DB.exists():
        try: return json.loads(DB.read_text(encoding="utf-8"))
        except Exception: pass
    return {"lots": {}}


def write(db):
    DB.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def entry(db, lot):
    return db["lots"].setdefault(str(lot), {
        "lot": str(lot), "saved": time.strftime("%Y-%m-%d"),
        "predicted": {}, "auction": {}, "actual": {},
    })


def put_prediction(lot, data):
    """Кладёт прогноз калькулятора. Факты не трогает — их писали руками."""
    db = load()
    e  = entry(db, lot)
    for k in ("title", "year", "km", "vin", "url"):
        if data.get(k) is not None:
            e[k] = data[k]
    e["predicted"] = {k: data[k] for k in
                      ("repair", "sale", "ceiling", "market_median", "market_n",
                       "cut", "bid", "structure", "damaged", "items", "costs")
                      if data.get(k) is not None}
    e["predicted"]["date"] = time.strftime("%Y-%m-%d")
    write(db)
    return e


# ── цена ухода с торгов ──────────────────────────────────────────────────
SOLD_RE = [
    r"Sold\s*(?:for|amount|price)?[\s:$]*\$?\s*([\d,]+(?:\.\d+)?)",
    r"Winning\s*bid[\s:$]*\$?\s*([\d,]+(?:\.\d+)?)",
    r"Final\s*bid[\s:$]*\$?\s*([\d,]+(?:\.\d+)?)",
]


def sold_from_text(text):
    for rx in SOLD_RE:
        m = re.search(rx, text, re.I)
        if m:
            try: return float(m.group(1).replace(",", ""))
            except ValueError: pass
    return None


def check(lots, refetch=True):
    """Перечитывает страницу лота и ищет цену ухода.

    Copart показывает её только после торгов и не всегда одним и тем же
    словом, поэтому находится не всегда. Если не нашлось — не выдумываем,
    просто говорим об этом: цену можно вписать руками через `auction`.
    """
    db = load()
    todo = lots or [k for k, v in db["lots"].items()
                    if not v.get("auction", {}).get("sold")]
    if not todo:
        print("Нечего проверять — по всем записям цена ухода уже есть.")
        return

    for lot in todo:
        e = db["lots"].get(str(lot))
        if not e:
            print(f"{lot}: нет в журнале"); continue
        page = ROOT / "lots" / str(lot) / "page.txt"
        if refetch and e.get("url"):
            print(f"{lot}: перечитываю страницу…")
            try:
                subprocess.run([sys.executable, str(ROOT / "lot.py"), e["url"]],
                               cwd=str(ROOT), capture_output=True, timeout=330)
            except Exception as ex:
                print(f"  не вышло: {ex}")
        if not page.exists():
            print(f"  {lot}: нет page.txt"); continue
        got = sold_from_text(page.read_text(encoding="utf-8", errors="replace"))
        if got:
            e.setdefault("auction", {})["sold"] = got
            e["auction"]["checked"] = time.strftime("%Y-%m-%d")
            print(f"  {lot}: ушёл за ${got:,.0f}")
        else:
            print(f"  {lot}: цены ухода на странице нет — впиши руками: "
                  f"python journal.py auction {lot} --sold <сумма>")
    write(db)


# ── отчёт ────────────────────────────────────────────────────────────────
def med(v):
    v = sorted(x for x in v if x is not None)
    if not v: return None
    n = len(v)
    return v[n//2] if n % 2 else (v[n//2 - 1] + v[n//2]) / 2


def report():
    db = load()
    rows = list(db["lots"].values())
    if not rows:
        print("Журнал пуст. Нажми «Записать в журнал» в калькуляторе на любом лоте.")
        return

    print(f"{'лот':>10} {'машина':<24} {'потолок':>8} {'ушёл':>8} {'смета':>7} "
          f"{'ремонт':>7} {'прогноз':>8} {'продал':>8}")
    print("─" * 88)
    for e in sorted(rows, key=lambda r: r.get("saved", "")):
        p, a, au = e.get("predicted", {}), e.get("actual", {}), e.get("auction", {})
        f = lambda v: f"${v:,.0f}" if v else "—"
        print(f"{e['lot']:>10} {str(e.get('title', ''))[:24]:<24} "
              f"{f(p.get('ceiling')):>8} {f(au.get('sold')):>8} "
              f"{f(p.get('repair')):>7} {f(a.get('repair')):>7} "
              f"{f(p.get('sale')):>8} {f(a.get('sold')):>8}")

    print("\n── чему это учит ───────────────────────────────────────────")

    # 1. Потолок против цены ухода — работает даже без единой покупки.
    pairs = [(e["predicted"]["ceiling"], e["auction"]["sold"]) for e in rows
             if e.get("predicted", {}).get("ceiling") and e.get("auction", {}).get("sold")]
    if pairs:
        over = sum(1 for c, s in pairs if s > c)
        rel  = med([s / c for c, s in pairs])
        print(f"  Лотов с известной ценой ухода: {len(pairs)}")
        print(f"  Ушли дороже моего потолка: {over} из {len(pairs)}")
        print(f"  Медиана «цена ухода / потолок»: {rel:.2f}"
              + ("  — потолки занижены, такие лоты не выиграть" if rel > 1.15 else
                 "  — потолки в рынке" if rel > 0.9 else
                 "  — потолки завышены, можно торговаться агрессивнее"))
    else:
        print("  Цен ухода пока нет — их достаточно записывать даже по лотам, "
              "которые не покупал.")

    # 2. Смета против счёта из сервиса.
    rp = [(e["predicted"]["repair"], e["actual"]["repair"]) for e in rows
          if e.get("predicted", {}).get("repair") and e.get("actual", {}).get("repair")]
    if rp:
        k = med([a / p for p, a in rp])
        print(f"  Смета против факта, {len(rp)} шт.: медиана {k:.2f} "
              f"({'занижаю' if k > 1.05 else 'завышаю' if k < .95 else 'в точку'})")

    # 3. Цена продажи против медианы рынка — это и есть настоящая скидка.
    cut = [(e["predicted"]["market_median"], e["actual"]["sold"]) for e in rows
           if e.get("predicted", {}).get("market_median") and e.get("actual", {}).get("sold")]
    if cut:
        real = med([1 - s / m for m, s in cut]) * 100
        print(f"  Реальная скидка от медианы AUTO.RIA, {len(cut)} шт.: {real:.0f}% "
              f"— её и ставь в поле «скидка» вместо догадки")

    # 4. Деньги.
    done = [e for e in rows if e.get("actual", {}).get("sold")]
    if done:
        prof = []
        for e in done:
            a = e["actual"]
            spent = sum(a.get(k) or 0 for k in
                        ("bought", "fees", "freight", "customs", "repair", "extra"))
            if spent:
                prof.append(a["sold"] - spent)
        if prof:
            print(f"  Продано машин: {len(prof)}, медианная прибыль ${med(prof):,.0f}, "
                  f"худшая ${min(prof):,.0f}, лучшая ${max(prof):,.0f}")
        d = [e["actual"].get("days") for e in done]
        if any(d):
            print(f"  Медиана дней на продажу: {med(d):.0f}")
    else:
        print("  Проданных машин в журнале нет — прибыль считать не из чего.")


def show_list():
    db = load()
    if not db["lots"]:
        print("Журнал пуст.")
        return
    for e in sorted(db["lots"].values(), key=lambda r: r.get("saved", "")):
        p, a, au = e.get("predicted", {}), e.get("actual", {}), e.get("auction", {})
        state = ("продана" if a.get("sold") else
                 "куплена" if a.get("bought") else
                 "ушла мимо" if au.get("sold") else "прогноз")
        print(f"{e['lot']:>10}  {str(e.get('title',''))[:26]:<26} {e.get('saved','')}  "
              f"потолок ${p.get('ceiling') or 0:,.0f}  [{state}]")


def main():
    ap = argparse.ArgumentParser(description="Журнал прогнозов и фактов")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list",   help="короткий список записей")
    sub.add_parser("report", help="насколько врут прогнозы")

    c = sub.add_parser("check", help="подтянуть цены ухода со страниц лотов")
    c.add_argument("lots", nargs="*")
    c.add_argument("--no-refetch", action="store_true",
                   help="не ходить в Copart, читать сохранённый page.txt")

    au = sub.add_parser("auction", help="вписать цену ухода руками")
    au.add_argument("lot")
    au.add_argument("--sold", type=float, required=True)
    au.add_argument("--date")

    fa = sub.add_parser("fact", help="вписать фактические расходы и продажу")
    fa.add_argument("lot")
    for k, h in FACTS.items():
        fa.add_argument(f"--{k}", type=float, help=h)
    fa.add_argument("--note", help="что пошло не так или неожиданно всплыло")

    a = ap.parse_args()

    if a.cmd == "list":
        show_list()
    elif a.cmd == "report":
        report()
    elif a.cmd == "check":
        check(a.lots, refetch=not a.no_refetch)
    elif a.cmd == "auction":
        db = load(); e = entry(db, a.lot)
        e["auction"]["sold"] = a.sold
        e["auction"]["date"] = a.date or time.strftime("%Y-%m-%d")
        write(db)
        print(f"{a.lot}: цена ухода ${a.sold:,.0f} записана")
        cl = e.get("predicted", {}).get("ceiling")
        if cl:
            d = a.sold - cl
            print(f"  мой потолок был ${cl:,.0f} — "
                  + (f"ушёл дороже на ${d:,.0f}, мимо" if d > 0
                     else f"влезал с запасом ${-d:,.0f}"))
    elif a.cmd == "fact":
        db = load(); e = entry(db, a.lot)
        wrote = []
        for k in FACTS:
            v = getattr(a, k)
            if v is not None:
                e["actual"][k] = v
                wrote.append(f"{k}={v:,.0f}")
        if a.note:
            e["actual"]["note"] = a.note
            wrote.append("заметка")
        write(db)
        print(f"{a.lot}: записано — {', '.join(wrote) or 'ничего не передал'}")


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    main()
