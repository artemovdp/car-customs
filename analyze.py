"""
Разбор фотографий лота через Claude Code.

    python analyze.py 60156236

Работает по подписке Claude Code — отдельный API-ключ не нужен, но и работает
только пока запущен твой компьютер. Один лот обходится примерно в $0.6-0.7 и
занимает около трёх минут: снимки читаются по одному.

Кладёт разбор в lots/<номер>/analysis.json.
"""
import sys, json, re, subprocess, pathlib, shutil, argparse

ROOT = pathlib.Path(__file__).parent
LOTS = ROOT / "lots"

# Названия совпадают со строками сметы в index.html — по ним ставятся галочки.
PARTS = ["Бампер передний", "Фара", "Крыло переднее", "Капот", "Решётка радиатора",
         "Радиатор", "Верхняя панель передка", "Бампер задний", "Фонарь задний",
         "Дверь багажника", "Дверь", "Крыло заднее", "Порог", "Зеркало",
         "Подушки и блок SRS"]

PROMPT = """Ты оцениваешь битый автомобиль с аукциона Copart по фотографиям.

Прочитай ВСЕ .jpg в папке {folder}. Имена содержат ракурс:
DSFA/DSRA перед/зад слева, PSFA/PSRA перед/зад справа, DIRF/DIRR перед/зад прямо,
CKPT салон, ODOM приборка, ENGN моторный отсек, DENT повреждение, VINS VIN.

Верни СТРОГО один JSON без markdown и пояснений:
{{
 "impact": "куда пришёлся удар, 1 фраза",
 "airbags": "deployed" | "intact" | "unclear",
 "airbags_why": "по чему видно, 1 фраза",
 "structure": "visible_ok" | "visible_damaged" | "not_visible",
 "structure_why": "1 фраза",
 "zones": ["front" и/или "rear" и/или "side" и/или "mech"],
 "damaged": ["названия деталей, которые ПОСТРАДАЛИ и требуют ремонта или замены"],
 "intact": ["названия деталей из списка, которые ЦЕЛЫ"],
 "notes": ["важное и то, чего на фото не видно, до 4 пунктов"]
}}

Список названий, из которого выбирать для damaged и intact (дословно):
{parts}.

Каждую деталь помещай ровно в один список. Если по фото не понять — не включай
никуда: неуверенность честнее выдуманной оценки. В zones добавляй только те
стороны кузова, где есть реальные повреждения, а не то, что написано в карточке
лота — карточка часто помечает Side из-за переднего крыла."""


def extract_json(text):
    """Модель иногда оборачивает ответ в ```json, иногда нет."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if m:
        t = m.group(1)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("в ответе нет JSON")
    return json.loads(t[i:j + 1])


def analyze(lot, model="sonnet"):
    folder = LOTS / str(lot)
    shots = sorted(folder.glob("*.jpg"))
    if not shots:
        raise FileNotFoundError(f"нет фотографий в {folder} — сначала загрузи лот")
    if not shutil.which("claude"):
        raise RuntimeError("Claude Code не найден в PATH. Поставь его или используй "
                           "разбор в чате.")

    prompt = PROMPT.format(folder=f"lots/{lot}/", parts=", ".join(PARTS))
    p = subprocess.run(
        ["claude", "-p", prompt, "--allowedTools", "Read", "Glob",
         "--model", model, "--output-format", "json"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "claude вернул ошибку")[:400])

    outer = json.loads(p.stdout)
    if outer.get("is_error"):
        raise RuntimeError(str(outer.get("result"))[:400])

    data = extract_json(outer["result"])
    data["cost_usd"] = round(outer.get("total_cost_usd") or 0, 3)
    data["photos"]   = len(shots)
    data["model"]    = model
    (folder / "analysis.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def main():
    ap = argparse.ArgumentParser(description="Разобрать фото лота через Claude Code")
    ap.add_argument("lot")
    ap.add_argument("--model", default="sonnet", help="sonnet (по умолчанию) или opus")
    a = ap.parse_args()

    print(f"→ читаю фото лота {a.lot}, это займёт пару минут …", flush=True)
    d = analyze(a.lot, a.model)
    print("─" * 58)
    print(f"  удар       {d.get('impact')}")
    print(f"  подушки    {d.get('airbags')} — {d.get('airbags_why')}")
    print(f"  силовая    {d.get('structure')} — {d.get('structure_why')}")
    print(f"  зоны       {', '.join(d.get('zones') or [])}")
    print(f"  пострадало {', '.join(d.get('damaged') or []) or '—'}")
    print(f"  целое      {', '.join(d.get('intact') or []) or '—'}")
    for n in d.get("notes") or []:
        print(f"  · {n}")
    print("─" * 58)
    print(f"  {d['photos']} фото · ${d['cost_usd']}")


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    main()
