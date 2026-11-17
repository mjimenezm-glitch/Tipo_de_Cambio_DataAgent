from __future__ import annotations

import csv
import math
import sqlite3
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator, TextIO

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel
from openpyxl.utils.exceptions import InvalidFileException


@dataclass(frozen=True)
class Observation:
    observed_on: date
    rate_type: str
    value: float
    source: str


class RateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    observed_on TEXT NOT NULL,
                    rate_type TEXT NOT NULL CHECK (rate_type IN ('buy', 'sell')),
                    value REAL NOT NULL CHECK (value > 0),
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (observed_on, rate_type)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_date "
                "ON observations (observed_on)"
            )

    def count(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM observations").fetchone()
            return int(row["count"])

    def upsert(self, observations: list[Observation]) -> int:
        if not observations:
            return 0
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [
            (
                item.observed_on.isoformat(),
                item.rate_type,
                item.value,
                item.source,
                fetched_at,
            )
            for item in observations
        ]
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO observations (observed_on, rate_type, value, source, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (observed_on, rate_type) DO UPDATE SET
                    value = excluded.value,
                    source = excluded.source,
                    fetched_at = excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def replace_source(self, observations: list[Observation], source: str) -> int:
        rows = [
            (
                item.observed_on.isoformat(),
                item.rate_type,
                item.value,
                source,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            for item in observations
        ]
        with self._connection() as connection:
            connection.execute("DELETE FROM observations WHERE source = ?", (source,))
            if rows:
                connection.executemany(
                    """
                    INSERT INTO observations (observed_on, rate_type, value, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (observed_on, rate_type) DO UPDATE SET
                        value = excluded.value,
                        source = excluded.source,
                        fetched_at = excluded.fetched_at
                    """,
                    rows,
                )
        return len(rows)

    def read(
        self,
        start: date | None = None,
        end: date | None = None,
        rate_type: str | None = None,
    ) -> list[Observation]:
        clauses: list[str] = []
        params: list[str] = []
        if start:
            clauses.append("observed_on >= ?")
            params.append(start.isoformat())
        if end:
            clauses.append("observed_on <= ?")
            params.append(end.isoformat())
        if rate_type:
            clauses.append("rate_type = ?")
            params.append(rate_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT observed_on, rate_type, value, source
                FROM observations {where}
                ORDER BY observed_on, rate_type
                """,
                params,
            ).fetchall()
        return [
            Observation(
                observed_on=date.fromisoformat(row["observed_on"]),
                rate_type=row["rate_type"],
                value=float(row["value"]),
                source=row["source"],
            )
            for row in rows
        ]

    def import_csv(self, path: Path | TextIO, source: str = "CSV importado") -> int:
        observations: list[Observation] = []
        if hasattr(path, "read"):
            file_context = path
            should_close = False
        else:
            file_context = path.open("r", encoding="utf-8-sig", newline="")
            should_close = True
        try:
            reader = csv.DictReader(file_context)
            observations = self._parse_csv_rows(reader)
        finally:
            if should_close:
                file_context.close()
        if not observations:
            raise ValueError("El CSV no contiene observaciones.")
        observations = [
            Observation(item.observed_on, item.rate_type, item.value, source)
            for item in observations
        ]
        count = self.replace_source(observations, source)
        return count

    @staticmethod
    def _parse_csv_rows(reader: csv.DictReader) -> list[Observation]:
        observations: list[Observation] = []
        if not reader.fieldnames:
            raise ValueError("El CSV debe tener las columnas date,buy,sell.")
        required = {"date", "buy", "sell"}
        if not required.issubset({name.strip().lower() for name in reader.fieldnames}):
            raise ValueError("El CSV debe tener las columnas date,buy,sell.")
        for row_number, row in enumerate(reader, start=2):
            normalized = {key.strip().lower(): (value or "").strip() for key, value in row.items()}
            try:
                observed_on = date.fromisoformat(normalized["date"])
                buy = float(normalized["buy"].replace(",", "."))
                sell = float(normalized["sell"].replace(",", "."))
                if not math.isfinite(buy) or not math.isfinite(sell) or buy <= 0 or sell <= 0:
                    raise ValueError("Los valores deben ser mayores que cero.")
            except (KeyError, ValueError) as error:
                raise ValueError(f"Fila {row_number} inválida: {error}") from error
            observations.extend(
                [
                    Observation(observed_on, "buy", buy, ""),
                    Observation(observed_on, "sell", sell, ""),
                ]
            )
        return observations

    def import_excel(
        self,
        file: Path | BinaryIO,
        source: str = "Excel importado",
    ) -> int:
        try:
            workbook = load_workbook(file, read_only=True, data_only=True)
        except (OSError, ValueError, InvalidFileException, zipfile.BadZipFile) as error:
            raise ValueError(f"No se pudo leer el archivo Excel: {error}") from error

        observations: list[Observation] = []
        try:
            for sheet in workbook.worksheets:
                rows = sheet.iter_rows(values_only=True)
                header_row_number = None
                date_index = buy_index = sell_index = None
                data_rows = []
                for row_number, row in enumerate(rows, start=1):
                    normalized = [_normalize_header(value) for value in row]
                    found_date = next(
                        (index for index, name in enumerate(normalized) if "fecha" in name),
                        None,
                    )
                    found_buy = next(
                        (index for index, name in enumerate(normalized) if "compra" in name),
                        None,
                    )
                    found_sell = next(
                        (index for index, name in enumerate(normalized) if "venta" in name),
                        None,
                    )
                    if None not in (found_date, found_buy, found_sell):
                        header_row_number = row_number
                        date_index, buy_index, sell_index = found_date, found_buy, found_sell
                        data_rows = list(rows)
                        break

                if header_row_number is None:
                    continue

                for row_number, row in enumerate(
                    data_rows,
                    start=header_row_number + 1,
                ):
                    if not row or all(value is None for value in row):
                        continue
                    try:
                        raw_date = row[date_index] if date_index < len(row) else None
                        raw_buy = row[buy_index] if buy_index < len(row) else None
                        raw_sell = row[sell_index] if sell_index < len(row) else None
                        if raw_date is None or raw_buy is None or raw_sell is None:
                            raise ValueError("la fecha o uno de los valores está vacío")
                        observed_on = _excel_date(raw_date, workbook.epoch)
                        buy = _excel_number(raw_buy)
                        sell = _excel_number(raw_sell)
                        if buy <= 0 or sell <= 0:
                            raise ValueError("los valores deben ser mayores que cero")
                    except (IndexError, TypeError, ValueError) as error:
                        raise ValueError(
                            f"Fila {row_number} de la hoja {sheet.title!r} inválida: {error}"
                        ) from error
                    observations.extend(
                        [
                            Observation(observed_on, "buy", buy, source),
                            Observation(observed_on, "sell", sell, source),
                        ]
                    )
                if observations:
                    break
        finally:
            workbook.close()

        if not observations:
            raise ValueError(
                "No se encontró una tabla con columnas de fecha, compra y venta "
                "en el archivo Excel."
            )
        observations = [
            Observation(item.observed_on, item.rate_type, item.value, source)
            for item in observations
        ]
        count = self.replace_source(observations, source)
        return count


def _normalize_header(value: object) -> str:
    text = str(value or "").strip().lower()
    text = "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(character)
    )
    return "".join(character for character in text if character.isalnum())


def _excel_date(value: object, epoch: datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        converted = from_excel(value, epoch=epoch)
        if isinstance(converted, datetime):
            return converted.date()
        if isinstance(converted, date):
            return converted
    if isinstance(value, str):
        normalized = value.strip()
        try:
            return date.fromisoformat(normalized[:10])
        except ValueError:
            for pattern in ("%d/%m/%Y", "%Y/%m/%d"):
                try:
                    return datetime.strptime(normalized, pattern).date()
                except ValueError:
                    pass
    raise ValueError(f"fecha no reconocida: {value!r}")


def _excel_number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("valor numérico inválido")
    try:
        parsed = float(str(value).strip().replace(",", "."))
    except ValueError as error:
        raise ValueError(f"valor numérico no reconocido: {value!r}") from error
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError(f"valor numérico no finito: {value!r}")
    return parsed
