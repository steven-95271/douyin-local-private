const state = {
  config: null,
  statusTimer: null,
  lastRunning: false,
  lastReturnCode: null,
};

const $ = (id) => document.getElementById(id);

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.style.display = "block";
  setTimeout(() => {
    el.style.display = "none";
  }, 3600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const data = await response.json();
  if (!response.ok || !data.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function badge(el, label, stateName) {
  el.className = `badge ${stateName || ""}`.trim();
  el.textContent = label;
}

function renderStatus(status) {
  badge($("cookieBadge"), status.cookie.ready ? "Cookie OK" : "Cookie 缺失", status.cookie.ready ? "ok" : "bad");
  badge($("keyBadge"), status.deepseek.ready ? "DeepSeek OK" : "DeepSeek 缺失", status.deepseek.ready ? "ok" : "bad");
  if (status.worker.running) {
    badge($("workerBadge"), `运行中 ${status.worker.pid}`, "warn");
  } else if (status.worker.returncode === 0) {
    badge($("workerBadge"), "完成", "ok");
  } else if (status.worker.returncode === null || status.worker.returncode === undefined) {
    badge($("workerBadge"), "Idle", "");
  } else {
    badge($("workerBadge"), `失败 ${status.worker.returncode}`, "bad");
  }
  $("subtitle").textContent = `${status.output.path}`;
  maybeNotify(status.worker);
}

function renderLogs(worker) {
  $("logPath").textContent = worker.log_path || "";
  $("logText").textContent = worker.log_tail || "暂无日志";
  renderRunMeta(worker);
  renderProgress(worker.progress || {});
}

function statusLabel(status) {
  if (status === "done") return "完成";
  if (status === "done_with_errors") return "完成，有失败";
  if (status === "error") return "错误";
  if (status === "failed") return "失败";
  if (status === "stopped") return "已停止";
  if (status === "unknown") return "异常结束";
  if (status === "success") return "成功";
  if (status === "skipped") return "跳过";
  if (status === "dry_run") return "演练";
  if (status === "pending") return "等待";
  return "运行中";
}

function formatCount(value) {
  return value === null || value === undefined ? "-" : String(value);
}

function renderFailureReasons(run) {
  const reasons = run.failure_reasons || run.errors || [];
  if (!reasons.length) return `<p class="empty">暂无失败</p>`;
  return `<ul class="reason-list">${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`;
}

function renderRunMeta(worker) {
  const history = worker.history || [];
  const latest = history[0] || null;
  if (!latest) {
    badge($("runSummary"), "暂无", "");
    $("historyList").innerHTML = `<p class="empty">暂无运行记录</p>`;
    return;
  }

  const failed = latest.failed_count ?? latest.error_count ?? (latest.errors || []).length;
  const planned = latest.planned_count ?? latest.candidate_count ?? latest.processed;
  badge(
    $("runSummary"),
    `${statusLabel(latest.status)} / 成功 ${formatCount(latest.success_count ?? (latest.wrote || []).length)} / 失败 ${formatCount(failed)}`,
    failed ? "bad" : (latest.status === "running" ? "warn" : "ok")
  );

  const older = history.slice(1);
  $("historyList").innerHTML = `
    ${renderLatestRun(latest, planned, failed)}
    <details class="history-archive">
      <summary>历史任务 ${older.length}</summary>
      <div class="archive-list">
        ${older.length ? older.map(renderArchiveRun).join("") : `<p class="empty">暂无更早任务</p>`}
      </div>
    </details>
  `;
}

function renderLatestRun(run, planned, failed) {
  const items = run.items || [];
  return `
    <div class="history-item ${escapeHtml(run.status || "")}">
      <div class="history-title">
        <div>
          <strong>${escapeHtml(run.run_id || statusLabel(run.status))}</strong>
          <span>${escapeHtml(run.current_creator || run.creator_filter || "全部启用博主")}</span>
        </div>
        <time>${escapeHtml(run.started_at || "")}</time>
      </div>
      <div class="history-metrics">
        <span><b>${formatCount(run.detected_count ?? run.seen)}</b>嗅探到</span>
        <span><b>${formatCount(run.success_count ?? (run.wrote || []).length)}</b>抓取成功</span>
        <span><b>${formatCount(planned)}</b>本轮计划</span>
        <span><b>${formatCount(failed)}</b>失败</span>
      </div>
      <p>状态：${statusLabel(run.status)} · 阶段：${escapeHtml(run.current_stage || "-")} · 跳过：${formatCount(run.skipped_count)}</p>
      <div class="history-reasons">
        <em>失败原因</em>
        ${renderFailureReasons(run)}
      </div>
      <details class="run-items" open>
        <summary>视频明细 ${items.length}</summary>
        ${items.length ? `<div class="item-table">${items.map(renderRunItem).join("")}</div>` : `<p class="empty">新任务启动后会显示每条视频进度</p>`}
      </details>
    </div>
  `;
}

function renderRunItem(item) {
  return `
    <div class="run-item ${escapeHtml(item.status || "")}">
      <span class="item-status">${statusLabel(item.status)}</span>
      <span class="item-title">${escapeHtml(item.title || item.video_id || "")}</span>
      <span class="item-stage">${escapeHtml(item.stage || "-")}</span>
      <span class="item-error">${escapeHtml(item.error_human || "")}</span>
    </div>
  `;
}

function renderArchiveRun(run) {
  const failed = run.failed_count ?? run.error_count ?? (run.errors || []).length;
  return `
    <div class="archive-item ${escapeHtml(run.status || "")}">
      <strong>${escapeHtml(run.run_id || run.started_at || "")}</strong>
      <span>${statusLabel(run.status)} · 嗅探 ${formatCount(run.detected_count ?? run.seen)} · 成功 ${formatCount(run.success_count ?? (run.wrote || []).length)} · 失败 ${formatCount(failed)}</span>
    </div>
  `;
}

function renderProgress(progress) {
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  $("progressLabel").textContent = progress.label || "暂无进度";
  $("progressPercent").textContent = `${percent}%`;
  $("progressFill").style.width = `${percent}%`;

  const parts = [];
  if (progress.total_creators) {
    parts.push(`博主 ${progress.fetched_creators || 0}/${progress.total_creators}`);
  }
  if (progress.total_items !== null && progress.total_items !== undefined) {
    parts.push(`视频 ${progress.completed_items || 0}/${progress.total_items}`);
  }
  if (progress.current_video) {
    parts.push(`当前 ${progress.current_video}`);
  }
  if (progress.retry_count) {
    parts.push(`自动重试 ${progress.retry_count} 次`);
  }
  if (progress.error_count) {
    parts.push(`错误 ${progress.error_count}`);
  }
  $("progressMeta").textContent = parts.join(" · ") || "任务启动后会显示扫描、处理和重试进度";
}

function maybeNotify(worker) {
  const running = Boolean(worker.running);
  const finished = state.lastRunning && !running;
  if (finished) {
    const ok = worker.returncode === 0;
    const title = ok ? "抖音同步完成" : "抖音同步失败";
    const message = ok ? `文件已写入 ${worker.output_path || ""}` : `返回码 ${worker.returncode}`;
    toast(`${title}：${message}`);
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification(title, { body: message });
    }
  }
  state.lastRunning = running;
  state.lastReturnCode = worker.returncode;
}

function creatorTemplate(creator = {}) {
  const row = document.createElement("div");
  row.className = "creator-row";
  row.innerHTML = `
    <input data-field="sec_user_id" type="hidden" value="${escapeHtml(creator.sec_user_id || "")}">
    <input data-field="key" type="hidden" value="${escapeHtml(creator.key || "")}">
    <input data-field="bio" type="hidden" value="${escapeHtml(creator.bio || "")}">
    <label>名称<input data-field="name" value="${escapeHtml(creator.name || "")}"></label>
    <label>主页 URL<input data-field="url" value="${escapeHtml(creator.url || "")}"></label>
    <label>分类<input data-field="category" value="${escapeHtml(creator.category || "")}" placeholder="自动识别"></label>
    <label>语言<input data-field="language" value="${escapeHtml(creator.language || "")}" placeholder="中文"></label>
    <label>内容类型<input data-field="content_type" value="${escapeHtml(creator.content_type || "")}" placeholder="口播"></label>
    <label class="check"><input data-field="enabled" type="checkbox" ${creator.enabled !== false ? "checked" : ""}><span>启用</span></label>
    <label>标签<input data-field="tags" value="${escapeHtml((creator.tags || ["douyin", "口播"]).join(", "))}"></label>
    <button type="button" class="secondary" data-action="resolve">URL 补全</button>
    <button type="button" class="secondary" data-action="remove">删除</button>
  `;
  row.querySelector('[data-action="remove"]').addEventListener("click", () => row.remove());
  row.querySelector('[data-action="resolve"]').addEventListener("click", () => resolveCreator(row).catch((error) => toast(error.message)));
  return row;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderConfig(config) {
  state.config = config;
  $("vaultPath").value = config.vault_path || "";
  $("outputSubdir").value = config.output_subdir || "";
  const list = $("creatorList");
  list.innerHTML = "";
  for (const creator of config.creators || []) {
    list.appendChild(creatorTemplate(creator));
  }
  renderCreatorSelect();
}

function readCreators() {
  return Array.from(document.querySelectorAll(".creator-row")).map((row) => {
    const tags = row.querySelector('[data-field="tags"]').value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    return {
      key: row.querySelector('[data-field="key"]').value.trim(),
      name: row.querySelector('[data-field="name"]').value.trim(),
      url: row.querySelector('[data-field="url"]').value.trim(),
      sec_user_id: row.querySelector('[data-field="sec_user_id"]').value.trim(),
      bio: row.querySelector('[data-field="bio"]').value.trim(),
      category: row.querySelector('[data-field="category"]').value.trim(),
      language: row.querySelector('[data-field="language"]').value.trim(),
      content_type: row.querySelector('[data-field="content_type"]').value.trim(),
      enabled: row.querySelector('[data-field="enabled"]').checked,
      tags: tags.length ? tags : ["douyin", "口播"],
    };
  });
}

function renderCreatorSelect() {
  const select = $("runCreator");
  const current = select.value;
  const creators = readCreators().filter((creator) => creator.enabled);
  select.innerHTML = `<option value="">全部启用博主</option>`;
  for (const creator of creators) {
    const option = document.createElement("option");
    option.value = creator.key;
    option.textContent = `${creator.name} (${creator.key})`;
    select.appendChild(option);
  }
  if (current) select.value = current;
}

async function resolveCreator(row) {
  const urlInput = row.querySelector('[data-field="url"]');
  const url = urlInput.value.trim();
  if (!url) {
    throw new Error("请先填写主页 URL");
  }
  const button = row.querySelector('[data-action="resolve"]');
  button.disabled = true;
  button.textContent = "补全中";
  try {
    const result = await api("/api/creator/resolve", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    row.querySelector('[data-field="key"]').value = result.creator.key || "";
    row.querySelector('[data-field="name"]').value = result.creator.name || "";
    row.querySelector('[data-field="url"]').value = result.creator.url || url;
    row.querySelector('[data-field="sec_user_id"]').value = result.creator.sec_user_id || "";
    row.querySelector('[data-field="bio"]').value = result.creator.bio || "";
    row.querySelector('[data-field="category"]').value = result.creator.category || "";
    row.querySelector('[data-field="language"]').value = result.creator.language || "";
    row.querySelector('[data-field="content_type"]').value = result.creator.content_type || "";
    row.querySelector('[data-field="enabled"]').checked = result.creator.enabled !== false;
    row.querySelector('[data-field="tags"]').value = (result.creator.tags || ["douyin", "口播"]).join(", ");
    renderCreatorSelect();
    toast("博主信息已补全");
  } finally {
    button.disabled = false;
    button.textContent = "URL 补全";
  }
}

async function loadAll() {
  const config = await api("/api/config");
  renderConfig(config.config);
  const status = await api("/api/status");
  renderStatus(status.status);
  renderLogs(status.status.worker);
}

async function refreshStatus() {
  const status = await api("/api/status");
  renderStatus(status.status);
  renderLogs(status.status.worker);
}

async function saveConfig(includeCreators) {
  const payload = {
    vault_path: $("vaultPath").value.trim(),
    output_subdir: $("outputSubdir").value.trim(),
  };
  if (includeCreators) payload.creators = readCreators();
  const result = await api("/api/config", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  renderConfig(result.config);
  toast("配置已保存");
}

async function saveSecrets() {
  const payload = {
    douyin_cookie: $("cookieInput").value.trim(),
    deepseek_api_key: $("apiKeyInput").value.trim(),
  };
  const result = await api("/api/secrets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  $("cookieInput").value = "";
  $("apiKeyInput").value = "";
  renderStatus(result.status);
  toast("密钥已保存");
}

async function startRun() {
  await saveConfig(true);
  const payload = {
    creator: $("runCreator").value,
    limit: Number($("runLimit").value || 0),
    force: $("forceRun").checked,
  };
  const result = await api("/api/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  renderLogs(result.worker);
  toast("任务已启动");
  await refreshStatus();
}

async function crawlSelectedCreatorAll() {
  const creator = $("runCreator").value;
  if (!creator) {
    throw new Error("请先在“指定博主”里选择一个博主");
  }
  $("runLimit").value = "0";
  const creatorLabel = $("runCreator").selectedOptions[0]?.textContent || creator;
  const confirmed = window.confirm(`将正式抓取 ${creatorLabel} 的所有可扫描视频，并生成 Markdown。已成功处理过的视频会自动跳过。继续吗？`);
  if (!confirmed) return;
  await startRun();
}

async function runAllEnabledCreators() {
  $("runCreator").value = "";
  const limit = Number($("runLimit").value || 0);
  const limitText = limit > 0 ? `每个博主最多处理 ${limit} 条` : "处理所有可扫描的新视频";
  const confirmed = window.confirm(`将按博主列表顺序串行运行所有启用博主，${limitText}。已成功处理过的视频会自动跳过。继续吗？`);
  if (!confirmed) return;
  await startRun();
}

async function stopRun() {
  const result = await api("/api/run/stop", { method: "POST", body: "{}" });
  renderLogs(result.worker);
  toast("已发送停止信号");
  await refreshStatus();
}

async function openTarget(target) {
  const result = await api("/api/open", {
    method: "POST",
    body: JSON.stringify({ target }),
  });
  toast(`已打开：${result.opened.path}`);
}

async function requestNotifyPermission() {
  if (!("Notification" in window)) {
    toast("当前浏览器不支持通知");
    return;
  }
  const permission = await Notification.requestPermission();
  toast(permission === "granted" ? "完成提醒已开启" : "完成提醒未开启");
}

$("addCreator").addEventListener("click", () => {
  $("creatorList").appendChild(creatorTemplate({
    key: "",
    name: "",
    url: "",
    category: "",
    language: "",
    content_type: "",
    bio: "",
    enabled: true,
    tags: ["douyin", "口播"],
  }));
  renderCreatorSelect();
});

$("saveCreators").addEventListener("click", () => saveConfig(true).catch((error) => toast(error.message)));
$("saveSettings").addEventListener("click", () => saveConfig(false).catch((error) => toast(error.message)));
$("saveSecrets").addEventListener("click", () => saveSecrets().catch((error) => toast(error.message)));
$("refresh").addEventListener("click", () => refreshStatus().catch((error) => toast(error.message)));
$("startRun").addEventListener("click", () => startRun().catch((error) => toast(error.message)));
$("runAllCreators").addEventListener("click", () => runAllEnabledCreators().catch((error) => toast(error.message)));
$("crawlCreatorAll").addEventListener("click", () => crawlSelectedCreatorAll().catch((error) => toast(error.message)));
$("stopRun").addEventListener("click", () => stopRun().catch((error) => toast(error.message)));
$("notifyPermission").addEventListener("click", () => requestNotifyPermission().catch((error) => toast(error.message)));
$("openOutput").addEventListener("click", () => openTarget("output").catch((error) => toast(error.message)));
$("openLog").addEventListener("click", () => openTarget("log").catch((error) => toast(error.message)));
$("creatorList").addEventListener("input", renderCreatorSelect);

loadAll().catch((error) => toast(error.message));
state.statusTimer = setInterval(() => {
  refreshStatus().catch(() => {});
}, 3000);
