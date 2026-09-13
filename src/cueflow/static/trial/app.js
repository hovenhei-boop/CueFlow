const fingerprint = JSON.stringify({
  browser: navigator.userAgent,
  language: navigator.language,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  screen: `${screen.width}x${screen.height}@${window.devicePixelRatio || 1}`,
  platform: navigator.platform || "unknown"
});

const headers = () => ({ "X-CueFlow-Fingerprint": fingerprint });
const form = document.querySelector("#job-form");
const statusNode = document.querySelector("#form-status");
const submitButton = document.querySelector("#submit-button");
const mediaInput = document.querySelector("#media");
const mediaName = document.querySelector("#media-name");

mediaInput.addEventListener("change", () => {
  mediaName.textContent = mediaInput.files[0]?.name || "也可以把文件拖到这里";
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;
  statusNode.className = "form-status";
  statusNode.textContent = "正在检查并上传文件…";
  const data = new FormData();
  data.append("media", mediaInput.files[0]);
  for (const file of document.querySelector("#references").files) data.append("references", file);
  for (const keyword of document.querySelector("#keywords").value.split(/\r?\n/).map(v => v.trim()).filter(Boolean)) {
    data.append("keywords", keyword);
  }
  try {
    const response = await fetch("/trial/jobs", { method: "POST", headers: headers(), body: data });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "提交失败，请稍后再试。");
    statusNode.className = "form-status success";
    statusNode.textContent = "任务已开始。你可以留在此页查看进度。";
    form.reset();
    mediaName.textContent = "也可以把文件拖到这里";
    await loadJobs();
  } catch (error) {
    statusNode.className = "form-status error";
    statusNode.textContent = error.message;
  } finally {
    submitButton.disabled = false;
  }
});

function statusLabel(value) {
  return ({queued:"等待执行",running:"处理中",needs_review:"需要复核",succeeded:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已停止",rejected:"未接受"})[value] || value;
}

async function loadJobs() {
  const list = document.querySelector("#job-list");
  try {
    const response = await fetch("/trial/jobs", { headers: headers(), cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error("无法读取任务记录。");
    if (!body.jobs.length) {
      list.innerHTML = '<p class="empty-state">这个浏览器还没有提交任务。</p>';
      return;
    }
    list.replaceChildren(...body.jobs.map(job => {
      const article = document.createElement("article");
      article.className = "job-row";
      const info = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = `任务 ${job.job_id.slice(-8)}`;
      const meta = document.createElement("p");
      meta.textContent = `${new Date(job.created_at).toLocaleString()} · ${Math.round((job.audio_duration_ms || 0) / 60000)} 分钟`;
      info.append(title, meta);
      const actions = document.createElement("div");
      actions.className = "job-actions";
      const badge = document.createElement("span");
      badge.className = `job-status ${job.status}`;
      badge.textContent = statusLabel(job.status);
      actions.append(badge);
      if (job.result_available) {
        const link = document.createElement("a");
        link.className = "download-link";
        link.href = `/trial/jobs/${encodeURIComponent(job.job_id)}/result`;
        link.textContent = "下载 SRT";
        actions.append(link);
      }
      article.append(info, actions);
      return article;
    }));
  } catch (error) {
    const message = document.createElement("p");
    message.className = "empty-state error-text";
    message.textContent = error.message;
    list.replaceChildren(message);
  }
}

document.querySelector("#refresh-jobs").addEventListener("click", loadJobs);
async function heartbeat() {
  try { await fetch("/trial/heartbeat", { method: "POST", headers: headers(), cache: "no-store" }); } catch (_) { /* next pulse retries */ }
}
heartbeat();
setInterval(heartbeat, 60000);
loadJobs();
setInterval(loadJobs, 15000);
