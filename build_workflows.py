"""Генерирует сценарии n8n в workflows/*.json: python build_workflows.py

Сценарии собираются кодом, чтобы узлы и связи было легко читать и менять; n8n импортирует их при старте.
Адреса сервисов берутся из переменных окружения ($env.CRM_URL и т.д.) — в демо это заглушка mock/.
"""
import json
import uuid
from pathlib import Path

OUT = Path(__file__).parent / "workflows"


class Flow:
    def __init__(self, name: str, tags: str) -> None:
        self.name, self.tags, self.nodes, self.links = name, tags, [], {}

    def add(self, name: str, kind: str, version: float, params: dict, pos: tuple[int, int], **extra) -> str:
        self.nodes.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, self.name + name)), "name": name, "type": kind,
                           "typeVersion": version, "position": list(pos), "parameters": params, **extra})
        return name

    def link(self, a: str, b: str, out: int = 0) -> None:
        outs = self.links.setdefault(a, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": b, "type": "main", "index": 0})

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def save(self, file: str) -> None:
        key = uuid.uuid5(uuid.NAMESPACE_URL, self.name)
        # постоянные id: повторный импорт обновляет сценарий, а не создаёт копию
        data = {"id": key.hex[:16], "versionId": str(key), "name": self.name, "nodes": self.nodes, "connections": self.links, "active": False,
                "settings": {"executionOrder": "v1"}, "tags": [], "pinData": {}}
        (OUT / file).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# --- конструкторы узлов ---------------------------------------------------------------------------
def webhook(f: Flow, name: str, path: str, pos, respond: str = "responseNode") -> str:
    return f.add(name, "n8n-nodes-base.webhook", 2, {"httpMethod": "POST", "path": path, "responseMode": respond, "options": {}},
                 pos, webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, path)))


def code(f: Flow, name: str, js: str, pos) -> str:
    return f.add(name, "n8n-nodes-base.code", 2, {"jsCode": js.strip()}, pos)


def http(f: Flow, name: str, method: str, url: str, pos, body: str | None = None, query: dict | None = None) -> str:
    p = {"method": method, "url": url, "options": {}}
    if body:
        p |= {"sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify(" + body + ") }}"}
    if query:
        p |= {"sendQuery": True, "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in query.items()]}}
    return f.add(name, "n8n-nodes-base.httpRequest", 4.2, p, pos)


def telegram(f: Flow, name: str, text: str, pos) -> str:
    return http(f, name, "POST", "={{ $env.TG_URL }}/sendMessage", pos, body="{ chat_id: $env.TG_CHAT, text: " + text + " }")


def cond(f: Flow, name: str, left: str, op: dict, pos, right="") -> str:
    return f.add(name, "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f.name + name)), "leftValue": left,
                                       "rightValue": right, "operator": op}], "combinator": "and"},
        "looseTypeValidation": True, "options": {}}, pos)


def respond(f: Flow, name: str, body: str, pos, status: int = 200) -> str:
    return f.add(name, "n8n-nodes-base.respondToWebhook", 1.1,
                 {"respondWith": "json", "responseBody": "={{ JSON.stringify(" + body + ") }}", "options": {"responseCode": status}}, pos)


IS_TRUE = {"type": "boolean", "operation": "true", "singleValue": True}
LEAD = "$('Проверка заявки').item.json"

# 1. Заявка с сайта → дубль? → CRM → Telegram ----------------------------------------------------
f = Flow("1. Заявка с сайта → CRM → Telegram", "лиды")
webhook(f, "Заявка с сайта", "lead", (0, 300))
code(f, "Проверка заявки", """
// телефон приводим к +7XXXXXXXXXX, без имени или с неполным номером заявку не принимаем
const b = $input.first().json.body || {};
const digits = String(b.phone || '').replace(/\\D/g, '');
const name = String(b.name || '').trim();
return [{ json: {
  valid: name.length > 0 && (digits.length === 10 || digits.length === 11),
  name, phone: '+7' + digits.slice(-10), comment: String(b.comment || ''), source: b.source || 'сайт',
} }];
""", (220, 300))
cond(f, "Данные в порядке?", "={{ $json.valid }}", IS_TRUE, (440, 300))
respond(f, "Ошибка: нет имени или телефона", '{ ok: false, error: "нужны имя и телефон" }', (660, 500), 422)
http(f, "Поиск клиента в CRM", "GET", "={{ $env.CRM_URL }}/leads", (660, 200), query={"phone": "={{ $json.phone }}"})
cond(f, "Клиент уже есть?", "={{ $json.items.length > 0 }}", IS_TRUE, (880, 200))
http(f, "Заметка к сделке", "POST", "={{ $env.CRM_URL }}/leads/{{ $json.items[0].id }}/notes", (1100, 80),
     body=f"{{ text: 'Повторная заявка: ' + ({LEAD}.comment || 'без комментария') }}")
telegram(f, "Telegram: повторная заявка", f"'🔁 Повторная заявка: ' + {LEAD}.name + ', ' + {LEAD}.phone", (1320, 80))
respond(f, "Ответ: повторная", "{ ok: true, duplicate: true }", (1540, 80))
http(f, "Новая сделка в CRM", "POST", "={{ $env.CRM_URL }}/leads", (1100, 320),
     body=f"{{ name: {LEAD}.name, phone: {LEAD}.phone, comment: {LEAD}.comment, source: {LEAD}.source }}")
telegram(f, "Telegram: новая заявка", f"'🔥 Новая заявка №' + $json.id + ': ' + $json.name + ', ' + $json.phone + ($json.comment ? '\\n' + $json.comment : '')", (1320, 320))
respond(f, "Ответ: принято", "{ ok: true, id: $('Новая сделка в CRM').item.json.id }", (1540, 320))
f.chain("Заявка с сайта", "Проверка заявки", "Данные в порядке?")
f.link("Данные в порядке?", "Поиск клиента в CRM", 0)
f.link("Данные в порядке?", "Ошибка: нет имени или телефона", 1)
f.link("Поиск клиента в CRM", "Клиент уже есть?")
f.link("Клиент уже есть?", "Заметка к сделке", 0)
f.link("Клиент уже есть?", "Новая сделка в CRM", 1)
f.chain("Заметка к сделке", "Telegram: повторная заявка", "Ответ: повторная")
f.chain("Новая сделка в CRM", "Telegram: новая заявка", "Ответ: принято")
f.save("1-lead.json")

# 2. Заявка → строка в Google Таблице -------------------------------------------------------------
f = Flow("2. Заявка → Google Таблица", "таблицы")
webhook(f, "Заявка с формы", "sheet", (0, 300))
code(f, "Строка для таблицы", """
// столбцы таблицы: дата, имя, телефон, источник рекламы, страница
const b = $input.first().json.body || {};
const now = new Date().toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });
return [{ json: { values: [now, b.name || '', b.phone || '', b.utm_source || 'прямой заход', b.page || '/'] } }];
""", (220, 300))
http(f, "Добавить строку в таблицу", "POST", "={{ $env.SHEETS_URL }}/append", (440, 300), body="{ sheet: 'Заявки', values: $json.values }")
respond(f, "Ответ сайту", "{ ok: true }", (660, 300))
f.chain("Заявка с формы", "Строка для таблицы", "Добавить строку в таблицу", "Ответ сайту")
f.save("2-sheet.json")

# 3. Оплата → статус сделки → чек → Telegram ---------------------------------------------------
f = Flow("3. Оплата → CRM → чек → Telegram", "оплаты")
PAY = "$('Уведомление об оплате').item.json.body.object"
webhook(f, "Уведомление об оплате", "payment", (0, 300))
cond(f, "Оплата прошла?", "={{ $json.body.event }}", {"type": "string", "operation": "equals"}, (220, 300), "payment.succeeded")
http(f, "Сделка → «Оплачено»", "PATCH", f"={{{{ $env.CRM_URL }}}}/leads/{{{{ $json.body.object.metadata.lead_id }}}}", (440, 200),
     body="{ status: 'paid', paid: Number($json.body.object.amount.value) }")
http(f, "Чек клиенту на почту", "POST", "={{ $env.MAIL_URL }}/send", (660, 200),
     body=f"{{ to: {PAY}.metadata.email, subject: 'Оплата получена', text: 'Спасибо! Получили ' + {PAY}.amount.value + ' ₽ по заказу №' + {PAY}.metadata.lead_id }}")
telegram(f, "Telegram: оплата", f"'💰 Оплата ' + {PAY}.amount.value + ' ₽ по сделке №' + {PAY}.metadata.lead_id", (880, 200))
respond(f, "Ответ: принято", "{ ok: true }", (1100, 200))
respond(f, "Ответ: пропущено", "{ ok: true, ignored: true }", (440, 420))
f.link("Уведомление об оплате", "Оплата прошла?")
f.link("Оплата прошла?", "Сделка → «Оплачено»", 0)
f.link("Оплата прошла?", "Ответ: пропущено", 1)
f.chain("Сделка → «Оплачено»", "Чек клиенту на почту", "Telegram: оплата", "Ответ: принято")
f.save("3-payment.json")

# 4. Ежедневный отчёт по сделкам ---------------------------------------------------------------
f = Flow("4. Отчёт по сделкам в Telegram", "отчёты")
f.add("Каждый день в 9:00", "n8n-nodes-base.scheduleTrigger", 1.2,
      {"rule": {"interval": [{"field": "cronExpression", "expression": "0 9 * * *"}]}}, (0, 200))
webhook(f, "Отчёт по запросу", "report", (0, 400), respond="lastNode")
http(f, "Сделки из CRM", "GET", "={{ $env.CRM_URL }}/leads", (240, 300))
code(f, "Сводка", """
const items = $input.first().json.items || [];
const paid = items.filter((l) => l.status === 'paid');
const sum = paid.reduce((s, l) => s + (l.paid || 0), 0);
const text = `📊 Сделки: ${items.length}\\nНовые: ${items.length - paid.length}\\nОплачено: ${paid.length} на ${sum.toLocaleString('ru-RU')} ₽`;
return [{ json: { text, total: items.length, paid: paid.length, sum } }];
""", (460, 300))
telegram(f, "Telegram: отчёт", "$json.text", (680, 300))
f.link("Каждый день в 9:00", "Сделки из CRM")
f.link("Отчёт по запросу", "Сделки из CRM")
f.chain("Сделки из CRM", "Сводка", "Telegram: отчёт")
f.save("4-report.json")

# 5. Отзыв клиента: плохая оценка → руководителю, хорошая → просьба об отзыве на картах ----------
f = Flow("5. Отзыв клиента → реакция", "отзывы")
FB = "$('Оценка от клиента').item.json.body"
webhook(f, "Оценка от клиента", "feedback", (0, 300))
cond(f, "Оценка 3 или ниже?", "={{ Number($json.body.rating) }}", {"type": "number", "operation": "lte"}, (220, 300), 3)
telegram(f, "Тревога руководителю", f"'⚠️ Оценка ' + {FB}.rating + ' от ' + {FB}.name + ': ' + ({FB}.text || 'без текста') + '\\nПозвоните клиенту сегодня'", (440, 200))
http(f, "Просьба об отзыве на картах", "POST", "={{ $env.MAIL_URL }}/send", (440, 420),
     body=f"{{ to: {FB}.email, subject: 'Спасибо за оценку!', text: 'Будем рады отзыву на Яндекс Картах: https://yandex.ru/maps/' }}")
respond(f, "Ответ: передано руководителю", "{ ok: true, escalated: true }", (660, 200))
respond(f, "Ответ: спасибо", "{ ok: true, escalated: false }", (660, 420))
f.link("Оценка от клиента", "Оценка 3 или ниже?")
f.link("Оценка 3 или ниже?", "Тревога руководителю", 0)
f.link("Оценка 3 или ниже?", "Просьба об отзыве на картах", 1)
f.chain("Тревога руководителю", "Ответ: передано руководителю")
f.chain("Просьба об отзыве на картах", "Ответ: спасибо")
f.save("5-feedback.json")

print("ok", len(list(OUT.glob("*.json"))))
