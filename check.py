"""Проверка сценариев n8n: python check.py (после docker compose up -d --wait)

1. Первый запуск: создаёт владельца локального n8n (пароль — в .env, в git не попадает) и ключ API.
2. Включает сценарии через публичный API n8n.
3. Шлёт тестовые заявки/оплаты/отзывы на вебхуки и сверяет, что заглушка CRM/Telegram/таблицы/почты
   получила ровно нужные вызовы.
"""
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

N8N = "http://localhost:5678"
MOCK = "http://localhost:9000"
ENV = Path(__file__).parent / ".env"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(method: str, url: str, data=None, headers=None) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=30) as r:
            body = r.read()
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body.decode(errors="replace")[:300]}


def env() -> dict:
    return dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l) if ENV.exists() else {}


def setup() -> str:
    """Владелец + ключ API; при повторном запуске берёт их из .env."""
    cfg = env()
    if "N8N_PASSWORD" not in cfg:
        cfg = {"N8N_EMAIL": "owner@demo.local", "N8N_PASSWORD": "Demo-" + secrets.token_urlsafe(12) + "1a"}
        code, res = call("POST", f"{N8N}/rest/owner/setup", {"email": cfg["N8N_EMAIL"], "firstName": "Demo",
                                                              "lastName": "Owner", "password": cfg["N8N_PASSWORD"]})
        assert code == 200, f"owner setup: {code} {res}"
        ENV.write_text("\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n")
    code, res = call("POST", f"{N8N}/rest/login", {"emailOrLdapLoginId": cfg["N8N_EMAIL"], "password": cfg["N8N_PASSWORD"]})
    assert code == 200, f"login: {code} {res}"
    if "N8N_API_KEY" not in cfg:
        code, scopes = call("GET", f"{N8N}/rest/api-keys/scopes")
        code, res = call("POST", f"{N8N}/rest/api-keys", {"label": "check", "expiresAt": None, "scopes": scopes.get("data", [])})
        assert code == 200, f"api key: {code} {res}"
        cfg["N8N_API_KEY"] = res["data"]["rawApiKey"]
        ENV.write_text("\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n")
    return cfg["N8N_API_KEY"]


def activate(key: str) -> list[str]:
    code, res = call("GET", f"{N8N}/api/v1/workflows", headers={"X-N8N-API-KEY": key})
    assert code == 200, f"list workflows: {code} {res}"
    names = []
    for wf in res["data"]:
        if not wf["active"]:
            # импорт CLI не создаёт запись в истории версий, а включение на неё ссылается — пересохраняем через API
            body = {k: wf[k] for k in ("name", "nodes", "connections")} | {"settings": {"executionOrder": "v1"}}
            c, r = call("PUT", f"{N8N}/api/v1/workflows/{wf['id']}", body, headers={"X-N8N-API-KEY": key})
            assert c == 200, f"save {wf['name']}: {c} {r}"
            c, r = call("POST", f"{N8N}/api/v1/workflows/{wf['id']}/activate", headers={"X-N8N-API-KEY": key})
            assert c == 200, f"activate {wf['name']}: {c} {r}"
        names.append(wf["name"])
    return sorted(names)


def hook(path: str, data: dict) -> tuple[int, dict]:
    return call("POST", f"{N8N}/webhook/{path}", data)


def log() -> list[dict]:
    return call("GET", f"{MOCK}/_log")[1]


def ok(text: str) -> None:
    print(f"  ✓ {text}")


def main() -> None:
    key = setup()
    names = activate(key)
    ok(f"сценарии включены: {len(names)}")
    time.sleep(2)  # вебхуки регистрируются после включения
    call("POST", f"{MOCK}/_reset")

    # 1. новая заявка → сделка в CRM → Telegram
    code, res = hook("lead", {"name": "Анна", "phone": "8 (900) 123-45-67", "comment": "Ремонт двушки"})
    assert code == 200 and res == {"ok": True, "id": 1}, (code, res)
    l = log()
    assert [(c["method"], c["path"]) for c in l] == [("GET", "/crm/leads"), ("POST", "/crm/leads"), ("POST", "/tg/botDEMO/sendMessage")], l
    assert l[0]["query"] == "phone=%2B79001234567" and l[1]["body"]["phone"] == "+79001234567"
    assert "Новая заявка №1: Анна, +79001234567" in l[2]["body"]["text"]
    ok("заявка: телефон приведён к +7, сделка №1 создана, менеджеру ушло сообщение")

    # 1б. повторная заявка с тем же телефоном → заметка, а не дубль
    before = len(log())
    code, res = hook("lead", {"name": "Анна", "phone": "+7 900 123 45 67", "comment": "Перезвоните вечером"})
    calls = log()[before:]
    assert code == 200 and res == {"ok": True, "duplicate": True}, (code, res)
    assert [c["path"] for c in calls] == ["/crm/leads", "/crm/leads/1/notes", "/tg/botDEMO/sendMessage"], calls
    ok("повторная заявка: дубль не создан, добавлена заметка к сделке №1")

    # 1в. без телефона → 422, во внешние сервисы ничего не уходит
    before = len(log())
    code, res = hook("lead", {"name": "Без телефона"})
    assert code == 422 and len(log()) == before, (code, res)
    ok("заявка без телефона отклонена (422), CRM не тронута")

    # 2. строка в таблицу
    before = len(log())
    code, res = hook("sheet", {"name": "Олег", "phone": "+79005556677", "utm_source": "yandex", "page": "/remont"})
    c = log()[before:]
    assert code == 200 and c[0]["path"] == "/sheets/append" and c[0]["body"]["values"][1:] == ["Олег", "+79005556677", "yandex", "/remont"], c
    ok("заявка записана строкой в таблицу: дата, имя, телефон, utm, страница")

    # 3. оплата → статус → чек → Telegram; другое событие игнорируется
    before = len(log())
    pay = {"event": "payment.succeeded", "object": {"amount": {"value": "15000.00"}, "metadata": {"lead_id": 1, "email": "anna@example.com"}}}
    code, res = hook("payment", pay)
    c = log()[before:]
    assert code == 200 and [x["path"] for x in c] == ["/crm/leads/1", "/mail/send", "/tg/botDEMO/sendMessage"], c
    assert c[0]["body"] == {"status": "paid", "paid": 15000} and c[1]["body"]["to"] == "anna@example.com"
    before = len(log())
    code, res = hook("payment", {"event": "payment.canceled", "object": {}})
    assert res == {"ok": True, "ignored": True} and len(log()) == before
    ok("оплата: сделка → «оплачено», чек на почту, сообщение; отмена платежа пропущена")

    # 4. отчёт
    before = len(log())
    code, res = hook("report", {})
    c = log()[before:]
    assert code == 200 and c[-1]["path"].endswith("/sendMessage"), c
    assert "Сделки: 1" in c[-1]["body"]["text"] and "Оплачено: 1 на 15" in c[-1]["body"]["text"], c[-1]
    ok("отчёт: сводка по сделкам ушла в Telegram (по расписанию — каждый день в 9:00)")

    # 5. отзывы: плохой → руководителю, хороший → просьба об отзыве на картах
    before = len(log())
    code, bad = hook("feedback", {"name": "Иван", "rating": 2, "text": "Долго ждал мастера", "email": "ivan@example.com"})
    code2, good = hook("feedback", {"name": "Мария", "rating": 5, "email": "maria@example.com"})
    c = log()[before:]
    assert bad["escalated"] is True and good["escalated"] is False, (bad, good)
    assert c[0]["path"].endswith("/sendMessage") and "Оценка 2 от Иван" in c[0]["body"]["text"]
    assert c[1]["path"] == "/mail/send" and c[1]["body"]["to"] == "maria@example.com"
    ok("отзывы: оценка 2 → тревога руководителю, оценка 5 → просьба об отзыве на картах")
    print("▶ все сценарии работают")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"  ✗ {e}")
        sys.exit(1)
