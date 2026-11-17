from __future__ import annotations

import json
import io
import os
import re
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from data_agent.agent import answer_question
from data_agent.config import ROOT, get_setting
from data_agent.store import RateStore


STATIC_DIR = ROOT / "static"


class DataAgentHandler(BaseHTTPRequestHandler):
    store: RateStore

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            rows = self.store.read()
            payload = {
                "count": len(rows),
                "latest_date": max((row.observed_on.isoformat() for row in rows), default=None),
                "sources": sorted({row.source for row in rows}),
                "model": get_setting("OLLAMA_MODEL", "qwen2.5:3b"),
            }
            self._send_json(HTTPStatus.OK, payload)
            return
        if parsed.path == "/":
            self._send_file(ROOT / "templates" / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            relative = unquote(parsed.path.removeprefix("/static/"))
            file_path = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in file_path.parents:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = "text/css; charset=utf-8" if file_path.suffix == ".css" else "text/javascript; charset=utf-8"
            self._send_file(file_path, content_type)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/import":
            self._import_file()
            return
        if path != "/api/chat":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                raise ValueError("La solicitud está vacía o es demasiado grande.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("question"), str):
                raise ValueError("La solicitud debe incluir la pregunta como texto.")
            result = answer_question(
                self.store,
                payload["question"],
                model=get_setting("OLLAMA_MODEL", "qwen2.5:3b"),
                ollama_url=get_setting("OLLAMA_URL", "http://127.0.0.1:11434"),
            )
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except RuntimeError as error:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return
        self._send_json(HTTPStatus.OK, result)

    def _import_file(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 10 * 1024 * 1024:
                raise ValueError("El archivo está vacío o supera el límite de 10 MB.")
            content_type = self.headers.get("Content-Type", "")
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
                + self.rfile.read(length)
            )
            if not message.is_multipart():
                raise ValueError("La carga debe enviarse como formulario multipart.")
            uploaded = next(
                (
                    part
                    for part in message.iter_parts()
                    if part.get_param("name", header="content-disposition") == "file"
                ),
                None,
            )
            if uploaded is None:
                raise ValueError("Selecciona un archivo CSV o Excel para importar.")
            filename = uploaded.get_filename()
            content = uploaded.get_payload(decode=True)
            if not filename or not content:
                raise ValueError("El archivo seleccionado está vacío o no tiene nombre.")
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", Path(filename).name)[:160]
            extension = Path(safe_name).suffix.lower()
            if extension == ".xlsx":
                count = self.store.import_excel(
                    io.BytesIO(content),
                    source=f"Excel importado: {safe_name}",
                )
            elif extension == ".csv":
                count = self.store.import_csv(
                    io.StringIO(content.decode("utf-8-sig")),
                    source=f"CSV importado: {safe_name}",
                )
            else:
                raise ValueError("Formato no compatible. Selecciona un archivo .xlsx o .csv.")
            rows = self.store.read()
            self._send_json(
                HTTPStatus.OK,
                {
                    "message": f"Se importaron/actualizaron {count} observaciones.",
                    "count": len(rows),
                    "latest_date": max(
                        (row.observed_on.isoformat() for row in rows),
                        default=None,
                    ),
                    "sources": sorted({row.source for row in rows}),
                    "model": get_setting("OLLAMA_MODEL", "qwen2.5:3b"),
                },
            )
        except (ValueError, UnicodeDecodeError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(store: RateStore, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = type("ConfiguredDataAgentHandler", (DataAgentHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Data Agent disponible en http://{host}:{port} (Ctrl+C para detener)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()
