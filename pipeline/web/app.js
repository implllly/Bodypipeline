"use strict";
const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (x) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        x
      ],
  );
const sessionCaptures = new Set();
let config = null,
  captures = [],
  timer = null,
  frameTimer = null,
  healthTimer = null,
  clockTimer = null,
  frameController = null,
  frameFetching = false,
  previewPaused = false,
  clientHealth = null,
  busy = false,
  polling = false,
  selectedJob = null,
  serverUploads = [],
  serverCollections = [],
  activeCollection = null,
  selectedEpisodes = new Set(),
  serverJobs = [],
  serverMergeCandidates = [],
  serverVersionConversionCandidates = [],
  serverDatasets = [],
  serverDeletions = [],
  serverAnnotations = [],
  deletingDataset = null,
  selectedMergeDatasets = new Set(),
  activeVersionConversionSource = null,
  serverTab = "convert",
  clientTab = "capture",
  selectedCheck = null,
  detailSignature = null,
  logText = "选择日志查看执行诊断。",
  requestKey = null;
const names = {
  DRAFT: "待启动",
  STARTING: "正在启动",
  RECORDING: "录制中",
  STOPPING: "正在停止",
  RECOVERY_REQUIRED: "待恢复",
  REVIEW: "待确认保留",
  SEALING: "HDF5 打包中",
  SEALED: "已封存",
  DISCARDED: "已丢弃",
  QUEUED: "等待处理",
  UPLOADING: "正在上传",
  RETRY: "等待重试",
  ACKED: "远端已确认",
  NONE: "未上传",
  COMMITTING: "正在提交",
  COMMITTED: "已接收",
  RUNNING: "处理中",
  FAILED: "执行失败",
  QUARANTINED: "质检异常",
  BLOCKED: "待审批",
  INTERRUPTED: "任务中断",
  DEMO_READY: "演示完成",
  SYNTHETIC_READY: "模拟数据就绪",
  CONVERTED: "已转换 · 待质检",
  COMPLETED: "转换完成",
  MOCK: "模拟数据",
  PASS: "PASS",
  WARN: "WARN",
  FAIL: "FAIL",
  READY: "已就绪",
  STOPPED: "未启动",
  IMPORTED: "历史导入",
  MERGED: "合并数据集",
  VERSION_CONVERTED: "版本转换",
  SUBTASK_EXPORTED: "Subtask 数据集",
  DELETING: "正在删除",
  DELETE_FAILED: "删除失败",
  DELETED: "已删除",
  UNAVAILABLE: "暂不可删除",
};
const jobBadge = (job) => job.operation === "merge" && job.state === "CONVERTED"
  ? '<span class="badge">合并完成 · 待质检</span>'
  : job.operation === "subtask_export" && job.state === "CONVERTED"
    ? '<span class="badge">Subtask 已生成 · 待质检</span>'
    : badge(job.state);
// Every conversion-like job reuses CONVERTED, but their stage text differs.
const jobStageText = (job) => {
  if (job.operation === "subtask_export") return job.stage;
  return job.state === "CONVERTED" && job.operation !== "merge"
    ? "转换完成（独立质检见报告）"
    : job.stage;
};
const sourceBadge = (job) => {
  if (job.operation === "merge") return badge("MERGED") + (job.capture_mode === "mock" ? " " + badge("MOCK") : "");
  if (job.operation === "version_conversion") return badge("VERSION_CONVERTED") + (job.capture_mode === "mock" ? " " + badge("MOCK") : "");
  if (job.operation === "subtask_export") return badge("SUBTASK_EXPORTED") + (job.capture_mode === "mock" ? " " + badge("MOCK") : "");
  const upload = serverUploads.find((u) => u.id === job.upload_id);
  if (job.capture_mode === "mock" || upload?.capture_mode === "mock") return badge("MOCK");
  if (job.capture_mode === "imported" || upload?.capture_mode === "imported" || upload?.source_kind === "raw_import") return badge("IMPORTED");
  return "";
};
const collectionLocation = (collection) => collection.target === "imported"
  ? collection.uploads?.[0]?.import_path || "历史数据目录"
  : `${collection.target}/${collection.prefix || ""}`;
const bytes = (n) =>
  n > 1024 ** 3
    ? (n / 1024 ** 3).toFixed(2) + " GB"
    : n > 1024 ** 2
      ? (n / 1024 ** 2).toFixed(1) + " MB"
      : (n / 1024).toFixed(1) + " KB";
const badge = (s) =>
  `<span class="badge ${["FAILED", "FAIL", "RECOVERY_REQUIRED", "DELETE_FAILED"].includes(s) ? "error" : ["RUNNING", "RECORDING", "UPLOADING", "STARTING", "STOPPING", "DELETING"].includes(s) ? "busy" : ["BLOCKED", "QUARANTINED", "RETRY", "REVIEW", "INTERRUPTED", "WARN", "CONVERTED", "UNAVAILABLE"].includes(s) ? "warn" : ""}">${esc(names[s] || s)}</span>`;
function notice(message, error = false) {
  const box = $("notice");
  box.hidden = false;
  box.className = error ? "error" : "";
  box.textContent = message;
}
function noticeWithLink(prefix, label, href) {
  const box = $("notice");
  box.hidden = false;
  box.className = "";
  box.textContent = prefix + " ";
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = label;
  box.append(link);
}
async function api(path, method = "GET", body) {  const controller = new AbortController();
  // Writes get a longer budget, but never unbounded: a hung request must not
  // leave the page's busy flag stuck and swallow every later click.
  const timeout = setTimeout(() => controller.abort(), method === "GET" ? 12000 : 30000);
  try {
  const r = await fetch(path, {
    signal: controller.signal,
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json();
  if (!r.ok) {
    if (r.status === 401) showLogin();
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail || data),
    );
  }
  return data;
  } finally { clearTimeout(timeout); }
}
function showLogin() {
  window.ClientReplay.deactivate();
  window.AnnotationUI?.hide();
  clearTimeout(timer);
  clearTimeout(frameTimer);
  clearTimeout(healthTimer);
  clearInterval(clockTimer);
  frameController?.abort();
  $("login-view").hidden = false;
  $("app-view").hidden = true;
  config = null;
  captures = [];
  clientHealth = null;
  requestKey = null;
  setSidebarOpen(false);
}
async function action(fn) {
  if (busy) {
    notice("上一个操作仍在处理中，请稍候再试。");
    return;
  }
  busy = true;
  const client = config?.role === "client";
  if (client) {
    clearTimeout(frameTimer);
    frameController?.abort();
    renderClientCaptures();
  }
  try {
    await fn();
    if (!client) await refresh();
  } catch (e) {
    notice(e.message, true);
  } finally {
    busy = false;
    if (config?.role === "server") { updateConversionSelection(); updateMergeSelection(); updateVersionConversionSelection(); renderDatasetManagement(); if ($("dataset-delete-dialog").open) $("dataset-delete-confirmation").oninput(); }
    if (client && config?.role === "client") {
      renderClientCaptures();
      void refresh();
      if (!frameFetching) {
        clearTimeout(frameTimer);
        frameTimer = setTimeout(refreshFrames, 1500);
      }
    }
  }
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", "POST", { token: $("token").value });
    $("token").value = "";
    await boot();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});
$("logout").onclick = async () => {
  await api("/api/logout", "POST");
  showLogin();
};
async function boot() {
  try {
    config = await api("/api/config");
  } catch {
    return;
  }
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  const client = config.role === "client";
  $("client-nav").hidden = !client;
  $("server-nav").hidden = client;
  $("current-page").textContent = client ? "采集工作台" : "数据转换";
  $("client-view").hidden = !client;
  $("server-view").hidden = client;
  $("client-replay-panel").hidden = true;
  $("workspace-label").textContent = client ? "机器人采集" : "中心数据服务";
  $("nav-label").textContent = client ? "采集工作台" : "数据管理台";
  $("page-title").textContent = client ? "采集工作台" : "数据管理台";
  $("page-eyebrow").textContent = client
    ? "TELEOPERATION COLLECTION"
    : "DATA OPERATIONS";
  $("page-description").textContent = client
    ? "在机器人本地录制，确认保留后可靠上传。"
    : "从原始采集到可追溯、可审查的 LeRobot 数据版本。";
  $("mode-badge").textContent = client
    ? config.mode === "mock"
      ? "MOCK · 模拟模式"
      : "ROS2 · 遥操作"
    : config.processing_mode === "demo"
      ? "DEMO · 演示处理"
      : "SERVER · 中心服务";
  if (client) {
    $("mock-warning").hidden = config.mode !== "mock";
    $("robot-label").textContent = config.robot_id;
    $("local-root").innerHTML = config.local_roots
      .map((r) => `<option value="${esc(r.id)}">${esc(r.id)}</option>`)
      .join("");
    updateLocalPath();
    $("remote-target").innerHTML = '<option value="default">default（远端待连接）</option>';
    void loadTargets().catch(() => {
      $("target-note").textContent = "远端目标暂未更新，请检查连接后刷新页面。";
    });
    $("camera-count").textContent = config.profile.cameras.length;
    $("camera-grid").innerHTML = config.profile.cameras
      .map(
        (c) =>
          `<div class="camera-tile"><img id="cam-${esc(c.id)}" alt="${esc(c.label)} 实时预览"><div class="camera-error" id="err-${esc(c.id)}">等待相机图像…</div><div class="camera-caption">${esc(c.label)} <span>${esc(c.id.toUpperCase())}</span></div></div>`,
      )
      .join("");
    setClientTab(location.hash.slice(1) || "capture", false);
    clearTimeout(healthTimer);
    void refreshClientHealth();
    clearInterval(clockTimer);
    clockTimer = setInterval(updateCaptureClock, 250);
  } else {
    $("demo-warning").hidden = config.processing_mode !== "demo";
    $("health-label").textContent = "● 中心服务已连接";
    $("conversion-format").innerHTML = (config.conversion_formats || [])
      .map((f) => `<option value="${esc(f.id)}">${esc(f.label)}</option>`).join("");
    $("conversion-format").value = config.default_target_format || "v3.0";
    const roots = config.import_roots || [];
    $("import-roots").textContent = roots.length
      ? `允许导入路径：${roots.join("、")}`
      : "管理员尚未配置 import_roots；配置后重启 Server 才能导入历史目录。";
    $("import-submit").disabled = !roots.length;
    window.AnnotationUI?.configure({ api, notice, esc, bytes });
    setServerTab(location.hash.slice(1) || serverTab, false);
  }
  void poll();
}
function updateLocalPath() {
  const selected = config.local_roots.find(
    (x) => x.id === $("local-root").value,
  );
  $("local-path").textContent = selected?.path || "";
}
$("local-root").onchange = updateLocalPath;
async function loadTargets() {
  const d = await api("/api/targets");
  $("remote-target").innerHTML = d.targets.length
    ? d.targets
        .map(
          (t) =>
            `<option value="${esc(t.id)}">${esc(t.label)} / ${esc(t.id)}</option>`,
        )
        .join("")
    : '<option value="default">default（远端待连接）</option>';
  $("target-note").textContent = d.cached
    ? "远端离线或不可达，使用缓存/待验证目标；不阻止本地录制。"
    : "目标由远端授权，路径不代表当前电脑文件夹。";
}
async function refreshFrames() {
  clearTimeout(frameTimer);
  if (!config || config.role !== "client" || frameFetching) return;
  if (clientTab !== "capture" || previewPaused || busy || document.hidden) {
    frameTimer = setTimeout(refreshFrames, 1500);
    return;
  }
  frameFetching = true;
  const controller = new AbortController();
  frameController = controller;
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    await Promise.all(config.profile.cameras.map(async (c) => {
      const im = $("cam-" + c.id), err = $("err-" + c.id);
      try {
        const mode = $("preview-quality").value;
        const r = await fetch("/api/cameras/" + encodeURIComponent(c.id) + "/frame?preview=" + mode, {signal: controller.signal});
        if (!r.ok) throw Error();
        const blob = await r.blob();
        if (controller.signal.aborted) return;
        const url = URL.createObjectURL(blob);
        if (im.dataset.url) URL.revokeObjectURL(im.dataset.url);
        im.dataset.url = url;
        im.src = url;
        err.hidden = true;
      } catch {
        if (!controller.signal.aborted) { err.textContent = "预览暂未更新"; err.hidden = false; }
      }
    }));
  } finally {
    clearTimeout(timeout);
    frameFetching = false;
    if (config?.role === "client") frameTimer = setTimeout(refreshFrames, 1500);
  }
}
$("preview-toggle").onclick = () => {
  previewPaused = !previewPaused;
  $("preview-toggle").textContent = previewPaused ? "恢复预览" : "暂停预览";
  $("preview-note").textContent = previewPaused ? "预览已暂停，原始录制不受影响" : "预览独立于原始录制；弱网时可暂停";
  if (previewPaused) frameController?.abort();
  else void refreshFrames();
};
async function refreshClientHealth() {
  if (config?.role !== "client") return;
  try {
    const health = await api("/api/health");
    if (config?.role !== "client") return;
    clientHealth = health;
    $("health-detail").textContent = JSON.stringify(health, null, 2);
  } catch {
    clientHealth = null;
    $("health-detail").textContent = "健康检查暂未更新；采集控制独立刷新。";
  } finally {
    if (config?.role === "client") {
      renderClientCaptures();
      healthTimer = setTimeout(refreshClientHealth, 10000);
    }
  }
}
function acceptCaptures(rows) {
  const merged = new Map(captures.map((c) => [c.id, c]));
  for (const row of rows) {
    const previous = merged.get(row.id);
    if (!previous || (row.updated_at || 0) >= (previous.updated_at || 0)) merged.set(row.id, row);
  }
  captures = [...merged.values()].sort((a, b) => b.created_at - a.created_at);
  renderClientCaptures();
}
function currentCapture() {
  return (
    captures.find((c) =>
      ["STARTING", "RECORDING", "STOPPING", "RECOVERY_REQUIRED"].includes(c.state),
    ) ||
    captures.find((c) => c.state === "REVIEW" && sessionCaptures.has(c.id))
  );
}
function updateCaptureClock() {
  const current = currentCapture();
  if (["STARTING", "STOPPING"].includes(current?.state)) {
    $("capture-duration").textContent = names[current.state];
    return;
  }
  const elapsed = current?.started_at ? Math.max(0, (current.stopped_at || Date.now()/1000) - current.started_at) : 0;
  $("capture-duration").textContent = String(Math.floor(elapsed/60)).padStart(2,"0") + ":" + String(Math.floor(elapsed%60)).padStart(2,"0");
}
const normalizedName = (v) => v.normalize("NFKC").trim().replace(/\s+/g, " ");
function updateClientDatasetSummary() {
  const name = normalizedName($("dataset-name").value);
  $("capture-dataset-summary").textContent = name
    ? `当前数据集：${name}。保留的每次采集作为一个 episode 加入，instruction 在 Server 转换时填写。`
    : "选择已有数据集或输入新名称；同名任务持续追加 episodes。";
}
$("dataset-name").oninput = updateClientDatasetSummary;
function renderClientDatasets() {
  const names = [...new Set(captures.map((c) => c.dataset_name || c.instruction))];
  const options = names.map((name) => `<option value="${esc(name)}"></option>`).join("");
  if ($("dataset-names").innerHTML !== options) $("dataset-names").innerHTML = options;
  if (!$("dataset-name").dataset.initialized) {
    $("dataset-name").dataset.initialized = "true";
    if (!$("dataset-name").value && names.length) $("dataset-name").value = names[0];
  }
  const groups = new Map();
  for (const row of captures) {
    const id = row.collection_id || row.id;
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(row);
  }
  $("client-dataset-list").innerHTML = groups.size ? [...groups].map(([id, rows]) => {
    const c = rows[0];
    return `<tr><td><strong class="task-name">${esc(c.dataset_name || c.instruction)}</strong><span class="sub">${esc(id.slice(0, 12))}</span></td><td>${rows.filter((r) => r.state === "SEALED").length} / ${rows.filter((r) => r.upload_state === "ACKED").length}<span class="sub">${rows.length} 次采集，含待处理和已丢弃</span></td><td>${esc(c.remote_target)}/${esc(c.remote_prefix || "")}<span class="sub">本地 ${esc(c.local_root)}</span></td><td><button type="button" class="secondary" data-use-dataset="${esc(id)}">继续采集</button></td></tr>`;
  }).join("") : '<tr><td colspan="4" class="empty">填写数据集名称，开始第一段采集。</td></tr>';
  updateClientDatasetSummary();
}
function captureBadge(c) {
  return c.capture_failed
    ? '<span class="badge error">采集失败</span>'
    : badge(c.state);
}
function controls(c) {
  const buttons = [];
  if (["RECORDING", "RECOVERY_REQUIRED"].includes(c.state))
    buttons.push(
      `<button class="secondary" data-capture="${c.id}" data-op="stop">结束录制</button>`,
    );
  if (["RECORDING", "REVIEW", "RECOVERY_REQUIRED"].includes(c.state))
    buttons.push(
      `<button class="danger" data-capture="${c.id}" data-op="discard">丢弃</button>`,
    );
  if (c.state === "REVIEW" && !c.capture_failed)
    buttons.push(
      `<button class="primary" data-capture="${c.id}" data-op="keep">保留并上传</button>`,
    );
  if (c.upload_state === "RETRY")
    buttons.push(
      `<button class="secondary" data-capture="${c.id}" data-op="retry-upload">重试上传</button>`,
    );
  if (c.state === "SEALED")
    buttons.push(`<a class="secondary" href="#replay?capture=${encodeURIComponent(c.id)}">回放</a>`);
  return `<div class="action-row">${buttons.join("").replaceAll("<button ", busy ? "<button disabled " : "<button ") || '<span class="muted">—</span>'}</div>`;
}
$("capture-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    if (!requestKey)
      requestKey = Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
        b.toString(16).padStart(2, "0"),
      ).join("");
    const data = {
      dataset_name: $("dataset-name").value,
      idempotency_key: requestKey,
      local_root: $("local-root").value,
      local_prefix: $("local-prefix").value.trim(),
      remote_target: $("remote-target").value,
      remote_prefix: $("remote-prefix").value.trim(),
    };
    $("start-button").disabled = true;
    notice("正在请求开始采集，等待机器人确认…");
    const c = await api("/api/captures/start", "POST", data);
    acceptCaptures([c]);
    sessionCaptures.add(c.id);
    requestKey = null;
    if (c.state !== "RECORDING")
      throw Error(c.error || "启动未确认，请查看会话状态，勿重复启动");
    notice("采集已开始。保留后作为一个 episode 加入当前数据集并上传。");
  });
};
document.addEventListener("click", (e) => {
  const copy = e.target.closest("[data-copy-path]");
  if (copy) {
    copyText(copy.dataset.copyPath).then((copied) =>
      copied
        ? notice("已复制服务器数据集路径。")
        : notice("无法自动复制，请选中显示的路径手动复制。", true));
  }
  const dataset = e.target.closest("[data-use-dataset]");
  if (dataset) {
    const row = captures.find((c) => (c.collection_id || c.id) === dataset.dataset.useDataset);
    if (row) {
      $("dataset-name").value = row.dataset_name || row.instruction;
      $("local-root").value = row.local_root;
      $("local-prefix").value = row.local_prefix || "";
      $("remote-target").value = row.remote_target;
      $("remote-prefix").value = row.remote_prefix || "";
      updateLocalPath(); updateClientDatasetSummary();
      $("capture-form").scrollIntoView({behavior: "smooth", block: "center"});
    }
  }
  const b = e.target.closest("[data-op]");
  if (b)
    action(async () => {
      if (
        b.dataset.op === "discard" &&
        !confirm(
          "确认丢弃本次采集？若正在录制，会先停止。数据移入隔离目录，不会上传。",
        )
      )
        return;
      b.disabled = true;
      notice(b.dataset.op === "keep" ? "正在提交保留请求…" : "正在等待机器人确认操作…");
      const c = await api(
        `/api/captures/${b.dataset.capture}/${b.dataset.op}`,
        "POST",
      );
      acceptCaptures([c]);
      if (c.state === "SEALING") notice("已保留，后台校验后自动上传。现在可以采集下一条。");
      else if (c.error) notice(c.error, true);
      else notice("会话已更新：" + (names[c.state] || c.state));
    });
  const p = e.target.closest("[data-process]");
  if (p)
    action(async () => {
      $("conversion-upload").value = p.dataset.process;
      setServerTab("convert");
      updateConversionSelection();
      $("conversion-form").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  const d = e.target.closest("[data-detail]");
  if (d) {
    selectJob(d.dataset.detail);
    setServerTab("results");
    action(showJob);
  }
  const closePreview = e.target.closest("[data-close-preview]");
  if (closePreview)
    action(async () => {
      await api(`/api/jobs/${closePreview.dataset.closePreview}/viewer`, "DELETE");
      notice("已关闭该数据集的预览链接。");
    });
  const reportPage = e.target.closest("[data-report-page]");
  if (reportPage) {
    window.location.assign("/report/" + encodeURIComponent(reportPage.dataset.reportPage));
    return;
  }
  const v = e.target.closest("[data-viewer]");
  if (v) {
    // Open the tab inside the click so the popup blocker allows it; it shows a
    // waiting page until the always-on Visualizer serves the dataset.
    const popup = window.open("about:blank", "_blank");
    if (popup) {
      popup.opener = null;
      popup.document.title = "正在打开数据预览";
      popup.document.documentElement.dataset.theme = document.documentElement.dataset.theme;
      const themeStyle = popup.document.createElement("link");
      themeStyle.rel = "stylesheet";
      themeStyle.href = new URL("/assets/theme.css", location.href).href;
      popup.document.head.appendChild(themeStyle);
      const themeScript = popup.document.createElement("script");
      themeScript.src = new URL("/assets/theme.js", location.href).href;
      popup.document.head.appendChild(themeScript);
      popup.document.body.innerHTML =
        '<main class="viewer-starting-card" style="max-width:620px;margin:15vh auto;padding:32px;background:#fff;border:1px solid #dedee3;border-radius:12px;font:15px system-ui,sans-serif;line-height:1.7"><h1 style="font-size:22px;margin:0 0 12px">正在打开数据预览</h1><p style="margin:0">Visualizer 就绪后会自动显示所选数据集。</p></main>';
    }
    action(async () => {
      try {
        const preview = await api(`/api/jobs/${v.dataset.viewer}/viewer`, "POST");
        if (v.dataset.viewerMode === "batch") {
          const url = new URL(preview.url);
          url.pathname = url.pathname.replace(/\/episode_\d+\/?$/, "/batch");
          preview.url = url.href;
        }
        selectJob(v.dataset.viewer);
        setServerTab("results");
        if (popup && !popup.closed) {
          popup.location.replace(preview.url);
          notice("已打开数据预览。同时可以打开多个数据集，各自独立。");
        } else {
          noticeWithLink("浏览器拦截了新标签页，点击打开：", "打开数据预览 ↗", preview.url);
        }
      } catch (error) {
        if (popup && !popup.closed) popup.close();
        throw error;
      }
    });
  }
  const tab = e.target.closest("[data-server-tab]");
  if (tab) setServerTab(tab.dataset.serverTab);
  const check = e.target.closest("[data-doctor]");
  if (check) action(async () => {
    const r = await api(`/api/jobs/${check.dataset.doctor}/checks`, "POST");
    selectJob(check.dataset.doctor);
    selectedCheck = r.id;
    setServerTab("results");
    notice("Doctor 已进入质检队列。报告独立保存，不改变转换结果。");
  });
  const report = e.target.closest("[data-report]");
  if (report) {
    selectedCheck = report.dataset.report || null;
    detailSignature = null;
    action(showJob);
  }
  const l = e.target.closest("[data-log]");
  if (l)
    action(async () => {
      const path = l.dataset.checkLog
        ? `/api/checks/${l.dataset.checkLog}/logs/${l.dataset.log}`
        : `/api/jobs/${selectedJob}/logs/${l.dataset.log}`;
      const r = await api(path);
      logText = r.text;
      $("log-output").textContent = logText;
    });
});
function renderClientCaptures() {
  const rows = captures;
  renderClientDatasets();
  const active = rows.find((c) =>
    ["STARTING", "RECORDING", "STOPPING", "RECOVERY_REQUIRED"].includes(
      c.state,
    ),
  );
  for (const row of rows)
    if (
      ["STARTING", "RECORDING", "STOPPING", "RECOVERY_REQUIRED"].includes(
        row.state,
      )
    )
      sessionCaptures.add(row.id);
  // A stale REVIEW capture from an earlier visit must not hijack the panel:
  // only show it here when this page session actually recorded it. Anything
  // else stays in the list below, where keep/discard still work.
  const current =
    active ||
    rows.find((c) => c.state === "REVIEW" && sessionCaptures.has(c.id));
  $("start-button").disabled = !!active || busy;
  $("health-label").textContent = active
    ? "● " + (names[active.state] || active.state)
    : clientHealth?.ready
      ? "● 采集适配器就绪"
      : clientHealth ? "○ 适配器尚未就绪" : "○ 健康检查待更新";
  $("capture-state").textContent = !current
    ? "就绪"
    : current.capture_failed
      ? "采集失败，只能丢弃"
      : names[current.state];
  updateCaptureClock();
  $("session-actions").innerHTML = current
    ? `<span class="small muted">当前会话 ${esc(current.id.slice(0, 8))}</span>${controls(current)}`
    : "";
  $("capture-list").innerHTML = rows.length
    ? rows
        .map((c) => {
          const percent = c.total_bytes
            ? Math.min(
                100,
                Math.round((100 * (c.uploaded_bytes || 0)) / c.total_bytes),
              )
            : 0;
          return `<tr><td><strong class="task-name">${esc(c.dataset_name || c.instruction)}</strong><span class="sub">${esc(c.id.slice(0, 8))} · ${new Date(c.created_at * 1000).toLocaleString("zh-CN")}</span>${c.error ? `<span class="sub job-error">${esc(c.error)}</span>` : ""}</td><td>${captureBadge(c)}</td><td>${esc(c.remote_target)}<span class="sub">${esc(c.remote_prefix || "/")}</span></td><td>${badge(c.upload_state)} ${c.total_bytes ? `<span class="small">${percent}% · ${bytes(c.total_bytes)}</span><div class="progress"><span data-width="${percent}"></span></div>` : ""}${c.upload_error ? `<span class="sub job-error">${esc(c.upload_error)}</span>` : ""}</td><td>${controls(c)}</td></tr>`;
        })
        .join("")
    : '<tr><td colspan="5" class="empty">还没有采集记录。填写任务描述，开始第一段示教。</td></tr>';
  document
    .querySelectorAll("[data-width]")
    .forEach((el) => (el.style.width = el.dataset.width + "%"));
}
let datasetManagementPolling = false;
async function refreshDatasetManagement() {
  if (config?.role !== "server" || serverTab !== "merge" || datasetManagementPolling) return;
  datasetManagementPolling = true;
  try {
    const managed = await api("/api/datasets");
    serverDatasets = managed.datasets;
    serverDeletions = managed.deletions;
    renderServer(serverUploads, serverJobs);
  } catch (e) {
    if (serverTab === "merge") notice(e.message, true);
  } finally {
    datasetManagementPolling = false;
  }
}
async function refresh() {
  if (!config || polling) return;
  polling = true;
  try {
    if (config.role === "client") {
      const rows = await api("/api/capture-status");
      if (config?.role === "client") acceptCaptures(rows);
    } else {
      const [uploads, jobs, v, mergeCandidates, versionConversionCandidates, annotationRows] = await Promise.all([
        api("/api/collections"),
        api("/api/jobs"),
        api("/api/viewer"),
        api("/api/merge-candidates"),
        api("/api/version-conversion-candidates"),
        api("/api/annotations"),
      ]);
      serverCollections = uploads;
      serverUploads = uploads.flatMap((c) => c.uploads);
      serverJobs = jobs;
      serverMergeCandidates = mergeCandidates;
      serverVersionConversionCandidates = versionConversionCandidates;
      serverAnnotations = annotationRows;
      renderServer(serverUploads, jobs);
      renderViewerStatus(v);
      void refreshDatasetManagement();
      if (selectedJob) await showJob();
    }
  } catch (e) {
    notice(e.message, true);
  } finally {
    polling = false;
  }
}
function setSidebarOpen(open, restoreFocus = false) {
  if (open && !matchMedia("(max-width: 850px)").matches) return;
  $("app-view").classList.toggle("sidebar-open", open);
  $("menu-toggle").setAttribute("aria-expanded", String(open));
  $("sidebar-overlay").hidden = !open;
  $("workspace").inert = open;
  if (open) {
    const nav = $("sidebar").querySelector("nav:not([hidden])");
    (nav.querySelector('[aria-pressed="true"]') || nav.querySelector(".nav-item")).focus();
  } else if (restoreFocus) $("menu-toggle").focus();
}
$("menu-toggle").onclick = () => setSidebarOpen(!$("app-view").classList.contains("sidebar-open"));
$("menu-close").onclick = $("sidebar-overlay").onclick = () => setSidebarOpen(false, true);
document.querySelectorAll("[data-client-tab]").forEach((button) => {
  button.onclick = () => setClientTab(button.dataset.clientTab);
});
matchMedia("(max-width: 850px)").addEventListener("change", () => setSidebarOpen(false));
document.addEventListener("keydown", (event) => {
  if (!$("app-view").classList.contains("sidebar-open")) return;
  if (event.key === "Escape") { event.preventDefault(); setSidebarOpen(false, true); }
  if (event.key === "Tab") {
    const items = [...$("sidebar").querySelectorAll("button, a[href]")].filter((el) => el.getClientRects().length);
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
window.addEventListener("hashchange", () => {
  if (config?.role === "server") setServerTab(location.hash.slice(1), false);
  else if (config?.role === "client") setClientTab(location.hash.slice(1), false);
});
function setClientTab(value, updateHistory = true) {
  const tab = value.split("?")[0] === "replay" ? "replay" : "capture";
  clientTab = tab;
  if (updateHistory && location.hash !== "#" + value) { location.hash = value; return; }
  $("client-view").hidden = tab !== "capture";
  $("client-replay-panel").hidden = tab !== "replay";
  document.querySelectorAll("[data-client-tab]").forEach((button) => {
    const active = button.dataset.clientTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const title = tab === "replay" ? "数据回放" : "采集工作台";
  $("page-title").textContent = $("current-page").textContent = title;
  document.title = title + " · BodyPipeline";
  $("page-eyebrow").textContent = tab === "replay" ? "ROBOT REPLAY" : "TELEOPERATION COLLECTION";
  $("page-description").textContent = tab === "replay"
    ? "选择本机 Episode，查看轨迹信息、执行进度和日志。"
    : "在机器人本地录制，确认保留后可靠上传。";
  $("notice").hidden = true;
  clearTimeout(frameTimer); frameController?.abort();
  if (tab === "replay") void window.ClientReplay.activate(api, config.robot_id);
  else {
    window.ClientReplay.deactivate();
    $("mode-badge").textContent = config.mode === "mock" ? "MOCK · 模拟模式" : "ROS2 · 遥操作";
    void refreshFrames();
  }
  const wasOpen = $("app-view").classList.contains("sidebar-open");
  setSidebarOpen(false, wasOpen);
}
function setServerTab(tab, updateHistory = true) {
  if (!["uploads", "convert", "results", "merge", "annotations"].includes(tab)) tab = "convert";
  serverTab = tab;
  if (location.hash === "#replay") history.replaceState(null, "", "#" + tab);
  if (updateHistory && location.hash !== "#" + tab) location.hash = tab;
  document.querySelectorAll("[data-server-panel]").forEach((el) => {
    el.hidden = el.dataset.serverPanel !== tab;
  });
  document.querySelectorAll("[data-server-tab]").forEach((el) => {
    const active = el.dataset.serverTab === tab;
    el.setAttribute("aria-pressed", String(active));
    el.classList.toggle("active", active);
    if (active) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  const title = { uploads: "原始数据", convert: "数据转换", results: "预览与质检", merge: "数据集管理", annotations: "自动标注" }[tab];
  $("page-title").textContent = $("current-page").textContent = title;
  $("page-description").textContent = {
    uploads: "按任务组织原始采集，同一数据集持续接收新的 episodes。",
    convert: "填写 instruction，批量转换所选 episodes 为一个 LeRobot 数据集。",
    results: "浏览转换结果、预览多路视频，并查看数据质量报告。",
    merge: "按机器人管理原始数据与转换结果，或合并生成新的 LeRobot 数据集。",
    annotations: "调用 Argus review 为已登记的数据集生成逐 Episode 标注，并跟踪费用与结果。",
  }[tab];
  document.title = title + " · BodyPipeline";
  $("job-detail-panel").hidden = !selectedJob || tab !== "results";
  $("demo-warning").hidden = config?.processing_mode !== "demo" || ["merge", "annotations"].includes(tab);
  $("mode-badge").textContent = config?.processing_mode === "demo" ? "DEMO · 演示处理" : "SERVER · 中心服务";
  const wasOpen = $("app-view").classList.contains("sidebar-open");
  setSidebarOpen(false, wasOpen);
  if (tab === "annotations") window.AnnotationUI?.show();
  else window.AnnotationUI?.hide();
  void refreshDatasetManagement();
}
function selectJob(id) {
  if (id !== selectedJob) {
    selectedCheck = null;
    logText = "选择日志查看执行诊断。";
  }
  selectedJob = id;
  detailSignature = null;
}
const unknownRobotType = "__unknown__";
function rowRobotType(row) {
  return row.robot_type || row.profile?.robot_type || serverUploads.find((u) => u.id === row.upload_id)?.robot_type || unknownRobotType;
}
function robotTypeLabel(type) {
  return type === unknownRobotType ? "未标注类型（历史数据）" : type === "agilex_aloha" ? "松灵 AgileX ALOHA · agilex_aloha" : type;
}
function replaceOptions(element, options, selected) {
  if (element.dataset.options !== options) {
    element.dataset.options = options;
    element.innerHTML = options;
    element.value = selected;
  }
}
$("server-robot-type").onchange = () => {
  selectedMergeDatasets.clear();
  activeVersionConversionSource = null;
  $("conversion-upload").value = "";
  $("version-conversion-source").value = "";
  $("version-conversion-name").value = "";
  selectedJob = null;
  selectedCheck = null;
  detailSignature = null;
  $("job-detail-panel").hidden = true;
  $("notice").hidden = true;
  renderServer(serverUploads, serverJobs);
};
function conversionCorrectionSummary(job) {
  const options = job.conversion_options?.gripper_closure;
  if (!options) return "";
  const reports = job.alignment_report?.episodes || (job.alignment_report ? [job.alignment_report] : []);
  const corrections = reports.map((r) => r.action_corrections?.gripper_closure).filter(Boolean);
  const changed = corrections.reduce((count, r) => count + r.changed_frames_any_channel, 0);
  return `<p class="small">夹持阶段最小闭合度：<strong>${esc(options.minimum_percent)}%</strong>（名义行程）${corrections.length ? ` · 已调整 ${esc(changed)} 帧 · ${esc(corrections.length)} 个 episodes 已检查` : " · 等待处理"}</p>`;
}
function chosenCollection() {
  return serverCollections.find((c) => c.id === $("conversion-upload").value && rowRobotType(c) === $("server-robot-type").value);
}
let conversionOptionsRuntime = null;
function selectedConversionRuntime() {
  const collection = chosenCollection();
  const runtimes = (config.runtimes || []).filter((r) => r.robot_type === $("server-robot-type").value);
  if (collection) {
    const ids = [...new Set((collection.uploads || []).map((u) => u.runtime_id))];
    return ids.length === 1 ? runtimes.find((r) => r.id === ids[0]) : null;
  }
  return runtimes.length === 1 ? runtimes[0] : null;
}
function updateConversionOptions() {
  const runtime = selectedConversionRuntime();
  const capability = runtime?.conversion_capabilities?.gripper_closure;
  const identity = runtime ? `${$("server-robot-type").value}:${runtime.id}` : null;
  if (conversionOptionsRuntime !== identity) {
    conversionOptionsRuntime = identity;
    $("conversion-closure-enabled").checked = false;
    $("conversion-closure-percent").value = capability?.default_percent ?? "";
  }
  $("conversion-closure").hidden = !capability;
  if (!capability) {
    $("conversion-closure-enabled").checked = false;
    $("conversion-closure-percent").value = "";
  }
  const enabled = Boolean(capability && $("conversion-closure-enabled").checked);
  $("conversion-closure-enabled").disabled = busy || !capability;
  $("conversion-closure-fields").hidden = !enabled;
  $("conversion-closure-percent").disabled = busy || !enabled;
  $("conversion-closure-percent").required = enabled;
  if (capability) {
    $("conversion-closure-percent").min = capability.minimum_percent;
    $("conversion-closure-percent").max = capability.maximum_percent;
  }
  return !enabled || $("conversion-closure-percent").checkValidity();
}
function selectedConversionOptions() {
  const supported = selectedConversionRuntime()?.conversion_capabilities?.gripper_closure;
  return supported && $("conversion-closure-enabled").checked
    ? { gripper_closure: { minimum_percent: Number($("conversion-closure-percent").value) } }
    : {};
}
function updateConversionSelection() {
  const type = $("server-robot-type").value, c = chosenCollection();
  if (activeCollection !== (c?.id || null)) {
    activeCollection = c?.id || null;
    selectedEpisodes = new Set((c?.uploads || []).filter((u) => u.state === "COMMITTED").map((u) => u.id));
    $("conversion-instruction").value = "";
  }
  const ready = (c?.uploads || []).filter((u) => u.state === "COMMITTED");
  const optionsValid = updateConversionOptions();
  selectedEpisodes = new Set([...selectedEpisodes].filter((id) => ready.some((u) => u.id === id)));
  $("server-robot-type").disabled = busy;
  $("conversion-upload").disabled = busy || !type;
  $("conversion-invalid-policy").disabled = busy;
  $("conversion-source").innerHTML = c
    ? `<strong>${esc(c.dataset_name)}</strong><br>${c.ready_count} / ${c.episode_count} 个 episode 已接收 · ${bytes(c.total_bytes)}<br><span class="path-hint">数据集 ${esc(c.id)} · ${esc(collectionLocation(c))}</span>`
    : "请选择原始数据集；同一任务的多次采集将合并转换。";
  $("episode-picker").hidden = !c;
  const selected = ready.filter((u) => selectedEpisodes.has(u.id));
  const rows = (c?.uploads || []).map((u, index) => `<tr><td><input aria-label="选择采集 ${esc(u.id)}" data-episode="${esc(u.id)}" type="checkbox" ${selectedEpisodes.has(u.id) ? "checked" : ""} ${u.state !== "COMMITTED" || busy ? "disabled" : ""} /></td><td><strong>${index + 1} · ${esc(u.id.slice(0, 12))}</strong><span class="sub">${esc(u.robot_id)} · ${new Date(u.created_at * 1000).toLocaleString("zh-CN")}</span>${u.conversion_blockers?.length ? '<span class="sub job-error">转换配置待核实</span>' : ""}</td><td>${bytes(u.total_bytes)}<span class="sub">${u.file_count} 个文件</span></td><td>${badge(u.state)}</td></tr>`).join("");
  if ($("episode-list").dataset.content !== rows) {
    const focused = document.activeElement?.dataset.episode;
    $("episode-list").innerHTML = rows;
    $("episode-list").dataset.content = rows;
    if (focused) [...$("episode-list").querySelectorAll("[data-episode]")].find((el) => el.dataset.episode === focused)?.focus();
  }
  $("episode-all").checked = ready.length > 0 && selected.length === ready.length;
  $("episode-all").indeterminate = selected.length > 0 && selected.length < ready.length;
  $("episode-all").disabled = busy || !ready.length;
  $("episode-selection-count").textContent = `已选 ${selected.length} / ${ready.length} 段可转换采集`;
  const problems = [...new Set(selected.flatMap((u) => (u.conversion_blockers || []).map((b) => b.message)))];
  if (config.processing_mode === "real" && !config.letools_available) problems.push("服务器尚未配置可执行的 letools。");
  $("conversion-warning").hidden = !problems.length;
  $("conversion-warning").innerHTML = problems.length ? `<strong>转换前需要完成以下配置：</strong><ul>${problems.map((message) => `<li>${esc(message)}</li>`).join("")}</ul>` : "";
  const instruction = $("conversion-instruction").value.trim();
  $("conversion-submit").disabled = busy || !c || !selected.length || selected.length > 1000 || !instruction || !$("conversion-format").value || !optionsValid || problems.length > 0;
  $("conversion-submit").textContent = config.processing_mode === "demo" ? "提交批量演示" : `批量转换 ${selected.length} 个 episodes`;
  $("conversion-help").textContent = problems.length ? "所选采集的配置检查未通过。" : !instruction ? "请填写训练 instruction。" : "本次选择保存为固定快照；后续上传不会改变正在执行的任务。";
}
function chosenVersionConversionSource() {
  return serverVersionConversionCandidates.find(
    (source) =>
      source.source_job_id === $("version-conversion-source").value &&
      rowRobotType(source) === $("server-robot-type").value,
  );
}
function updateVersionConversionSelection() {
  const source = chosenVersionConversionSource();
  if (activeVersionConversionSource !== (source?.source_job_id || null)) {
    activeVersionConversionSource = source?.source_job_id || null;
    $("version-conversion-name").value = source
      ? `${source.dataset_name.slice(0, 1990)} · ${source.target_format}`
      : "";
  }
  $("version-conversion-source").disabled = busy || !$("server-robot-type").value;
  $("version-conversion-target").innerHTML = source
    ? `<option value="${esc(source.target_format)}">LeRobot ${esc(source.source_format)} → ${esc(source.target_format)}</option>`
    : '<option value="">选择来源后自动确定</option>';
  $("version-conversion-source-summary").innerHTML = source
    ? `<strong>${esc(source.dataset_name)}</strong><br>LeRobot ${esc(source.source_format)} · ${esc(source.episode_count)} episodes · ${esc(source.frame_count)} 帧 · ${esc(source.fps)} FPS<br><span class="path-hint">${esc(source.dataset_path || source.reason || "")}</span>`
    : "选择已转换、已导入或已合并的 LeRobot v2.1 / v3.0 数据集。";
  const problems = [];
  if (source && !source.selectable) problems.push(source.reason || "来源数据集当前不可用。");
  if (!config?.letools_available) problems.push("服务器尚未配置可执行的 LeTools 环境。");
  $("version-conversion-warning").hidden = !problems.length;
  $("version-conversion-warning").textContent = problems.join("；");
  const name = $("version-conversion-name").value.trim();
  $("version-conversion-submit").disabled = busy || !source || !source.selectable || !source.target_format || !name || problems.length > 0;
  $("version-conversion-help").textContent = problems.length
    ? "请先处理来源或工具环境问题。"
    : !source
      ? "请选择一个可用的 LeRobot 数据集。"
      : !name
        ? "请填写新数据集名称。"
        : `将 ${source.source_format} 转换为 ${source.target_format}，来源数据集保持不变。`;
}
$("import-form").onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    $("import-submit").disabled = true;
    try {
      const row = await api("/api/imports", "POST", {
        path: $("import-path").value.trim(),
        robot_type: $("import-robot-type").value.trim() || null,
      });
      $("server-robot-type").value = row.robot_type;
      $("import-path").value = "";
      $("import-robot-type").value = "";
      await refresh();
      if (row.kind === "raw") {
        setServerTab("convert");
        notice(`已导入原始数据集「${row.dataset_name}」：${row.file_count} 个文件，${bytes(row.total_bytes)}。请选择该数据集并提交批量转换。`);
      } else if (row.kind === "hdf5") {
        setServerTab("convert");
        notice(`已导入 HDF5 数据集「${row.dataset_name}」：${row.episode_count} 个 episodes，${bytes(row.total_bytes)}。请选择该数据集并提交批量转换。`);
      } else {
        setServerTab("results");
        notice(`已导入 LeRobot ${row.target_format} 数据集「${row.dataset_name}」：${row.episode_count} 个 episodes。现在可以直接预览或运行 Doctor。`);
      }
    } finally {
      $("import-submit").disabled = !(config.import_roots || []).length;
    }
  });
};
$("conversion-upload").onchange = updateConversionSelection;
$("conversion-format").onchange = updateConversionSelection;
$("conversion-instruction").oninput = updateConversionSelection;
$("conversion-closure-enabled").onchange = updateConversionSelection;
$("conversion-closure-percent").oninput = updateConversionSelection;
$("episode-all").onchange = () => {
  selectedEpisodes = new Set($("episode-all").checked ? chosenCollection().uploads.filter((u) => u.state === "COMMITTED").map((u) => u.id) : []);
  updateConversionSelection();
};
$("episode-list").onchange = (e) => {
  const id = e.target.dataset.episode;
  if (id) { if (e.target.checked) selectedEpisodes.add(id); else selectedEpisodes.delete(id); updateConversionSelection(); }
};
$("conversion-form").onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    $("conversion-submit").disabled = true;
    const row = await api(`/api/collections/${$("conversion-upload").value}/process`, "POST", {
      target_format: $("conversion-format").value, run_doctor: $("conversion-doctor").checked,
      instruction: $("conversion-instruction").value.trim(), upload_ids: [...selectedEpisodes],
      conversion_options: selectedConversionOptions(),
      invalid_episode_policy: $("conversion-invalid-policy").value,
    });
    notice(`数据集 ${row.dataset_name} · ${row.source_count} 个 episodes · ${row.target_format} · ${names[row.state] || row.state}。`);
  });
};
$("version-conversion-source").onchange = updateVersionConversionSelection;
$("version-conversion-name").oninput = updateVersionConversionSelection;
$("version-conversion-doctor").onchange = updateVersionConversionSelection;
$("version-conversion-form").onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    const source = chosenVersionConversionSource();
    if (!source) return;
    $("version-conversion-submit").disabled = true;
    const row = await api("/api/version-conversions", "POST", {
      source_job_id: source.source_job_id,
      target_format: $("version-conversion-target").value,
      name: $("version-conversion-name").value.trim(),
      run_doctor: $("version-conversion-doctor").checked,
    });
    notice(`已提交「${row.dataset_name}」：LeRobot ${row.source_format} → ${row.target_format}，${row.episode_count} 个 episodes。`);
  });
};
function renderServer(uploads, jobs) {
  const runtimeTypes = (config.runtimes || []).map((r) => r.robot_type).filter(Boolean);
  const types = [...new Set([...runtimeTypes, ...uploads.map(rowRobotType), ...jobs.map(rowRobotType), ...serverDatasets.map(rowRobotType), ...serverDeletions.map(rowRobotType)])].sort();
  const oldType = $("server-robot-type").value;
  const type = types.includes(oldType) ? oldType : "";
  if (oldType && !type) {
    selectedJob = null;
    detailSignature = null;
    $("job-detail-panel").hidden = true;
  }
  replaceOptions($("server-robot-type"), '<option value="">请选择机器人类型</option>' + types.map((t) => `<option value="${esc(t)}">${esc(robotTypeLabel(t))}</option>`).join(""), type);
  const robotTypes = [...new Set([config.profile?.robot_type, ...runtimeTypes, ...types].filter(Boolean))];
  replaceOptions($("import-robot-types"), robotTypes.map((t) => `<option value="${esc(t)}">${esc(robotTypeLabel(t))}</option>`).join(""), $("import-robot-type").value);
  $("robot-scope-hint").textContent = type ? `当前范围：${robotTypeLabel(type)}。下方数据、任务、结果仅显示此类型；同类型多台设备通过设备 ID 区分。` : "选择后，再选择该类型下的数据集。原始数据、转换任务、合并任务和预览结果会一起筛选。";
  uploads = uploads.filter((u) => rowRobotType(u) === type);
  jobs = jobs.filter((j) => rowRobotType(j) === type);
  $("metric-uploads").textContent = uploads.filter((x) => x.state === "COMMITTED").length;
  const conversions = jobs.filter((j) => j.operation !== "merge");
  $("metric-jobs").textContent = conversions.length;
  $("nav-upload-count").textContent = uploads.length;
  $("nav-job-count").textContent = conversions.length;
  $("nav-merge-count").textContent = serverDatasets.filter((d) => rowRobotType(d) === type).length;
  $("nav-result-count").textContent = jobs.filter((j) => j.dataset_available).length;
  $("nav-annotation-count").textContent = serverAnnotations.filter((a) => ["QUEUED", "RUNNING"].includes(a.state)).length;
  $("metric-completed").textContent = jobs.filter((x) => x.dataset_available).length;
  const collections = serverCollections.filter((c) => rowRobotType(c) === type);
  $("nav-upload-count").textContent = collections.length;
  const selected = $("conversion-upload").value;
  const options = '<option value="">请选择数据集</option>' + collections.map((c) => `<option value="${esc(c.id)}">${c.capture_mode === "mock" ? "[模拟] " : ""}${esc(c.dataset_name)} · ${c.ready_count}/${c.episode_count} episodes · ${esc(c.target)}/${esc(c.prefix)} · ${esc(c.id.slice(0, 6))}</option>`).join("");
  replaceOptions($("conversion-upload"), options, collections.some((c) => c.id === selected) ? selected : "");
  updateConversionSelection();
  const versionSources = serverVersionConversionCandidates.filter((source) => rowRobotType(source) === type);
  const selectedVersionSource = $("version-conversion-source").value;
  const versionOptions = '<option value="">请选择已有 LeRobot 数据集</option>' + versionSources.map(
    (source) => `<option value="${esc(source.source_job_id)}">${source.selectable ? "" : "[不可用] "}${esc(source.dataset_name)} · LeRobot ${esc(source.source_format || "?")} → ${esc(source.target_format || "?")} · ${esc(source.episode_count ?? "?")} episodes</option>`,
  ).join("");
  replaceOptions(
    $("version-conversion-source"),
    versionOptions,
    versionSources.some((source) => source.source_job_id === selectedVersionSource)
      ? selectedVersionSource
      : "",
  );
  updateVersionConversionSelection();
  $("upload-list").innerHTML = collections.length ? collections.map((c) =>
    `<tr><td><strong class="task-name">${esc(c.dataset_name)}</strong><span class="sub">${esc(c.id.slice(0, 12))} · ${esc(collectionLocation(c))}</span></td><td>${esc([...new Set(c.uploads.map((u) => u.robot_id))].join(", "))} ${c.capture_mode === "mock" ? badge("MOCK") : c.target === "imported" ? badge("IMPORTED") : ""}</td><td>${bytes(c.total_bytes)}<span class="sub">${c.episode_count} 个 episodes</span></td><td>${c.ready_count} / ${c.episode_count}</td><td><button class="secondary" data-process="${esc(c.id)}">查看 / 批量转换</button></td></tr>`
  ).join("") : '<tr><td colspan="5" class="empty">请选择机器人类型；该类型的数据集将在此显示。</td></tr>';
  $("job-list").innerHTML = conversions.length ? conversions.map((j) =>
    `<tr><td><strong class="task-name">${esc(j.dataset_name || j.instruction || j.id.slice(0, 12))}</strong><span class="sub">${esc(j.id.slice(0, 12))} · ${esc(jobOriginSummary(j))}</span>${j.operation === "version_conversion" ? `<span class="sub">来源：${esc(j.source_dataset_name)} · ${esc(j.source_format)} → ${esc(j.target_format)}</span>` : ""}${j.operation === "subtask_export" ? `<span class="sub">Subtask：${esc(j.source_dataset_name)} · 标注 ${esc(String(j.source_annotation_id || j.annotation_id || "").slice(0, 12))} · 覆盖 ${esc(Math.round(100 * (j.subtask_summary?.ratio ?? 0)))}%</span>` : ""}</td><td>LeRobot ${esc(j.target_format)}<span class="sub">${esc(j.operation === "subtask_export" ? "复制并写入 subtask_index + 深度验证" : j.run_doctor === false ? "仅转换 + 深度验证" : "转换 + Doctor")}</span></td><td>${esc(jobStageText(j))}${j.error ? `<span class="sub job-error">${esc(j.error)}</span>` : ""}</td><td>${jobBadge(j)} ${sourceBadge(j)}</td><td><button class="secondary" data-detail="${esc(j.id)}">详情与结果</button></td></tr>`
  ).join("") : '<tr><td colspan="5" class="empty">请选择机器人类型，查看该类型的转换任务。</td></tr>';
  const available = jobs.filter((j) => j.dataset_available);
  $("result-list").innerHTML = available.length ? available.map((j) =>
    `<tr><td><strong class="task-name">${esc(j.dataset_name || j.instruction || j.id.slice(0, 12))}</strong><span class="sub">${esc(j.id.slice(0, 12))} · ${esc(episodeSelectionSummary(j))}</span><span class="sub">${j.operation === "version_conversion" ? `版本转换：${esc(j.source_format)} → ${esc(j.target_format)}` : esc(j.instruction)}</span></td><td>LeRobot ${esc(j.target_format)}</td><td>${jobBadge(j)} ${sourceBadge(j)}</td><td>${datasetLocation(j)}</td><td><div class="action-row"><button class="primary" data-viewer="${esc(j.id)}">预览数据</button><button class="secondary" data-viewer="${esc(j.id)}" data-viewer-mode="batch">批量预览</button><button class="secondary" data-doctor="${esc(j.id)}">运行质检</button><button class="secondary" data-report-page="${esc(j.id)}">查看报告</button></div></td></tr>`
  ).join("") : '<tr><td colspan="5" class="empty">请选择机器人类型，查看该类型已转换的数据集。受阻任务的原因可在“数据转换”查看。</td></tr>';
  updateMergeSelection();
  renderMergeJobs(jobs.filter((j) => j.operation === "merge"));
  renderDatasetManagement();
}
function renderDatasetManagement() {
  const type = $("server-robot-type").value, kind = $("dataset-kind").value;
  const search = $("dataset-search").value.trim().toLocaleLowerCase();
  const rows = serverDatasets.filter((d) => rowRobotType(d) === type && (!kind || d.kind === kind) && (!search || `${d.dataset_name} ${d.id}`.toLocaleLowerCase().includes(search)));
  const paths = (d) => !d.paths.length ? '<span class="small muted">范围见删除记录</span>' : `<code>${esc(d.paths[0])}</code>${d.paths.length > 1 ? `<details><summary>共 ${d.paths.length} 个路径</summary>${d.paths.slice(1).map((p) => `<div><code>${esc(p)}</code></div>`).join("")}</details>` : ""}`;
  const html = rows.length ? rows.map((d) => `<tr><td><strong class="task-name">${esc(d.dataset_name)}</strong><span class="sub">${d.kind === "raw" ? "原始数据" : "LeRobot " + esc(d.target_format || "数据集")} · ${esc(d.id.slice(0, 12))}</span></td><td>${d.episode_count ?? "—"} episodes<span class="sub">${d.total_bytes == null ? "" : bytes(d.total_bytes)}</span></td><td class="dataset-paths">${paths(d)}</td><td>${badge(d.state)}${d.reason ? `<span class="sub job-error">${esc(d.reason)}</span>` : ""}</td><td><button class="danger" data-delete-dataset="${esc(d.id)}" data-delete-kind="${d.kind}" ${busy || !d.deletable ? "disabled" : ""}>删除${d.kind === "raw" ? "原始数据" : "数据集"}</button></td></tr>`).join("") : '<tr><td colspan="5" class="empty">请选择机器人类型；可管理的原始数据和 LeRobot 数据集将在此显示。</td></tr>';
  const list = $("dataset-list");
  if (list.dataset.content !== html) {
    const focused = document.activeElement?.dataset.deleteDataset;
    list.innerHTML = html;
    list.dataset.content = html;
    if (focused) [...list.querySelectorAll("[data-delete-dataset]")].find((el) => el.dataset.deleteDataset === focused)?.focus();
  }
  const operations = serverDeletions.filter((d) => rowRobotType(d) === type).slice(0, 20);
  $("dataset-deletion-panel").hidden = !operations.length;
  $("dataset-deletion-list").innerHTML = operations.map((d) => `<tr><td><strong class="task-name">${esc(d.dataset_name)}</strong><span class="sub">${new Date(d.created_at * 1000).toLocaleString("zh-CN")}</span></td><td>${d.kind === "raw" ? "原始数据" : "LeRobot 数据集"}</td><td>${badge(d.state === "COMPLETED" ? "DELETED" : d.state === "FAILED" ? "DELETE_FAILED" : "DELETING")}</td><td>${d.state === "FAILED" ? `<span class="sub job-error">${esc(d.error)}</span><button class="secondary" data-retry-deletion="${esc(d.id)}" ${busy ? "disabled" : ""}>重试删除</button>` : d.state === "COMPLETED" ? "文件已删除，登记已更新" : "正在后台永久删除文件，请稍候"}</td></tr>`).join("");
}
$("dataset-kind").onchange = renderDatasetManagement;
$("dataset-search").oninput = renderDatasetManagement;
$("dataset-list").onclick = (event) => {
  const button = event.target.closest("[data-delete-dataset]");
  if (!button || busy) return;
  const dataset = serverDatasets.find((d) => d.id === button.dataset.deleteDataset && d.kind === button.dataset.deleteKind);
  if (!dataset?.deletable) return;
  deletingDataset = dataset;
  $("dataset-delete-description").textContent = `${robotTypeLabel(dataset.robot_type)} · ${dataset.kind === "raw" ? "原始数据" : "LeRobot 数据集"} · ${dataset.dataset_name}。${dataset.kind === "raw" ? "已生成的转换结果会保留。导入的原始数据仅删除登记清单中的文件。" : "原始数据及已生成的独立合并结果会保留。"}`;
  $("dataset-delete-paths").textContent = dataset.paths.join("\n");
  $("dataset-delete-confirmation").value = "";
  $("dataset-delete-confirmation").placeholder = dataset.dataset_name;
  $("dataset-delete-submit").disabled = true;
  $("dataset-delete-dialog").showModal();
  $("dataset-delete-confirmation").focus();
};
$("dataset-delete-confirmation").oninput = () => {
  $("dataset-delete-submit").disabled = busy || $("dataset-delete-confirmation").value !== deletingDataset?.dataset_name;
};
$("dataset-delete-close").onclick = $("dataset-delete-cancel").onclick = () => $("dataset-delete-dialog").close();
$("dataset-delete-form").onsubmit = (event) => {
  event.preventDefault();
  if (!deletingDataset || $("dataset-delete-confirmation").value !== deletingDataset.dataset_name) return;
  action(async () => {
    $("dataset-delete-submit").disabled = true;
    try {
      await api("/api/dataset-deletions", "POST", { kind: deletingDataset.kind, id: deletingDataset.id, confirmation: $("dataset-delete-confirmation").value });
      if (deletingDataset.kind === "result" && selectedJob === deletingDataset.id) {
        selectedJob = null;
        $("job-detail-panel").hidden = true;
      }
      $("dataset-delete-dialog").close();
      notice(`已提交「${deletingDataset.dataset_name}」的永久删除任务，可在删除记录中查看进度。`);
    } finally { $("dataset-delete-submit").disabled = false; }
  });
};
$("dataset-deletion-list").onclick = (event) => {
  const id = event.target.closest("[data-retry-deletion]")?.dataset.retryDeletion;
  if (id) action(async () => {
    await api(`/api/dataset-deletions/${encodeURIComponent(id)}/retry`, "POST");
    notice("已重新提交删除任务。");
  });
};
function updateMergeSelection() {
  const type = $("server-robot-type").value;
  const sources = serverMergeCandidates.filter((s) => rowRobotType(s) === type);
  selectedMergeDatasets = new Set([...selectedMergeDatasets].filter((id) => sources.some((s) => s.id === id)));
  const selected = [...selectedMergeDatasets].map((id) => sources.find((s) => s.id === id));
  const first = selected[0];
  const reason = (s) => !s.selectable ? s.reason : first && s.compatibility_key !== first.compatibility_key ? "与已选来源不兼容（版本、帧率、特征结构或模拟标识不同）" : "";
  const rows = sources.length ? sources.map((s) => {
    const checked = selectedMergeDatasets.has(s.id), issue = reason(s);
    const order = [...selectedMergeDatasets].indexOf(s.id) + 1;
    return `<tr><td><label class="check-label"><input type="checkbox" data-merge-source="${esc(s.id)}" aria-label="选择数据集 ${esc(s.dataset_name)}" ${checked ? "checked" : ""} ${busy || (issue && !checked) ? "disabled" : ""} />${checked ? `第 ${order} 个` : ""}</label></td><td><strong class="task-name">${esc(s.dataset_name)}</strong><span class="sub">${esc(s.id.slice(0, 12))}${s.synthetic ? " · 模拟数据" : ""}</span></td><td>${esc(s.target_format || "—")}<span class="sub">${s.fps ? esc(s.fps) + " FPS" : "—"}</span></td><td>${s.episode_count ?? "—"} episodes<span class="sub">${s.frame_count ?? "—"} 帧</span></td><td>${jobBadge(s)}${issue ? `<span class="sub job-error">${esc(issue)}</span>` : '<span class="sub muted">可合并</span>'}</td></tr>`;
  }).join("") : '<tr><td colspan="5" class="empty">请选择机器人类型；已转换或已导入的 LeRobot 数据集将在此显示。</td></tr>';
  const list = $("merge-source-list");
  if (list.dataset.content !== rows) {
    const focused = document.activeElement?.dataset.mergeSource;
    list.innerHTML = rows;
    list.dataset.content = rows;
    if (focused) [...list.querySelectorAll("[data-merge-source]")].find((el) => el.dataset.mergeSource === focused)?.focus();
  }
  $("merge-name").disabled = busy;
  $("merge-clear").disabled = busy || !selected.length;
  $("merge-summary").textContent = `已选 ${selected.length} 个数据集 · 共 ${selected.reduce((n, s) => n + (s.episode_count || 0), 0)} 个 episodes · ${selected.reduce((n, s) => n + (s.frame_count || 0), 0)} 帧${first ? " · LeRobot " + first.target_format : ""}`;
  const problems = [...new Set(selected.map(reason).filter(Boolean))];
  if (!config?.letools_available) problems.push("服务器尚未配置 LeTools 合并环境。");
  if (selected.length > 100) problems.push("一次最多合并 100 个数据集。");
  $("merge-warning").hidden = !problems.length;
  $("merge-warning").textContent = problems.join("；");
  $("merge-submit").disabled = busy || selected.length < 2 || !$("merge-name").value.trim() || problems.length > 0;
  $("merge-help").textContent = selected.length < 2 ? "请选择至少两个兼容的数据集。" : !$("merge-name").value.trim() ? "请填写新数据集名称。" : problems.length ? "请先处理来源兼容性问题。" : "合并在后台执行，完成后显示新数据集。";
}
$("merge-source-list").onchange = (event) => {
  const id = event.target.dataset.mergeSource;
  if (!id) return;
  if (event.target.checked) selectedMergeDatasets.add(id); else selectedMergeDatasets.delete(id);
  updateMergeSelection();
};
$("merge-clear").onclick = () => { selectedMergeDatasets.clear(); updateMergeSelection(); };
$("merge-name").oninput = updateMergeSelection;
$("merge-form").onsubmit = (event) => {
  event.preventDefault();
  action(async () => {
    const name = $("merge-name").value.trim(), ids = [...selectedMergeDatasets];
    updateMergeSelection();
    const job = await api("/api/merges", "POST", { name, source_job_ids: ids });
    selectedMergeDatasets.clear();
    $("merge-name").value = "";
    notice(`已提交合并任务「${job.dataset_name}」：${job.merge_source_count} 个数据集、${job.source_count} 个 episodes。`);
  });
};
function renderMergeJobs(jobs) {
  $("merge-job-list").innerHTML = jobs.length ? jobs.map((j) => `<tr><td><strong class="task-name">${esc(j.dataset_name)}</strong><span class="sub">${esc(j.id.slice(0, 12))} · LeRobot ${esc(j.target_format)}</span></td><td>${j.merge_source_count} 个数据集<span class="sub">${j.source_count} episodes · ${j.frame_count} 帧</span></td><td>${esc(j.stage)}${j.error ? `<span class="sub job-error">${esc(j.error)}</span>` : ""}</td><td>${jobBadge(j)} ${sourceBadge(j)}</td><td><div class="action-row">${j.dataset_available ? `<button class="primary" data-viewer="${esc(j.id)}">预览数据</button><button class="secondary" data-viewer="${esc(j.id)}" data-viewer-mode="batch">批量预览</button><button class="secondary" data-detail="${esc(j.id)}">查看结果</button>` : ["FAILED", "INTERRUPTED"].includes(j.state) ? `<button class="secondary" data-merge-retry="${esc(j.id)}">重新选择 / 重试</button><button class="secondary" data-detail="${esc(j.id)}">查看详情</button>` : '<span class="small muted">后台处理中</span>'}</div></td></tr>`).join("") : '<tr><td colspan="5" class="empty">该机器人类型暂无合并任务。提交后可在此查看进度和结果。</td></tr>';
}
$("merge-job-list").addEventListener("click", (event) => {
  const id = event.target.closest("[data-merge-retry]")?.dataset.mergeRetry;
  const job = serverJobs.find((j) => j.id === id);
  if (!job || busy) return;
  selectedMergeDatasets = new Set(job.source_job_ids);
  $("merge-name").value = job.dataset_name;
  updateMergeSelection();
  $("merge-form").scrollIntoView({ behavior: "smooth", block: "start" });
  notice("已填入原合并配置，请检查来源后重新提交；新任务会使用新的输出目录。");
});
function jobOriginSummary(job) {
  // A subtask export copies a whole dataset, so input/accepted/skipped counts
  // would describe a conversion that never ran.
  if (job.operation === "subtask_export") {
    return `${job.episode_count ?? job.source_episode_count ?? 1} 个 episodes · 来自标注`;
  }
  return episodeSelectionSummary(job);
}
function episodeSelectionSummary(job) {
  const selection = job.selection_report;
  if (!selection) return `${job.episode_count ?? job.source_count ?? 1} 个 episodes`;
  return `输入 ${selection.input_count} · 通过 ${selection.accepted_count} · 跳过 ${selection.skipped_count}${selection.failed_count ? ` · 失败 ${selection.failed_count}` : ""}${job.dataset_available ? ` · 输出 ${job.episode_count} 个 episodes` : ""}`;
}
function renderEpisodeSelection(job) {
  if (!job.selection_report) return "";
  const rejected = (job.episode_diagnostics || []).filter((row) => !row.passed);
  const policy = job.invalid_episode_policy === "skip" ? "跳过不合格采集" : "全部通过才转换";
  return `<div class="${rejected.length ? "warning" : "source-summary"}"><strong>${esc(policy)}</strong><p>${esc(episodeSelectionSummary(job))}</p>${rejected.length ? `<details><summary>查看未通过的采集与原因（${rejected.length} 条）</summary><ul>${rejected.map((row) => `<li>输入 episode ${esc(row.episode_index)} · <code>${esc(row.upload_id)}</code> · ${row.skippable && job.invalid_episode_policy === "skip" ? "已跳过" : "任务失败"}：${esc(row.error)}</li>`).join("")}</ul></details>` : ""}</div>`;
}
function datasetLocation(job) {
  if (!job.dataset_available) return '<span class="small muted">尚无可用转换结果</span>';
  return `<div class="dataset-location"><span class="small muted">服务器文件系统路径</span><code>${esc(job.dataset_path)}</code><button type="button" class="secondary" data-copy-path="${esc(job.dataset_path)}">复制路径</button></div>`;
}
function doctorReport(report, gate) {
  if (!report) return '<p class="small muted">尚无 Doctor 报告。完成转换后可运行质检。</p>';
  return `<div class="report-summary">${badge(report.overall_severity)}<span>${esc(report.total_episodes)} 个 episode · ${esc(report.total_frames)} 帧</span></div>
    <div class="table-scroll"><table class="doctor-table"><thead><tr><th>检查项</th><th>结论</th><th>说明</th></tr></thead><tbody>${(report.checks || []).map((c) =>
      `<tr><td>${esc(c.name)}</td><td>${badge(c.severity)}</td><td>${(c.messages || []).map((m) => `<div>${esc(m.message)}</div>`).join("") || "—"}</td></tr>`
    ).join("")}</tbody></table></div>
    ${gate?.skipped?.length ? `<div class="warning small">未覆盖项：${esc(gate.skipped.map((s) => s.check + "：" + s.reasons.join("；")).join("；"))}</div>` : ""}
    <details class="report-json"><summary>原始 Doctor 报告</summary><pre>${esc(JSON.stringify(report, null, 2))}</pre></details>`;
}
async function showJob() {
  const id = selectedJob;
  if (!id) return;
  const [j, checks] = await Promise.all([api("/api/jobs/" + id), api(`/api/jobs/${id}/checks`)]);
  if (selectedJob !== id) return;
  $("job-detail-panel").hidden = serverTab !== "results";
  const signature = JSON.stringify([j, checks, selectedCheck]);
  if (signature === detailSignature) return;
  detailSignature = signature;
  $("job-detail-title").textContent = "任务 " + j.id.slice(0, 12) + " · LeRobot " + j.target_format;
  const latest = selectedCheck === "automatic" ? null : checks.find((c) => c.id === selectedCheck) || checks[0];
  const report = latest ? latest.doctor_report : j.doctor_report;
  const gate = latest ? latest.quality_gate : j.quality_gate;
  $("job-detail").innerHTML = `
    <p class="small"><strong>${esc(j.dataset_name || "历史数据集")}</strong> · ${esc(episodeSelectionSummary(j))}</p>
    <p class="small">Instruction：${esc(j.instruction || "—")}</p>
    ${conversionCorrectionSummary(j)}
    ${renderEpisodeSelection(j)}
    ${j.operation === "version_conversion" ? `<p class="small">来源：<strong>${esc(j.source_dataset_name)}</strong>（任务 ${esc(j.source_job_id.slice(0, 12))}） · LeRobot ${esc(j.source_format)} → ${esc(j.target_format)}</p>` : ""}
    ${jobBadge(j)} ${sourceBadge(j)} <span class="small muted">${esc(jobStageText(j))}</span>
    ${j.error && !j.blockers?.length ? `<p class="job-error">${esc(j.error)}</p>` : ""}
    ${j.state === "BLOCKED" ? `<div class="warning preflight-detail"><strong>转换尚未启动：此任务使用的机器人配置尚未完成核实</strong><ul>${(j.blockers || []).map((b) => `<li><code>${esc(b.field)}</code>：${esc(b.message)}</li>`).join("")}</ul><p>请核实关节顺序、单位、动作来源和时钟，更新服务器 Profile 后重新提交转换。旧任务保留提交时的配置与阻断记录。</p></div>` : ""}
    ${datasetLocation(j)}
    ${j.dataset_available ? "" : `<p class="small muted">${esc(j.dataset_unavailable_reason)}</p>`}
    ${j.work_path ? `<p class="path-hint">任务工作目录：${esc(j.work_path)}</p>` : ""}
    ${j.operation === "merge" ? `<h3 class="section-label">合并来源（拼接顺序）</h3><ol>${(j.merge_sources || []).map((s) => `<li>${esc(s.dataset_name)} · ${s.episode_count} episodes · ${s.frame_count} 帧</li>`).join("")}</ol>` : ""}
    <div class="action-row">${j.dataset_available ? `<button class="primary" data-viewer="${esc(j.id)}">打开数据预览</button><button class="secondary" data-doctor="${esc(j.id)}">运行 / 重新运行质检</button>` : ""}<button class="secondary" data-report-page="${esc(j.id)}">打开独立报告页</button>${j.operation === "merge" ? '<button class="secondary" data-log="merge">合并日志</button>' : j.operation === "version_conversion" ? '<button class="secondary" data-log="environment">环境日志</button><button class="secondary" data-log="source-validate">来源验证日志</button><button class="secondary" data-log="convert">版本转换日志</button><button class="secondary" data-log="validate">结果验证日志</button><button class="secondary" data-log="doctor">自动质检日志</button>' : '<button class="secondary" data-log="convert">转换日志</button><button class="secondary" data-log="validate">验证日志</button><button class="secondary" data-log="doctor">自动质检日志</button>'}</div>
    <h3 class="section-label">LeRobot Doctor 质量报告</h3>
    <p class="small muted">Doctor 报告只用于展示数据质量，不会修改或删除已转换的数据集。模拟数据始终保留模拟标识。</p>
    <div class="action-row"><button class="secondary" data-report="automatic">转换时的报告</button>${checks.map((c) => `<button class="secondary" data-report="${esc(c.id)}">${esc(new Date(c.created_at * 1000).toLocaleTimeString("zh-CN"))} · ${esc(names[c.state] || c.state)}${c.severity ? " · " + esc(c.severity) : ""}</button>`).join("")}</div>
    ${latest ? `<p class="small">${badge(latest.state)} ${esc(latest.stage)}${latest.error ? `<span class="job-error"> ${esc(latest.error)}</span>` : ""}</p><div class="action-row"><button class="secondary" data-log="doctor" data-check-log="${esc(latest.id)}">本次质检日志</button><button class="secondary" data-log="validate" data-check-log="${esc(latest.id)}">本次验证日志</button></div>` : ""}
    ${j.processing_mode === "demo" ? '<p class="small muted">演示模式不生成 LeRobot 数据或 Doctor 报告。</p>' : j.state === "BLOCKED" ? '<p class="small muted">机器人配置检查未通过，Doctor 尚未运行；这不是质检失败。</p>' : doctorReport(report, gate)}
    <details class="report-json"><summary>${j.operation === "version_conversion" ? "版本转换来源与检查" : "时间对齐与转换检查"}</summary><pre>${esc(JSON.stringify(j.operation === "version_conversion" ? { source: j.source_description, snapshot: j.source_snapshot, quality_gate: j.quality_gate } : { alignment: j.alignment_report, robot: j.robot_report, quality_gate: j.quality_gate, selection: j.selection_report, episode_diagnostics: j.episode_diagnostics }, null, 2))}</pre></details>
    <pre id="log-output">${esc(logText)}</pre>`;
}
$("close-detail").onclick = () => {
  selectedJob = null;
  $("job-detail-panel").hidden = true;
};
function renderViewerStatus(v) {
  const previews = v.previews || [];
  const label = { READY: "运行中", STARTING: "启动中", FAILED: "已退出", UNAVAILABLE: "不可用", STOPPED: "未启动", INTERRUPTED: "已中断" }[v.state] || v.state;
  const cls = v.state === "READY" ? "" : ["FAILED", "UNAVAILABLE"].includes(v.state) ? "error" : "busy";
  const rows = previews
    .map((p) => {
      const job = serverJobs.find((j) => j.id === p.job_id);
      const name = job?.dataset_name || job?.instruction || p.job_id.slice(0, 12);
      return `<li><span>${esc(name)}</span> <a href="${esc(p.url)}" target="_blank" rel="noopener">打开 ↗</a> <button type="button" class="text-button" data-close-preview="${esc(p.job_id)}">关闭</button></li>`;
    })
    .join("");
  $("viewer-status").innerHTML =
    `<span class="badge ${cls}">${esc(label)}</span>` +
    (previews.length ? `<ul class="preview-list">${rows}</ul>` : '<span class="sub small muted">尚无打开的预览</span>') +
    (v.error ? `<p class="job-error">${esc(v.error)}</p>` : "");
}
async function poll() {
  if (!config) return;
  await refresh();
  if (config) timer = setTimeout(poll, 2000);
}
boot();
