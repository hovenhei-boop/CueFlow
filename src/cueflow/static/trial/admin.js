(() => {
  "use strict";
  const ui = window.CueFlowUI;
  const $ = selector => document.querySelector(selector);
  const gate = $("#operator-gate"), dashboard = $("#dashboard"), dialog = $("#control-dialog");
  let bearer = "";
  try { bearer = sessionStorage.getItem("cueflow-trial-operator") || ""; } catch (_) { /* Memory-only access remains available. */ }
  let loading = null, controlling = false, pendingControl = null, lastSummary = null, lastUpdated = null;
  const authHeaders = () => ({"Authorization": "Bearer " + bearer, "Content-Type": "application/json"});
  const api = (path, options = {}) => ui.requestJson(path, {...options, headers: authHeaders()});

  $("#operator-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (loading) return;
    bearer = $("#operator-secret").value;
    try { sessionStorage.setItem("cueflow-trial-operator", bearer); } catch (_) { /* Do not require browser storage. */ }
    await loadDashboard();
  });
  function renderService() {
    const paused = lastSummary?.paused;
    $("#service-state").textContent = paused === true ? "接收已暂停" : paused === false ? "接收已开启" : "状态待确认";
    $("#service-state").className = "job-status";
    $("#pause-service").hidden = paused === true;
    $("#resume-service").hidden = paused !== true;
    $("#pause-service").disabled = controlling || typeof paused !== "boolean";
    $("#resume-service").disabled = controlling || typeof paused !== "boolean";
    for (const id of ["set-budget", "clear-budget", "budget-yuan"]) $( "#" + id).disabled = controlling;
    $("#storage-state").textContent = lastSummary?.storage_ready === true ? "通过" : lastSummary?.storage_ready === false ? "未通过" : "待确认";
    $("#disk-state").textContent = lastSummary?.disk?.ready === true ? "充足" : lastSummary?.disk?.ready === false ? "不足或无法确认" : "待确认";
  }
  async function loadDashboard(fresh = false) {
    if (loading) { if (!fresh) return loading; await loading; }
    loading = (async () => {
      $("#refresh-dashboard").setAttribute("aria-busy", "true");
      $("#operator-form button").disabled = true;
      try {
        const [summary, requests] = await Promise.all([api("/trial/admin/summary"), api("/trial/admin/requests?limit=100")]);
        if (!Array.isArray(requests.requests)) throw new Error("请求记录返回异常，请稍后刷新。");
        lastSummary = summary; lastUpdated = new Date();
        gate.hidden = true; dashboard.hidden = false;
        for (const node of document.querySelectorAll("[data-metric]")) ui.setText(node, String(summary[node.dataset.metric] ?? "—"));
        for (const node of document.querySelectorAll("[data-money]")) ui.setText(node, ui.money(summary[node.dataset.money]));
        for (const node of document.querySelectorAll("[data-percent]")) ui.setText(node, ui.percent(summary[node.dataset.percent]));
        for (const node of document.querySelectorAll("[data-duration]")) ui.setText(node, summary[node.dataset.duration] == null ? "—" : ui.duration(summary[node.dataset.duration]));
        renderService(); renderRequests(requests.requests);
        $("#updated-at").textContent = "更新于 " + lastUpdated.toLocaleString("zh-CN", {hour12: false});
        $("#dashboard-status").textContent = "";
        $("#operator-error").textContent = "";
      } catch (error) {
        if (error.status === 401) {
          if (dialog.open) dialog.close();
          gate.hidden = false; dashboard.hidden = true; $("#operator-secret").focus();
        }
        const node = dashboard.hidden ? $("#operator-error") : $("#dashboard-status");
        node.className = "form-status error";
        node.textContent = error.message + (lastUpdated && !dashboard.hidden ? " 已保留上次成功加载的数据。" : "");
      } finally {
        $("#refresh-dashboard").removeAttribute("aria-busy");
        $("#operator-form button").disabled = false;
      }
    })();
    try { await loading; } finally { loading = null; }
  }
  function renderRequests(rows) {
    const tbody = $("#request-rows");
    const costs = {calculated: "已计算", pending: "待确认", unknown: "未知", not_incurred: "未发生", non_billable: "不计费"};
    const actions = {create: "创建", retry: "重试", resume: "继续"};
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const tr = document.createElement("tr");
      const values = [
        ui.dateTime(row.created_at), row.job_id ? String(row.job_id).slice(-8) : "—",
        actions[row.action_kind] || "待确认", ui.stateInfo(row.execution_status).label,
        ui.duration(row.audio_duration_ms), costs[row.cost_status] || "待确认"
      ];
      for (const value of values) { const td = document.createElement("td"); td.textContent = value; tr.append(td); }
      fragment.append(tr);
    }
    if (!rows.length) {
      const tr = document.createElement("tr"), td = document.createElement("td");
      td.colSpan = 6; td.textContent = "暂无请求记录。"; tr.append(td); fragment.append(tr);
    }
    tbody.replaceChildren(fragment);
    $("#request-count").textContent = "当前 " + rows.length + " 条，最多显示最近 100 条请求。一条请求不等于一个任务。";
  }
  function openControl(action, opener, extra = {}) {
    if (controlling) return;
    const controls = {
      pause: ["暂停新任务", "暂停接收新任务，正在处理的任务会继续运行。", "/trial/admin/pause", "POST"],
      resume: ["恢复接收", "恢复接收仍须满足服务器的容量、预算和其他准入条件。", "/trial/admin/resume", "POST"],
      set: ["设置预算覆盖", "将当日预算覆盖设为 " + ui.money(extra.daily_budget_micros) + "。", "/trial/admin/daily-budget-override", "POST"],
      clear: ["清除预算覆盖", "清除当日覆盖值，恢复部署配置的默认预算。", "/trial/admin/daily-budget-override", "DELETE"]
    };
    const [title, description, path, method] = controls[action];
    pendingControl = {path, method, extra, opener};
    $("#control-title").textContent = title; $("#confirm-control").textContent = title;
    $("#control-description").textContent = description;
    $("#control-reason").value = ""; $("#dialog-status").textContent = "";
    dialog.showModal(); $("#control-reason").focus();
  }
  $("#cancel-control").addEventListener("click", () => { if (!controlling) dialog.close(); });
  dialog.addEventListener("cancel", event => { if (controlling) event.preventDefault(); });
  dialog.addEventListener("close", () => {
    const opener = pendingControl?.opener;
    if (opener && !opener.hidden && !opener.disabled && !dashboard.hidden) opener.focus();
    else if (!dashboard.hidden) $("#refresh-dashboard").focus();
  });
  $("#control-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!pendingControl || controlling) return;
    const reason = $("#control-reason").value.trim();
    if (!reason) { $("#dialog-status").textContent = "请填写操作原因。"; $("#control-reason").focus(); return; }
    controlling = true; renderService();
    for (const control of $("#control-form").querySelectorAll("button, textarea")) control.disabled = true;
    $("#dialog-status").textContent = "正在提交…";
    const {path, method, extra} = pendingControl;
    let completed = false;
    try {
      await api(path, {method, body: JSON.stringify({reason, ...extra})});
      $("#control-status").className = "form-status success";
      $("#control-status").textContent = "控制变更已记录。";
      // Keep the mutation locked while the confirmed server state is being refreshed.
      await loadDashboard(true);
      completed = true;
    } catch (error) {
      $("#dialog-status").textContent = error.uncertain ? "操作结果尚未确认。请取消此窗口并刷新数据核对，不要直接重复提交。" : error.message;
    } finally {
      controlling = false;
      for (const control of $("#control-form").querySelectorAll("button, textarea")) control.disabled = false;
      renderService();
    }
    if (completed) dialog.close();
  });
  $("#refresh-dashboard").addEventListener("click", () => loadDashboard());
  $("#pause-service").addEventListener("click", event => openControl("pause", event.currentTarget));
  $("#resume-service").addEventListener("click", event => openControl("resume", event.currentTarget));
  $("#set-budget").addEventListener("click", event => {
    const input = $("#budget-yuan"), yuan = Number(input.value), micros = Math.round(yuan * 1000000);
    if (!input.value || !input.checkValidity() || !Number.isSafeInteger(micros) || micros <= 0) {
      $("#control-status").className = "form-status error";
      $("#control-status").textContent = "请填写有效的预算金额，至少 0.01 元，以分为单位。";
      input.focus(); return;
    }
    openControl("set", event.currentTarget, {daily_budget_micros: micros});
  });
  $("#clear-budget").addEventListener("click", event => openControl("clear", event.currentTarget));
  if (bearer) loadDashboard();
})();
