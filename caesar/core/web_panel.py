"""Web Panel + Webhook (минимальная версия, без внешних зависимостей).

Использует asyncio.start_server (stdlib) — не требует aiohttp/flask.
Daemon может опционально запустить через config: web: {enabled: true, port: 8080}.

Endpoints:
- GET /status — JSON: version, daemon uptime, task counts, memory stats
- GET /tasks — JSON: active + pending tasks
- GET /mind?entity=X — JSON: L2 facts + KG relations for entity
- POST /webhook — JSON body: {message, user_id, source} → создаёт task в очереди
"""
import asyncio
import json
from datetime import datetime


class WebPanel:
    """Минимальный web-сервер для inspectability + webhook."""

    def __init__(self, config, storage, queue=None, event_bus=None, daemon=None):
        self.config = config
        self.storage = storage
        self.queue = queue
        self.event_bus = event_bus
        self.daemon = daemon
        self.log = None
        self._server = None
        self._running = False

    async def start(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        from caesar.logging_setup import get_logger
        self.log = get_logger("web_panel")
        self._server = await asyncio.start_server(self._handle, host, port)
        self._running = True
        self.log.info(f"Web panel started on http://{host}:{port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._running = False
            self.log.info("Web panel stopped")

    async def _handle(self, reader, writer):
        """Handle HTTP request (minimal parser)."""
        try:
            data = await reader.read(65536)
            request = data.decode("utf-8", errors="replace")
            lines = request.split("\r\n")
            if not lines:
                return
            method, path, _ = lines[0].split(" ", 2)
            # Extract body for POST
            body = ""
            if "\r\n\r\n" in request:
                body = request.split("\r\n\r\n", 1)[1]

            # Route
            if path.startswith("/status"):
                response = self._handle_status()
            elif path.startswith("/tasks"):
                response = self._handle_tasks()
            elif path.startswith("/mind"):
                entity = ""
                if "?" in path:
                    params = path.split("?", 1)[1]
                    for p in params.split("&"):
                        if p.startswith("entity="):
                            entity = p[7:].replace("%20", " ").replace("+", " ")
                response = self._handle_mind(entity)
            elif path == "/webhook" and method == "POST":
                response = await self._handle_webhook(body)
            elif path == "/" or path == "":
                response = self._handle_index()
            else:
                response = {"error": "not found", "path": path}

            resp_json = json.dumps(response, ensure_ascii=False, default=str)
            writer.write(
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(resp_json.encode('utf-8'))}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"\r\n{resp_json}".encode("utf-8")
            )
            await writer.drain()
        except Exception as e:
            try:
                err = json.dumps({"error": str(e)})
                writer.write(
                    f"HTTP/1.1 500\r\nContent-Type: application/json\r\n\r\n{err}".encode()
                )
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    def _handle_index(self) -> dict:
        return {
            "service": "Caesar Web Panel",
            "endpoints": ["/status", "/tasks", "/mind?entity=X", "/webhook (POST)"],
            "version": self._get_version(),
        }

    def _handle_status(self) -> dict:
        from caesar import __version__
        result = {
            "version": __version__,
            "timestamp": datetime.now().isoformat(),
        }
        if self.daemon:
            uptime = getattr(self.daemon, "_start_time", None)
            if uptime:
                result["uptime_sec"] = (datetime.now() - uptime).total_seconds()
        if self.queue:
            result["tasks"] = {
                "interactive_active": self.queue.get_active_count("interactive"),
                "interactive_pending": self.queue.get_pending_count("interactive"),
                "background_active": self.queue.get_active_count("background"),
                "background_pending": self.queue.get_pending_count("background"),
            }
        if self.storage:
            with self.storage._conn() as conn:
                result["db"] = {
                    "tasks_total": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    "l2_facts_active": conn.execute(
                        "SELECT COUNT(*) FROM l2_facts WHERE valid_until IS NULL"
                    ).fetchone()[0],
                    "l3_chunks": conn.execute("SELECT COUNT(*) FROM l3_chunks").fetchone()[0],
                    "kg_entities": conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0],
                    "kg_relations": conn.execute("SELECT COUNT(*) FROM kg_relations").fetchone()[0],
                    "l4_skills": conn.execute("SELECT COUNT(*) FROM l4_skills").fetchone()[0],
                }
        return result

    def _handle_tasks(self) -> dict:
        if not self.queue:
            return {"error": "queue not available"}
        active = self.queue.list_active_tasks()
        pending = self.queue.list_pending_tasks()
        return {
            "active": [{"id": t.id, "status": str(t.status), "message": t.user_message[:80],
                        "step": t.current_step} for t in active],
            "pending": [{"id": t.id, "message": t.user_message[:80],
                         "priority": t.priority.name} for t in pending],
        }

    def _handle_mind(self, entity: str) -> dict:
        if not entity:
            return {"error": "add ?entity=X", "hint": "/mind?entity=Postgres"}
        from caesar.memory.mind_mirror import MindMirror
        mirror = MindMirror(self.storage)
        text = mirror.query(entity)
        return {"entity": entity, "result": text}

    async def _handle_webhook(self, body: str) -> dict:
        if not self.queue:
            return {"error": "queue not available"}
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {"error": "invalid JSON body"}
        message = data.get("message", "")
        user_id = data.get("user_id", "")
        source = data.get("source", "webhook")
        if not message:
            return {"error": "missing 'message' field"}
        task = await self.queue.add_task(
            user_message=message,
            user_id=user_id or "webhook",
            channel_id=f"channel:webhook:{source}",
            author_id=user_id or "webhook",
            source=source,
            source_chat_id="webhook",
        )
        return {"status": "accepted", "task_id": task.id, "message": message[:100]}

    @staticmethod
    def _get_version() -> str:
        from caesar import __version__
        return __version__
