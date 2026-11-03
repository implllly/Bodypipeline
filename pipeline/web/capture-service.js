"use strict";
window.ClientCaptureService = (() => {
  let context = null, settings = null, lastHealth = null, pendingCommand = null;
  const $ = (id) => document.getElementById(id);
  const optionInputs = () => [...$("managed-capture-options").querySelectorAll("input[data-option]")];
  const options = () => Object.fromEntries(optionInputs().map((input) => [input.dataset.option, input.checked]));
  const requestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

  function configure(value) {
    context = value;
    settings = context.config.capture_capabilities?.service_control;
    $("managed-capture-fields").hidden = !settings;
    $("capture-instruction").required = !!settings;
    $("capture-safety-note").textContent = settings
      ? "启动数采服务会启动控制程序和遥操。停止录制、停止服务均不等同于急停。"
      : "采集服务不会启动机械臂。停止录制不等同于机器人急停。";
    $("dataset-name").maxLength = settings?.dataset_max_length || 2000;
    if (!settings) {
      $("dataset-name").removeAttribute("pattern");
      return;
    }
    $("dataset-name").pattern = settings.dataset_pattern;
    $("capture-instruction").maxLength = settings.instruction_max_length;
    $("capture-instruction").value = settings.default_instruction || "";
    $("managed-capture-options").replaceChildren();
    for (const option of settings.options || []) {
      if (option.type !== "boolean") continue;
      const label = document.createElement("label"), input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.option = option.id;
      input.checked = !!option.default;
      input.disabled = !!option.readonly;
      input.onchange = () => { input.dataset.dirty = "true"; };
      label.append(input, document.createTextNode(option.label));
      $("managed-capture-options").append(label);
    }
    $("prepare-capture-service").onclick = () => context.action(async () => {
      await context.api("/api/capture-service/prepare", "POST");
      await context.refreshHealth();
      context.notice("基础服务已启动。请确认相机和遥操状态后启动数采。");
    });
    $("apply-capture-options").onclick = () => context.action(async () => {
      await context.api("/api/capture-service/options", "PATCH", {options: options()});
      optionInputs().forEach((input) => { delete input.dataset.dirty; });
      await context.refreshHealth();
      context.notice("采集配置已更新，等待相机恢复就绪。");
    });
  }

  function render(health, busy) {
    if (!settings) return;
    lastHealth = health;
    const service = health?.capture_service;
    const batch = health?.batch;
    const phaseNames = {idle: "服务待启动", starting: "启动中", running: "运行中", stopping: "停止中", error: "服务异常", blocked: "归属待确认", unavailable: "基础服务未就绪"};
    $("managed-service-status").textContent = [phaseNames[service?.phase] || "状态待更新", service?.output, service?.error || service?.exporter?.error].filter(Boolean).join(" · ");
    $("managed-source-status").replaceChildren();
    for (const source of service?.sources || []) {
      const item = document.createElement("span");
      item.className = "badge" + (source.online ? "" : " warn");
      item.textContent = source.label + " · " + (source.online ? "在线" : "离线");
      $("managed-source-status").append(item);
    }
    $("sonic-sources").replaceChildren();
    for (const source of service?.sources || []) {
      const card = document.createElement("div");
      const heading = document.createElement("div");
      const dot = document.createElement("span");
      const name = document.createElement("strong");
      const detail = document.createElement("div");
      card.className = "sonic-source";
      dot.className = "sonic-source-dot" + (source.online ? " online" : "");
      name.textContent = source.label;
      heading.append(dot, name);
      detail.className = "small muted";
      detail.textContent = source.online
        ? [
            source.rate_hz !== undefined ? source.rate_hz + " Hz" : "",
            source.age_ms !== undefined ? Math.round(source.age_ms) + " ms" : "",
          ].filter(Boolean).join(" · ")
        : source.error || "等待数据";
      card.append(heading, detail);
      $("sonic-sources").append(card);
    }
    for (const input of optionInputs()) {
      const option = settings.options.find((o) => o.id === input.dataset.option);
      input.disabled = !!option.readonly || busy || !!batch || !["idle", "unavailable"].includes(service?.phase);
      const value = service?.options?.[option.id];
      if (!input.dataset.dirty && typeof value === "boolean") input.checked = value;
    }
    $("prepare-capture-service").disabled = busy || !!batch || !["idle", "unavailable"].includes(service?.phase);
    $("apply-capture-options").disabled = busy || !!batch || service?.phase !== "idle";
    $("capture-instruction").disabled = busy || !!batch;
    $("managed-episode-actions").replaceChildren();
    if (!batch) return;
    for (const command of context.config.capture_capabilities.episode_commands || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = command.id === "discard" ? "danger" : "secondary";
      button.textContent = command.label;
      button.disabled = busy || batch.state === "STOPPING" || !service?.owned || !command.states.includes(batch.adapter_status?.state);
      button.onclick = () => context.action(async () => {
        if (command.id === "discard" && !confirm("确认丢弃当前段？该段不会上传。")) return;
        if (!pendingCommand || pendingCommand.action !== command.id || pendingCommand.batch !== batch.id) {
          pendingCommand = {action: command.id, batch: batch.id, request_id: requestId()};
        }
        const result = await context.api(`/api/capture-batches/${batch.id}/commands`, "POST", {action: command.id, request_id: pendingCommand.request_id});
        if (!result.pending) pendingCommand = null;
        await context.refreshHealth();
        context.notice(result.pending ? "指令已接收，等待采集器确认。" : "采集指令已执行。");
      });
      $("managed-episode-actions").append(button);
    }
  }

  async function prepareForStart() {
    if (!settings) return {};
    await context.api("/api/capture-service/prepare", "POST");
    await context.api("/api/capture-service/options", "PATCH", {options: options()});
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      const status = await context.api("/api/capture-service");
      if (status.phase !== "idle") throw Error(status.error || "请先结束已有数采服务，再创建本批次。");
      if (status.camera?.ready) return {capture_options: options(), instruction: $("capture-instruction").value.trim()};
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw Error("相机尚未恢复就绪，请稍后重新启动数采。");
  }

  return {configure, render, prepareForStart, get settings() { return settings; }, get health() { return lastHealth; }};
})();
