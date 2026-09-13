/* Test-only transport. Included in the source tests, never in the runtime wheel. */
(() => {
  const parameters = new URLSearchParams(location.search);
  const sample = parameters.has("sample");
  // A supplemental layout stress check; this does not emulate OS accessibility settings.
  if (parameters.get("zoom") === "2") document.documentElement.style.zoom = "2";
  const states = ["running", "succeeded", "needs_review", "failed", "interrupted", "cancelled", "rejected", "queued"];
  const fixture = window.trialFixture = {
    requests: [], error: {}, delay: {}, submissions: [], controls: [],
    jobs: Array.from({length: sample ? 4 : 205}, (_, index) => ({
      job_id: "job_" + String(index).padStart(32, "0"), request_id: "req_" + index,
      status: states[index % states.length], result_available: index % states.length === 1,
      created_at: new Date(Date.UTC(2026, 8, 13, 8, 0, 0) - index * 60000).toISOString(),
      audio_duration_ms: index % 3 === 0 ? null : index % 3 === 1 ? 42000 : 83000,
      reject_reason: index % 8 === 6 ? "visitor_daily_quota" : null
    })),
    summary: {paused: false, storage_ready: true, disk: {ready: true}, online_now: 3,
      today_cookie_uv: 28, new_visitors_today: 19, returning_visitors_today: 9,
      today_user_runs: 42, today_jobs: 37, today_audio_minutes: 286.5,
      success_rate: .925, quality_denominator: 40, today_calculated_cost_micros: 12860000,
      daily_budget_micros: 100000000, active_budget_occupancy_micros: 4400000,
      estimated_unknown_cost_micros: 1200000, cost_coverage: .9, unknown_request_count: 2,
      cost_per_job_p50_micros: 320000, cost_per_job_p95_micros: null,
      processing_ms_p50: 83000, processing_ms_p95: 362000, failures: 3, interrupted: 2,
      second_active_day_rate: .32, total_cookie_uv: 156, peak_online: 12}
  };
  const response = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  window.fetch = async (url, options = {}) => {
    const path = String(url), method = options.method || "GET";
    const key = method + " " + path;
    fixture.requests.push(key);
    if (fixture.delay[key]) await new Promise(resolve => setTimeout(resolve, fixture.delay[key]));
    const error = fixture.error[key];
    if (error === "network") throw new TypeError("fixture network interruption");
    if (error === "invalid-success") return new Response("<html>malformed success</html>", {status: 202});
    if (typeof error === "number") return new Response("<html>fixture proxy error</html>", {status: error});
    if (error?.code) return response({error: {code: error.code}}, error.status);
    if (path.startsWith("/trial/admin/")) {
      if (options.headers?.Authorization !== "Bearer preview") return response({error: {code: "unauthorized"}}, 401);
      if (path === "/trial/admin/summary") return response(fixture.summary);
      if (path.startsWith("/trial/admin/requests")) return response({requests: fixture.jobs.slice(0, 100).map(job => ({...job, execution_status: job.status, action_kind: "create", cost_status: "unknown"}))});
      const body = JSON.parse(options.body);
      if (!body.reason?.trim()) return response({error: {code: "invalid_request"}}, 400);
      fixture.controls.push({path, method, ...body});
      if (path.endsWith("/pause")) fixture.summary.paused = true;
      if (path.endsWith("/resume")) fixture.summary.paused = false;
      if (path.endsWith("/daily-budget-override")) fixture.summary.daily_budget_micros = method === "DELETE" ? 100000000 : body.daily_budget_micros;
      return response({ok: true});
    }
    if (path === "/trial/heartbeat") return response({online_now: 3});
    if (path === "/trial/jobs" && method === "GET") return response({jobs: fixture.jobs});
    if (path === "/trial/jobs" && method === "POST") {
      fixture.submissions.push({media: options.body.get("media").name,
        references: options.body.getAll("references").map(file => file.name), keywords: options.body.getAll("keywords")});
      const job = {...fixture.jobs[0], job_id: "job_created_by_fixture", status: "queued", result_available: false};
      fixture.jobs.unshift(job); return response(job, 202);
    }
    throw new Error("Unexpected fixture request: " + key);
  };
})();
