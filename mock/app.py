"""Заглушка внешних сервисов для проверки сценариев n8n: CRM, Telegram, таблица, почта.

Записывает каждый вызов — проверка (check.py) смотрит, что именно сценарий отправил.
GET /_log — журнал вызовов, POST /_reset — очистить журнал и CRM.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOG: list[dict] = []
LEADS: dict[int, dict] = {}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        return json.loads(raw) if raw else {}

    def handle_any(self, method: str) -> None:
        url = urlparse(self.path)
        data = self.body() if method in ("POST", "PATCH") else {}
        if url.path == "/_log":
            return self.reply(200, LOG)
        if url.path == "/_reset":
            LOG.clear(); LEADS.clear()
            return self.reply(200, {"ok": True})
        LOG.append({"method": method, "path": url.path, "query": url.query, "body": data})
        if url.path == "/crm/leads" and method == "GET":
            phone = parse_qs(url.query).get("phone", [""])[0]
            found = [l for l in LEADS.values() if not phone or l["phone"] == phone]
            return self.reply(200, {"items": found})
        if url.path == "/crm/leads" and method == "POST":
            lead = {"id": len(LEADS) + 1, "status": "new", **data}
            LEADS[lead["id"]] = lead
            return self.reply(201, lead)
        if m := re.fullmatch(r"/crm/leads/(\d+)", url.path):
            lead = LEADS.get(int(m[1]))
            if not lead:
                return self.reply(404, {"error": "lead not found"})
            lead.update(data)
            return self.reply(200, lead)
        if re.fullmatch(r"/crm/leads/\d+/notes", url.path):
            return self.reply(201, {"ok": True})
        if url.path.endswith("/sendMessage"):
            return self.reply(200, {"ok": True, "result": {"message_id": len(LOG)}})
        if url.path in ("/sheets/append", "/mail/send"):
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "unknown endpoint"})

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_PATCH(self): self.handle_any("PATCH")

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
