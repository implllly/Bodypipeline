"use strict";
window.ClientCaptureTelemetry = (() => {
  const colors = [
    "#58a6ff", "#43d17c", "#ff9f43", "#d978ff", "#ff6470", "#46d9e8",
    "#ffd166", "#95e06c", "#f78fb3", "#a29bfe", "#00cec9", "#e17055",
    "#74b9ff", "#55efc4", "#fdcb6e", "#fab1a0",
  ];
  const preferred = [
    "robot.body_q[0]", "robot.body_q[1]",
    "robot.last_action[0]", "robot.last_action[1]",
    "planner.movement[0]", "planner.movement[1]",
  ];
  const $ = (id) => document.getElementById(id);
  let context = null;
  let known = [];
  let selected = [];
  let windowSeconds = 20;
  let catalogTimer = null;
  let historyTimer = null;
  let generation = 0;
  let latestSeries = {};
  let latestNow = Date.now() / 1000;
  let catalogLoading = false;
  let historyLoading = false;

  function enabled() {
    return !!context?.config?.capture_capabilities?.telemetry;
  }

  function active() {
    return enabled() && !document.hidden && context?.isActive?.() !== false;
  }

  function maxSignals() {
    return context?.config?.capture_capabilities?.telemetry?.max_signals || 16;
  }

  function restoreSelection() {
    try {
      const rows = JSON.parse(localStorage.getItem("bodypipeline.sonic.signals.v1") || "[]");
      selected = Array.isArray(rows)
        ? rows.filter((name) => typeof name === "string").slice(0, maxSignals())
        : [];
    } catch {
      selected = [];
    }
  }

  function saveSelection() {
    try {
      localStorage.setItem("bodypipeline.sonic.signals.v1", JSON.stringify(selected));
    } catch {
      // Preferences are optional; telemetry must remain usable without storage.
    }
  }

  function defaultSelection() {
    selected = preferred.filter((name) => known.includes(name));
    if (!selected.length) selected = known.slice(0, Math.min(6, maxSignals()));
    saveSelection();
  }

  function renderList() {
    if (!enabled()) return;
    const query = $("sonic-signal-search").value.trim().toLowerCase();
    const items = known.filter((name) => name.toLowerCase().includes(query));
    $("sonic-signal-list").innerHTML = items.length
      ? items.slice(0, 500).map((name) => (
          '<label title="' + context.esc(name) + '">' +
          '<input type="checkbox" data-sonic-signal="' + context.esc(name) + '"' +
          (selected.includes(name) ? " checked" : "") + ">" +
          context.esc(name) + "</label>"
        )).join("")
      : '<span class="muted">没有匹配信号</span>';
    $("sonic-signal-list").querySelectorAll("[data-sonic-signal]").forEach((input) => {
      input.onchange = () => {
        const name = input.dataset.sonicSignal;
        if (input.checked) {
          if (selected.length >= maxSignals()) {
            input.checked = false;
            context.notice("最多选择 " + maxSignals() + " 条曲线。", true);
            return;
          }
          if (!selected.includes(name)) selected.push(name);
        } else {
          selected = selected.filter((item) => item !== name);
        }
        saveSelection();
        renderList();
        void refreshHistory();
      };
    });
  }

  function draw(series = {}, now = Date.now() / 1000) {
    const canvas = $("sonic-signal-chart");
    if (!canvas || !enabled()) return;
    const ratio = window.devicePixelRatio || 1;
    const bounds = canvas.getBoundingClientRect();
    if (canvas.width !== Math.round(bounds.width * ratio)
        || canvas.height !== Math.round(bounds.height * ratio)) {
      canvas.width = Math.round(bounds.width * ratio);
      canvas.height = Math.round(bounds.height * ratio);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const width = bounds.width;
    const height = bounds.height;
    const legendColumns = width < 520 ? 2 : 4;
    const legendRows = Math.ceil(selected.length / legendColumns);
    const legendHeight = 20 + legendRows * 14;
    const pad = {
      left: 55,
      right: 16,
      top: 20,
      bottom: Math.max(75, legendHeight + 20),
    };
    ctx.clearRect(0, 0, width, height);
    const values = selected.flatMap((name) => (series[name] || []).map((point) => point[1]));
    if (!values.length) {
      ctx.fillStyle = "#8fa1b5";
      ctx.textAlign = "center";
      ctx.font = "12px system-ui";
      ctx.fillText(
        selected.length ? "等待所选信号数据…" : "请勾选要显示的信号",
        width / 2,
        height / 2,
      );
      return;
    }
    let low = Math.min(...values);
    let high = Math.max(...values);
    if (low === high) {
      low -= 1;
      high += 1;
    }
    const margin = (high - low) * 0.08;
    low -= margin;
    high += margin;
    const xmin = now - windowSeconds;
    ctx.strokeStyle = "#263445";
    ctx.fillStyle = "#8fa1b5";
    ctx.font = "11px system-ui";
    ctx.textAlign = "right";
    for (let index = 0; index <= 4; index += 1) {
      const y = pad.top + ((height - pad.top - pad.bottom) * index) / 4;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.fillText(
        (high - ((high - low) * index) / 4).toFixed(2),
        pad.left - 7,
        y + 4,
      );
    }
    selected.forEach((name, index) => {
      ctx.strokeStyle = colors[index % colors.length];
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      (series[name] || []).forEach((point, pointIndex) => {
        const x = pad.left
          + ((point[0] - xmin) / windowSeconds) * (width - pad.left - pad.right);
        const y = pad.top
          + ((high - point[1]) / (high - low)) * (height - pad.top - pad.bottom);
        if (pointIndex) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
      });
      ctx.stroke();
    });
    ctx.textAlign = "left";
    selected.forEach((name, index) => {
      ctx.fillStyle = colors[index % colors.length];
      const columnWidth = width / legendColumns;
      const maxCharacters = Math.max(10, Math.floor((columnWidth - 12) / 6));
      const label = name.length > maxCharacters
        ? name.slice(0, Math.max(7, maxCharacters - 1)) + "…"
        : name;
      ctx.fillText(
        label,
        8 + (index % legendColumns) * columnWidth,
        height - legendHeight + Math.floor(index / legendColumns) * 14,
      );
    });
  }

  async function refreshCatalog() {
    clearTimeout(catalogTimer);
    if (!active() || catalogLoading) return;
    catalogLoading = true;
    const requestGeneration = generation;
    try {
      const result = await context.api("/api/capture-telemetry/signals");
      if (requestGeneration !== generation || !active()) return;
      const next = Array.isArray(result.signals) ? result.signals : [];
      if (JSON.stringify(next) !== JSON.stringify(known)) {
        const first = known.length === 0;
        known = next;
        selected = selected.filter((name) => known.includes(name));
        if (first && !selected.length) defaultSelection();
        renderList();
      }
      const telemetry = context.service?.()?.telemetry || {};
      $("capture-telemetry-summary").textContent =
        (telemetry.uptime_seconds !== undefined
          ? "运行 " + Math.round(telemetry.uptime_seconds) + "s · "
          : "")
        + known.length
        + " 个数值信号"
        + (result.truncated ? " · 仅显示前 5000 个" : "");
    } catch (error) {
      if (requestGeneration === generation && active()) {
        $("capture-telemetry-summary").textContent = "遥测连接失败：" + error.message;
      }
    } finally {
      catalogLoading = false;
      if (active()) catalogTimer = setTimeout(refreshCatalog, 5000);
    }
  }

  async function refreshHistory() {
    clearTimeout(historyTimer);
    if (!active()) return;
    if (!selected.length) {
      latestSeries = {};
      draw();
      historyTimer = setTimeout(refreshHistory, 1000);
      return;
    }
    if (historyLoading) {
      historyTimer = setTimeout(refreshHistory, 250);
      return;
    }
    historyLoading = true;
    const requestGeneration = generation;
    const query = selected.map((name) => "signal=" + encodeURIComponent(name)).join("&");
    try {
      const data = await context.api("/api/capture-telemetry/history?" + query);
      if (requestGeneration !== generation || !active()) return;
      latestSeries = data.series || {};
      latestNow = Number(data.now) || Date.now() / 1000;
      windowSeconds = Number(data.window_seconds) || windowSeconds;
      draw(latestSeries, latestNow);
    } catch (error) {
      if (requestGeneration === generation && active()) {
        $("capture-telemetry-summary").textContent = "曲线读取失败：" + error.message;
      }
    } finally {
      historyLoading = false;
      if (active()) historyTimer = setTimeout(refreshHistory, 250);
    }
  }

  function configure(value) {
    stop();
    context = value;
    const panel = $("sonic-signal-panel");
    panel.hidden = !enabled();
    if (!enabled()) return;
    restoreSelection();
    $("sonic-signal-search").oninput = renderList;
    $("sonic-signal-clear").onclick = () => {
      selected = [];
      saveSelection();
      renderList();
      draw();
      void refreshHistory();
    };
    generation += 1;
    void refreshCatalog();
    void refreshHistory();
  }

  function stop() {
    generation += 1;
    clearTimeout(catalogTimer);
    clearTimeout(historyTimer);
    catalogTimer = null;
    historyTimer = null;
    catalogLoading = false;
    historyLoading = false;
  }

  function refresh() {
    if (!enabled()) return;
    stop();
    void refreshCatalog();
    void refreshHistory();
  }

  addEventListener("resize", () => draw(latestSeries, latestNow));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else refresh();
  });

  return { configure, refresh, stop, get selected() { return [...selected]; } };
})();
