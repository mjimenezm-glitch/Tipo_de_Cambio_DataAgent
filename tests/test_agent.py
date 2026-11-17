from __future__ import annotations

import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from openpyxl import Workbook

from app import initialize_store
from data_agent.agent import answer_question, validate_intent
from data_agent.store import Observation, RateStore
from data_agent.web import DataAgentHandler


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RateStore(Path(self.temp_dir.name) / "rates.sqlite3")
        self.store.upsert(
            [
                Observation(date(2026, 1, 5), "buy", 499.8, "BCCR (Excel oficial 2026)"),
                Observation(date(2026, 1, 5), "sell", 505.2, "BCCR (Excel oficial 2026)"),
                Observation(date(2026, 1, 6), "buy", 500.1, "BCCR (Excel oficial 2026)"),
                Observation(date(2026, 1, 6), "sell", 505.5, "BCCR (Excel oficial 2026)"),
            ]
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ollama_plan_controls_verified_calculation(self) -> None:
        ollama_response = unittest.mock.MagicMock()
        ollama_response.__enter__.return_value.read.return_value = (
            b'{"message":{"content":"{\\"action\\":\\"average\\",'
            b'\\"rate\\":\\"sell\\",\\"start\\":\\"2026-01-01\\",'
            b'\\"end\\":\\"2026-12-31\\"}"}}'
        )
        with patch("data_agent.agent.urllib.request.urlopen", return_value=ollama_response) as request_mock:
            result = answer_question(
                self.store,
                "Compara el promedio de venta durante 2026",
                model="qwen2.5:3b",
            )
        self.assertEqual(result["intent"]["action"], "average")
        self.assertEqual(result["results"][0]["value"], 505.35)
        prompt = request_mock.call_args.args[0].data.decode("utf-8")
        self.assertIn("2026-01-05", prompt)
        self.assertIn("compra=2, venta=2", prompt)

    def test_ollama_is_required_by_default(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Ollama es necesario"):
            answer_question(self.store, "Promedio de venta")

    def test_disallowed_model_operation_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_intent({"action": "execute_sql", "rate": "sell"})

class WorkbookTests(unittest.TestCase):
    def test_included_workbook_contains_official_2026_series(self) -> None:
        workbook = Path(__file__).resolve().parents[1] / "data" / "tipo_cambio_bccr_2026.xlsx"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RateStore(Path(temp_dir) / "rates.sqlite3")
            count = store.import_excel(workbook, source="BCCR (Excel oficial 2026)")
            rows = store.read()
        self.assertEqual(count, 638)
        self.assertEqual(len({row.observed_on for row in rows}), 319)
        self.assertEqual(min(row.observed_on for row in rows), date(2026, 1, 1))
        self.assertEqual(max(row.observed_on for row in rows), date(2026, 11, 15))

    def test_agent_returns_maximum_sell_rate_from_included_workbook(self) -> None:
        workbook = Path(__file__).resolve().parents[1] / "data" / "tipo_cambio_bccr_2026.xlsx"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RateStore(Path(temp_dir) / "rates.sqlite3")
            store.import_excel(workbook, source="BCCR (Excel oficial 2026)")
            with patch(
                "data_agent.agent._ask_ollama",
                return_value={
                    "action": "maximum",
                    "rate": "sell",
                    "start": None,
                    "end": None,
                },
            ):
                result = answer_question(
                    store,
                    "¿Cuál fue el máximo del tipo de cambio de venta?",
                    model="qwen2.5:3b",
                )

        self.assertEqual(result["results"][0]["value"], 502.64)
        self.assertEqual(result["results"][0]["date"], "2026-01-03")
        self.assertEqual(result["results"][0]["rate"], "sell")

    def test_startup_refreshes_replaced_workbook_and_removes_stale_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workbook_path = root / "rates.xlsx"
            store = RateStore(root / "rates.sqlite3")

            first = Workbook()
            sheet = first.active
            sheet.append(["Fecha", "Compra", "Venta"])
            sheet.append([date(2026, 1, 5), 500.0, 505.0])
            sheet.append([date(2026, 1, 6), 501.0, 506.0])
            first.save(workbook_path)
            initialize_store(store, workbook_path)

            updated = Workbook()
            sheet = updated.active
            sheet.append(["Fecha", "Compra", "Venta"])
            sheet.append([date(2026, 1, 5), 499.0, 504.0])
            updated.save(workbook_path)
            initialize_store(store, workbook_path)

            rows = store.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].value, 499.0)
        self.assertEqual(rows[0].source, "BCCR (Excel oficial 2026)")

    def test_imports_excel_with_report_heading_above_table(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Reporte BCCR"])
        sheet.append(["Fecha", "Tipo cambio compra", "Tipo cambio venta"])
        sheet.append([date(2026, 1, 5), 499.8, 505.2])
        content = BytesIO()
        workbook.save(content)
        content.seek(0)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RateStore(Path(temp_dir) / "rates.sqlite3")
            self.assertEqual(store.import_excel(content), 2)
            self.assertEqual([row.rate_type for row in store.read()], ["buy", "sell"])


class WebTests(unittest.TestCase):
    def test_import_and_chat_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RateStore(Path(temp_dir) / "rates.sqlite3")
            store.upsert(
                [
                    Observation(date(2026, 1, 5), "buy", 499.8, "Test"),
                    Observation(date(2026, 1, 5), "sell", 505.2, "Test"),
                ]
            )
            handler = type("TestHandler", (DataAgentHandler,), {"store": store})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                workbook = Workbook()
                sheet = workbook.active
                sheet.append(["Fecha", "Compra", "Venta"])
                sheet.append([date(2026, 1, 6), 500.0, 506.0])
                file_bytes = BytesIO()
                workbook.save(file_bytes)
                boundary = "test-boundary-123"
                body = (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="file"; filename="rates.xlsx"\r\n'
                    "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
                ).encode() + file_bytes.getvalue() + f"\r\n--{boundary}--\r\n".encode()
                upload = Request(
                    f"{base_url}/api/import",
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                with urlopen(upload, timeout=3) as response:
                    self.assertEqual(response.status, 200)

                chat = Request(
                    f"{base_url}/api/chat",
                    data='{"question":"último dato de venta"}'.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with patch(
                    "data_agent.agent._ask_ollama",
                    return_value={
                        "action": "latest",
                        "rate": "sell",
                        "start": None,
                        "end": None,
                    },
                ):
                    with urlopen(chat, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(b'"value": 506.0', response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
