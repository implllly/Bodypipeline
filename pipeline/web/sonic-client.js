"use strict";

window.SonicClient = (() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[char]);
  const names = {
    DRAFT: "待启动",
    STARTING: "正在启动",
    RECORDING: "录制中",
    STOPPING: "正在停止",
    RECOVERY_REQUIRED: "待恢复",
    REVIEW: "待确认保留",
    SEALING: "待校验 / 封存中",
    PAUSED: "已暂停",
    SAVING: "保存中",
    SEALED: "已封存",
    DISCARDED: "已丢弃",
    QUEUED: "等待处理",
    UPLOADING: "正在上传",
    RETRY: "等待重试",
    ACKED: "远端已确认",
    NONE: "未上传",
    ACTIVE: "采集中",
    PREPARING: "正在启动",
    ENDED: "已结束",
  };
  let config = null;
  let captures = [];
  let health = null;
  let service = null;
  let busy = false;
  let pollTimer = null;

  function notice(message, error = false) {
    const box = $("bp-notice");
    box.hidden = false;
    box.classList.toggle("bp-error", error);
    box.textContent = message;
  }

  async function api(path, method = "GET", body) {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      method === "GET" ? 12000 : 30000,
    );
    try {
      const response = await fetch(path, {
        method,
        signal: controller.signal,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        cache: "no-store",
      });
      let data = null;
      try {
        data = await response.json();
      } catch {
        data = {};
      }
      if (!response.ok) {
        const detail =
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail || data);
        throw Error(detail || `请求失败 (${response.status})`);
      }
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function action(fn) {
    if (busy) {
      notice("上一个操作仍在处理中，请稍候。");
      return;
    }
    busy = true;
    render();
    try {
      await fn();
    } catch (error) {
      if (error.name !== "AbortError") notice(error.message, true);
    } finally {
      busy = false;
      render();
      void refresh();
    }
  }

  function badge(state) {
    const text = names[state] || state || "未知";
    const tone =
      ["FAILED", "RECOVERY_REQUIRED"].includes(state)
        ? " bp-bad"
        : ["RETRY", "REVIEW", "CONVERTED"].includes(state)
          ? " bp-warn"
          : ["STARTING", "RECORDING", "STOPPING", "UPLOADING", "SEALING"].includes(
                state,
              )
            ? " bp-ok"
            : "";
    return `<span class="bp-badge${tone}">${esc(text)}</span>`;
  }

  function bytes(value) {
    const n = Number(value) || 0;
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
    return (n / 1024).toFixed(1) + " KB";
  }

  function renderDatasets() {
    const groups = new Map();
    for (const row of captures) {
      const id = row.collection_id || row.id;
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(row);
    }
    $("client-dataset-list").innerHTML = groups.size
      ? [...groups.values()]
          .map((rows) => {
            const first = rows[0];
            const name = first.dataset_name || first.instruction || "未命名任务";
            return `<tr>
              <td><span class="bp-task">${esc(name)}</span><span class="bp-sub">${esc(
                rows.length,
              )} 段采集</span></td>
              <td>${rows.filter((row) => row.state === "SEALED").length} / ${
                rows.filter((row) => row.upload_state === "ACKED").length
              }</td>
              <td><span class="bp-task">${esc(first.remote_target || "default")}/${esc(
                first.remote_prefix || "",
              )}</span><span class="bp-sub">本地 ${esc(
                first.local_root || "default",
              )}</span></td>
              <td><button type="button" data-use-dataset="${esc(
                first.dataset_name || first.instruction || "",
              )}">继续采集</button></td>
            </tr>`;
          })
          .join("")
      : '<tr><td colspan="4" class="bp-empty">填写任务名称并启动第一条采集。</td></tr>';
  }

  function controls(row) {
    const buttons = [];
    if (row.state === "REVIEW" && !row.capture_failed) {
      buttons.push(
        `<button type="button" data-op="keep" data-capture="${esc(
          row.id,
        )}">保留并上传</button>`,
      );
    }
    if (row.upload_state === "RETRY") {
      buttons.push(
        `<button type="button" data-op="retry-upload" data-capture="${esc(
          row.id,
        )}">重试上传</button>`,
      );
    }
    return buttons.length
      ? `<div class="bp-actions">${buttons.join("")}</div>`
      : '<span class="bp-sub">—</span>';
  }

  function renderCaptures() {
    renderDatasets();
    $("capture-list").innerHTML = captures.length
      ? captures
          .map((row) => {
            const percent = row.total_bytes
              ? Math.min(
                  100,
                  Math.round(
                    (100 * (row.uploaded_bytes || 0)) / row.total_bytes,
                  ),
                )
              : 0;
            return `<tr>
              <td><span class="bp-task">${esc(
                row.dataset_name || row.instruction || "未命名任务",
              )}</span><span class="bp-sub">${esc(
                String(row.id).slice(0, 8),
              )} · ${esc(
                row.created_at
                  ? new Date(row.created_at * 1000).toLocaleString("zh-CN")
                  : "",
              )}</span>${row.error ? `<span class="bp-sub">${esc(row.error)}</span>` : ""}</td>
              <td>${badge(row.state)}</td>
              <td>${esc(row.remote_target || "default")}<span class="bp-sub">${esc(
                row.remote_prefix || "/",
              )}</span></td>
              <td>${badge(row.upload_state)} ${
                row.total_bytes
                  ? `<span class="bp-sub">${percent}% · ${bytes(row.total_bytes)}</span>
                     <div class="bp-progress"><span style="width:${percent}%"></span></div>`
                  : ""
              }</td>
              <td>${controls(row)}</td>
            </tr>`;
          })
          .join("")
      : '<tr><td colspan="5" class="bp-empty">还没有采集记录。</td></tr>';
  }

  function renderBatch() {
    const batch = health?.batch;
    $("bp-batch-state").textContent = batch
      ? names[batch.state] || batch.state
      : "无活动批次";
    $("bp-batch-state").className =
      "bp-badge" +
      (batch
        ? batch.state === "RECOVERY_REQUIRED"
          ? " bp-bad"
          : batch.state === "ACTIVE"
            ? " bp-ok"
            : " bp-warn"
        : "");
    $("bp-batch-summary").innerHTML = batch
      ? `<strong>${esc(batch.dataset_name || batch.instruction || "未命名批次")}</strong><br>
         作业 ${esc(batch.state)} · 采集器 ${esc(
           batch.adapter_status?.state || "状态待更新",
         )} · ${esc(batch.episode_count || 0)} 段
         ${batch.error ? `<br><span class="bp-error">${esc(batch.error)}</span>` : ""}`
      : "尚未启动采集批次。启动基础服务后，使用上方原版采集控制创建批次。";
  }

  function renderService() {
    const phase = service?.phase || "unavailable";
    const labels = {
      idle: "基础服务空闲",
      starting: "正在启动",
      running: "采集中",
      stopping: "正在停止",
      error: "服务异常",
      unavailable: "服务未启动",
    };
    $("bp-service-state").textContent = labels[phase] || phase;
    $("bp-service-state").className =
      "bp-badge" +
      (phase === "running"
        ? " bp-ok"
        : phase === "error"
          ? " bp-bad"
          : phase === "unavailable"
            ? " bp-warn"
            : "");
    $("bp-service-summary").innerHTML = service
      ? `<strong>${esc(labels[phase] || phase)}</strong><br>
         ${service.camera?.ready ? "相机已就绪" : "相机待就绪"} · ${esc(
       service.error || "8081 原生采集控制可连接",
         )}`
      : "8081 原生采集服务尚未启动。";
    $("bp-prepare-service").disabled =
      busy || !["unavailable", "idle"].includes(phase);
    $("bp-prepare-service").textContent =
      phase === "unavailable" ? "启动数采基础服务" : "刷新基础服务";
  }

  function render() {
    if (!config) return;
    $("health-detail").textContent = health
      ? JSON.stringify(health, null, 2)
      : "正在读取健康状态…";
    $("bp-connection").textContent = health?.ready
      ? "采集适配器已就绪"
      : "适配器状态待更新";
    $("bp-connection").className =
      "bp-badge " + (health?.ready ? "bp-ok" : "bp-warn");
    $("bp-refresh").disabled = busy;
    renderService();
    renderCaptures();
    renderBatch();
  }

  async function refresh() {
    if (!config || busy) return;
    clearTimeout(pollTimer);
    try {
      const [nextHealth, nextCaptures, nextService] = await Promise.all([
        api("/api/health"),
        api("/api/capture-status"),
        api("/api/capture-service").catch((error) => ({
          phase: "unavailable",
          error: error.message,
        })),
      ]);
      health = nextHealth;
      captures = nextCaptures;
      service = nextService;
      render();
    } catch (error) {
      notice(error.message, true);
    } finally {
      if (config) pollTimer = setTimeout(refresh, 2000);
    }
  }

  async function boot() {
    clearTimeout(pollTimer);
    try {
      config = await api("/api/config");
    } catch {
      return;
    }
    if (config.role !== "client") {
      throw Error("当前页面仅用于 Client");
    }
    await refresh();
  }

  $("bp-refresh").addEventListener("click", () => void refresh());

  $("bp-prepare-service").addEventListener("click", () => {
    void action(async () => {
      notice("正在启动或刷新 8081 数采基础服务…");
      await api("/api/capture-service/prepare", "POST");
      notice("数采基础服务已就绪。现在可使用上方原版采集控制。");
    });
  });

  document.addEventListener("click", (event) => {
    const useDataset = event.target.closest("[data-use-dataset]");
    if (useDataset) {
      const input = document.querySelector(
        '#collection input[name="dataset_name"]',
      );
      if (input) {
        input.value = useDataset.dataset.useDataset;
        input.focus();
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      return;
    }
    const operation = event.target.closest("[data-op]");
    if (operation) {
      void action(async () => {
        const captureId = operation.dataset.capture;
        const endpoint =
          operation.dataset.op === "keep"
            ? `/api/captures/${encodeURIComponent(captureId)}/keep`
            : `/api/captures/${encodeURIComponent(captureId)}/retry-upload`;
        await api(endpoint, "POST");
        await refresh();
        notice(
          operation.dataset.op === "keep"
            ? "已提交保留请求，校验完成后自动上传。"
            : "上传已重新排队。",
        );
      });
    }
  });

  setInterval(() => {
    const rawOnly = document.querySelector(
      '#collection input[name="raw_only"]',
    );
    if (!rawOnly) return;
    rawOnly.checked = true;
    rawOnly.disabled = true;
    rawOnly.indeterminate = false;
    rawOnly.title = "BodyPipeline Client 仅支持 Raw，上传后由 Server 转换";
  }, 250);

  boot().catch((error) => notice(`Client 初始化失败：${error.message}`, true));
  return { boot, refresh };
})();
