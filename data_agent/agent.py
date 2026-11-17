from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Any

from data_agent.store import Observation, RateStore


ALLOWED_ACTIONS = {
    "latest",
    "summary",
    "average",
    "maximum",
    "minimum",
    "change",
    "history",
    "unsupported",
}
ALLOWED_RATES = {"buy", "sell", "both"}
OLLAMA_SYSTEM_PROMPT = """Eres el intérprete de preguntas de un Data Agent sobre el tipo de cambio USD/CRC.
Devuelve solo JSON con las claves action, rate, start y end.
action debe ser latest, summary, average, maximum, minimum, change, history o unsupported.
rate debe ser buy, sell o both.
start y end deben ser fechas YYYY-MM-DD o null.
No respondas la pregunta ni inventes datos. Si no hay rango explícito, usa null.
Interpreta expresiones como "este año", "el mes pasado" y "primer trimestre" usando
el periodo del conjunto de datos y la fecha actual que se proporciona en el contexto.
Si piden una variable no disponible o una predicción, usa unsupported.
latest = último dato; summary = resumen; average = promedio; maximum/minimum = extremos;
change = variación entre límites; history = serie histórica.
unsupported = predicción, consejo financiero u otra solicitud no cubierta.
Usa both si no se especifica compra o venta."""


def _ask_ollama(
    question: str,
    model: str,
    base_url: str,
    context: str,
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": f"{OLLAMA_SYSTEM_PROMPT}\n\nContexto actual de los datos:\n{context}",
                },
                {"role": "user", "content": question},
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"No se pudo consultar Ollama ({model}): {error}") from error
    try:
        intent = json.loads(response_data["message"]["content"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Ollama no devolvió un plan JSON válido.") from error
    return validate_intent(intent)


def validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
    action = intent.get("action")
    rate = intent.get("rate")
    if action not in ALLOWED_ACTIONS or rate not in ALLOWED_RATES:
        raise ValueError("El plan del agente contiene una operación o indicador no permitido.")
    validated: dict[str, Any] = {"action": action, "rate": rate, "start": None, "end": None}
    for field in ("start", "end"):
        value = intent.get(field)
        if value is not None:
            try:
                validated[field] = date.fromisoformat(value).isoformat()
            except (TypeError, ValueError) as error:
                raise ValueError(f"Fecha inválida en el plan del agente: {field}.") from error
    if validated["start"] and validated["end"] and validated["start"] > validated["end"]:
        raise ValueError("La fecha inicial del plan es posterior a la fecha final.")
    return validated


def _format_money(value: float) -> str:
    return f"₡{value:,.2f}"


def _rows_by_rate(rows: list[Observation]) -> dict[str, list[Observation]]:
    return {
        rate: sorted((row for row in rows if row.rate_type == rate), key=lambda row: row.observed_on)
        for rate in ("buy", "sell")
    }


def answer_question(
    store: RateStore,
    question: str,
    *,
    model: str = "",
    ollama_url: str = "http://127.0.0.1:11434",
) -> dict[str, Any]:
    if not question.strip():
        raise ValueError("Escribe una pregunta.")
    rows = store.read()
    if not rows:
        raise ValueError("No hay datos cargados. Coloca el Excel oficial en data/tipo_cambio_bccr_2026.xlsx y reinicia.")
    if model:
        coverage = {
            rate: sum(1 for row in rows if row.rate_type == rate)
            for rate in ("buy", "sell")
        }
        sources = ", ".join(sorted({row.source for row in rows}))
        context = (
            f"Fecha actual: {date.today().isoformat()}. "
            f"Datos USD/CRC disponibles desde {min(row.observed_on for row in rows).isoformat()} "
            f"hasta {max(row.observed_on for row in rows).isoformat()}. "
            f"Observaciones: compra={coverage['buy']}, venta={coverage['sell']}. "
            f"Fuentes registradas: {sources}. "
            "Los únicos campos consultables son fecha, tipo (compra/venta) y valor en CRC por USD."
        )
        intent = _ask_ollama(question, model, ollama_url, context)
    else:
        raise RuntimeError(
            "Ollama es necesario para interpretar preguntas. Configura OLLAMA_MODEL "
            "y asegúrate de que Ollama esté iniciado."
        )
    action = intent["action"]
    if action == "unsupported":
        raise ValueError(
            "No reconocí una consulta compatible. Prueba con último dato, promedio, "
            "máximo, mínimo, cambio entre fechas o evolución histórica."
        )
    rate = intent["rate"]
    start = date.fromisoformat(intent["start"]) if intent["start"] else None
    end = date.fromisoformat(intent["end"]) if intent["end"] else None
    data = store.read(start, end)
    selected = _rows_by_rate(data)
    rates = ("buy", "sell") if rate == "both" else (rate,)
    results: list[dict[str, Any]] = []
    chart: list[dict[str, Any]] = []
    observation_counts: dict[str, int] = {}

    for rate_type in rates:
        items = selected[rate_type]
        if not items:
            continue
        observation_counts[rate_type] = len(items)
        label = "Compra" if rate_type == "buy" else "Venta"
        if action == "latest":
            chosen = items[-1]
            summary = f"Tipo de cambio de {label.lower()} más reciente: {_format_money(chosen.value)} "
            summary += f"el {chosen.observed_on.isoformat()}."
            value = chosen.value
            result_date = chosen.observed_on.isoformat()
        elif action == "average":
            value = sum(item.value for item in items) / len(items)
            summary = f"Promedio de {label.lower()}: {_format_money(value)} en {len(items)} observaciones."
            result_date = None
        elif action in {"maximum", "minimum"}:
            chosen = (max if action == "maximum" else min)(items, key=lambda item: item.value)
            value = chosen.value
            extreme = "máximo" if action == "maximum" else "mínimo"
            summary = f"{extreme.capitalize()} de {label.lower()}: {_format_money(value)} "
            summary += f"el {chosen.observed_on.isoformat()}."
            result_date = chosen.observed_on.isoformat()
        elif action == "change":
            first, last = items[0], items[-1]
            value = last.value - first.value
            percent = (value / first.value * 100) if first.value else 0
            summary = (
                f"Cambio de {label.lower()} entre {first.observed_on.isoformat()} "
                f"y {last.observed_on.isoformat()}: {_format_money(value)} "
                f"({percent:+.2f}%)."
            )
            result_date = None
        elif action == "summary":
            first, last = items[0], items[-1]
            change = last.value - first.value
            percent = change / first.value * 100 if first.value else 0
            value = last.value
            summary = (
                f"{label}: {len(items)} observaciones entre {first.observed_on.isoformat()} "
                f"y {last.observed_on.isoformat()}; pasó de {_format_money(first.value)} "
                f"a {_format_money(last.value)} ({change:+.2f}, {percent:+.2f}%)."
            )
            result_date = last.observed_on.isoformat()
        else:
            first, last = items[0], items[-1]
            value = last.value
            summary = (
                f"Serie de {label.lower()}: {len(items)} observaciones, "
                f"del {first.observed_on.isoformat()} al {last.observed_on.isoformat()}."
            )
            result_date = None
        results.append(
            {
                "rate": rate_type,
                "label": label,
                "value": round(value, 4),
                "date": result_date,
                "summary": summary,
            }
        )
        chart.extend(
            {
                "date": item.observed_on.isoformat(),
                "rate": rate_type,
                "value": item.value,
            }
            for item in items
        )

    if not results:
        raise ValueError("No hay datos para ese indicador y periodo. Importa un archivo Excel o CSV.")

    start_used = min(
        item["date"] for item in chart
    )
    end_used = max(
        item["date"] for item in chart
    )
    sources = sorted({item.source for item in data if item.rate_type in rates})
    response_text = " ".join(result["summary"] for result in results)
    response_text += f" Periodo observado: {start_used} a {end_used}."
    response_text += f" Fuente: {', '.join(sources)}."
    return {
        "answer": response_text,
        "intent": intent,
        "results": results,
        "observations": observation_counts,
        "sources": sources,
        "chart": chart,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
