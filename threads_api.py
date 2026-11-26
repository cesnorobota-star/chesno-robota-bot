"""Мінімальний клієнт офіційного Meta Threads API.

Документація: https://developers.facebook.com/docs/threads
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
API_BASE = os.environ.get("THREADS_API_BASE", "https://graph.threads.net/v1.0")
OAUTH_BASE = os.environ.get("THREADS_OAUTH_BASE", "https://graph.threads.net")
TOKEN_FILE = BASE_DIR / "data" / "token.json"
MAX_POST_CHARS = 500  # ліміт Threads на один пост


def load_env(path: Path = BASE_DIR / ".env") -> None:
    """Простий завантажувач .env (без зовнішніх бібліотек)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class ThreadsError(RuntimeError):
    pass


def _check(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise ThreadsError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code >= 400 or "error" in data:
        raise ThreadsError(f"HTTP {resp.status_code}: {json.dumps(data.get('error', data), ensure_ascii=False)}")
    return data


# ---------- токени ----------

def save_token(data: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    if "expires_in" in data:
        data["expires_at"] = int(time.time()) + int(data["expires_in"])
    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_token() -> str:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["access_token"]
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        raise ThreadsError("Немає токена. Запустіть: python auth_setup.py")
    return token


def refresh_token_if_needed(days_left: int = 10) -> None:
    """Довгий токен живе 60 днів. Оновлюємо, коли лишилось < days_left днів."""
    if not TOKEN_FILE.exists():
        return
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    expires_at = data.get("expires_at")
    if expires_at and expires_at - time.time() > days_left * 86400:
        return
    resp = requests.get(
        f"{OAUTH_BASE}/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": data["access_token"]},
        timeout=30,
    )
    save_token(_check(resp))
    print("Токен оновлено.")


# ---------- клієнт ----------

class Threads:
    def __init__(self, token: str | None = None):
        self.token = token or load_token()
        self.session = requests.Session()

    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return _check(self.session.get(f"{API_BASE}/{path}", params=params, timeout=30))

    def _post(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return _check(self.session.post(f"{API_BASE}/{path}", data=params, timeout=30))

    def me(self) -> dict:
        return self._get("me", fields="id,username")

    def publishing_limit(self) -> dict:
        data = self._get(
            "me/threads_publishing_limit",
            fields="quota_usage,config,reply_quota_usage,reply_config",
        )
        return (data.get("data") or [{}])[0]

    def _wait_container(self, container_id: str, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self._get(container_id, fields="status,error_message")
            if status.get("status") == "FINISHED":
                return
            if status.get("status") in ("ERROR", "EXPIRED"):
                raise ThreadsError(f"Контейнер {container_id}: {status}")
            time.sleep(3)
        raise ThreadsError(f"Контейнер {container_id} не готовий за {timeout} с")

    def publish_text(self, text: str, reply_to_id: str | None = None, topic_tag: str | None = None) -> str:
        """Створює текстовий пост (або відповідь) і публікує. Повертає id поста."""
        if len(text) > MAX_POST_CHARS:
            raise ThreadsError(f"Текст {len(text)} символів, ліміт {MAX_POST_CHARS}")
        params = {"media_type": "TEXT", "text": normalize_dashes(text)}
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        tag = clean_topic_tag(topic_tag)
        if tag:
            params["topic_tag"] = tag
        container = self._post("me/threads", **params)["id"]
        self._wait_container(container)
        return self._post("me/threads_publish", creation_id=container)["id"]

    def publish_chain(self, parts: list[str], topic_tag: str | None = None) -> list[str]:
        """Публікує довгий текст ланцюжком: перший пост (з темою) + відповіді на самого себе."""
        ids: list[str] = []
        for i, part in enumerate(parts):
            ids.append(self.publish_text(
                part,
                reply_to_id=ids[-1] if ids else None,
                topic_tag=None if ids else topic_tag,
            ))
            if i < len(parts) - 1:
                time.sleep(5)
        return ids

    def permalink(self, media_id: str) -> str:
        return self._get(media_id, fields="permalink").get("permalink", "")

    def keyword_search(self, q: str, search_type: str = "TOP", limit: int = 25, since: str | None = None) -> list[dict]:
        params = {
            "q": q,
            "search_type": search_type,
            "limit": limit,
            "fields": "id,text,media_type,permalink,timestamp,username,has_replies,is_reply,is_quote_post",
        }
        if since:
            params["since"] = since
        return self._get("keyword_search", **params).get("data", [])


def normalize_dashes(text: str) -> str:
    """Замінює довге (—) і середнє (–) тире на коротке дефіс-тире (-). Вимкнути: NORMALIZE_DASHES=0."""
    if os.environ.get("NORMALIZE_DASHES", "1") == "0":
        return text
    return text.replace("\u2014", "-").replace("\u2013", "-")


def clean_topic_tag(tag: str | None) -> str | None:
    """Тема Threads: 1–50 символів, без '.' і '&' (вимога Meta)."""
    if not tag:
        return None
    tag = tag.strip().lstrip("#").replace(".", "").replace("&", "і").strip()
    return tag[:50] or None


def split_for_threads(text: str, limit: int = MAX_POST_CHARS) -> list[str]:
    """Ділить довгий текст на частини ≤ limit.

    Якщо в тексті є рядок '---', ділимо саме по ньому (ручний контроль).
    Інакше — по абзацах, потім по реченнях. Додає нумерацію 1/3, 2/3...
    """
    text = text.strip()
    if "\n---\n" in text:
        chunks = [c.strip() for c in text.split("\n---\n") if c.strip()]
    elif len(text) <= limit:
        return [text]
    else:
        budget = limit - 8  # місце під " (1/9)"
        chunks, current = [], ""
        pieces: list[str] = []
        for para in text.split("\n\n"):
            if len(para) <= budget:
                pieces.append(para)
            else:  # дуже довгий абзац — ріжемо по реченнях
                sent_buf = ""
                for sent in para.replace("! ", "!\x00").replace("? ", "?\x00").replace(". ", ".\x00").split("\x00"):
                    if len(sent_buf) + len(sent) + 1 > budget and sent_buf:
                        pieces.append(sent_buf.strip())
                        sent_buf = ""
                    sent_buf += sent + " "
                if sent_buf.strip():
                    pieces.append(sent_buf.strip())
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= budget:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece[:budget]
        if current:
            chunks.append(current)
    if len(chunks) > 1:
        chunks = [f"{c} ({i}/{len(chunks)})" for i, c in enumerate(chunks, 1)]
    for c in chunks:
        if len(c) > limit:
            raise ThreadsError(f"Частина довша за {limit} символів: {c[:60]}...")
    return chunks
