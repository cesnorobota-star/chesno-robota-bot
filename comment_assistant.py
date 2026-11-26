"""Асистент коментарів для Threads: пошук → чернетки від ШІ → ВАШЕ підтвердження → публікація.

Нічого не публікується без вашого рішення в команді `review`.

Команди:
  python comment_assistant.py find       # знайти популярні треди за ключовими словами
  python comment_assistant.py add        # додати пост вручну (вставити текст і посилання)
  python comment_assistant.py draft      # згенерувати 2–3 варіанти коментаря для кожного
  python comment_assistant.py review     # переглянути й схвалити / відредагувати / пропустити
  python comment_assistant.py send       # опублікувати схвалені з обмеженням частоти
  python comment_assistant.py list       # що зараз у роботі

Пошук чужих постів працює лише після того, як Meta схвалить дозвіл threads_keyword_search
(App Review). До того використовуйте `add` і публікуйте схвалений текст вручну.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from threads_api import BASE_DIR, MAX_POST_CHARS, normalize_dashes, Threads, ThreadsError, load_env, refresh_token_if_needed

load_env()
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Kyiv"))
DATA = BASE_DIR / "data"
CANDIDATES = DATA / "candidates.json"
LOG = DATA / "comments_log.csv"

KEYWORDS = [k.strip() for k in os.environ.get(
    "COMMENT_KEYWORDS",
    "відгуки про роботу,відгук про роботодавця,співбесіда,роботодавець,звільнився,перша робота,"
    "пошук роботи,тестове завдання,HR,зарплата",
).split(",") if k.strip()]
# Теми, яких не торкаємось взагалі (пост пропускається ще до ШІ)
STOP_WORDS = [w.strip().lower() for w in os.environ.get(
    "COMMENT_STOP_WORDS",
    "війн,фронт,мобіліз,тцк,зсу,окупант,обстріл,загинув,загибл,помер,смерть,похорон,політи,вибори,"
    "президент,верховна рада,депутат,зеленськ,путін,росія,росії,релігі,церкв,самогуб,суїцид,онкол,рак ",
).split(",") if w.strip()]
SEARCH_DAYS = int(os.environ.get("SEARCH_DAYS", "3"))
PER_KEYWORD = int(os.environ.get("SEARCH_LIMIT_PER_KEYWORD", "15"))
MAX_PER_HOUR = int(os.environ.get("MAX_COMMENTS_PER_HOUR", "4"))
MAX_PER_DAY = int(os.environ.get("MAX_COMMENTS_PER_DAY", "15"))
DELAY_RANGE = (
    int(os.environ.get("COMMENT_DELAY_MIN_SECONDS", "240")),
    int(os.environ.get("COMMENT_DELAY_MAX_SECONDS", "600")),
)
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
BRAND = os.environ.get("BRAND_NAME", "Чесно Робота")

SYSTEM_PROMPT = f"""Ти пишеш коментарі українською мовою від імені акаунта «{BRAND}» у Threads.
Акаунт — про чесні відгуки щодо роботи: роботодавці, співбесіди, HR, зарплати, офісне життя.

Твоє завдання: до чужого посту запропонувати 2–3 різні варіанти короткого коментаря.

Тон:
- Якщо автор жартує або описує абсурдну робочу ситуацію — дотепний, іронічний, з легким сарказмом.
  Сарказм спрямований на ситуацію, кліше та «корпоративну мову» («дружній колектив», «ми як сім'я»,
  «конкурентна зарплата», «стресостійкість»), а НЕ на автора.
- Якщо автору важко (звільнили, вигорання, токсичний керівник, не беруть на роботу) — підтримка
  з теплим гумором або без гумору зовсім. Не знецінюй, не повчай.
- Провокативно = з влучним спостереженням, яке хочеться лайкнути чи відповісти, а не образливо.

Заборонено:
- політика, війна, мобілізація, релігія, національність, регіони, мова як тема суперечки;
- образи, лайка, приниження автора чи інших людей, коментарі про зовнішність, вік, стать, здоров'я, сім'ю;
- звинувачення конкретних компаній чи людей за назвою/іменем (наклеп), навіть якщо автор їх називає;
- посилання, хештеги, реклама, заклики підписатися, згадки про ШІ;
- вигадані факти й статистика.

Формат кожного варіанта: 1-3 речення, до 280 символів, жива розмовна українська, можна 0-1 емодзі.\nНіколи не використовуй довге тире (—), лише коротке (-).
Варіанти мають відрізнятися підходом (наприклад: жарт / іронічне спостереження / підтримка).

Якщо пост про заборонені теми, трагедію, або в ньому нема чого доречно сказати — поверни skip=true
і коротко поясни причину в skip_reason, а comments залиш порожнім."""

SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "skip_reason": {"type": "string"},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "style": {"type": "string", "enum": ["жарт", "іронія", "підтримка"]},
                    "text": {"type": "string"},
                },
                "required": ["style", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["skip", "skip_reason", "comments"],
    "additionalProperties": False,
}


# ---------- сховище ----------

def load() -> list[dict]:
    if CANDIDATES.exists():
        return json.loads(CANDIDATES.read_text(encoding="utf-8"))
    return []


def save(items: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CANDIDATES.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CANDIDATES)


def now() -> datetime:
    return datetime.now(TZ)


def has_stop_word(text: str) -> bool:
    low = f" {text.lower()} "
    return any(w in low for w in STOP_WORDS)


# ---------- find / add ----------

def cmd_find() -> None:
    refresh_token_if_needed()
    api = Threads()
    me = api.me().get("username", "")
    items = load()
    known = {i["id"] for i in items}
    since = (now() - timedelta(days=SEARCH_DAYS)).strftime("%Y-%m-%d")
    added = own_only = 0
    for kw in KEYWORDS:
        try:
            results = api.keyword_search(kw, search_type="TOP", limit=PER_KEYWORD, since=since)
        except ThreadsError as e:
            print(f"[{kw}] помилка пошуку: {e}")
            continue
        for p in results:
            if p.get("username") == me:
                own_only += 1
                continue
            if p["id"] in known or p.get("is_reply") or not p.get("text"):
                continue
            if has_stop_word(p["text"]):
                continue
            items.append({
                "id": p["id"], "username": p.get("username", ""), "text": p["text"],
                "permalink": p.get("permalink", ""), "timestamp": p.get("timestamp", ""),
                "keyword": kw, "source": "search", "status": "new",
            })
            known.add(p["id"])
            added += 1
        print(f"[{kw}] знайдено {len(results)}")
    save(items)
    print(f"Нових кандидатів: {added}")
    if added == 0 and own_only:
        print("Пошук повертає лише ваші пости — у застосунку ще нема схваленого threads_keyword_search."
              " Поки що використовуйте: python comment_assistant.py add")


def cmd_add() -> None:
    print("Вставте текст чужого посту. Завершіть порожнім рядком.")
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        sys.exit("Порожній текст.")
    link = input("Посилання на пост (необов'язково): ").strip()
    items = load()
    items.append({
        "id": f"manual-{int(time.time())}", "username": "", "text": text, "permalink": link,
        "timestamp": now().isoformat(), "keyword": "", "source": "manual", "status": "new",
    })
    save(items)
    print("Додано. Далі: python comment_assistant.py draft")


# ---------- draft ----------

def generate(text: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()  # ключ з ANTHROPIC_API_KEY
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # якщо модель відмовить — запит перезапуститься на резервній
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": f"Пост у Threads:\n\n<post>\n{text}\n</post>"}],
    )
    if resp.stop_reason == "refusal":
        return {"skip": True, "skip_reason": "модель відмовилась", "comments": []}
    raw = "".join(b.text for b in resp.content if b.type == "text")
    data = json.loads(raw)
    for c in data.get("comments", []):
        c["text"] = normalize_dashes(c["text"])
    data["comments"] = [c for c in data.get("comments", []) if 0 < len(c["text"]) <= MAX_POST_CHARS]
    return data


def cmd_draft() -> None:
    items = load()
    todo = [i for i in items if i["status"] == "new"]
    print(f"Генерую чернетки для {len(todo)} постів...")
    for it in todo:
        try:
            res = generate(it["text"])
        except Exception as e:
            print(f"  {it['id']}: помилка ШІ: {e}")
            continue
        if res.get("skip") or not res.get("comments"):
            it.update(status="ai_skipped", skip_reason=res.get("skip_reason", ""))
            print(f"  {it['id']}: пропущено ({it['skip_reason']})")
        else:
            it.update(status="drafted", drafts=res["comments"])
            print(f"  {it['id']}: {len(res['comments'])} варіанти")
        save(items)


# ---------- review ----------

def cmd_review() -> None:
    items = load()
    todo = [i for i in items if i["status"] == "drafted"]
    if not todo:
        print("Нема чернеток для перегляду. Спочатку: find/add, потім draft.")
        return
    for it in todo:
        print("\n" + "=" * 70)
        print(f"@{it['username']}  {it['permalink']}")
        print(it["text"][:800])
        print("-" * 70)
        for n, d in enumerate(it["drafts"], 1):
            print(f"[{n}] ({d['style']}) {d['text']}")
        print("Вибір: номер = схвалити | e = свій текст | r = згенерувати ще | s = пропустити | q = вийти")
        while True:
            choice = input("> ").strip().lower()
            if choice == "q":
                save(items)
                return
            if choice == "s":
                it["status"] = "rejected"
                break
            if choice == "r":
                res = generate(it["text"])
                if res.get("comments"):
                    it["drafts"] = res["comments"]
                    for n, d in enumerate(it["drafts"], 1):
                        print(f"[{n}] ({d['style']}) {d['text']}")
                else:
                    print("ШІ радить пропустити:", res.get("skip_reason"))
                continue
            if choice == "e":
                text = input("Ваш коментар: ").strip()
            elif choice.isdigit() and 1 <= int(choice) <= len(it["drafts"]):
                text = it["drafts"][int(choice) - 1]["text"]
                edit = input("Enter — як є, або введіть виправлений текст: ").strip()
                text = edit or text
            else:
                print("Не зрозумів. Ще раз.")
                continue
            if not text or len(text) > MAX_POST_CHARS:
                print(f"Текст порожній або довший за {MAX_POST_CHARS} символів.")
                continue
            it.update(final_text=text, approved_at=now().isoformat())
            it["status"] = "approved" if it["source"] == "search" else "manual_ready"
            if it["status"] == "manual_ready":
                print("Цей пост додано вручну — скопіюйте текст і залиште коментар у застосунку Threads.")
            break
        save(items)
    print("\nПерегляд завершено. Далі: python comment_assistant.py send")


# ---------- send ----------

def read_log() -> list[dict]:
    if not LOG.exists():
        return []
    with LOG.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def append_log(row: dict) -> None:
    new = not LOG.exists()
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sent_at", "post_id", "username", "permalink", "reply_id", "text"])
        if new:
            w.writeheader()
        w.writerow(row)


def cmd_send(dry: bool) -> None:
    items = load()
    queue = [i for i in items if i["status"] == "approved"]
    manual = [i for i in items if i["status"] == "manual_ready"]
    for it in manual:
        print(f"[вручну] {it['permalink'] or '(без посилання)'}\n   {it['final_text']}")
    if not queue:
        print("Нема схвалених коментарів для автопублікації.")
        return
    refresh_token_if_needed()
    api = None if dry else Threads()
    for it in queue:
        log = read_log()
        times = [datetime.fromisoformat(r["sent_at"]) for r in log]
        last_hour = [t for t in times if now() - t < timedelta(hours=1)]
        today = [t for t in times if t.date() == now().date()]
        if len(today) >= MAX_PER_DAY:
            print(f"Денний ліміт {MAX_PER_DAY} коментарів вичерпано. Решта — завтра.")
            break
        if len(last_hour) >= MAX_PER_HOUR:
            print(f"Ліміт {MAX_PER_HOUR}/год вичерпано. Запустіть send пізніше.")
            break
        if any(r["username"] == it["username"] and r["sent_at"][:10] == now().date().isoformat() for r in log):
            print(f"@{it['username']}: вже коментували сьогодні — відкладаю.")
            continue
        if times:
            wait = random.randint(*DELAY_RANGE) - (now() - max(times)).total_seconds()
            if wait > 0:
                print(f"Пауза {int(wait)} с, щоб коментарі не йшли пачкою...")
                if not dry:
                    time.sleep(wait)
        print(f"→ @{it['username']}: {it['final_text']}")
        if dry:
            print("  [dry-run] не опубліковано")
            continue
        try:
            reply_id = api.publish_text(it["final_text"], reply_to_id=it["id"])
        except ThreadsError as e:
            it.update(status="send_error", error=str(e)[:300])
            print("  помилка:", e)
            save(items)
            continue
        it.update(status="sent", reply_id=reply_id, sent_at=now().isoformat())
        append_log({"sent_at": now().isoformat(), "post_id": it["id"], "username": it["username"],
                    "permalink": it["permalink"], "reply_id": reply_id, "text": it["final_text"]})
        save(items)
        print("  опубліковано")


def cmd_list() -> None:
    counts: dict[str, int] = {}
    for i in load():
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    print(counts or "Порожньо.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["find", "add", "draft", "review", "send", "list"])
    p.add_argument("--dry-run", action="store_true", help="для send: показати, що буде, без публікації")
    args = p.parse_args()
    {"find": cmd_find, "add": cmd_add, "draft": cmd_draft, "review": cmd_review,
     "list": cmd_list, "send": lambda: cmd_send(args.dry_run)}[args.command]()


if __name__ == "__main__":
    main()
