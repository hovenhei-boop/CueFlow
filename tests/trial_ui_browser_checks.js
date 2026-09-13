/* Runs product scripts in a real browser DOM, with test-only in-memory HTTP fixtures. */
(() => {
  const frame = document.querySelector("#subject"), output = document.querySelector("#results");
  let doc, win, fixture, passed = 0;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(predicate, message) {
    for (let i = 0; i < 100; i++) { if (predicate()) return; await wait(20); }
    throw new Error(message);
  }
  const assert = (value, message) => { if (!value) throw new Error(message); };
  async function test(name, action) {
    await action(); passed++;
    const li = document.createElement("li"); li.textContent = "PASS · " + name; output.append(li);
  }
  async function page(path) {
    const loaded = new Promise(resolve => frame.addEventListener("load", resolve, {once: true}));
    frame.src = path; await loaded; win = frame.contentWindow; doc = win.document; fixture = win.trialFixture;
  }
  function drop(names) {
    const transfer = new win.DataTransfer();
    for (const name of names) transfer.items.add(new win.File(["synthetic audio"], name, {type: "audio/wav"}));
    doc.querySelector("#drop-zone").dispatchEvent(new win.DragEvent("drop", {bubbles: true, cancelable: true, dataTransfer: transfer}));
  }
  function submit() { doc.querySelector("#job-form").dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true})); }
  const formStatus = () => doc.querySelector("#form-status").textContent;
  const key = "POST /trial/jobs";
  async function refreshJobs() {
    const before = fixture.requests.filter(value => value === "GET /trial/jobs").length;
    doc.querySelector("#refresh-jobs").click();
    await until(() => fixture.requests.filter(value => value === "GET /trial/jobs").length > before, "list refresh started");
    await until(() => !doc.querySelector("#refresh-jobs").hasAttribute("aria-busy"), "list refresh finished");
  }
  document.querySelector("#run").addEventListener("click", async () => {
    const button = document.querySelector("#run"); button.disabled = true; output.replaceChildren(); passed = 0;
    try {
      await page("/trial");
      await until(() => doc.querySelectorAll(".job-row").length === 100, "initial list");
      await test("Initial 100 of 205 tasks; more tasks stay reachable", async () => {
        assert(doc.querySelector("#job-count").textContent.includes("100 / 共 205"), "true total");
        doc.querySelector("#show-earlier").click();
        assert(doc.querySelectorAll(".job-row").length === 200, "second batch");
        doc.querySelector("#show-earlier").click();
        assert(doc.querySelectorAll(".job-row").length === 205, "last batch");
        assert(doc.querySelector("#show-earlier").hidden, "hide exhausted button");
      });
      await test("Refresh preserves expanded tasks and focused download node", async () => {
        const link = doc.querySelector(".job-actions a:not([hidden])"); link.focus();
        await refreshJobs();
        assert(doc.querySelectorAll(".job-row").length === 205, "expanded count retained");
        assert(doc.activeElement === link && link.isConnected, "focused node retained");
      });
      await test("Needs-review is independent; durations are truthful", async () => {
        const badge = doc.querySelector(".job-status.needs_review");
        assert(badge.closest(".job-row").textContent.includes("暂不支持在线复核"), "review notice");
        assert(win.getComputedStyle(badge.querySelector(".status-symbol")).animationName === "none", "review has no spinner");
        assert(doc.body.textContent.includes("42 秒") && doc.body.textContent.includes("时长待确认"), "short and unknown duration");
      });
      await test("Refreshing a failed list preserves successful data", async () => {
        fixture.error["GET /trial/jobs"] = 502; await refreshJobs();
        assert(doc.querySelectorAll(".job-row").length === 205, "keep history");
        assert(doc.querySelector("#jobs-status").textContent.includes("记录更新失败"), "visible error");
        delete fixture.error["GET /trial/jobs"]; await refreshJobs();
      });
      await test("Empty history has an explicit zero count and recovers on refresh", async () => {
        const saved = fixture.jobs; fixture.jobs = []; await refreshJobs();
        assert(doc.querySelector("#job-count").textContent.includes("0 / 共 0"), "zero count");
        assert(doc.querySelector(".empty-state").textContent.includes("还没有任务"), "empty state");
        fixture.jobs = saved; await refreshJobs();
        assert(doc.querySelectorAll(".job-row").length === 205, "restored list");
      });
      await test("Drop rejects multiple files without selecting the first", async () => {
        drop(["one.wav", "two.wav"]);
        assert(formStatus().includes("不会自动取第一个"), "explicit rejection");
        assert(doc.querySelector("#submit-button").disabled, "no hidden first file");
      });
      await test("Long and hostile filenames render as text", async () => {
        const name = "采访资料".repeat(35) + "<img src=x onerror=alert(1)>.wav";
        drop([name]);
        assert(doc.querySelector("#media-name").textContent === name, "exact filename");
        assert(!doc.querySelector("#media-name img"), "no HTML interpretation");
      });
      for (const status of [413, 429, 502, 503, 504]) await test("HTML " + status + " keeps form input and clear feedback", async () => {
        fixture.error[key] = status; drop(["interview.wav"]); submit();
        await until(() => !doc.querySelector("#submit-button").disabled, "upload released");
        assert(doc.querySelector("#media-name").textContent === "interview.wav", "file retained");
        assert(!formStatus().includes("JSON") && !formStatus().includes("<html>"), "no raw parse error");
        if (status === 413) assert(formStatus().includes("上传内容过大"), "413 message");
        if (status >= 500) assert(formStatus().includes("尚未确认"), "ambiguous result");
      });
      await test("Unknown submission never automatically resends", async () => {
        fixture.error[key] = "network";
        const before = fixture.requests.filter(value => value === key).length;
        submit(); await wait(80);
        assert(fixture.requests.filter(value => value === key).length === before + 1, "one POST");
        assert(formStatus().includes("不要直接重复提交"), "reconcile instruction");
      });
      await test("Malformed successful submission retains input for reconciliation", async () => {
        fixture.error[key] = "invalid-success"; submit();
        await until(() => !doc.querySelector("#submit-button").disabled, "upload released");
        assert(formStatus().includes("尚未确认"), "uncertain outcome");
        assert(doc.querySelector("#media-name").textContent === "interview.wav", "input retained");
      });
      await test("Successful upload locks duplicate submits and sends ordered auxiliary inputs", async () => {
        delete fixture.error[key]; fixture.delay[key] = 80;
        const files = new win.DataTransfer(); files.items.add(new win.File(["notes"], "notes.txt", {type: "text/plain"}));
        const refs = doc.querySelector("#references"); refs.files = files.files; refs.dispatchEvent(new win.Event("change"));
        const keywords = doc.querySelector("#keywords"); keywords.value = "嘉宾姓名\n产品名"; keywords.dispatchEvent(new win.Event("input"));
        const before = fixture.requests.filter(value => value === key).length;
        submit(); submit();
        assert(refs.disabled && keywords.disabled, "inputs locked");
        await until(() => fixture.submissions.length === 1 && formStatus().includes("任务已提交"), "successful upload");
        assert(fixture.requests.filter(value => value === key).length === before + 1, "one accepted POST");
        assert(fixture.submissions[0].references[0] === "notes.txt", "references sent");
        assert(fixture.submissions[0].keywords.join("|") === "嘉宾姓名|产品名", "keyword order");
        assert(doc.querySelector("#submit-button").disabled && !keywords.value, "reset only after acceptance");
      });
      await page("/trial/admin");
      await test("Operator page emits no visitor heartbeat or jobs request", async () => {
        await wait(40); assert(!fixture.requests.some(value => value.includes("heartbeat") || value === "GET /trial/jobs"), "operator isolation");
      });
      doc.querySelector("#operator-secret").value = "preview";
      doc.querySelector("#operator-form").dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
      await until(() => !doc.querySelector("#dashboard").hidden, "dashboard login");
      await test("Operator costs keep unknown separate and explain request count", async () => {
        assert(doc.querySelector('[data-money="cost_per_job_p95_micros"]').textContent === "—", "unknown cost");
        assert(doc.querySelector("#request-count").textContent.includes("最多显示最近 100 条请求"), "request limit");
        assert(doc.body.textContent.includes("近 5 分钟活跃浏览器"), "active window label");
      });
      await test("Pause requires a reason and sends one request", async () => {
        doc.querySelector("#pause-service").click();
        assert(doc.querySelector("#control-dialog").open, "reason dialog");
        const form = doc.querySelector("#control-form");
        form.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
        assert(fixture.controls.length === 0, "empty reason not sent");
        fixture.delay["GET /trial/admin/summary"] = 250;
        doc.querySelector("#control-reason").value = "fixture incident";
        form.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
        form.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
        await until(() => fixture.controls.length === 1, "control accepted before slow refresh");
        assert(doc.querySelector("#confirm-control").disabled, "locked through state refresh");
        form.dispatchEvent(new win.Event("submit", {bubbles: true, cancelable: true}));
        await until(() => !doc.querySelector("#control-dialog").open, "pause completed");
        delete fixture.delay["GET /trial/admin/summary"];
        assert(fixture.controls.length === 1 && fixture.controls[0].reason === "fixture incident", "one control mutation");
        assert(!doc.querySelector("#resume-service").hidden, "new server state visible");
      });
      await test("Network errors retain dashboard; authentication errors return to gate", async () => {
        fixture.error["GET /trial/admin/summary"] = 503; doc.querySelector("#refresh-dashboard").click();
        await until(() => doc.querySelector("#dashboard-status").textContent.includes("保留"), "error notice");
        assert(!doc.querySelector("#dashboard").hidden, "dashboard preserved");
        fixture.error["GET /trial/admin/summary"] = 401; doc.querySelector("#refresh-dashboard").click();
        await until(() => doc.querySelector("#dashboard").hidden, "auth gate");
        assert(!doc.querySelector("#operator-gate").hidden, "gate visible");
      });
      document.querySelector("#result").textContent = passed + " browser checks passed";
    } catch (error) {
      document.querySelector("#result").textContent = "FAILED after " + passed + " checks: " + error.message;
    } finally { button.disabled = false; }
  });
})();
