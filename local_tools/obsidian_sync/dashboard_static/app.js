const state = {
  config: null,
  statusTimer: null,
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
}

function renderLogs(worker) {
  $("logPath").textContent = worker.log_path || "";
  $("logText").textContent = worker.log_tail || "暂无日志";
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

async function stopRun() {
  const result = await api("/api/run/stop", { method: "POST", body: "{}" });
  renderLogs(result.worker);
  toast("已发送停止信号");
  await refreshStatus();
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
$("stopRun").addEventListener("click", () => stopRun().catch((error) => toast(error.message)));
$("creatorList").addEventListener("input", renderCreatorSelect);

loadAll().catch((error) => toast(error.message));
state.statusTimer = setInterval(() => {
  refreshStatus().catch(() => {});
}, 3000);
