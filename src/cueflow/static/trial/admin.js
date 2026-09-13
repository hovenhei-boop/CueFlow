let bearer = sessionStorage.getItem("cueflow-trial-operator") || "";
const gate = document.querySelector("#operator-gate");
const dashboard = document.querySelector("#dashboard");
const authHeaders = () => ({ "Authorization": `Bearer ${bearer}`, "Content-Type": "application/json" });

document.querySelector("#operator-form").addEventListener("submit", async event => {
  event.preventDefault();
  bearer = document.querySelector("#operator-secret").value;
  sessionStorage.setItem("cueflow-trial-operator", bearer);
  await loadDashboard();
});

function money(value) { return value == null ? "—" : `¥${(value / 1000000).toFixed(2)}`; }
function percent(value) { return value == null ? "—" : `${(value * 100).toFixed(1)}%`; }
function duration(value) { return value == null ? "—" : value < 60000 ? `${(value/1000).toFixed(1)} 秒` : `${(value/60000).toFixed(1)} 分`; }

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || "请求失败");
  return body;
}

async function loadDashboard() {
  try {
    const [summary, requests] = await Promise.all([api("/trial/admin/summary"), api("/trial/admin/requests?limit=100")]);
    gate.hidden = true; dashboard.hidden = false;
    document.querySelectorAll("[data-metric]").forEach(node => node.textContent = summary[node.dataset.metric] ?? "—");
    document.querySelectorAll("[data-money]").forEach(node => node.textContent = money(summary[node.dataset.money]));
    document.querySelectorAll("[data-percent]").forEach(node => node.textContent = percent(summary[node.dataset.percent]));
    document.querySelectorAll("[data-duration]").forEach(node => node.textContent = duration(summary[node.dataset.duration]));
    const state = document.querySelector("#service-state");
    state.textContent = summary.paused ? "已暂停" : "接收中";
    state.className = `job-status ${summary.paused ? "interrupted" : "succeeded"}`;
    document.querySelector("#updated-at").textContent = `更新于 ${new Date().toLocaleTimeString()} · 当前预算 ${money(summary.daily_budget_micros)}`;
    renderRequests(requests.requests);
  } catch (error) {
    gate.hidden = false; dashboard.hidden = true;
    document.querySelector("#operator-error").textContent = error.message;
  }
}

function renderRequests(rows) {
  const tbody = document.querySelector("#request-rows"); tbody.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    const createdAt = row.created_at ? new Date(row.created_at).toLocaleString() : "—";
    const jobId = row.job_id ? String(row.job_id).slice(-8) : "—";
    const values = [createdAt, jobId, row.action_kind || "—", row.execution_status || "—", `${Math.round((row.audio_duration_ms || 0)/60000)} 分`, row.cost_status || "—"];
    for (const value of values) { const td = document.createElement("td"); td.textContent = value; tr.append(td); }
    tbody.append(tr);
  }
}

async function control(path, method = "POST", extra = {}) {
  const reason = document.querySelector("#control-reason").value.trim();
  const node = document.querySelector("#control-status");
  if (!reason) { node.className = "form-status error"; node.textContent = "请填写操作原因。"; return; }
  try { await api(path, { method, body: JSON.stringify({ reason, ...extra }) }); node.className = "form-status success"; node.textContent = "控制变更已记录。"; await loadDashboard(); }
  catch (error) { node.className = "form-status error"; node.textContent = error.message; }
}

document.querySelector("#refresh-dashboard").addEventListener("click", loadDashboard);
document.querySelector("#pause-service").addEventListener("click", () => control("/trial/admin/pause"));
document.querySelector("#resume-service").addEventListener("click", () => control("/trial/admin/resume"));
document.querySelector("#set-budget").addEventListener("click", () => {
  const yuan = Number(document.querySelector("#budget-yuan").value);
  if (!Number.isFinite(yuan) || yuan <= 0) return;
  control("/trial/admin/daily-budget-override", "POST", { daily_budget_micros: Math.round(yuan * 1000000) });
});
document.querySelector("#clear-budget").addEventListener("click", () => control("/trial/admin/daily-budget-override", "DELETE"));
if (bearer) loadDashboard();
