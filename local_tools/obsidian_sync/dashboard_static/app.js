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
  if (status === "error") return "错误";
  if (status === "unknown") return "异常结束";
  return "运行中";
}

function renderRunMeta(worker) {
  const analysis = worker.analysis || {};
  const errorCount = analysis.error_count || 0;
  const recentFiles = worker.recent_files || [];
  badge(
    $("runSummary"),
    `错误 ${errorCount} / 文件 ${recentFiles.length}`,
    errorCount ? "bad" : "ok"
  );

  const errors = (analysis.errors || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
  $("logSummary").innerHTML = `
    <div><strong>错误说明</strong><ul>${errors || "<li>暂无错误</li>"}</ul></div>
  `;

  const history = worker.history || [];
  $("historyList").innerHTML = history.length ? history.slice().reverse().map((run) => `
    <div class="history-item ${escapeHtml(run.status || "")}">
      <div>
        <strong>${statusLabel(run.status)}</strong>
        <span>${escapeHtml(run.started_at || "")}</span>
      </div>
      <p>seen=${run.seen ?? "-"} processed=${run.processed ?? "-"} wrote=${(run.wrote || []).length}</p>
      <code>${escapeHtml(run.command || "")}</code>
    </div>
  `).join("") : `<p class="empty">暂无运行记录</p>`;

  $("recentFiles").innerHTML = recentFiles.length ? recentFiles.map((file) => `
    <button type="button" class="file-item" data-path="${escapeHtml(file.path)}">
      <span>${escapeHtml(file.name)}</span>
      <small>${escapeHtml(file.modified || "")}</small>
    </button>
  `).join("") : `<p class="empty">暂无 Markdown 文件</p>`;
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
    <label>Key<input data-field="key" value="${escapeHtml(creator.key || "")}"></label>
    <label>名称<input data-field="name" value="${escapeHtml(creator.name || "")}"></label>
    <label>主页 URL<input data-field="url" value="${escapeHtml(creator.url || "")}"></label>
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
    dry_run: $("dryRun").checked,
    skip_summary: $("skipSummary").checked,
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
  $("dryRun").checked = false;
  $("skipSummary").checked = false;
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
