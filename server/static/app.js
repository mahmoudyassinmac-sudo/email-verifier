/* Email Verifier dashboard - vanilla JS, no build step. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
  const pct = (a, b) => (b ? ((100 * a) / b).toFixed(1) + "%" : "—");
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const STATUS_LABEL = { valid: "Valid", risky: "Risky", invalid: "Invalid", duplicate: "Duplicate" };
  const STATUS_COLOR = { valid: "var(--valid)", risky: "var(--risky)", invalid: "var(--invalid)" };

  let selectedId = null;
  let selectedFile = null;
  let jobTimer = null;
  let listTimer = null;
  let lastJob = null;

  // ------------------------------------------------------------ api
  async function api(path, opts) {
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch (_) { /* not json */ }
    if (!res.ok) throw new Error((body && body.detail) || res.statusText || "Request failed");
    return body;
  }

  function toast(msg, isErr) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.className = "toast"), 3800);
  }

  function eta(sec) {
    if (sec == null) return "—";
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + "h " + String(m).padStart(2, "0") + "m";
    if (m) return m + "m " + String(s).padStart(2, "0") + "s";
    return s + "s";
  }

  function ago(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  // ------------------------------------------------------------ health / SMTP chips
  async function loadHealth(refresh) {
    try {
      const h = await api("/api/health" + (refresh ? "?refresh=true" : ""));
      const s = h.smtp || {};
      const chip = $("chipSmtp");
      if (s.checking) {
        chip.className = "chip";
        chip.lastElementChild.textContent = "Checking port 25…";
        setTimeout(() => loadHealth(false), 2500);
        return;
      }
      if (!s.enabled) {
        chip.className = "chip warn";
        chip.lastElementChild.textContent = "SMTP stage disabled";
      } else if (s.port_open) {
        chip.className = "chip ok";
        chip.lastElementChild.textContent = "Port 25 open · SMTP probing on";
      } else {
        chip.className = "chip bad";
        chip.lastElementChild.textContent = "Port 25 blocked · stage 6 skipped";
      }
      chip.title = s.detail || "";
      $("chipIp").lastElementChild.textContent = "IP " + (s.public_ip || "unknown");
      $("chipHelo").lastElementChild.textContent = "HELO " + (s.helo || "—") + " · FROM " + (s.mail_from || "—");
    } catch (e) {
      $("chipSmtp").className = "chip bad";
      $("chipSmtp").lastElementChild.textContent = "Engine offline";
    }
  }

  // ------------------------------------------------------------ upload
  function setFile(f) {
    selectedFile = f || null;
    const dz = $("dropzone");
    if (selectedFile) {
      dz.classList.add("has-file");
      $("dzTitle").textContent = selectedFile.name;
      $("dzSub").textContent = (selectedFile.size / 1024).toFixed(0) + " KB · ready to upload";
    } else {
      dz.classList.remove("has-file");
      $("dzTitle").textContent = "Drop a CSV or Excel file";
      $("dzSub").textContent = "or click to browse · the email column is detected automatically";
    }
    $("btnUpload").disabled = !selectedFile;
  }

  function initUpload() {
    const dz = $("dropzone");
    $("fileInput").addEventListener("change", (e) => setFile(e.target.files[0]));
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
    dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]); });

    $("btnUpload").addEventListener("click", async () => {
      if (!selectedFile) return;
      const btn = $("btnUpload");
      btn.disabled = true;
      btn.textContent = "Importing (stages 1-4)…";
      try {
        const fd = new FormData();
        fd.append("file", selectedFile);
        fd.append("email_column", $("emailColumn").value.trim());
        fd.append("autostart", $("autostart").checked ? "true" : "false");
        const job = await api("/api/jobs", { method: "POST", body: fd });
        toast("Imported " + fmt(job.total) + " rows · email column: " + job.email_column);
        setFile(null);
        $("fileInput").value = "";
        await loadJobs();
        selectJob(job.id);
      } catch (e) {
        toast(e.message, true);
      } finally {
        btn.textContent = "Upload & verify";
        btn.disabled = !selectedFile;
      }
    });
  }

  // ------------------------------------------------------------ quick check
  function initQuick() {
    $("quickForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const email = $("quickEmail").value.trim();
      if (!email) return;
      const box = $("quickResult");
      const btn = $("btnQuick");
      btn.disabled = true;
      btn.textContent = "…";
      box.hidden = false;
      box.innerHTML = '<span class="muted">Checking ' + esc(email) + " — SMTP can take up to ~30s…</span>";
      try {
        const r = await api("/api/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) });
        const cls = "st-" + String(r.status).toLowerCase();
        box.innerHTML =
          '<div class="qr-head"><span class="pill ' + cls + '">' + esc(r.status) + "</span><b>" + esc(r.email || email) + "</b></div>" +
          "<div>" + esc(r.reason) + "</div>" +
          "<dl><dt>Domain</dt><dd>" + esc(r.domain || "—") + "</dd><dt>MX</dt><dd>" + esc(r.mx_host || "—") + "</dd>" +
          "<dt>Role</dt><dd>" + (r.is_role ? "Yes" : "No") + "</dd><dt>Disposable</dt><dd>" + (r.is_disposable ? "Yes" : "No") + "</dd>" +
          (r.typo_fixed ? "<dt>Typo fixed</dt><dd>" + esc(r.typo_fixed) + "</dd>" : "") + "</dl>";
      } catch (err) {
        box.innerHTML = '<span style="color:var(--invalid-ink)">' + esc(err.message) + "</span>";
      } finally {
        btn.disabled = false;
        btn.textContent = "Check";
      }
    });
  }

  // ------------------------------------------------------------ jobs list
  function miniBar(j) {
    const c = j.counts || {}, t = j.total || 1;
    const w = (k) => ((100 * (c[k] || 0)) / t).toFixed(2) + "%";
    return '<div class="mini"><div style="width:' + w("valid") + ';background:var(--valid)"></div>' +
      '<div style="width:' + w("risky") + ';background:var(--risky)"></div>' +
      '<div style="width:' + w("invalid") + ';background:var(--invalid)"></div>' +
      '<div style="width:' + w("duplicate") + ';background:var(--dup)"></div></div>';
  }

  async function loadJobs() {
    try {
      const data = await api("/api/jobs");
      const jobs = data.jobs || [];
      $("jobsCount").textContent = jobs.length ? jobs.length + " total" : "";
      const list = $("jobsList");
      if (!jobs.length) {
        list.innerHTML = '<p class="muted small">No jobs yet — upload a list.</p>';
        return jobs;
      }
      list.innerHTML = jobs.map((j) =>
        '<div class="job' + (j.id === selectedId ? " active" : "") + '" data-id="' + j.id + '">' +
          '<div class="job-top"><span class="job-name" title="' + esc(j.name) + '">#' + j.id + " · " + esc(j.name) + "</span>" +
          '<span class="pill ' + esc(j.status) + '">' + esc(j.status) + "</span></div>" +
          miniBar(j) +
          '<div class="job-sub"><span>' + fmt(j.processed) + " / " + fmt(j.total) + " · " + j.percent + "%</span><span>" + ago(j.created_at) + "</span></div>" +
        "</div>").join("");
      list.querySelectorAll(".job").forEach((el) => el.addEventListener("click", () => selectJob(Number(el.dataset.id))));
      if (selectedId == null && jobs.length) selectJob(jobs[0].id);
      return jobs;
    } catch (e) {
      return [];
    }
  }

  // ------------------------------------------------------------ job detail
  function selectJob(id) {
    selectedId = id;
    document.querySelectorAll(".job").forEach((el) => el.classList.toggle("active", Number(el.dataset.id) === id));
    $("emptyState").hidden = true;
    $("jobView").hidden = false;
    refreshJob();
  }

  async function refreshJob() {
    clearTimeout(jobTimer);
    if (selectedId == null) return;
    try {
      const j = await api("/api/jobs/" + selectedId);
      lastJob = j;
      renderJob(j);
      jobTimer = setTimeout(refreshJob, j.running ? 1500 : 6000);
    } catch (e) {
      if (/not found/i.test(e.message)) {
        selectedId = null;
        $("jobView").hidden = true;
        $("emptyState").hidden = false;
        return;
      }
      jobTimer = setTimeout(refreshJob, 4000);
    }
  }

  function renderJob(j) {
    const c = j.counts || {};
    const unique = (c.total || 0) - (c.duplicate || 0);
    $("jvName").textContent = "#" + j.id + " · " + j.name;
    const st = $("jvStatus");
    st.textContent = j.status;
    st.className = "pill " + j.status;
    $("jvMeta").textContent = "Email column: " + (j.email_column || "—") + " · " + fmt(j.total) + " rows · " +
      fmt(unique) + " unique · created " + ago(j.created_at) + (j.finished_at ? " · finished " + ago(j.finished_at) : "");

    // progress bar (stacked by status, 2px gaps)
    const segs = $("jvBar").children, t = j.total || 1;
    const vals = [c.valid || 0, c.risky || 0, c.invalid || 0, c.duplicate || 0];
    ["valid", "risky", "invalid", "duplicate"].forEach((k, i) => {
      segs[i].style.width = ((100 * vals[i]) / t).toFixed(3) + "%";
      segs[i].title = STATUS_LABEL[k] + ": " + fmt(vals[i]);
    });
    $("jvBar").setAttribute("aria-valuenow", j.percent);
    $("jvStage").textContent = (j.stage_label || "—") + " · " + j.percent + "% processed";
    $("jvRate").textContent = j.running ? (fmt(j.rate_per_min) + " emails/min · ETA " + eta(j.eta_seconds)) : (fmt(j.processed) + " / " + fmt(j.total) + " processed");

    const notice = $("jvNotice");
    if (j.error) {
      notice.hidden = false; notice.className = "notice err"; notice.textContent = "Error: " + j.error;
    } else if (j.message) {
      notice.hidden = false; notice.className = "notice"; notice.textContent = j.message;
    } else notice.hidden = true;

    // buttons
    const done = j.status === "completed";
    const unresolved = (j.reasons || []).filter((r) => r.status === "risky" &&
      /^(Unknown: |Not SMTP-checked: )/.test(r.reason)).reduce((a, r) => a + r.n, 0);
    $("btnStart").dataset.mode = done ? "recheck" : "start";
    $("btnStart").textContent = done ? "Re-check " + fmt(unresolved) + " unresolved" : (j.processed > 0 ? "Resume" : "Start");
    $("btnStart").disabled = j.running || (done && !unresolved);
    $("btnPause").disabled = !j.running;
    $("btnDelete").disabled = j.running;
    $("btnXlsx").href = "/api/jobs/" + j.id + "/download/xlsx";
    $("btnCsv").href = "/api/jobs/" + j.id + "/download/csv";

    // KPIs
    $("kValid").textContent = fmt(c.valid || 0);
    $("kRisky").textContent = fmt(c.risky || 0);
    $("kInvalid").textContent = fmt(c.invalid || 0);
    $("kDup").textContent = fmt(c.duplicate || 0);
    $("kPending").textContent = fmt(c.pending || 0);
    $("kValidP").textContent = pct(c.valid || 0, unique) + " of unique";
    $("kRiskyP").textContent = pct(c.risky || 0, unique) + " of unique";
    $("kInvalidP").textContent = pct(c.invalid || 0, unique) + " of unique";
    $("kEta").textContent = j.running ? "ETA " + eta(j.eta_seconds) : (c.pending ? "paused" : "none left");

    // reasons
    const reasons = (j.reasons || []).filter((r) => r.status !== "duplicate");
    const maxN = Math.max(1, ...reasons.map((r) => r.n));
    $("reasonsTotal").textContent = reasons.length + " reasons";
    $("reasons").innerHTML = reasons.length ? reasons.map((r) =>
      '<div class="reason" title="' + esc(STATUS_LABEL[r.status] + " · " + r.reason + " · " + fmt(r.n) + " (" + pct(r.n, unique) + ")") + '">' +
        '<span class="r-text"><span class="r-cat"><i class="sw ' + r.status + '"></i>' + STATUS_LABEL[r.status] + "</span> · " + esc(r.reason) + "</span>" +
        '<span class="r-num">' + fmt(r.n) + "<span>" + pct(r.n, unique) + "</span></span>" +
        '<div class="r-track"><div class="r-bar" style="width:' + ((100 * r.n) / maxN).toFixed(2) + "%;background:" + STATUS_COLOR[r.status] + '"></div></div>' +
      "</div>").join("") : '<p class="muted small">Nothing processed yet.</p>';

    // top domains
    $("domainsTable").tBodies[0].innerHTML = (j.top_domains || []).map((d) => {
      const decided = (d.valid || 0) + (d.risky || 0) + (d.invalid || 0);
      const share = decided ? (100 * (d.valid || 0)) / decided : 0;
      return "<tr><td title=\"" + esc(d.domain) + "\">" + esc(d.domain) + '</td><td class="num">' + fmt(d.total) + '</td><td class="num">' + fmt(d.valid) +
        '</td><td class="num">' + fmt(d.risky) + '</td><td class="num">' + fmt(d.invalid) + "</td>" +
        '<td><div class="vbar" title="' + share.toFixed(1) + '% valid of processed"><div class="track"><div class="fill" style="width:' + share.toFixed(1) + '%"></div></div><span>' +
        (decided ? share.toFixed(0) + "%" : "—") + "</span></div></td></tr>";
    }).join("") || '<tr><td colspan="6" class="muted">—</td></tr>';

    // recent
    $("recentTable").tBodies[0].innerHTML = (j.recent || []).map((r) =>
      '<tr><td class="email-cell" title="' + esc(r.email) + '">' + esc(r.email || "(empty)") + "</td>" +
      '<td><span class="pill st-' + r.status + '">' + STATUS_LABEL[r.status] + "</span></td>" +
      '<td title="' + esc(r.reason) + '">' + esc(r.reason) + '</td><td title="' + esc(r.mx_host) + '">' + esc(r.mx_host || "—") + "</td></tr>").join("") ||
      '<tr><td colspan="4" class="muted">Waiting for results…</td></tr>';
  }

  function initJobButtons() {
    $("btnStart").addEventListener("click", async () => {
      const recheck = $("btnStart").dataset.mode === "recheck";
      try {
        const r = await api("/api/jobs/" + selectedId + (recheck ? "/recheck" : "/start"), { method: "POST" });
        toast(recheck ? r.message : "Job started");
        refreshJob(); loadJobs();
      } catch (e) { toast(e.message, true); }
    });
    $("btnPause").addEventListener("click", async () => {
      try { const r = await api("/api/jobs/" + selectedId + "/pause", { method: "POST" }); toast(r.message); refreshJob(); }
      catch (e) { toast(e.message, true); }
    });
    $("btnDelete").addEventListener("click", async () => {
      const b = $("btnDelete");
      if (!b.dataset.confirm) {               // two-step confirm, no browser dialogs
        b.dataset.confirm = "1"; b.textContent = "Click again to delete";
        setTimeout(() => { delete b.dataset.confirm; b.textContent = "Delete"; }, 3500);
        return;
      }
      delete b.dataset.confirm; b.textContent = "Delete";
      try {
        await api("/api/jobs/" + selectedId, { method: "DELETE" });
        toast("Job deleted");
        selectedId = null;
        $("jobView").hidden = true;
        $("emptyState").hidden = false;
        loadJobs();
      } catch (e) { toast(e.message, true); }
    });
    ["btnXlsx", "btnCsv"].forEach((id) => $(id).addEventListener("click", () => toast("Preparing download…")));
    $("btnRefreshSmtp").addEventListener("click", () => {
      $("chipSmtp").className = "chip";
      $("chipSmtp").lastElementChild.textContent = "Checking port 25…";
      loadHealth(true);
    });
  }

  // ------------------------------------------------------------ boot
  initUpload();
  initQuick();
  initJobButtons();
  loadHealth(false);
  loadJobs();
  listTimer = setInterval(loadJobs, 4000);
})();
