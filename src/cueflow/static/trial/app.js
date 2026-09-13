/* Shared presentation helpers; the operator page loads these without starting visitor activity. */
window.CueFlowUI = (() => {
  "use strict";
  const messages = {
    visitor_blocked: "当前浏览器的试用访问已受限。",
    trial_paused: "试用服务已暂停接收新任务，请稍后再试。",
    visitor_hourly_quota: "本小时的试用次数已用完，请稍后再试。",
    visitor_daily_quota: "今日试用次数已用完，请改日再试。",
    visitor_daily_audio_quota: "今日可处理的音频时长已用完，请改日再试。",
    ip_hourly_quota: "当前网络本小时的试用次数已用完，请稍后再试。",
    ip_daily_quota: "当前网络今日的试用次数已用完，请改日再试。",
    visitor_concurrency: "已有任务正在处理，请等待完成后再提交。",
    ip_concurrency: "当前网络同时处理的任务较多，请稍后再试。",
    global_concurrency: "当前使用人数较多，请稍后再试。",
    daily_budget: "试用服务暂时无法接收新任务，请稍后再试。",
    disk_capacity: "试用服务暂时无法接收新任务，请稍后再试。",
    storage_not_ready: "试用服务暂时无法接收新任务，请稍后再试。",
    invalid_media: "无法处理这份媒体。请检查文件是否完整、可播放，且时长少于 60 分钟。",
    media_required: "请选择一份音频或视频。",
    empty_upload: "文件为空，请重新选择。",
    too_many_references: "辅助材料最多可添加 20 份。",
    too_many_keywords: "关键词最多可填写 100 个。",
    upload_too_large: "上传内容过大。媒体需小于 500 MB，辅助材料合计需小于 100 MB。",
    not_found: "记录或结果暂不可用，请刷新记录后重试。",
    invalid_request: "提交内容不符合要求，请检查输入后重试。",
    stale_worker: "任务处理已中断。",
    operationally_stopped: "任务处理已停止。"
  };
  function formatTrialError({status = 0, code} = {}) {
    if (status === 413) return "上传内容过大，超过服务器限制。媒体需小于 500 MB；若已符合，请缩小文件或减少辅助材料后重试。";
    if (code && Object.hasOwn(messages, code)) return messages[code];
    if (code === "invalid_response") return "服务返回内容异常，请刷新后核对结果。";
    return ({
      400: "提交内容不符合要求，请检查输入后重试。",
      401: "访问凭据无效或已失效，请重新输入。",
      403: "当前请求未获允许，请刷新页面后重试。",
      404: "记录或结果暂不可用，请刷新后重试。",
      408: "请求超时，请检查连接后重试。",
      429: "请求过于频繁，请稍后再试。",
      500: "服务暂时出现异常，请稍后再试。",
      502: "服务暂时无法连接，请稍后再试。",
      503: "服务暂时不可用，请稍后再试。",
      504: "服务响应超时，请稍后再试。"
    })[status] || (status >= 400 ? "请求未能完成，请稍后再试。" : "连接中断或请求超时，请检查网络。");
  }
  class TrialUIError extends Error {
    constructor(status, code, uncertain = false) {
      super(formatTrialError({status, code}));
      this.status = status;
      this.code = code;
      this.uncertain = uncertain;
    }
  }
  async function requestJson(path, options = {}) {
    const {timeoutMs = 30000, ...init} = options;
    const controller = new AbortController();
    const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
    let response;
    try {
      response = await fetch(path, {cache: "no-store", ...init, signal: controller.signal});
      let body = null;
      try { body = JSON.parse(await response.text()); } catch (_) { /* Use HTTP status, never proxy HTML. */ }
      if (!response.ok) throw new TrialUIError(response.status, body?.error?.code, response.status >= 500 || response.status === 408);
      if (!body || typeof body !== "object" || Array.isArray(body)) {
        throw new TrialUIError(response.status, "invalid_response", true);
      }
      return body;
    } catch (error) {
      if (error instanceof TrialUIError) throw error;
      throw new TrialUIError(response?.status || 0, undefined, true);
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }
  function duration(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "时长待确认";
    if (value > 0 && value < 1000) return "不足 1 秒";
    const seconds = Math.floor(value / 1000);
    return seconds < 60 ? seconds + " 秒" : Math.floor(seconds / 60) + " 分 " + seconds % 60 + " 秒";
  }
  function money(value) {
    return typeof value === "number" && Number.isFinite(value) ? "¥" + (value / 1000000).toFixed(2) : "—";
  }
  function percent(value) {
    return typeof value === "number" && Number.isFinite(value) ? (value * 100).toFixed(1) + "%" : "—";
  }
  function dateTime(value) {
    if (!value) return "时间待确认";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "时间待确认" : date.toLocaleString("zh-CN", {hour12: false});
  }
  const states = {
    queued: ["准备开始", "", ""],
    running: ["处理中", "", "任务正在处理，可稍后回到这个浏览器查看结果。"],
    succeeded: ["已完成", "✓", ""],
    failed: ["执行失败", "×", "任务未能完成，可重新选择文件提交新任务。"],
    interrupted: ["已中断", "!", "任务处理已中断，可重新选择文件提交新任务。"],
    cancelled: ["已取消", "−", "该任务已取消。"],
    rejected: ["未接受", "!", "任务未被接受，请检查输入或稍后再试。"],
    needs_review: ["需要人工复核", "!", "该任务需要人工复核。当前 Trial 版暂不支持在线复核。"]
  };
  function stateInfo(status) {
    const value = Object.hasOwn(states, status) ? states[status] : null;
    return value ? {kind: status, label: value[0], symbol: value[1], note: value[2]}
      : {kind: "unknown", label: "状态待确认", symbol: "?", note: "暂时无法确认任务状态，请刷新记录。"};
  }
  function validateMedia(files) {
    if (files.length !== 1) return "一次只能上传一份媒体，请重新选择；不会自动取第一个文件。";
    if (!files[0].size) return "文件为空，请重新选择。";
    if (files[0].size >= 500000000) return "媒体文件需小于 500 MB，请重新选择。";
    return "";
  }
  function keywords(value) { return value.split(/\r?\n/).map(v => v.trim()).filter(Boolean); }
  function setText(node, value) { if (node.textContent !== value) node.textContent = value; }

  // One timer and one in-flight request per stream. Restarting never replays missed ticks.
  function createVisiblePoller(task, interval, environment = {}) {
    const visibility = environment.visibility || document;
    const schedule = environment.setTimer || setTimeout;
    const cancel = environment.clearTimer || clearTimeout;
    let timer = null, inFlight = null, active = false;
    const clear = () => { if (timer !== null) cancel(timer); timer = null; };
    const visible = () => visibility.visibilityState === "visible";
    function refresh() {
      if (!active || !visible()) return Promise.resolve();
      if (inFlight) return inFlight;
      clear();
      inFlight = Promise.resolve().then(task).catch(() => {}).finally(() => {
        inFlight = null;
        if (active && visible()) timer = schedule(refresh, interval);
      });
      return inFlight;
    }
    function change() { clear(); if (visible()) refresh(); }
    return {
      start() {
        if (!active) { active = true; visibility.addEventListener("visibilitychange", change); }
        return refresh();
      },
      stop() { active = false; clear(); visibility.removeEventListener("visibilitychange", change); },
      async refresh(fresh = false) {
        if (fresh && inFlight) await inFlight;
        return refresh();
      }
    };
  }
  return {formatTrialError, TrialUIError, requestJson, duration, money, percent, dateTime,
    stateInfo, validateMedia, keywords, setText, createVisiblePoller};
})();

(() => {
  "use strict";
  const form = document.querySelector("#job-form");
  if (!form) return;
  const ui = window.CueFlowUI;
  const $ = selector => document.querySelector(selector);
  const mediaInput = $("#media"), refsInput = $("#references"), keywordInput = $("#keywords");
  const dropZone = $("#drop-zone"), submit = $("#submit-button"), statusNode = $("#form-status");
  const list = $("#job-list"), more = $("#show-earlier");
  let selectedMedia = null, references = [], submitting = false;
  let jobs = [], visibleCount = 100, loaded = false, lastUpdated = null, visitorInitialized = false;
  const rows = new Map();
  const fingerprint = JSON.stringify({
    browser: navigator.userAgent, language: navigator.language,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: screen.width + "x" + screen.height + "@" + (window.devicePixelRatio || 1),
    platform: navigator.platform || "unknown"
  });
  const headers = () => ({"X-CueFlow-Fingerprint": fingerprint});
  function status(message, kind = "") {
    statusNode.className = "form-status" + (kind ? " " + kind : "");
    ui.setText(statusNode, message);
  }
  function renderUploadState() {
    $("#media-name").textContent = selectedMedia ? selectedMedia.name : "拖入一份音频或视频";
    const bytes = selectedMedia?.size || 0;
    const unit = bytes < 1000 ? [1, " B"] : bytes < 1000000 ? [1000, " KB"] : [1000000, " MB"];
    $("#media-size").textContent = selectedMedia ? (bytes / unit[0]).toLocaleString("zh-CN", {maximumFractionDigits: 2}) + unit[1] : "也可以从设备中选择文件";
    $("#choose-media").textContent = selectedMedia ? "更换文件" : "选择文件";
    $("#remove-media").hidden = !selectedMedia;
    for (const control of form.querySelectorAll("button, input, textarea")) control.disabled = submitting;
    submit.disabled = submitting || !selectedMedia;
    $("#submit-label").textContent = submitting ? "正在提交…" : "生成字幕";
    form.setAttribute("aria-busy", String(submitting));
  }
  function selectMedia(files) {
    if (submitting || !files.length) return;
    const error = ui.validateMedia(files);
    selectedMedia = error ? null : files[0];
    if (error) mediaInput.value = "";
    renderUploadState();
    status(error || "文件已选择，可按需添加辅助材料后生成字幕。", error ? "error" : "");
  }
  $("#choose-media").addEventListener("click", () => mediaInput.click());
  mediaInput.addEventListener("change", () => selectMedia(Array.from(mediaInput.files)));
  $("#remove-media").addEventListener("click", () => {
    selectedMedia = null; mediaInput.value = ""; renderUploadState();
    status("选择文件后即可生成字幕。"); $("#choose-media").focus();
  });
  for (const name of ["dragenter", "dragover"]) dropZone.addEventListener(name, event => {
    event.preventDefault();
    if (!submitting) dropZone.classList.add("dragging");
  });
  dropZone.addEventListener("dragleave", event => {
    if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("dragging");
  });
  dropZone.addEventListener("drop", event => {
    event.preventDefault(); dropZone.classList.remove("dragging");
    if (submitting) return;
    const files = Array.from(event.dataTransfer?.files || []);
    if (!files.length) { status("请拖入一份音频或视频文件。", "error"); return; }
    selectMedia(files);
  });
  // A misplaced file must not navigate away and discard an unfinished form.
  for (const name of ["dragover", "drop"]) document.addEventListener(name, event => {
    if (Array.from(event.dataTransfer?.types || []).includes("Files")) event.preventDefault();
  });
  function renderOptional() {
    const count = ui.keywords(keywordInput.value).length;
    $("#optional-summary").textContent = references.length || count ? references.length + " 份材料 · " + count + " 个关键词" : "可选";
    const nodes = references.map((file, index) => {
      const item = document.createElement("li"), text = document.createElement("span"), remove = document.createElement("button");
      text.textContent = file.name;
      remove.type = "button"; remove.className = "text-button"; remove.textContent = "移除";
      remove.setAttribute("aria-label", "移除材料 " + file.name);
      remove.disabled = submitting;
      remove.addEventListener("click", () => {
        references.splice(index, 1); renderOptional(); refsInput.focus();
      });
      item.append(text, remove); return item;
    });
    $("#reference-list").replaceChildren(...nodes);
  }
  refsInput.addEventListener("change", () => {
    const incoming = Array.from(refsInput.files);
    if (!incoming.length) return;
    if (incoming.length > 20 || incoming.reduce((sum, file) => sum + file.size, 0) >= 100000000 || incoming.some(file => !file.size)) {
      status("辅助材料需为非空文件，最多 20 份，合计小于 100 MB。请重新选择。", "error");
      refsInput.value = ""; return;
    }
    references = incoming; renderOptional();
  });
  keywordInput.addEventListener("input", renderOptional);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (submitting) return;
    if (!selectedMedia) { status("请选择一份音频或视频。", "error"); $("#choose-media").focus(); return; }
    const words = ui.keywords(keywordInput.value);
    if (words.length > 100) {
      $("#optional-inputs").open = true; keywordInput.focus();
      status("关键词最多可填写 100 个，请减少后重试。", "error"); return;
    }
    submitting = true; renderUploadState(); status("正在上传并提交，请保持页面打开…");
    const data = new FormData();
    data.append("media", selectedMedia);
    for (const file of references) data.append("references", file);
    for (const word of words) data.append("keywords", word);
    let accepted = false;
    try {
      // Large uploads have no artificial short timeout; an uncertain result never triggers a retry.
      const body = await ui.requestJson("/trial/jobs", {method: "POST", headers: headers(), body: data, timeoutMs: 0});
      if (typeof body.job_id !== "string" || typeof body.status !== "string") throw new ui.TrialUIError(202, "invalid_response", true);
      accepted = true;
      status("任务已提交，可在最近任务中查看状态。", "success");
      selectedMedia = null; references = []; form.reset(); renderOptional();
    } catch (error) {
      status(error.uncertain ? "提交结果尚未确认。请先刷新最近任务，确认是否已创建；不要直接重复提交。" : error.message, "error");
    } finally {
      submitting = false; renderUploadState();
    }
    if (accepted) await jobPolling.refresh(true);
  });

  function createRow(job) {
    const row = document.createElement("article");
    row.className = "job-row"; row.dataset.jobId = job.job_id;
    const info = document.createElement("div"), title = document.createElement("h3");
    const meta = document.createElement("p"), note = document.createElement("p");
    meta.className = "job-meta"; note.className = "job-note";
    info.append(title, meta, note);
    const actions = document.createElement("div"), badge = document.createElement("span");
    actions.className = "job-actions";
    const symbol = document.createElement("span"), label = document.createElement("span");
    symbol.className = "status-symbol"; symbol.setAttribute("aria-hidden", "true");
    badge.append(symbol, label);
    const download = document.createElement("a");
    download.className = "secondary-button"; download.textContent = "下载 SRT";
    download.href = "/trial/jobs/" + encodeURIComponent(job.job_id) + "/result";
    download.setAttribute("aria-label", "下载任务 " + job.job_id.slice(-8) + " 的 SRT");
    const upload = document.createElement("button");
    upload.type = "button"; upload.className = "text-button"; upload.textContent = "重新上传";
    upload.addEventListener("click", () => { $("#choose-media").focus(); });
    actions.append(badge, download, upload); row.append(info, actions);
    return {row, title, meta, note, badge, symbol, label, download, upload, lastState: job.status};
  }
  function renderJobs() {
    const selected = jobs.slice(0, visibleCount), keep = new Set(selected.map(job => job.job_id));
    let changes = 0;
    for (const [id, item] of rows) if (!keep.has(id)) { item.row.remove(); rows.delete(id); }
    for (const placeholder of list.querySelectorAll(".empty-state")) placeholder.remove();
    selected.forEach((job, index) => {
      const item = rows.get(job.job_id) || createRow(job);
      const state = ui.stateInfo(job.status);
      if (item.lastState !== job.status) changes++;
      item.lastState = job.status; rows.set(job.job_id, item);
      ui.setText(item.title, "任务 " + job.job_id.slice(-8));
      ui.setText(item.meta, ui.dateTime(job.created_at) + " · " + ui.duration(job.audio_duration_ms));
      let note = state.note;
      if (job.status === "succeeded" && !job.result_available) note = "已完成，但结果暂不可用。请稍后刷新记录。";
      if (job.status === "rejected" && job.reject_reason) note = ui.formatTrialError({code: job.reject_reason});
      ui.setText(item.note, note); item.note.hidden = !note;
      item.badge.className = "job-status " + state.kind;
      ui.setText(item.symbol, state.symbol); ui.setText(item.label, state.label);
      item.download.hidden = job.result_available !== true;
      item.upload.hidden = !["failed", "interrupted", "cancelled"].includes(job.status);
      // Reuse nodes, including focused download links, instead of replacing the list on each poll.
      if (list.children[index] !== item.row) list.insertBefore(item.row, list.children[index] || null);
    });
    if (!selected.length) {
      const empty = document.createElement("p"); empty.className = "empty-state";
      empty.textContent = "这个浏览器还没有任务。上传第一份文件，字幕结果会出现在这里。";
      list.append(empty);
    }
    ui.setText($("#job-count"), "已显示 " + selected.length + " / 共 " + jobs.length + " 个任务");
    if (visibleCount >= jobs.length && document.activeElement === more) $("#refresh-jobs").focus();
    more.hidden = visibleCount >= jobs.length;
    return changes;
  }
  async function loadJobs() {
    // Establish the anonymous visitor before the first list request; avoid racing two new cookies.
    if (!visitorInitialized) { await heartbeatPolling.refresh(); visitorInitialized = true; }
    if (document.visibilityState !== "visible") return;
    const refresh = $("#refresh-jobs");
    refresh.setAttribute("aria-busy", "true");
    try {
      const body = await ui.requestJson("/trial/jobs", {headers: headers()});
      if (!Array.isArray(body.jobs) || body.jobs.some(job => !job || typeof job.job_id !== "string" || typeof job.status !== "string")) {
        throw new Error("任务记录返回异常，请稍后刷新。");
      }
      jobs = body.jobs; loaded = true; lastUpdated = new Date();
      const changes = renderJobs();
      $("#jobs-status").className = "field-help";
      ui.setText($("#jobs-status"), "更新于 " + lastUpdated.toLocaleTimeString("zh-CN", {hour12: false}) + (changes ? " · " + changes + " 个任务状态已更新" : ""));
    } catch (error) {
      $("#jobs-status").className = "field-help error-text";
      ui.setText($("#jobs-status"), "记录更新失败。" + (lastUpdated ? "上次更新于 " + lastUpdated.toLocaleTimeString("zh-CN", {hour12: false}) + "。" : "") + error.message);
      if (!loaded) { list.querySelector(".empty-state").textContent = "暂时无法读取记录，请点击刷新重试。"; $("#job-count").textContent = "记录待确认"; }
    } finally { refresh.removeAttribute("aria-busy"); }
  }
  async function heartbeat() {
    try { await ui.requestJson("/trial/heartbeat", {method: "POST", headers: headers()}); } catch (_) { /* Next visible pulse retries. */ }
  }
  const heartbeatPolling = ui.createVisiblePoller(heartbeat, 60000);
  const jobPolling = ui.createVisiblePoller(loadJobs, 15000);
  $("#refresh-jobs").addEventListener("click", () => jobPolling.refresh());
  more.addEventListener("click", () => { visibleCount += 100; renderJobs(); });
  function start() { heartbeatPolling.start(); jobPolling.start(); }
  window.addEventListener("pagehide", () => { heartbeatPolling.stop(); jobPolling.stop(); });
  window.addEventListener("pageshow", start);
  start();
})();
