/* VM Pilot frontend helpers: actions, auto-refresh, uptime chart. */
async function vmAction(kind) {
  const msg = document.getElementById("action-msg");
  const map = { start: "/api/start", stop: "/api/stop", restart: "/api/restart", poll: "/api/poll" };
  const labels = { start: "запуск", stop: "остановку", restart: "рестарт", poll: "обновление" };
  if ((kind === "stop" || kind === "restart") && !confirm(`Точно ${labels[kind]} виртуалки?`)) return;
  if (msg) msg.textContent = "Выполняю…";
  try {
    const r = await fetch(map[kind], { method: "POST" });
    const d = await r.json();
    if (msg) msg.textContent = d.msg || (d.ok ? "OK" : "Ошибка");
    setTimeout(refreshStatus, 2500);
  } catch (e) {
    if (msg) msg.textContent = "Ошибка сети: " + e;
  }
}

async function refreshStatus() {
  const box = document.getElementById("vm-status");
  if (!box) return;
  try {
    const r = await fetch("/api/status");
    const p = await r.json();
    if (p.active && p.run) {
      box.innerHTML = `
        <div class="pill run">● РАБОТАЕТ</div>
        <table class="kv">
          <tr><td>Запуск</td><td>#${p.run.number} <a href="${p.run.url}" target="_blank">↗</a></td></tr>
          <tr><td>Старт</td><td>${p.run.started}</td></tr>
          <tr><td>Работает</td><td>${p.run.elapsed}</td></tr>
          <tr><td>Осталось до лимита 6ч</td><td>${p.run.left_min} мин</td></tr>
        </table>
        <div class="bar"><div class="fill" style="width:${p.run.pct}%"></div></div>`;
    } else if (p.configured) {
      box.innerHTML = `<div class="pill stop">○ ОСТАНОВЛЕНА</div><p class="muted">Активных запусков workflow нет.</p>`;
    }
  } catch (e) { /* silent */ }
}

function drawChart() {
  const cv = document.getElementById("uptime-chart");
  if (!cv) return;
  const data = JSON.parse(cv.dataset.chart || "[]");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, pad = 28;
  ctx.clearRect(0, 0, W, H);
  const max = Math.max(6 * 3600, ...data.map(d => d.uptime_sec));
  const bw = (W - pad * 2) / Math.max(1, data.length);
  ctx.fillStyle = "#8b96b3"; ctx.font = "10px sans-serif";
  data.forEach((d, i) => {
    const h = (H - pad * 2) * (d.uptime_sec / max);
    const x = pad + i * bw + 2, y = H - pad - h;
    ctx.fillStyle = d.uptime_sec > 0 ? "#34c77b" : "#26314a";
    ctx.fillRect(x, y, bw - 4, Math.max(2, h));
    if (i % 2 === 0) {
      ctx.fillStyle = "#8b96b3";
      ctx.fillText(d.day.slice(5), x, H - 8);
    }
    if (d.uptime_sec > 0) {
      ctx.fillStyle = "#e8edf7";
      ctx.fillText(Math.round(d.uptime_sec / 3600) + "ч", x, y - 4);
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  drawChart();
  if (document.getElementById("vm-status")) setInterval(refreshStatus, 30000);
});
