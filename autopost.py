"""Автопостинг у Threads з черги posts_queue.csv.

Команди:
  python autopost.py schedule      # розставити дати постам без дати по слотах POST_SLOTS
  python autopost.py run           # опублікувати один пост, час якого настав (для cron)
  python autopost.py loop          # працювати безперервно, перевіряючи чергу щохвилини
  python autopost.py status        # показати чергу і квоту публікацій
Додайте --dry-run, щоб нічого не публікувати, а лише показати, що буде.

Колонки CSV: id, scheduled_at, text, status, post_id, posted_at, attempts, error
  (необов'язкові) topic_tag, self_reply, self_reply_due, self_reply_id та будь-які ваші власні —
  скрипт їх зберігає при перезаписі файлу.
  topic_tag — тема поста в Threads (до 50 символів, без '.' і '&')
  self_reply — ваша перша відповідь під власним постом; виходить через SELF_REPLY_DELAY_MINUTES
  scheduled_at — "2026-11-27 09:00" за київським часом (TIMEZONE у .env)
  text — до 500 символів; довший текст стане ланцюжком (поділ по '---' або по абзацах)
  status — queued / posted / error / skip
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from threads_api import BASE_DIR, Threads, ThreadsError, load_env, refresh_token_if_needed, split_for_threads

load_env()
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Kyiv"))
QUEUE = BASE_DIR / os.environ.get("POST_QUEUE", "posts_queue.csv")
SLOTS = [s.strip() for s in os.environ.get("POST_SLOTS", "08:30,12:30,17:00,20:30").split(",") if s.strip()]
JITTER_MIN = int(os.environ.get("POST_JITTER_MINUTES", "15"))
MIN_GAP_MIN = int(os.environ.get("MIN_GAP_MINUTES", "90"))
MAX_PER_DAY = int(os.environ.get("MAX_POSTS_PER_DAY", "5"))
SELF_REPLY_DELAY = tuple(int(x) for x in os.environ.get("SELF_REPLY_DELAY_MINUTES", "3-10").split("-"))
MAX_ATTEMPTS = 3
FIELDS = ["id", "scheduled_at", "text", "status", "post_id", "posted_at", "attempts", "error",
          "topic_tag", "self_reply", "self_reply_due", "self_reply_id"]
_extra_fields: list[str] = []  # додаткові колонки з файлу (rubric тощо) — не губимо їх
FMT = "%Y-%m-%d %H:%M"


def read_queue() -> list[dict]:
    if not QUEUE.exists():
        sys.exit(f"Немає файлу черги {QUEUE}. Скопіюйте posts_queue.example.csv → posts_queue.csv")
    with QUEUE.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        _extra_fields[:] = [c for c in (reader.fieldnames or []) if c and c not in FIELDS]
    for i, r in enumerate(rows, 1):
        for k in FIELDS:
            r[k] = (r.get(k) or "").strip() if k != "text" else (r.get(k) or "").strip("﻿")
        r["id"] = r["id"] or str(i)
        r["status"] = r["status"] or "queued"
    return rows


def write_queue(rows: list[dict]) -> None:
    tmp = QUEUE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + _extra_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(QUEUE)


def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, FMT).replace(tzinfo=TZ)


def now() -> datetime:
    return datetime.now(TZ)


def cmd_schedule(rows: list[dict], dry: bool) -> None:
    """Дає дати постам без scheduled_at: наступні вільні слоти з випадковим зсувом ±JITTER."""
    taken = {parse_dt(r["scheduled_at"]).strftime("%Y-%m-%d %H") for r in rows if r["scheduled_at"]}
    day = now().date()
    slot_iter = []
    for d in range(0, 365):
        date = day + timedelta(days=d)
        for s in SLOTS:
            h, m = map(int, s.split(":"))
            base = datetime(date.year, date.month, date.day, h, m, tzinfo=TZ)
            if base > now() + timedelta(minutes=10) and base.strftime("%Y-%m-%d %H") not in taken:
                slot_iter.append(base)
    it = iter(slot_iter)
    n = 0
    for r in rows:
        if r["status"] == "queued" and not r["scheduled_at"]:
            slot = next(it) + timedelta(minutes=random.randint(-JITTER_MIN, JITTER_MIN))
            r["scheduled_at"] = slot.strftime(FMT)
            n += 1
            print(f"#{r['id']}: {r['scheduled_at']}  {r['text'][:60]!r}")
    if not dry:
        write_queue(rows)
    print(f"Розставлено дат: {n}")


def posted_today(rows: list[dict]) -> list[datetime]:
    out = []
    for r in rows:
        if r["status"] == "posted" and r["posted_at"]:
            t = parse_dt(r["posted_at"])
            if t and t.date() == now().date():
                out.append(t)
    return out


def run_self_replies(rows: list[dict], dry: bool) -> None:
    """Публікує відкладені авторські відповіді (self_reply), час яких настав."""
    due = [r for r in rows if r["status"] == "posted" and r["self_reply"] and r["post_id"]
           and not r["self_reply_id"] and r["self_reply_due"] and parse_dt(r["self_reply_due"]) <= now()]
    if not due:
        return
    api = None if dry else Threads()
    for r in due:
        print(f"Авторська відповідь під #{r['id']}: {r['self_reply']}")
        if dry:
            continue
        try:
            r["self_reply_id"] = api.publish_text(r["self_reply"], reply_to_id=r["post_id"])
        except ThreadsError as e:
            print("  помилка:", e)
            r["self_reply_id"] = "error"
            r["error"] = f"self_reply: {str(e)[:250]}"
    if not dry:
        write_queue(rows)


def cmd_run(rows: list[dict], dry: bool) -> bool:
    """Публікує ОДИН пост, час якого настав. Повертає True, якщо щось опубліковано."""
    refresh_token_if_needed()
    if dry:
        try:
            me = Threads().me()
            print(f"Токен дійсний: акаунт @{me.get('username')}")
            print("Квота:", Threads().publishing_limit())
        except ThreadsError as e:
            print("Токен не працює:", e)
            raise SystemExit(1)
    run_self_replies(rows, dry)
    today = posted_today(rows)
    if len(today) >= MAX_PER_DAY:
        print(f"Ліміт {MAX_PER_DAY} постів на сьогодні вичерпано.")
        return False
    last = max(today, default=None)
    if last and now() - last < timedelta(minutes=MIN_GAP_MIN):
        print(f"Останній пост був о {last:%H:%M}; чекаємо мінімум {MIN_GAP_MIN} хв між постами.")
        return False

    due = [
        r for r in rows
        if r["status"] in ("queued", "error")
        and int(r["attempts"] or 0) < MAX_ATTEMPTS
        and r["scheduled_at"] and parse_dt(r["scheduled_at"]) <= now()
        and r["text"]
    ]
    if not due:
        print("Немає постів, час яких настав.")
        return False
    due.sort(key=lambda r: parse_dt(r["scheduled_at"]))
    row = due[0]
    parts = split_for_threads(row["text"])
    tag = f" [тема: {row['topic_tag']}]" if row["topic_tag"] else ""
    print(f"Публікую #{row['id']} ({len(parts)} част.){tag}:\n" + "\n---\n".join(parts))
    if dry:
        print("[dry-run] нічого не опубліковано")
        return False

    api = Threads()
    row["attempts"] = str(int(row["attempts"] or 0) + 1)
    try:
        ids = api.publish_chain(parts, topic_tag=row["topic_tag"] or None)
        row.update(status="posted", post_id=ids[0], posted_at=now().strftime(FMT), error="")
        if row["self_reply"]:
            due = now() + timedelta(minutes=random.randint(*SELF_REPLY_DELAY))
            row["self_reply_due"] = due.strftime(FMT)
            print(f"Авторська відповідь вийде о {row['self_reply_due']}")
        try:
            print("Опубліковано:", api.permalink(ids[0]))
        except ThreadsError:
            print("Опубліковано, id:", ids[0])
    except ThreadsError as e:
        row.update(status="error", error=str(e)[:300])
        print("Помилка:", e)
    write_queue(rows)
    return row["status"] == "posted"


def cmd_status(rows: list[dict]) -> None:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("Черга:", counts)
    upcoming = sorted((r for r in rows if r["status"] == "queued" and r["scheduled_at"]), key=lambda r: r["scheduled_at"])[:10]
    for r in upcoming:
        print(f"  {r['scheduled_at']}  #{r['id']}  {r['text'][:70]!r}")
    no_date = sum(1 for r in rows if r["status"] == "queued" and not r["scheduled_at"])
    if no_date:
        print(f"Без дати: {no_date} (запустіть: python autopost.py schedule)")
    try:
        print("Квота Threads:", Threads().publishing_limit())
    except ThreadsError as e:
        print("Квоту не отримано:", e)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["schedule", "run", "loop", "status"])
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.command == "schedule":
        cmd_schedule(read_queue(), args.dry_run)
    elif args.command == "run":
        cmd_run(read_queue(), args.dry_run)
    elif args.command == "status":
        cmd_status(read_queue())
    else:
        print("Режим loop: Ctrl+C для зупинки.")
        while True:
            try:
                cmd_run(read_queue(), args.dry_run)
            except Exception as e:  # не падаємо назавжди через одну помилку мережі
                print("Помилка циклу:", e)
            time.sleep(60)


if __name__ == "__main__":
    main()
