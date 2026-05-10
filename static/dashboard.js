/**
 * NetPulse AI — dashboard charts (Chart.js).
 * Fetches JSON from Flask; updates charts on an interval + time-range controls.
 */
(() => {
  const root = document.getElementById("dashboard-charts");
  if (!root) return;

  const REFRESH_MS = Number(root.dataset.refreshMs) || 30000;
  const METRICS_HOURS_DEFAULT = Number(root.dataset.metricsHours) || 24;
  const MAX_LOG_LIMIT = Number(root.dataset.logLimit) || 1200;

  /** Brand-consistent series colors (google=blue, ibm=orange, att=green). */
  const HOST_COLORS = {
    google: { border: "#3da9fc", fill: "rgba(61, 169, 252, 0.12)" },
    ibm: { border: "#ff9f43", fill: "rgba(255, 159, 67, 0.12)" },
    att: { border: "#2cb67d", fill: "rgba(44, 182, 125, 0.12)" },
  };

  const FALLBACK_PALETTE = ["#3da9fc", "#ff9f43", "#2cb67d", "#a78bfa"];

  const latencyCanvas = document.getElementById("latencyChart");
  const outageCanvas = document.getElementById("outageChart");
  const donutCanvas = document.getElementById("uptimeDonutChart");
  const targetSelect = document.getElementById("chart-target-select");
  const chartStatusEl = document.getElementById("charts-status");
  const headerUpdatedEl = document.getElementById("header-last-updated");
  const sidebarSyncEl = document.getElementById("sidebar-last-sync");
  const donutCaptionEl = document.getElementById("uptime-donut-caption");

  const rangeButtons = root.querySelectorAll(".range-btn");

  /** Active window for /api/logs (hours). Separate from card METRICS window in config. */
  let selectedLogHours = METRICS_HOURS_DEFAULT;

  Chart.defaults.color = "#9aa7b8";
  Chart.defaults.borderColor = "rgba(148, 163, 184, 0.14)";
  Chart.defaults.font.family = 'system-ui, "Segoe UI", Roboto, sans-serif';

  function parseUtcTimestamp(ts) {
    const s = String(ts || "").trim();
    if (!s) return new Date(NaN);
    const iso = s.includes("T") ? s : s.replace(" ", "T");
    const hasZone = /Z|[+-]\d\d:?(\d\d)?$/.test(iso);
    return new Date(hasZone ? iso : `${iso}Z`);
  }

  function latencyY(row) {
    if (row.status === "DOWN") return null;
    if (row.response_time_ms === null || row.response_time_ms === undefined) return null;
    const v = Number(row.response_time_ms);
    return Number.isFinite(v) ? v : null;
  }

  function hostKey(hostname) {
    const h = String(hostname || "").toLowerCase();
    if (h.includes("google")) return "google";
    if (h.includes("ibm")) return "ibm";
    if (h.includes("att")) return "att";
    return "";
  }

  function colorsForHost(hostname, index) {
    const key = hostKey(hostname);
    if (key && HOST_COLORS[key]) return HOST_COLORS[key];
    const c = FALLBACK_PALETTE[index % FALLBACK_PALETTE.length];
    return { border: c, fill: `${c}22` };
  }

  /** Enough samples for ~30s probes: cap to backend max (2000). */
  function logLimitForHours(hours) {
    const approxPerHour = 120;
    return Math.min(MAX_LOG_LIMIT, Math.max(100, Math.ceil(hours * approxPerHour)));
  }

  /**
   * Soft cap Y axis when a single spike dwarfs normal traffic (readability).
   */
  function suggestedYMaxMs(datasets) {
    const ys = [];
    datasets.forEach((ds) => {
      ds.data.forEach((pt) => {
        if (pt.y !== null && pt.y !== undefined && !Number.isNaN(pt.y)) ys.push(pt.y);
      });
    });
    if (!ys.length) return 120;
    ys.sort((a, b) => a - b);
    const max = ys[ys.length - 1];
    const p95 = ys[Math.min(ys.length - 1, Math.floor(ys.length * 0.95))];
    const med = ys[Math.floor(ys.length / 2)];
    let cap = max;
    if (max > Math.max(med * 4, 80) && med > 0) {
      cap = Math.min(max, Math.max(p95 * 1.35, med * 3));
    }
    cap = Math.max(cap * 1.08, 40);
    return Math.ceil(cap / 20) * 20;
  }

  async function fetchJson(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  let latencyChart = null;
  let outageChart = null;
  let uptimeDonutChart = null;
  let timerId = null;

  function setChartsStatus(text) {
    if (chartStatusEl) chartStatusEl.textContent = text;
  }

  function stampTimestamps() {
    const ts = new Date();
    const readable = ts.toLocaleString();
    if (headerUpdatedEl) headerUpdatedEl.textContent = `Last updated ${readable}`;
    if (sidebarSyncEl) sidebarSyncEl.textContent = `Last sync ${ts.toLocaleTimeString()}`;
  }

  function ensureLatencyChart(initialConfig) {
    if (latencyChart) return latencyChart;
    latencyChart = new Chart(latencyCanvas.getContext("2d"), initialConfig);
    return latencyChart;
  }

  function ensureOutageChart(initialConfig) {
    if (outageChart) return outageChart;
    outageChart = new Chart(outageCanvas.getContext("2d"), initialConfig);
    return outageChart;
  }

  function ensureDonutChart(initialConfig) {
    if (uptimeDonutChart) return uptimeDonutChart;
    uptimeDonutChart = new Chart(donutCanvas.getContext("2d"), initialConfig);
    return uptimeDonutChart;
  }

  function buildLatencyDatasets(logs, selectedValue) {
    const ascending = [...logs].sort(
      (a, b) => parseUtcTimestamp(a.timestamp) - parseUtcTimestamp(b.timestamp),
    );

    const mkDataset = (label, rows, colorIdx) => {
      const cols = colorsForHost(label, colorIdx);
      return {
        label,
        data: rows.map((row) => ({
          x: parseUtcTimestamp(row.timestamp),
          y: latencyY(row),
        })),
        borderColor: cols.border,
        backgroundColor: cols.fill,
        tension: 0.3,
        spanGaps: false,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
      };
    };

    if (selectedValue === "all") {
      const groups = new Map();
      for (const row of ascending) {
        const id = row.target_id;
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(row);
      }

      const datasets = [];
      let idx = 0;
      for (const [, rows] of [...groups.entries()].sort((a, b) => Number(a[0]) - Number(b[0]))) {
        const label = rows[0]?.target || rows[0]?.hostname || `target ${rows[0]?.target_id}`;
        datasets.push(mkDataset(label, rows, idx));
        idx += 1;
      }
      return datasets.length ? datasets : [mkDataset("No data", [], 0)];
    }

    const tid = Number(selectedValue);
    const rows = ascending.filter((r) => Number(r.target_id) === tid);
    const label = rows[0]?.target || rows[0]?.hostname || `target ${tid}`;
    return [mkDataset(label, rows, 0)];
  }

  function syncTargetOptions(statusRows) {
    const previous = targetSelect.value;
    targetSelect.innerHTML = "";

    const optAll = document.createElement("option");
    optAll.value = "all";
    optAll.textContent = "All targets";
    targetSelect.appendChild(optAll);

    const sorted = [...statusRows].sort((a, b) => Number(a.target_id) - Number(b.target_id));
    for (const row of sorted) {
      const opt = document.createElement("option");
      opt.value = String(row.target_id);
      opt.textContent = row.hostname || row.target || `target ${row.target_id}`;
      targetSelect.appendChild(opt);
    }

    const allowed = new Set(["all", ...sorted.map((r) => String(r.target_id))]);
    targetSelect.value = allowed.has(previous) ? previous : "all";
  }

  function updateOutageChart(statusRows) {
    const sorted = [...statusRows].sort((a, b) => Number(a.target_id) - Number(b.target_id));
    const labels = sorted.map((r) => r.hostname || r.target || `target ${r.target_id}`);
    const colors = labels.map((lb, i) => colorsForHost(lb, i).border);
    const values = sorted.map((r) => Number(r.outage_count || 0));

    const cfg = {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "DOWN samples",
            data: values,
            backgroundColor: colors.map((c) => `${c}44`),
            borderColor: colors.map((c) => `${c}cc`),
            borderWidth: 1,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
        },
        scales: {
          x: {
            beginAtZero: true,
            ticks: { color: "#9aa7b8", precision: 0 },
            grid: { color: "rgba(148, 163, 184, 0.08)" },
            border: { display: false },
          },
          y: {
            ticks: { color: "#9aa7b8" },
            grid: { display: false },
            border: { display: false },
          },
        },
      },
    };

    const chart = ensureOutageChart(cfg);
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.data.datasets[0].backgroundColor = colors.map((c) => `${c}44`);
    chart.data.datasets[0].borderColor = colors.map((c) => `${c}cc`);
    chart.update("none");
  }

  function updateUptimeDonut(statusRows) {
    const pts = statusRows
      .map((r) => r.uptime_pct)
      .filter((v) => v !== null && v !== undefined && !Number.isNaN(Number(v)));
    if (!pts.length) {
      if (donutCaptionEl) donutCaptionEl.textContent = "No uptime samples yet.";
      const chart = ensureDonutChart({
        type: "doughnut",
        data: { labels: ["UP", "DOWN"], datasets: [{ data: [1, 0], backgroundColor: ["#2cb67d33", "#ef456533"] }] },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "68%",
          plugins: { legend: { position: "bottom", labels: { color: "#cbd5e1", boxWidth: 10 } } },
        },
      });
      chart.data.datasets[0].data = [1, 0];
      chart.update("none");
      return;
    }

    const overall = pts.reduce((a, b) => a + Number(b), 0) / pts.length;
    const downPct = Math.max(0, Math.min(100, 100 - overall));

    if (donutCaptionEl) {
      donutCaptionEl.textContent = `Mean uptime across targets · ${overall.toFixed(1)}% UP / ${downPct.toFixed(1)}% DOWN (approx.)`;
    }

    const cfg = {
      type: "doughnut",
      data: {
        labels: ["UP", "DOWN"],
        datasets: [
          {
            data: [overall, downPct],
            backgroundColor: ["rgba(44, 182, 125, 0.65)", "rgba(239, 69, 101, 0.55)"],
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "68%",
        plugins: {
          legend: { position: "bottom", labels: { color: "#cbd5e1", boxWidth: 10 } },
        },
      },
    };

    const chart = ensureDonutChart(cfg);
    chart.data.datasets[0].data = [overall, downPct];
    chart.update("none");

    /** Center text via plugin-free subtitle in caption; Chart.js center text needs plugin — caption explains. */
  }

  function applyLatencyChartData(logs) {
    const datasets = buildLatencyDatasets(logs, targetSelect.value);
    const yMax = suggestedYMaxMs(datasets);

    const baseOptions = {
      responsive: true,
      maintainAspectRatio: false,
      parsing: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: {
          type: "time",
          time: { tooltipFormat: "MMM d, HH:mm" },
          ticks: { color: "#9aa7b8", maxRotation: 0, autoSkip: true },
          grid: { color: "rgba(148, 163, 184, 0.08)" },
          border: { display: false },
        },
        y: {
          beginAtZero: true,
          suggestedMax: yMax,
          title: { display: true, text: "ms", color: "#7d8a9b", font: { size: 11 } },
          ticks: { color: "#9aa7b8" },
          grid: { color: "rgba(148, 163, 184, 0.08)" },
          border: { display: false },
        },
      },
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: "#e5edf7",
            boxWidth: 10,
            usePointStyle: true,
          },
        },
        tooltip: {
          callbacks: {
            label(ctx) {
              const y = ctx.parsed?.y;
              if (y === null || y === undefined || Number.isNaN(y)) {
                return `${ctx.dataset.label}: DOWN / no latency`;
              }
              return `${ctx.dataset.label}: ${Math.round(y)} ms`;
            },
          },
        },
      },
    };

    if (!latencyChart) {
      ensureLatencyChart({
        type: "line",
        data: { datasets },
        options: baseOptions,
      });
      return;
    }

    latencyChart.data.datasets = datasets;
    latencyChart.options.scales.y.suggestedMax = yMax;
    latencyChart.update("none");
  }

  async function refreshDashboardCharts() {
    try {
      setChartsStatus("Updating charts…");

      const limit = logLimitForHours(selectedLogHours);
      const limitParam = encodeURIComponent(String(limit));
      const hoursParam = encodeURIComponent(String(selectedLogHours));

      const [statusRows, logs] = await Promise.all([
        fetchJson("/api/status"),
        fetchJson(`/api/logs?hours=${hoursParam}&limit=${limitParam}`),
      ]);

      syncTargetOptions(statusRows);
      updateOutageChart(statusRows);
      updateUptimeDonut(statusRows);

      const filteredLogs =
        targetSelect.value === "all"
          ? logs
          : logs.filter((row) => String(row.target_id) === String(targetSelect.value));

      applyLatencyChartData(filteredLogs);

      stampTimestamps();
      setChartsStatus(
        `Range: ${selectedLogHours}h · ${logs.length} samples loaded · next refresh in ${Math.round(REFRESH_MS / 1000)}s`,
      );
    } catch (err) {
      console.error(err);
      setChartsStatus(`Chart refresh failed: ${err.message || err}`);
    }
  }

  function restartTimer() {
    if (timerId) window.clearInterval(timerId);
    timerId = window.setInterval(refreshDashboardCharts, REFRESH_MS);
  }

  function setActiveRangeButton(hours) {
    rangeButtons.forEach((btn) => {
      btn.classList.toggle("is-active", Number(btn.dataset.hours) === Number(hours));
    });
  }

  rangeButtons.forEach((btn) => {
    btn.addEventListener("click", async () => {
      selectedLogHours = Number(btn.dataset.hours) || 24;
      setActiveRangeButton(selectedLogHours);
      await refreshDashboardCharts();
    });
  });

  targetSelect.addEventListener("change", async () => {
    try {
      setChartsStatus("Switching target…");
      const limit = logLimitForHours(selectedLogHours);
      const hoursParam = encodeURIComponent(String(selectedLogHours));
      const limitParam = encodeURIComponent(String(limit));
      const logs = await fetchJson(`/api/logs?hours=${hoursParam}&limit=${limitParam}`);
      const filteredLogs =
        targetSelect.value === "all"
          ? logs
          : logs.filter((row) => String(row.target_id) === String(targetSelect.value));
      applyLatencyChartData(filteredLogs);
      setChartsStatus("Target updated.");
    } catch (err) {
      console.error(err);
      setChartsStatus(`Failed to load logs: ${err.message || err}`);
    }
  });

  /* Initial range button state matches selectedLogHours */
  setActiveRangeButton(selectedLogHours);

  refreshDashboardCharts().finally(() => {
    restartTimer();
  });
})();

/**
 * AI summary panel — loads once on dashboard visit + manual refresh only (no polling).
 */
(() => {
  const panel = document.getElementById("ai-summary-panel");
  if (!panel) return;

  const textEl = document.getElementById("ai-summary-text");
  const metaEl = document.getElementById("ai-summary-meta");
  const badgeEl = document.getElementById("ai-status-badge");
  const btn = document.getElementById("ai-refresh-btn");

  const cacheTtlMs = (Number(panel.dataset.cacheTtlSeconds) || 300) * 1000;

  function setBadge(kind, label) {
    badgeEl.textContent = label;
    badgeEl.className = "ai-badge";
    if (kind === "fresh") badgeEl.classList.add("ai-badge-fresh");
    else if (kind === "stale") badgeEl.classList.add("ai-badge-stale");
    else if (kind === "loading") badgeEl.classList.add("ai-badge-loading");
    else if (kind === "error") badgeEl.classList.add("ai-badge-error");
    else if (kind === "offline") badgeEl.classList.add("ai-badge-offline");
    else badgeEl.classList.add("ai-badge-muted");
  }

  function parseGeneratedAt(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function isStale(generatedAt) {
    const d = parseGeneratedAt(generatedAt);
    if (!d) return true;
    return Date.now() - d.getTime() > cacheTtlMs;
  }

  async function loadAiSummary(forceRefresh) {
    btn.disabled = true;
    setBadge("loading", forceRefresh ? "Generating…" : "Loading…");
    if (textEl) textEl.textContent = forceRefresh ? "Requesting a fresh summary from Gemini…" : "Loading AI summary…";

    try {
      const url = forceRefresh ? "/api/ai-summary?refresh=1" : "/api/ai-summary";
      const res = await fetch(url, { cache: "no-store" });
      const data = await res.json();

      const summary = data.summary || "";
      const generatedAt = data.generated_at || "";
      const ok = data.status === "success";
      const configured = data.gemini_configured !== false;

      if (!configured || data.error === "missing_api_key") {
        setBadge("offline", "AI offline · no API key");
        textEl.textContent =
          summary ||
          "Gemini API key is missing. Add GEMINI_API_KEY to your .env file.";
        metaEl.textContent = "Set GEMINI_API_KEY in .env (project root) and restart Flask.";
        return;
      }

      if (!ok) {
        setBadge("error", "AI error");
        textEl.textContent =
          summary ||
          "AI summary unavailable right now. Check the server logs for Gemini details.";
        const metaParts = [];
        if (data.error_code) metaParts.push(`code: ${data.error_code}`);
        if (generatedAt) metaParts.push(`Last attempt · ${generatedAt}`);
        if (data.detail) metaParts.push(data.detail);
        metaEl.textContent = metaParts.join(" · ");
        return;
      }

      textEl.textContent = summary;
      const cachedNote = data.cached ? " (served from cache)" : "";
      metaEl.textContent = `${generatedAt ? `Generated ${generatedAt}` : ""}${cachedNote}`;

      const stale = isStale(generatedAt);
      if (stale) setBadge("stale", "Summary stale · refresh recommended");
      else setBadge("fresh", "AI online · summary fresh");
    } catch (err) {
      console.error(err);
      setBadge("error", "Request failed");
      textEl.textContent = "Could not reach the AI summary API.";
      metaEl.textContent = String(err.message || err);
    } finally {
      btn.disabled = false;
    }
  }

  btn.addEventListener("click", () => loadAiSummary(true));
  loadAiSummary(false);
})();
