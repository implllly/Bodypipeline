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

const CHECK_META = {
  "Metadata & Format Compliance": {
    zh: "元数据与格式合规",
    desc: "info.json 字段、版本、目录结构、Parquet 与 episode 元数据是否完整且数量一致。",
  },
  "Temporal Consistency": {
    zh: "时间戳与帧序一致性",
    desc: "时间戳单调性、丢帧间隔、帧率偏差、frame_index 与 episode 序号连续性。",
  },
  "Action Quality": {
    zh: "动作质量",
    desc: "NaN/Inf、动作饱和裁剪、长时间冻结不变、超出分布的大幅跳变。",
  },
  "Video Integrity": {
    zh: "视频完整性",
    desc: "视频文件是否缺失、能否解码，帧率、分辨率与帧数是否和数据集元信息一致。",
  },
  "Data Distribution": {
    zh: "数据分布",
    desc: "观测数值是否有 NaN/Inf、零方差维度、极端离群点，以及 stats.json 是否覆盖全部特征。",
  },
  "Episode Health": {
    zh: "Episode 健康度",
    desc: "Episode 长度分布、过短或空 episode、长度离群、任务分布与策略窗口兼容性。",
  },
  "Feature Consistency": {
    zh: "特征一致性",
    desc: "跨 episode 的列、shape、dtype 是否一致，episode 内部各帧 shape 是否稳定。",
  },
  "Training Readiness": {
    zh: "训练可用性",
    desc: "是否具备 action/state/图像特征，归一化统计是否可用，能否支撑常见策略的预测窗口。",
  },
  "Anomaly Detection": {
    zh: "异常检测",
    desc: "疑似卡死的执行器、近似重复的 episode、前后分布漂移、恒定不变的传感器通道。",
  },
  Portability: {
    zh: "可移植性",
    desc: "绝对路径、符号链接、不可读文件、超大文件与非标准文件，影响复制、归档和 Hub 上传。",
  },
  "Per-Episode Summary": {
    zh: "逐 Episode 汇总",
    desc: "把其他检查发现的问题定位到具体 episode，给出需要人工复查的样本列表。",
  },
  "Kinematic Feasibility": {
    zh: "运动学可达性",
    desc: "动作是否超出 URDF 或内置机型注册表的关节位置与速度限制。",
  },
};
const CHECK_ORDER = Object.keys(CHECK_META);
const SEVERITY_RANK = { FAIL: 0, WARN: 1, PASS: 2 };
const STATE_NAMES = {
  QUEUED: "等待处理",
  RUNNING: "处理中",
  FAILED: "执行失败",
  QUARANTINED: "质检异常",
  BLOCKED: "待审批",
  INTERRUPTED: "任务中断",
  DEMO_READY: "演示完成",
  SYNTHETIC_READY: "模拟数据就绪",
  CONVERTED: "已转换 · 待质检",
  COMPLETED: "转换完成",
  COMPLETED_CHECK: "质检完成",
};
const SEVERITY_NAMES = { PASS: "通过", WARN: "警告", FAIL: "失败" };
const CHECK_STATE_NAMES = {
  QUEUED: "排队中",
  RUNNING: "质检中",
  COMPLETED: "质检完成",
  FAILED: "质检失败",
  INTERRUPTED: "质检中断",
};

let jobId = null;
let currentJob = null;
let currentChecks = [];
let selectedRun = "automatic";
let filter = "all";
let query = "";
let pollTimer = null;
let logText = "";
let analyses = [];

function badge(severity) {
  const cls = severity === "FAIL" ? "error" : severity === "WARN" ? "warn" : "";
  return `<span class="badge ${cls}">${esc(SEVERITY_NAMES[severity] || severity)}</span>`;
}
function stateName(state) {
  return STATE_NAMES[state] || state || "未知";
}
function timeText(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleString("zh-CN", { hour12: false });
}
function notice(message, error = false) {
  const el = $("notice");
  el.hidden = false;
  el.className = error ? "error" : "";
  el.textContent = message;
}
function clearNotice() {
  $("notice").hidden = true;
}
function showLogin() {
  clearTimeout(pollTimer);
  $("login-view").hidden = false;
  $("report-view").hidden = true;
  $("logout").hidden = true;
  $("token")?.focus();
}
let trustedNetwork = null;
async function resolveTrust() {
  if (trustedNetwork !== null) return;
  try {
    const config = await api("/api/config");
    trustedNetwork = Boolean(config.trusted_network);
  } catch {
    trustedNetwork = false;
  }
}
function hideLogin() {
  $("login-view").hidden = true;
  $("report-view").hidden = false;
  // A trusted-network deployment has no browser session to end.
  $("logout").hidden = Boolean(trustedNetwork);
}

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw Error("请先登录");
  }
  if (!response.ok) {
    const detail = data && data.detail;
    throw Error(
      typeof detail === "string"
        ? detail
        : JSON.stringify(detail || data || response.statusText),
    );
  }
  return data;
}

function parseJobId() {
  const match = location.pathname.match(/^\/report\/([^/]+)\/?$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function reportFor(job, checks) {
  if (selectedRun === "automatic") {
    return {
      report: job.doctor_report || null,
      gate: job.quality_gate || null,
      row: null,
      label: "转换时的报告",
    };
  }
  const row =
    checks.find((c) => c.id === selectedRun) || checks[0] || null;
  if (row) selectedRun = row.id;
  if (!row) {
    return {
      report: job.doctor_report || null,
      gate: job.quality_gate || null,
      row: null,
      label: "转换时的报告",
    };
  }
  return {
    report: row.doctor_report || null,
    gate: row.quality_gate || null,
    row,
    label: "独立质检 " + timeText(row.created_at),
  };
}

function severityCounts(report) {
  const counts = { PASS: 0, WARN: 0, FAIL: 0 };
  for (const check of report?.checks || []) {
    if (counts[check.severity] !== undefined) counts[check.severity] += 1;
  }
  return counts;
}

function messageCounts(check) {
  const counts = { PASS: 0, WARN: 0, FAIL: 0 };
  for (const message of check.messages || []) {
    if (counts[message.severity] !== undefined) counts[message.severity] += 1;
  }
  return counts;
}

function metaItem(label, value, extra = "") {
  return `<div><dt>${esc(label)}</dt><dd class="${extra}">${value}</dd></div>`;
}

function renderMeta(job, report) {
  const mode =
    job.capture_mode === "mock"
      ? "模拟数据"
      : job.capture_mode === "imported"
        ? "历史导入"
        : job.operation === "version_conversion"
          ? "LeRobot 版本转换"
          : "实机采集";
  return `<dl class="report-meta">
    ${metaItem("数据集", esc(job.dataset_name || job.instruction || "未命名数据集"))}
    ${metaItem("机器人类型", esc(job.robot_type || "未标注"))}
    ${metaItem("目标格式", "LeRobot " + esc(job.target_format || "v3.0"))}
    ${
      job.operation === "version_conversion"
        ? metaItem(
            "来源版本",
            `LeRobot ${esc(job.source_format || "—")} → ${esc(job.target_format || "—")}`,
          )
        : ""
    }
    ${metaItem("来源模式", esc(mode))}
    ${metaItem("Episodes", esc(report?.total_episodes ?? job.episode_count ?? job.source_count ?? "—"))}
    ${metaItem("总帧数", esc(report?.total_frames ?? job.frame_count ?? "—"))}
    ${metaItem("任务状态", badge_state(job.state))}
    ${metaItem("FPS", esc(report?.fps ?? "—"))}
    ${metaItem("创建时间", esc(timeText(job.created_at)))}
    ${metaItem("完成时间", esc(timeText(job.finished_at)))}
    ${metaItem("Instruction", esc(job.instruction || "—"))}
    ${job.conversion_options?.gripper_closure ? (
      job.conversion_options.gripper_closure.increase_percent == null
        ? metaItem("夹持阶段最小闭合度（旧版）", `${esc(job.conversion_options.gripper_closure.minimum_percent)}%（电机名义行程）`)
        : metaItem("夹持阶段闭合度增加", `+${esc(job.conversion_options.gripper_closure.increase_percent)} 个百分点（电机名义行程）`)
    ) : ""}
    ${
      job.operation === "version_conversion"
        ? metaItem(
            "来源数据集",
            `${esc(job.source_dataset_name || "—")} · ${esc(job.source_job_id || "—")}`,
          )
        : ""
    }
    ${metaItem("任务 ID", esc(job.id), "mono")}
    ${
      job.dataset_path
        ? `<div class="wide"><dt>服务器数据集路径</dt><dd class="mono">${esc(job.dataset_path)} <button type="button" class="secondary" data-copy="${esc(job.dataset_path)}">复制路径</button></dd></div>`
        : ""
    }
    ${
      job.work_path
        ? `<div class="wide"><dt>任务工作目录</dt><dd class="mono">${esc(job.work_path)}</dd></div>`
        : ""
    }
  </dl>`;
}
function badge_state(state, label) {
  const cls =
    state === "FAILED" || state === "FAIL"
      ? "error"
      : ["QUARANTINED", "BLOCKED", "INTERRUPTED", "CONVERTED"].includes(state)
        ? "warn"
        : ["RUNNING", "QUEUED", "UPLOADING"].includes(state)
          ? "busy"
          : "";
  return `<span class="badge ${cls}">${esc(label || stateName(state))}</span>`;
}

function renderHero(report, gate) {
  if (!report) {
    return `<div class="report-hero">
      <div><h2>尚无质检报告</h2><p>该任务还没有可展示的 Doctor 报告。可在下方运行一次质检，或查看独立质检记录。</p></div>
    </div>`;
  }
  const severity = report.overall_severity || "PASS";
  const counts = severityCounts(report);
  const title =
    severity === "PASS"
      ? "全部检查通过"
      : severity === "WARN"
        ? "存在需要关注的警告"
        : "存在必须处理的失败项";
  const desc =
    severity === "PASS"
      ? "当前数据集通过了 BodyPipeline 固定的 11 项非运动学检查。报告不修改、不裁剪任何数据。"
      : severity === "WARN"
        ? "数据已保留，仍可预览。建议按下方检查项逐条人工复查，确认是否影响训练语义。"
        : "数据已保留并标记为质检异常。请先定位失败项原因；Doctor 不会自动修复或删除数据。";
  return `<div class="report-hero ${severity.toLowerCase()}">
    <div>
      <h2>${badge(severity)} ${esc(title)}</h2>
      <p>${esc(desc)}</p>
      <p class="small muted">覆盖 ${esc(report.total_episodes ?? "—")} 个 episode · ${esc(report.total_frames ?? "—")} 帧${
        gate?.coverage
          ? ` · Gate ${gate.passed ? "通过" : "未通过"}`
          : ""
      }</p>
    </div>
    <div class="hero-counts">
      <span class="sev-chip pass"><strong>${counts.PASS}</strong> 通过</span>
      <span class="sev-chip warn"><strong>${counts.WARN}</strong> 警告</span>
      <span class="sev-chip fail"><strong>${counts.FAIL}</strong> 失败</span>
    </div>
  </div>`;
}

function renderRuns(job, checks, active) {
  const rows = checks
    .slice()
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  const buttons = [
    `<button type="button" class="run-row ${selectedRun === "automatic" ? "active" : ""}" data-run="automatic">
      <span class="run-label">转换时的报告</span>
      <span class="run-time">${esc(timeText(job.finished_at || job.created_at))}</span>
      <span class="run-spacer"></span>
      ${
        job.doctor_report
          ? badge(job.doctor_report.overall_severity)
          : '<span class="state-pill">无报告</span>'
      }
    </button>`,
    ...rows.map(
      (row) => `<button type="button" class="run-row ${selectedRun === row.id ? "active" : ""}" data-run="${esc(row.id)}">
        <span class="run-label">独立质检</span>
        <span class="run-time">${esc(timeText(row.created_at))}</span>
        <span class="run-spacer"></span>
        ${
          row.severity
            ? badge(row.severity)
            : `<span class="state-pill">${esc(CHECK_STATE_NAMES[row.state] || row.state)}</span>`
        }
      </button>`,
    ),
  ];
  const sourceValidate = job.operation === "version_conversion"
    ? '<button class="secondary" type="button" data-log="environment">环境日志</button><button class="secondary" type="button" data-log="source-validate">来源验证日志</button>'
    : "";
  const logButtons = active.row
    ? `<div class="log-actions">
        <button class="secondary" type="button" data-log="doctor" data-check="${esc(active.row.id)}">质检日志</button>
        <button class="secondary" type="button" data-log="validate" data-check="${esc(active.row.id)}">验证日志</button>
        ${sourceValidate}
        <button class="secondary" type="button" data-log="convert">转换日志</button>
      </div>`
    : `<div class="log-actions">
        <button class="secondary" type="button" data-log="doctor">自动质检日志</button>
        <button class="secondary" type="button" data-log="validate">验证日志</button>
        ${sourceValidate}
        <button class="secondary" type="button" data-log="convert">转换日志</button>
      </div>`;
  const stateLabel = active.row
    ? CHECK_STATE_NAMES[active.row.state] || active.row.state
    : "";
  return `<section class="panel runs-panel">
    <div class="panel-heading">
      <h2>质检记录</h2>
      <span class="step">DOCTOR RUNS</span>
    </div>
    <div class="run-list">${buttons.join("")}</div>
    ${
      active.row
        ? `<p class="small">${badge_state(
            active.row.state,
            stateLabel,
          )} ${esc(active.row.stage || "")}${
            active.row.error
              ? ` <span class="job-error">${esc(active.row.error)}</span>`
              : ""
          }</p>`
        : ""
    }
    <div class="action-row">
      ${
        job.dataset_available
          ? `<button class="primary" type="button" id="run-doctor">运行 / 重新运行 Doctor</button>`
          : ""
      }
      <button class="secondary" type="button" id="expand-all">展开全部明细</button>
      <button class="secondary" type="button" id="collapse-all">收起全部明细</button>
    </div>
    ${logButtons}
    <div class="log-output">${logText ? `<pre>${esc(logText)}</pre>` : ""}</div>
  </section>`;
}

function messageItem(message) {
  return `<li class="message ${(message.severity || "PASS").toLowerCase()}"><span class="message-text">${esc(message.message)}</span></li>`;
}

function checkCard(check) {
  const meta = CHECK_META[check.name] || { zh: check.name, desc: "" };
  const severity = check.severity || "PASS";
  const messages = check.messages || [];
  const counts = messageCounts(check);
  const problems = messages.filter((m) => m.severity !== "PASS");
  const passed = messages.filter((m) => m.severity === "PASS");
  const desc = [
    meta.desc,
    `${counts.FAIL} 项失败 · ${counts.WARN} 项警告 · ${counts.PASS} 项通过`,
  ]
    .filter(Boolean)
    .join(" ");
  const rawText = (
    check.name +
    " " +
    meta.zh +
    " " +
    meta.desc +
    " " +
    messages.map((m) => m.message).join(" ")
  ).toLowerCase();
  return `<article class="check-card sev-${severity.toLowerCase()}" data-severity="${esc(severity)}" data-text="${esc(rawText)}">
    <header class="check-head">
      <div>
        <h3 class="check-name">${esc(meta.zh)}<span class="check-en">${esc(check.name)}</span></h3>
        <p class="check-desc">${esc(desc)}</p>
      </div>
      <div class="check-meta">
        ${badge(severity)}
        <span class="small">${counts.FAIL} 失败 · ${counts.WARN} 警告</span>
      </div>
    </header>
    ${
      problems.length
        ? `<ul class="message-list">${problems.map(messageItem).join("")}</ul>`
        : messages.length
          ? ""
          : '<p class="messages-empty">该检查项没有输出明细。</p>'
    }
    ${
      passed.length
        ? `<details class="pass-details"><summary>查看 ${passed.length} 条通过项</summary><ul class="message-list">${passed
            .map(messageItem)
            .join("")}</ul></details>`
        : ""
    }
  </article>`;
}

function renderChecks(report) {
  if (!report) return "";
  const checks = (report.checks || []).slice().sort((a, b) => {
    const bySeverity =
      (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3);
    if (bySeverity) return bySeverity;
    const ia = CHECK_ORDER.indexOf(a.name);
    const ib = CHECK_ORDER.indexOf(b.name);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  if (!checks.length) {
    return '<p class="muted small">报告中没有检查项。</p>';
  }
  return `<section class="report-section">
    <h2>检查项明细</h2>
    <div class="check-list">${checks.map(checkCard).join("")}</div>
  </section>`;
}

function renderRaw(job, active) {
  return `<section class="report-section">
    <h2>原始数据与诊断</h2>
    <div class="raw-grid">
      <details>
        <summary>原始 Doctor 报告 JSON</summary>
        <pre>${esc(JSON.stringify(active.report || {}, null, 2))}</pre>
      </details>
      <details>
        <summary>质检 Gate 与覆盖范围</summary>
        <pre>${esc(JSON.stringify(active.gate || {}, null, 2))}</pre>
      </details>
      <details>
        <summary>时间对齐与转换检查</summary>
        <pre>${esc(
          JSON.stringify(
            {
              alignment_report: job.alignment_report || null,
              conversion_options: job.conversion_options || {},
              robot_report: job.robot_report || null,
              quality_gate: job.quality_gate || null,
              episode_diagnostics: job.episode_diagnostics || null,
              selection_report: job.selection_report || null,
            },
            null,
            2,
          ),
        )}</pre>
      </details>
    </div>
  </section>`;
}

function renderEpisodeSelection(job) {
  const selection = job.selection_report;
  if (!selection) return "";
  const rejected = (job.episode_diagnostics || []).filter((row) => !row.passed);
  return `<section class="panel"><h2>采集筛选结果</h2><p>${job.invalid_episode_policy === "skip" ? "跳过不合格采集" : "全部通过才转换"} · 输入 ${esc(selection.input_count)} · 通过 ${esc(selection.accepted_count)} · 跳过 ${esc(selection.skipped_count)} · 失败 ${esc(selection.failed_count)}</p>${rejected.length ? `<ul>${rejected.map((row) => `<li>输入 episode ${esc(row.episode_index)} · <code>${esc(row.upload_id)}</code> · ${row.skippable && job.invalid_episode_policy === "skip" ? "已跳过" : "任务失败"}：${esc(row.error)}</li>`).join("")}</ul>` : ""}</section>`;
}

function renderBlocked(job) {
  if (job.state !== "BLOCKED") return "";
  const blockers = job.blockers || [];
  return `<div class="warning">
    <strong>转换尚未启动：机器人配置未通过核实</strong>
    ${
      blockers.length
        ? `<ul>${blockers
            .map(
              (b) =>
                `<li><code>${esc(b.field)}</code>：${esc(b.message)}</li>`,
            )
            .join("")}</ul>`
        : ""
    }
    <p>请在 Server 工作台完成 Profile 核实后重新提交转换。Doctor 尚未运行，这不是质检失败。</p>
  </div>`;
}

const ANALYSIS_STATE_NAMES = {
  QUEUED: "排队中",
  RUNNING: "分析中",
  COMPLETED: "已完成",
  PARTIAL: "部分完成",
  FAILED: "执行失败",
  INTERRUPTED: "已中断",
  UNAVAILABLE: "不可用",
};
const ANALYSIS_VERDICT = {
  CLEAN: { zh: "未发现问题", cls: "pass" },
  REVIEW: { zh: "需人工复查", cls: "warn" },
  INCOMPLETE: { zh: "覆盖不完整", cls: "warn" },
};
const ANALYSIS_MODE_NAMES = { local: "本地检查", full: "本地检查 + 语义标注" };

function analysisBadge(state) {
  const cls =
    state === "FAILED"
      ? "error"
      : ["QUEUED", "RUNNING"].includes(state)
        ? "busy"
        : ["PARTIAL", "INTERRUPTED", "UNAVAILABLE"].includes(state)
          ? "warn"
          : "";
  return `<span class="badge ${cls}">${esc(ANALYSIS_STATE_NAMES[state] || state)}</span>`;
}

function analysisRows(checks) {
  if (!checks) return "";
  const rows = [];
  const add = (label, detail, value) => {
    rows.push(
      `<tr><td>${esc(label)}</td><td class="small muted">${esc(detail)}</td><td class="tabular">${esc(value)}</td></tr>`,
    );
  };
  const pair = checks.stream_pairing || {};
  if (pair.assessed) {
    add(
      "相机与机械臂对应",
      "腕部相机的画面变化应与对应机械臂的运动相关",
      `${pair.assessed} 个 episode 已判定${pair.crossed ? `，${pair.crossed} 个疑似接反` : "，未发现接反"}`,
    );
  } else if (pair.undefined) {
    add(
      "相机与机械臂对应",
      "机械臂在该 episode 内没有运动，相关系数无定义",
      `${pair.undefined} 个无法计算`,
    );
  } else if (pair.not_assessed) {
    add("相机与机械臂对应", "缺少相机安装关系或时间对齐证据", `${pair.not_assessed} 个未评估`);
  }
  const jumps = checks.recorded_jumps || {};
  if (jumps.flagged) {
    add("记录跳变", "数值跳变但对应相机画面没有同样变化", `${jumps.flagged} 个 episode 命中`);
  } else if (jumps.clean) {
    add("记录跳变", "数值跳变但对应相机画面没有同样变化", `${jumps.clean} 个 episode 未命中`);
  }
  const grip = checks.gripper_channels || {};
  if (grip.flagged) {
    add("夹爪通道", "整段录制数值完全不变", `${grip.flagged} 个 episode 命中`);
  } else if (grip.clean) {
    add("夹爪通道", "整段录制数值完全不变", `${grip.clean} 个 episode 未命中`);
  }
  const na = checks.capture_qc_not_assessed || 0;
  const layoutNa = checks.capture_qc_layout_not_applicable || 0;
  const missing = checks.capture_qc_missing_evidence || 0;
  add(
    "画面质量",
    "曝光、对比度、重复帧、画面冻结",
    `${checks.capture_qc_flags || 0} 项命中` +
      (layoutNa ? `，${layoutNa} 项在当前机器人布局上不适用` : "") +
      (missing ? `，${missing} 项缺少证据未评估` : "") +
      (!layoutNa && !missing && na ? `，${na} 项未评估` : ""),
  );
  return `<div class="table-scroll"><table class="doctor-table"><thead><tr><th>检查</th><th>说明</th><th>结果</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function renderAnalyses(job) {
  if (!analyses.length) {
    return `<section class="report-section">
      <h2>内容分析</h2>
      <p class="small muted">尚未运行内容分析。运行 Doctor 会同时启动一次免费的本地内容分析；也可以单独提交。</p>
      <div class="action-row"><button class="secondary" type="button" data-analysis="local">运行内容分析</button></div>
    </section>`;
  }
  const latest = analyses
    .slice()
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0];
  const verdict = ANALYSIS_VERDICT[latest.verdict] || null;
  const gate = latest.cross_modal || {};
  const runs = analyses
    .slice()
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
    .map(
      (row) =>
        `<div class="analysis-run">
          <span class="small muted">${esc(timeText(row.created_at))}</span>
          ${analysisBadge(row.state)}
          <span class="small">${esc(ANALYSIS_MODE_NAMES[row.mode] || row.mode)}</span>
          ${row.verdict ? `<span class="badge ${(ANALYSIS_VERDICT[row.verdict] || {}).cls || ""}">${esc((ANALYSIS_VERDICT[row.verdict] || {}).zh || row.verdict)}</span>` : ""}
          ${row.cost_usd ? `<span class="small muted">$${Number(row.cost_usd).toFixed(4)}</span>` : '<span class="small muted">免费</span>'}
        </div>`,
    )
    .join("");
  return `<section class="report-section">
    <h2>内容分析</h2>
    <p class="small muted">内容分析检查录像里实际发生了什么，与上方 Doctor 的结构和统计检查互补；它不改变任务状态。</p>
    <div class="analysis-summary">
      ${verdict ? `<span class="badge ${verdict.cls}">${esc(verdict.zh)}</span>` : ""}
      <span class="small muted">覆盖 ${esc(latest.episodes ?? latest.episode_count ?? "—")} 个 episode</span>
    </div>
    ${
      latest.state === "FAILED"
        ? `<div class="warning small">${esc(latest.error || "分析执行失败")}</div>`
        : analysisRows(latest.checks)
    }
    ${
      gate.cross_modal_ready === false
        ? `<div class="warning small">${esc(gate.reason)}</div>`
        : ""
    }
    ${
      latest.not_assessed && Object.keys(latest.not_assessed).length
        ? `<details class="analysis-na">
            <summary>未评估的检查项（${Object.keys(latest.not_assessed).length}）</summary>
            <ul>${Object.entries(latest.not_assessed)
              .map(([check, why]) => `<li><code>${esc(check)}</code> ${esc(why)}</li>`)
              .join("")}</ul>
          </details>`
        : ""
    }
    <div class="analysis-runs">${runs}</div>
    <div class="action-row">
      <button class="secondary" type="button" data-analysis="local">重新运行本地分析</button>
      <button class="secondary" type="button" data-analysis="full">运行完整分析（含语义标注，计费）</button>
    </div>
    ${
      latest.state === "QUEUED" || latest.state === "RUNNING"
        ? '<p class="small muted">分析进行中，页面会自动刷新。</p>'
        : ""
    }
  </section>`;
}

function renderReport(job, checks) {
  const active = reportFor(job, checks);
  const report = active.report;
  $("report-heading").textContent = job.dataset_name || "数据质检报告";
  $("report-subtitle").textContent = `任务 ${job.id.slice(0, 12)} · ${
    active.label
  } · 报告只读，不修改数据集`;
  $("report-content").innerHTML = `
    ${renderMeta(job, report)}
    ${renderBlocked(job)}
    ${renderEpisodeSelection(job)}
    ${renderHero(report, active.gate)}
    ${renderRuns(job, checks, active)}
    ${
      report
        ? `<section class="report-toolbar">
            <div class="filter-group" role="group" aria-label="按结论筛选">
              <button type="button" class="filter-button ${filter === "all" ? "active" : ""}" data-filter="all">全部检查项</button>
              <button type="button" class="filter-button ${filter === "issues" ? "active" : ""}" data-filter="issues">仅警告与失败</button>
              <button type="button" class="filter-button ${filter === "fail" ? "active" : ""}" data-filter="fail">仅失败</button>
            </div>
            <input class="report-search" id="report-search" type="search" placeholder="搜索检查项或明细内容" value="${esc(query)}" />
          </section>
          <div id="filter-empty" class="empty-report" hidden>没有匹配当前筛选条件的检查项。</div>
          ${renderChecks(report)}`
        : `<section class="panel empty-report">
            <h2>尚未生成 Doctor 报告</h2>
            <p class="small muted">运行一次质检后，报告会保存在 Server 状态目录，不会改动已转换的数据集。</p>
            ${
              job.dataset_available
                ? '<div class="action-row"><button class="primary" type="button" id="run-doctor">运行 Doctor</button></div>'
                : '<p class="small muted">该任务当前没有可用的转换结果。</p>'
            }
          </section>`
    }
    ${renderAnalyses(job)}
    ${renderRaw(job, active)}
    <p class="report-footer-note">
      <strong>边界说明：</strong>Doctor 只生成质量报告，不执行关节/夹爪软限位门禁、URDF 运动学准入，也不会自动修复、裁剪或删除数据。
      BodyPipeline 固定运行 11 项非运动学检查；报告中的明细消息保留 Doctor 原始英文，便于与上游工具对照排查。
      WARN / FAIL 的结果仍保留数据集，可继续预览和人工复查。
      <br />
      内容分析是独立的第三方检查，结论不参与任务状态；「不适用」表示该检查在当前机器人布局上没有定义（例如关节角录制没有末端位姿），
      不等同于通过，也不等同于失败。
    </p>`;
  applyFilter();
}

function pickerAnalysis(job) {
  const rows = analyses.filter((row) => row.job_id === job.id);
  if (!rows.length) return '<span class="state-pill">未分析</span>';
  const latest = rows.sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0];
  const verdict = ANALYSIS_VERDICT[latest.verdict];
  if (!verdict) return analysisBadge(latest.state);
  return `<span class="badge ${verdict.cls}">${esc(verdict.zh)}</span>`;
}

function renderPicker(jobs) {
  $("report-heading").textContent = "数据质检报告";
  $("report-subtitle").textContent = "选择一次已转换任务，查看完整 Doctor 报告。";
  $("job-picker").hidden = false;
  $("report-content").innerHTML = "";
  const list = jobs
    .slice()
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  $("job-picker-empty").hidden = list.length > 0;
  $("job-picker-list").innerHTML = list.length
    ? list
        .map((job) => {
          const report = job.doctor_report;
          return `<tr>
            <td><strong class="task-name">${esc(job.dataset_name || job.instruction || job.id.slice(0, 12))}</strong><span class="sub">${esc(job.id.slice(0, 12))} · ${esc(job.instruction || "")}</span></td>
            <td>${esc(job.robot_type || "未标注")}</td>
            <td>LeRobot ${esc(job.target_format || "v3.0")}</td>
            <td>${report ? badge(report.overall_severity) : '<span class="state-pill">尚未质检</span>'}</td>
            <td>${badge_state(job.state)}</td>
            <td>${pickerAnalysis(job)}</td>
            <td><button class="secondary" type="button" data-open="${esc(job.id)}">查看报告</button></td>
          </tr>`;
        })
        .join("")
    : "";
}

function applyFilter() {
  const cards = [...document.querySelectorAll(".check-card")];
  if (!cards.length) return;
  const needle = query.trim().toLowerCase();
  let visible = 0;
  for (const card of cards) {
    const severity = card.dataset.severity;
    const passesFilter =
      filter === "all" ||
      (filter === "issues" && severity !== "PASS") ||
      (filter === "fail" && severity === "FAIL");
    const matches = !needle || card.dataset.text.includes(needle);
    card.hidden = !(passesFilter && matches);
    if (!card.hidden) visible += 1;
  }
  $("filter-empty").hidden = visible > 0;
}

async function load() {
  clearTimeout(pollTimer);
  try {
    const jobs = await api("/api/jobs?summary=true");
    await resolveTrust();
    hideLogin();
    clearNotice();
    if (!jobId) {
      // The picker shows each job's newest verdict, so it needs the analyses of
      // every job, not just one.
      const all = await Promise.all(
        jobs.map((job) =>
          api(`/api/jobs/${job.id}/analyses`).catch(() => []),
        ),
      );
      analyses = all.flat();
      renderPicker(jobs);
      return;
    }
    // The summary list omits the reports; the selected job is fetched in full.
    const job = await api("/api/jobs/" + jobId);
    const [checks, runs] = await Promise.all([
      api(`/api/jobs/${jobId}/checks`),
      api(`/api/jobs/${jobId}/analyses`).catch(() => []),
    ]);
    currentJob = job;
    currentChecks = checks;
    analyses = runs;
    renderReport(job, checks);
    const active = reportFor(job, checks);
    const running =
      checks.some((row) => row.state === "QUEUED" || row.state === "RUNNING") ||
      (active.row && active.row.state === "RUNNING") ||
      runs.some((row) => row.state === "QUEUED" || row.state === "RUNNING");
    if (running) pollTimer = setTimeout(load, 3000);
  } catch (error) {
    if (error.message !== "请先登录") notice(error.message || String(error), true);
  }
}

async function loadLog(kind, checkId) {
  if (!currentJob) return;
  const path = checkId
    ? `/api/checks/${checkId}/logs/${kind}`
    : `/api/jobs/${currentJob.id}/logs/${kind}`;
  try {
    const result = await api(path);
    logText = result.text || "日志为空。";
  } catch (error) {
    logText = "读取日志失败：" + (error.message || error);
  }
  const box = document.querySelector(".log-output");
  if (box) box.innerHTML = `<pre>${esc(logText)}</pre>`;
}

async function runDoctor() {
  if (!currentJob) return;
  try {
    const row = await api(`/api/jobs/${currentJob.id}/checks`, "POST");
    selectedRun = row.id;
    notice("Doctor 已进入质检队列。报告独立保存，不改变转换结果。");
    await load();
  } catch (error) {
    notice(error.message || String(error), true);
  }
}

function exportReport() {
  if (!currentJob) return;
  const active = reportFor(currentJob, currentChecks);
  const payload = {
    exported_at: new Date().toISOString(),
    job: currentJob,
    selected_report: active.label,
    doctor_report: active.report,
    quality_gate: active.gate,
    check_runs: currentChecks,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `doctor-report-${currentJob.id.slice(0, 12)}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

document.addEventListener("click", async (event) => {
  const open = event.target.closest("[data-open]");
  if (open) {
    location.href = "/report/" + encodeURIComponent(open.dataset.open);
    return;
  }
  const run = event.target.closest("[data-run]");
  if (run) {
    selectedRun = run.dataset.run;
    logText = "";
    clearNotice();
    renderReport(currentJob, currentChecks);
    return;
  }
  const filterButton = event.target.closest("[data-filter]");
  if (filterButton) {
    filter = filterButton.dataset.filter;
    document
      .querySelectorAll("[data-filter]")
      .forEach((el) => el.classList.toggle("active", el === filterButton));
    applyFilter();
    return;
  }
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    copyText(copy.dataset.copy).then((copied) =>
      copied
        ? notice("已复制服务器数据集路径。")
        : notice("无法自动复制，请手动选中路径。", true));
    return;
  }
  const analysis = event.target.closest("[data-analysis]");
  if (analysis) {
    const mode = analysis.dataset.analysis;
    if (mode === "full") {
      const ok = window.confirm(
        "完整分析会调用视觉语言模型，按 token 计费（受任务与每日预算限制）。继续吗？",
      );
      if (!ok) return;
    }
    analysis.disabled = true;
    try {
      await api(`/api/jobs/${jobId}/analyses`, "POST", { mode });
      notice(
        mode === "full"
          ? "完整分析已进入队列，完成后自动刷新。"
          : "本地内容分析已进入队列。",
      );
      await load();
    } catch (error) {
      analysis.disabled = false;
      notice(error.message || String(error), true);
    }
    return;
  }
  const log = event.target.closest("[data-log]");
  if (log) {
    await loadLog(log.dataset.log, log.dataset.check || null);
    return;
  }
  if (event.target.closest("#run-doctor")) {
    await runDoctor();
    return;
  }
  if (event.target.closest("#expand-all")) {
    document
      .querySelectorAll(".pass-details")
      .forEach((el) => (el.open = true));
    return;
  }
  if (event.target.closest("#collapse-all")) {
    document
      .querySelectorAll(".pass-details")
      .forEach((el) => (el.open = false));
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id === "report-search") {
    query = event.target.value;
    applyFilter();
  }
});

$("refresh").onclick = () => {
  logText = "";
  load();
};
$("export").onclick = exportReport;
$("logout").onclick = async () => {
  try {
    await api("/api/logout", "POST");
  } catch {
    // The cookie may already be invalid; show the login form either way.
  }
  showLogin();
};
$("login-form").onsubmit = async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("/api/login", "POST", { token: $("token").value });
    $("token").value = "";
    await load();
  } catch (error) {
    $("login-error").textContent = error.message || String(error);
  }
};

jobId = parseJobId();
load();
