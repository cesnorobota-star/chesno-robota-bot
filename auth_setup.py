"""Одноразове отримання довгого (60 днів) токена Threads.

Запуск:  python auth_setup.py
Потрібні в .env: THREADS_APP_ID, THREADS_APP_SECRET, THREADS_REDIRECT_URI
"""
import os
import sys
import urllib.parse

import requests

from threads_api import OAUTH_BASE, Threads, _check, load_env, save_token

SCOPES = "threads_basic,threads_content_publish,threads_manage_replies,threads_keyword_search"


def main() -> None:
    load_env()
    try:
        app_id = os.environ["THREADS_APP_ID"]
        secret = os.environ["THREADS_APP_SECRET"]
        redirect = os.environ["THREADS_REDIRECT_URI"]
    except KeyError as e:
        sys.exit(f"Заповніть {e.args[0]} у файлі .env (див. .env.example)")

    url = "https://threads.net/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": app_id,
        "redirect_uri": redirect,
        "scope": SCOPES,
        "response_type": "code",
    })
    print("1) Відкрийте в браузері й дозвольте доступ:\n\n" + url + "\n")
    print("2) Після переходу скопіюйте ПОВНУ адресу з рядка браузера (з ?code=...) і вставте сюди.")
    pasted = input("> ").strip()
    query = urllib.parse.urlparse(pasted).query or pasted
    code = urllib.parse.parse_qs(query).get("code", [pasted])[0].split("#")[0]

    short = _check(requests.post(f"{OAUTH_BASE}/oauth/access_token", data={
        "client_id": app_id,
        "client_secret": secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
        "code": code,
    }, timeout=30))
    long_lived = _check(requests.get(f"{OAUTH_BASE}/access_token", params={
        "grant_type": "th_exchange_token",
        "client_secret": secret,
        "access_token": short["access_token"],
    }, timeout=30))
    save_token(long_lived)
    me = Threads(long_lived["access_token"]).me()
    print(f"\nГотово. Акаунт @{me.get('username')} (id {me.get('id')}). Токен збережено в data/token.json")
    print("Токен діє 60 днів; autopost.py сам його оновлює, коли лишається < 10 днів.")


if __name__ == "__main__":
    main()
