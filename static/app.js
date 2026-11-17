const form = document.querySelector("#question-form");
const questionInput = document.querySelector("#question");
const answerPanel = document.querySelector("#answer");
const errorPanel = document.querySelector("#error");
const submitButton = document.querySelector("#submit-button");
const importForm = document.querySelector("#import-form");
const importButton = document.querySelector("#import-button");
const importStatus = document.querySelector("#import-status");
const chart = document.querySelector("#chart");
const NS = "http://www.w3.org/2000/svg";

document.querySelectorAll("[data-question]").forEach((button) => {
  button.addEventListener("click", () => {
    questionInput.value = button.dataset.question;
    questionInput.focus();
  });
});

importForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.querySelector("#data-file").files[0];
  if (!file) return;
  importButton.disabled = true;
  importStatus.textContent = "Importando y validando el archivo…";
  importStatus.className = "import-status";
  try {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch("/api/import", { method: "POST", body: formData });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "No fue posible importar el archivo.");
    importStatus.textContent = `${payload.message} Registro disponible hasta ${payload.latest_date || "sin fecha"}.`;
    importStatus.classList.add("success");
    updateStatus(payload);
  } catch (error) {
    importStatus.textContent = error.message;
    importStatus.classList.add("failure");
  } finally {
    importButton.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  answerPanel.hidden = true;
  errorPanel.hidden = true;
  submitButton.disabled = true;
  submitButton.textContent = "Analizando…";
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: questionInput.value }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "No fue posible procesar la pregunta.");
    renderAnswer(payload);
  } catch (error) {
    errorPanel.textContent = error.message;
    errorPanel.hidden = false;
  } finally {
    submitButton.disabled = false;
    submitButton.innerHTML = 'Analizar <span aria-hidden="true">→</span>';
  }
});

function renderAnswer(payload) {
  document.querySelector("#answer-text").textContent = payload.answer;
  document.querySelector("#results").replaceChildren(
    ...payload.results.map((result) => {
      const row = document.createElement("tr");
      for (const value of [result.label, `₡${Number(result.value).toLocaleString("es-CR", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`, result.date || "—"]) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      }
      return row;
    }),
  );
  document.querySelector("#provenance").textContent =
    `Cobertura: ${Object.entries(payload.observations).map(([rate, count]) => `${rate === "buy" ? "compra" : "venta"} ${count}`).join(", ")} observaciones · Fuente: ${payload.sources.join(", ")} · ` +
    `Operación: ${payload.intent.action} · Consultado: ${new Date(payload.generated_at).toLocaleString("es-CR")}`;
  drawChart(payload.chart);
  answerPanel.hidden = false;
}

function drawChart(points) {
  chart.replaceChildren();
  const width = 760;
  const height = 240;
  const pad = { left: 52, right: 12, top: 16, bottom: 30 };
  const allValues = points.map((point) => point.value);
  if (!allValues.length) return;
  const dates = [...new Set(points.map((point) => point.date))].sort();
  const dateIndexes = new Map(dates.map((date, index) => [date, index]));
  let min = Math.min(...allValues);
  let max = Math.max(...allValues);
  if (min === max) { min -= 1; max += 1; }
  const x = (index, count) => pad.left + (count < 2 ? (width - pad.left - pad.right) / 2 : index * (width - pad.left - pad.right) / (count - 1));
  const y = (value) => pad.top + (max - value) * (height - pad.top - pad.bottom) / (max - min);
  for (let tick = 0; tick < 4; tick += 1) {
    const value = min + (max - min) * tick / 3;
    const line = svgElement("line", { x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value), class: "grid" });
    const label = svgElement("text", { x: pad.left - 7, y: y(value) + 3, "text-anchor": "end", class: "axis" });
    label.textContent = value.toFixed(2);
    chart.append(line, label);
  }
  const rates = [...new Set(points.map((point) => point.rate))];
  for (const rate of rates) {
    const series = points.filter((point) => point.rate === rate);
    const color = rate === "buy" ? "#228b80" : "#e3a34f";
    const coords = series.map((point) => `${x(dateIndexes.get(point.date), dates.length)},${y(point.value)}`).join(" ");
    chart.append(svgElement("polyline", { points: coords, fill: "none", stroke: color, "stroke-width": 2.5, "stroke-linecap": "round", "stroke-linejoin": "round" }));
    series.forEach((point) => {
      const dot = svgElement("circle", { cx: x(dateIndexes.get(point.date), dates.length), cy: y(point.value), r: 3, fill: color });
      const title = svgElement("title", {});
      title.textContent = `${point.date}: ₡${point.value.toFixed(2)}`;
      dot.append(title);
      chart.append(dot);
    });
  }
  for (const index of [0, Math.floor((dates.length - 1) / 2), dates.length - 1]) {
    if (dates[index] === undefined || index !== dates.indexOf(dates[index])) continue;
    const label = svgElement("text", { x: x(index, dates.length), y: height - 8, "text-anchor": index === 0 ? "start" : index === dates.length - 1 ? "end" : "middle", class: "axis" });
    label.textContent = dates[index];
    chart.append(label);
  }
}

function svgElement(tag, attributes) {
  const element = document.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  return element;
}

fetch("/api/status")
  .then((response) => response.json())
  .then(updateStatus)
  .catch(() => {
    document.querySelector("#status").textContent = "No se pudo leer el estado";
    document.querySelector("#source-name").textContent = "Fuente no disponible";
  });

function updateStatus(status) {
  document.querySelector("#status").textContent = status.latest_date
    ? `Datos disponibles hasta ${status.latest_date}`
    : "Sin datos disponibles";
  document.querySelector("#source-name").textContent = status.sources.length ? status.sources.join(", ") : "Sin fuente configurada";
  document.querySelector("#demo-notice").hidden = status.count > 0;
}
