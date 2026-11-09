#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Веб-версия ИИ-агента (HTTP-сервер на стандартной библиотеке + Ollama).

Запуск:
    python web_agent.py
    # или
    WEB_HOST=0.0.0.0 WEB_PORT=8080 python web_agent.py

После запуска откройте в браузере: http://127.0.0.1:8080
"""

import json
import os
import queue
import socket
import threading
import time
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

import tool_agent as TA
from tool_agent import ToolAgent, _create_client, SYSTEM_PROMPT, MODEL, OLLAMA_HOST

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

PORT = int(os.environ.get("WEB_PORT", "8080"))
HOST = os.environ.get("WEB_HOST", "127.0.0.1")

sessions: Dict[str, ToolAgent] = {}
sessions_lock = threading.Lock()
chat_lock = threading.Lock()


def get_agent(session_id: str) -> ToolAgent:
    with sessions_lock:
        agent = sessions.get(session_id)
        if agent is None:
            client = _create_client()
            agent = ToolAgent(client=client, model=MODEL, system_prompt=SYSTEM_PROMPT)
            sessions[session_id] = agent
        return agent


def make_tool_wrapper(name: str, original: Any, event_queue: queue.Queue) -> Any:
    def wrapper(**kwargs: Any) -> Any:
        event_queue.put({"type": "tool_call", "name": name, "args": kwargs})
        started = time.time()
        try:
            result = original(**kwargs)
            ok = True
        except Exception as exc:  # noqa: BLE001
            result = f"Ошибка инструмента '{name}': {exc}"
            ok = False
        event_queue.put(
            {
                "type": "tool_result",
                "name": name,
                "result": str(result),
                "ok": ok,
                "duration_ms": int((time.time() - started) * 1000),
            }
        )
        return result

    wrapper.__name__ = f"web_wrapped_{name}"
    return wrapper


def history_payload(agent: ToolAgent) -> Dict[str, Any]:
    items = []
    for msg in agent.messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            continue
        if content is None and role != "tool":
            continue
        items.append({"role": role, "content": content if content is not None else "", "name": msg.get("name")})
    return {"messages": items}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WebAgent/1.0"

    def _set_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt: str, *args: Any) -> None:
        # Уменьшаем шум в консоли.
        return

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._set_common_headers()
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _send_sse(self, event: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _serve_static(self, relative_path: str) -> None:
        # Защита от выхода за пределы web/
        safe = (WEB_DIR / relative_path.lstrip("/")).resolve()
        if not str(safe).startswith(str(WEB_DIR.resolve())):
            self.send_error(403, "Forbidden")
            return
        if safe.is_dir():
            safe = safe / "index.html"
        if not safe.exists() or not safe.is_file():
            self.send_error(404, "Not Found")
            return
        content_type = "text/html; charset=utf-8"
        if safe.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif safe.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif safe.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        elif safe.suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}:
            content_type = f"image/{safe.suffix.lstrip('.').replace('jpg', 'jpeg')}"
        data = safe.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._set_common_headers()
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._set_common_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._serve_static("index.html")
            return

        if path == "/api/health":
            self._send_json({"status": "ok", "model": MODEL, "host": OLLAMA_HOST})
            return

        if path == "/api/tools":
            self._send_json({"tools": sorted(TA._TOOLS.keys())})
            return

        if path == "/api/config":
            self._send_json({"model": MODEL, "ollama_host": OLLAMA_HOST, "system_prompt": SYSTEM_PROMPT})
            return

        if path == "/api/history":
            session_id = (query.get("session_id") or [""])[0]
            if not session_id:
                self._send_json({"error": "session_id is required"}, 400)
                return
            agent = get_agent(session_id)
            self._send_json(history_payload(agent))
            return

        # Статика: /style.css, /app.js и т.п.
        if not path.startswith("/api/"):
            self._serve_static(path.lstrip("/"))
            return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        data = self._read_json()

        if path == "/api/clear":
            session_id = data.get("session_id") or str(uuid.uuid4())
            agent = get_agent(session_id)
            with chat_lock:
                agent.reset()
            self._send_json({"session_id": session_id, "status": "cleared"})
            return

        if path == "/api/save":
            session_id = data.get("session_id") or ""
            if not session_id:
                self._send_json({"error": "session_id is required"}, 400)
                return
            path_to_save = data.get("path") or "history.json"
            agent = get_agent(session_id)
            with chat_lock:
                msg = agent.save_history(path_to_save)
            self._send_json({"status": msg})
            return

        if path == "/api/chat":
            session_id = data.get("session_id") or str(uuid.uuid4())
            message = (data.get("message") or "").strip()
            if not message:
                self._send_json({"error": "message is required"}, 400)
                return
            agent = get_agent(session_id)
            started = time.time()
            try:
                with chat_lock:
                    answer = agent.handle_user_message(message)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc), "session_id": session_id}, 500)
                return
            self._send_json(
                {
                    "session_id": session_id,
                    "answer": answer,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            )
            return

        if path == "/api/chat/stream":
            session_id = data.get("session_id") or str(uuid.uuid4())
            message = (data.get("message") or "").strip()
            if not message:
                self._send_json({"error": "message is required"}, 400)
                return
            agent = get_agent(session_id)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self._set_common_headers()
            self.end_headers()
            self.close_connection = True
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:  # noqa: BLE001
                pass

            q: queue.Queue = queue.Queue()
            result: Dict[str, Any] = {}

            def worker() -> None:
                with chat_lock:
                    originals: Dict[str, Any] = {}
                    try:
                        # Временно оборачиваем инструменты для стриминга событий.
                        for name, func in list(TA._TOOLS.items()):
                            originals[name] = func
                            TA._TOOLS[name] = make_tool_wrapper(name, func, q)
                        result["answer"] = agent.handle_user_message(message)
                    except Exception as exc:  # noqa: BLE001
                        result["error"] = str(exc)
                    finally:
                        for name, func in originals.items():
                            TA._TOOLS[name] = func
                        q.put({"type": "__done__"})

            threading.Thread(target=worker, daemon=True).start()

            self._send_sse("start", {"session_id": session_id})
            started = time.time()
            while True:
                try:
                    item = q.get(timeout=0.25)
                except queue.Empty:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except BrokenPipeError:
                        break
                    continue

                if item.get("type") == "__done__":
                    if "error" in result:
                        self._send_sse("error", {"message": result["error"]})
                    else:
                        self._send_sse(
                            "final",
                            {
                                "answer": result.get("answer", ""),
                                "elapsed_ms": int((time.time() - started) * 1000),
                            },
                        )
                    self._send_sse("done", {"session_id": session_id})
                    break

                event_type = item.get("type", "message")
                self._send_sse(event_type, item)

            return

        self.send_error(404, "Not Found")


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST if HOST != '0.0.0.0' else '127.0.0.1'}:{PORT}"
    print(f"Веб-агент запущен: {url}")
    print(f"Модель: {MODEL}  |  Ollama: {OLLAMA_HOST}")
    print("Остановка: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
