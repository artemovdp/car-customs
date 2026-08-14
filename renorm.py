"""Разовый пересчёт lot.json из сохранённого page.txt — после правок парсера."""
import sys, json, pathlib
from lot import normalize

sys.stdout.reconfigure(encoding="utf-8")
for lot in sys.argv[1:]:
    d = pathlib.Path("lots") / lot
    old = json.loads((d / "lot.json").read_text(encoding="utf-8"))
    n = normalize((d / "page.txt").read_text(encoding="utf-8"), lot, old["url"], old["photos"])
    (d / "lot.json").write_text(json.dumps(n, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{n['title']:24} | {n['odometer_km']:>7} км | "
          f"{n['damage_primary']} + {n['damage_secondary']} | {n['location']} | "
          f"ретейл ${n['est_retail']:.0f}")
