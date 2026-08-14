"""
Отчёт по лоту в PDF: план против факта.

    python report.py 57314226

Собирает всё, что известно о машине, в один документ с пустой колонкой
«факт» — её заполняешь по счетам, когда машина приедет и встанет в сервис.
Тогда видно построчно, где вылезли и где сэкономили, а не «в целом дороже».

Данные берутся из уже сохранённого:
    lots/<лот>/lot.json        карточка Copart
    lots/<лот>/analysis.json   разбор фотографий
    journal.json               прогноз из калькулятора и факты

Смета в журнал попадает построчно, когда в калькуляторе нажимаешь
«Записать в журнал». Если её там нет, отчёт всё равно соберётся — просто
без разбивки по позициям.

PDF печатает Chrome в фоне; рядом остаётся .html, его можно открыть и
поправить руками.
"""
import sys, json, time, shutil, pathlib, argparse, subprocess

ROOT = pathlib.Path(__file__).parent
LOTS = ROOT / "lots"
OUT  = ROOT / "reports"

CHROME = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
          r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
          "google-chrome", "chromium", "chrome"]

ZONES = {"front": "перёд", "rear": "зад", "side": "бок",
         "srs": "подушки", "mech": "агрегаты", "any": "общее"}


def money(v, sign=False):
    if v is None:
        return "—"
    s = f"{abs(v):,.0f}".replace(",", " ")
    if sign:
        return ("−$" if v < 0 else "+$") + s
    return ("−$" if v < 0 else "$") + s


def load(p, default=None):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


# ── расчёт: та же формула, что в калькуляторе ────────────────────────────
def compute(bid, c):
    """c — словарь расходов и курсов, как его кладёт калькулятор."""
    usd, eur, pm = c.get("usd") or 1, c.get("eur") or 1, c.get("pm") or 3328
    e2u = eur / usd
    vol, year = c.get("vol") or 2.0, c.get("year") or 2020
    age = max(1, min(15, time.localtime().tm_year - year))

    fees = (round(min(bid * .06, 1000) + 400) if c.get("autofee")
            else (c.get("fees") or 0))
    freight = c.get("freight") or 0
    cv = bid + fees + freight
    duty = cv * (c.get("origin") or .10)
    exE = 50 * vol * age
    exU = exE * e2u
    vat = .20 * (cv + duty + exU)
    uah = cv * usd
    rate = .03 if uah <= 165 * pm else (.04 if uah <= 290 * pm else .05)
    pens = cv * rate
    other = ((c.get("broker") or 0) + (c.get("agent") or 0) +
             (c.get("transfer") or 0) + (c.get("delivery") or 0) +
             (c.get("regfee") or 0) / usd)
    return {"bid": bid, "fees": fees, "freight": freight, "cv": cv, "uah": uah,
            "duty": duty, "exE": exE, "exU": exU, "vat": vat, "age": age,
            "rate": rate, "pens": pens, "other": other, "usd": usd,
            "regfee_uah": c.get("regfee") or 0,
            "total": cv + duty + exU + vat + pens + other}


def row(label, plan, note="", cls=""):
    """Строка таблицы: план, пустая клетка под факт, пустая под разницу."""
    return (f'<tr class="{cls}"><td>{label}'
            f'{f"<span class=n>{note}</span>" if note else ""}</td>'
            f'<td class="num">{money(plan) if plan is not None else ""}</td>'
            f'<td class="fill"></td><td class="fill"></td></tr>')


def build(lot, args):
    d = load(LOTS / lot / "lot.json") or {}
    a = load(LOTS / lot / "analysis.json") or {}
    j = (load(ROOT / "journal.json", {}) or {}).get("lots", {}).get(lot, {})
    p, act = j.get("predicted", {}), j.get("actual", {})
    auc = j.get("auction", {})

    bid = args.bid or act.get("bought") or auc.get("sold") or p.get("bid") or 0
    costs = dict(p.get("costs") or {})
    if not costs:
        sys.exit("В журнале нет расходов по этому лоту.\n"
                 "Открой лот в калькуляторе и нажми «Записать в журнал».")
    if d.get("year"):
        costs.setdefault("year", d["year"])
    if d.get("engine_l"):
        costs.setdefault("vol", d["engine_l"])

    r = compute(bid, costs)
    items = p.get("items") or []
    repair = sum(i["price"] for i in items) or p.get("repair") or 0
    sale = p.get("sale") or 0

    # ── шапка машины ─────────────────────────────────────────────────────
    km = d.get("odometer_km")
    facts = [
        ("VIN", d.get("vin") or "—"),
        ("Пробег", f"{km:,} км".replace(",", " ") +
                   (" · подтверждён" if d.get("odometer_actual") else " · не подтверждён")
                   if km else "—"),
        ("Двигатель", f'{d.get("engine_raw","—")} · {d.get("fuel_raw","")}'.strip(" ·")),
        ("Title", d.get("title_code") or "—"),
        ("Повреждения по карточке",
         " + ".join(x for x in (d.get("damage_primary"), d.get("damage_secondary")) if x) or "—"),
        ("Локация", d.get("location") or "—"),
    ]
    fact_html = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)

    # ── разбор фото ──────────────────────────────────────────────────────
    STRUCT = {"visible_ok": "видно, что цела", "visible_damaged": "задета",
              "not_visible": "на фото не видно"}
    AIR = {"deployed": "сработали", "intact": "целые", "unclear": "не понять"}
    ai_html = ""
    if a:
        notes = "".join(f"<li>{n}</li>" for n in (a.get("notes") or []))
        ai_html = f"""
        <h2>Что показали фотографии</h2>
        <dl class="ai">
          <div><dt>Удар</dt><dd>{a.get('impact','—')}</dd></div>
          <div><dt>Подушки</dt><dd>{AIR.get(a.get('airbags'),'—')} — {a.get('airbags_why','')}</dd></div>
          <div><dt>Силовая</dt><dd><b>{STRUCT.get(a.get('structure'),'—')}</b> — {a.get('structure_why','')}</dd></div>
          <div><dt>Пострадало</dt><dd>{', '.join(a.get('damaged') or []) or '—'}</dd></div>
        </dl>
        {f'<ul class="notes">{notes}</ul>' if notes else ''}"""

    # ── расходы ──────────────────────────────────────────────────────────
    cost_rows = [
        row("Ставка на аукционе", r["bid"]),
        row("Сборы аукциона", r["fees"], "оценка от ставки" if costs.get("autofee") else ""),
        row("Фрахт США → Украина", r["freight"], "входит в таможенную базу"),
        row("<b>Таможенная стоимость</b>", r["cv"],
            f'{r["uah"]:,.0f} ₴'.replace(",", " "), "sub"),
        row(f'Пошлина {int((costs.get("origin") or .1)*100)}%', r["duty"]),
        row("Акциз", r["exU"], f'€{r["exE"]:,.0f} · {r["age"]} лет · от цены не зависит'.replace(",", " ")),
        row("НДС 20%", r["vat"], "от ТС + пошлина + акциз"),
        row(f'Пенсионный сбор {r["rate"]*100:.0f}%', r["pens"], "при первой регистрации"),
        row("Перевод денег на аукцион", costs.get("transfer")),
        row("Доставка по Украине", costs.get("delivery")),
        row("Брокер, сертификат", costs.get("broker")),
        row("Постановка на учёт", (r["regfee_uah"] / r["usd"]) if r["regfee_uah"] else None,
            f'{r["regfee_uah"]:,.0f} ₴'.replace(",", " ")),
        row("Посредник", costs.get("agent")),
        row("<b>Всего до ремонта</b>", r["total"], "", "grand"),
    ]

    # ── смета ────────────────────────────────────────────────────────────
    if items:
        rep_rows = "".join(
            row(i["name"], i["price"], f'{ZONES.get(i["zone"], "")} · вилка {i["lo"]}–{i["hi"]}')
            for i in items)
        rep_rows += row("<b>Итого ремонт</b>", repair, "", "grand")
    else:
        rep_rows = row("<b>Ремонт целиком</b>", repair,
                       "построчно не сохранено", "grand")

    # ── итог ─────────────────────────────────────────────────────────────
    full = r["total"] + repair
    diff = (sale - full) if sale else None
    verdict = (f'Такая же машина в Украине — <b>{money(sale)}</b>. '
               f'Разница <b>{money(diff, True)}</b>.') if sale else ""

    css = """
    @page{size:A4;margin:14mm 13mm 12mm}
    *{box-sizing:border-box}
    body{font:11px/1.5 "Segoe UI",system-ui,sans-serif;color:#1c2b28;margin:0}
    h1{font-size:17px;margin:0 0 2px}
    h2{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:#5d716c;
       margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid #cfd9d5;
       page-break-after:avoid}
    .head{display:flex;justify-content:space-between;align-items:flex-end;
          border-bottom:2px solid #0b6a68;padding-bottom:7px}
    .head .sub{color:#5d716c;font-size:11px}
    .head .when{font:10px ui-monospace,monospace;color:#7d908b;text-align:right}
    dl{display:grid;grid-template-columns:1fr 1fr;gap:2px 18px;margin:0}
    dl>div{display:flex;gap:8px;padding:2px 0;border-bottom:1px dotted #dde4e1}
    dt{color:#5d716c;min-width:130px;flex:0 0 130px}
    dd{margin:0;font-weight:600}
    dl.ai{grid-template-columns:1fr}
    dl.ai dt{flex:0 0 82px;min-width:82px}
    dl.ai dd{font-weight:400}
    table{width:100%;border-collapse:collapse;margin-top:2px}
    th{font:10px ui-monospace,monospace;text-transform:uppercase;letter-spacing:.08em;
       color:#5d716c;text-align:right;padding:3px 6px;border-bottom:1px solid #adbcb7}
    th:first-child{text-align:left}
    td{padding:3px 6px;border-bottom:1px solid #e8edeb;vertical-align:top}
    td.num{text-align:right;font:11px ui-monospace,monospace;white-space:nowrap}
    td.fill{width:74px;border-bottom:1px solid #adbcb7;background:#fafbfa}
    .n{display:block;font-size:9.5px;color:#7d908b;font-weight:400}
    tr.sub td{background:#eef3f1;font-weight:600}
    tr.grand td{background:#0b6a68;color:#fff;font-weight:700;border-bottom:none}
    tr.grand td.fill{background:#dfe8e5}
    .verdict{margin-top:10px;padding:9px 12px;background:#eef3f1;
             border-left:3px solid #0b6a68;font-size:12px}
    ul.notes{margin:6px 0 0;padding-left:16px;color:#41544f}
    ul.notes li{margin-bottom:2px}
    .tip{margin-top:8px;font-size:10px;color:#7d908b}
    .warn{border-left-color:#b8400b;background:#fdeee6}
    """

    return f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Лот {lot} — план и факт</title><style>{css}</style>
<div class="head">
  <div><h1>{d.get('title') or 'Лот ' + lot}</h1>
    <div class="sub">Лот {lot} · Copart{' · ' + (d.get('location') or '')}</div></div>
  <div class="when">отчёт {time.strftime('%d.%m.%Y')}<br>курс {r['usd']:.4f} ₴/$</div>
</div>

<h2>Машина</h2>
<dl>{fact_html}</dl>
{ai_html}

<h2>Расходы до ремонта</h2>
<table><thead><tr><th>Статья</th><th>План</th><th>Факт</th><th>Разница</th></tr></thead>
<tbody>{''.join(cost_rows)}</tbody></table>

<h2>Смета ремонта</h2>
<table><thead><tr><th>Позиция</th><th>План</th><th>Факт</th><th>Разница</th></tr></thead>
<tbody>{rep_rows}</tbody></table>

<h2>Итог</h2>
<table><tbody>
{row("<b>Всё вместе, с ремонтом</b>", full, "", "grand")}
</tbody></table>
<div class="verdict">{verdict}</div>
<div class="verdict warn">
  <b>Где обычно вылезает.</b> Силовая структура — её по фотографиям Copart не
  видно, и промер на стапеле делают уже здесь. Скрытые повреждения за смятой
  панелью. Датчики и проводка, которых не видно, пока не разобрали. Таможня
  может не принять инвойс и начислить по справочной базе.
</div>
<div class="tip">Колонку «факт» заполняй по счетам: инвойс аукциона, счёт
  перевозчика, декларация, счета сервиса и за запчасти. Потом
  <code>python journal.py fact {lot} --repair СУММА --customs СУММА</code> —
  и отчёт будет считать промах сметы по всем машинам сразу.</div>
</html>"""


def to_pdf(html_path, pdf_path):
    exe = next((c for c in CHROME
                if pathlib.Path(c).exists() or shutil.which(c)), None)
    if not exe:
        return False
    exe = exe if pathlib.Path(exe).exists() else shutil.which(exe)
    r = subprocess.run([exe, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf_path}", html_path.as_uri()],
                       capture_output=True, timeout=120)
    return pdf_path.exists()


def main():
    ap = argparse.ArgumentParser(description="Отчёт по лоту: план против факта")
    ap.add_argument("lot")
    ap.add_argument("--bid", type=float, help="ставка, если отличается от журнала")
    ap.add_argument("--html", action="store_true", help="только html, без pdf")
    a = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    html = OUT / f"{a.lot}.html"
    html.write_text(build(a.lot, a), encoding="utf-8")
    print(f"  {html}")

    if not a.html:
        pdf = OUT / f"{a.lot}.pdf"
        if to_pdf(html, pdf):
            print(f"  {pdf}  ({pdf.stat().st_size // 1024} КБ)")
        else:
            print("  Chrome не найден — PDF не сделал, открой html и печатай "
                  "в PDF из браузера.")


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    main()
