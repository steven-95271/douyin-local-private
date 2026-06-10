const state = {
  config: null,
  statusTimer: null,
  lastRunning: false,
  lastReturnCode: null,
};

const $ = (id) => document.getElementById(id);

const PLATFORM_META = {
  douyin: { label: "抖音", runnable: true, defaultTags: ["douyin", "口播"] },
  weibo: { label: "微博", runnable: false, defaultTags: ["weibo", "文字"] },
  youtube: { label: "YouTube", runnable: false, defaultTags: ["youtube", "视频"] },
  bilibili: { label: "B站", runnable: false, defaultTags: ["bilibili", "视频"] },
  tiktok: { label: "TikTok", runnable: false, defaultTags: ["tiktok", "视频"] },
};

function inferPlatformFromUrl(url) {
  const text = String(url || "").toLowerCase();
  if (text.includes("weibo.com")) return "weibo";
  if (text.includes("youtube.com") || text.includes("youtu.be")) return "youtube";
  if (text.includes("bilibili.com") || text.includes("b23.tv")) return "bilibili";
  if (text.includes("tiktok.com")) return "tiktok";
  return "douyin";
}

function normalizePlatform(value, url = "") {
  const raw = String(value || "").trim().toLowerCase();
  const inferred = inferPlatformFromUrl(url);
  const key = inferred !== "douyin" && (!raw || raw === "douyin") ? inferred : (raw || inferred);
  return PLATFORM_META[key] ? key : "douyin";
}

function platformMeta(platform) {
  return PLATFORM_META[normalizePlatform(platform)] || PLATFORM_META.douyin;
}

function platformLabel(platform) {
  return platformMeta(platform).label;
}

function isRunnablePlatform(platform) {
  return Boolean(platformMeta(platform).runnable);
}

function defaultTagsForPlatform(platform) {
  return [...platformMeta(platform).defaultTags];
}

function isDefaultTagText(value) {
  const normalized = String(value || "").replace(/\s+/g, "");
  return Object.values(PLATFORM_META).some((item) => item.defaultTags.join(",") === normalized);
}

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
  const douyinCookie = status.accounts?.douyin?.cookie || status.cookie || {};
  const weiboCookie = status.accounts?.weibo?.cookie || {};
  badge($("douyinCookieBadge"), douyinCookie.ready ? "抖音 Cookie OK" : "抖音 Cookie 缺失", douyinCookie.ready ? "ok" : "bad");
  badge($("weiboCookieBadge"), weiboCookie.ready ? "微博 Cookie OK" : "微博待接入", weiboCookie.ready ? "ok" : "");
  $("douyinAccountText").textContent = douyinCookie.ready ? "Cookie 已导入，可用于抖音抓取" : "Cookie 缺失，请用 Chrome 插件导入";
  $("weiboAccountText").textContent = weiboCookie.ready ? "Cookie 已保存，抓取适配器待接入" : "待接入 Cookie 导入与抓取适配器";
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

function needsAttention(item) {
  return ["failed", "pending", "running"].includes(String(item.status || ""));
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
  const attentionItems = items.filter(needsAttention);
  const foldedItems = items.filter((item) => !needsAttention(item));
  const failedItems = items.filter((item) => item.status === "failed");
  return `
    <div class="history-item ${escapeHtml(run.status || "")}">
      <div class="history-title">
        <div>
          <strong>${escapeHtml(run.run_id || statusLabel(run.status))}</strong>
          <span>${escapeHtml(run.current_creator || run.creator_filter || "全部可抓取来源")}</span>
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
      <div class="run-actions">
        ${failedItems.length ? `<button type="button" class="secondary small" data-action="retry-failed" data-run-id="${escapeHtml(run.run_id || "")}">重爬失败项 ${failedItems.length}</button>` : ""}
      </div>
      <details class="run-items" open>
        <summary>需要关注 ${attentionItems.length} / 全部 ${items.length}</summary>
        ${attentionItems.length ? `<div class="item-table">${attentionItems.map(renderRunItem).join("")}</div>` : `<p class="empty">暂无失败、等待或进行中的视频</p>`}
      </details>
      <details class="run-items folded">
        <summary>已成功 / 已跳过 ${foldedItems.length}</summary>
        ${foldedItems.length ? `<div class="item-table">${foldedItems.map(renderRunItem).join("")}</div>` : `<p class="empty">暂无已完成或跳过的视频</p>`}
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
    parts.push(`来源 ${progress.fetched_creators || 0}/${progress.total_creators}`);
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
    const title = ok ? "内容同步完成" : "内容同步失败";
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
  const platform = normalizePlatform(creator.platform, creator.url);
  const meta = platformMeta(platform);
  const rawTags = typeof creator.tags === "string"
    ? creator.tags.split(",").map((item) => item.trim()).filter(Boolean)
    : creator.tags;
  const tags = Array.isArray(rawTags) && rawTags.length ? rawTags : defaultTagsForPlatform(platform);
  const row = document.createElement("details");
  row.className = "creator-row";
  row.open = !creator.name || !creator.url || creator.enabled === false;
  row.innerHTML = `
    <summary class="creator-summary">
      <span data-summary="platform" class="platform-pill ${escapeHtml(platform)}">${escapeHtml(meta.label)}</span>
      <span data-summary="name">${escapeHtml(creator.name || "未命名来源")}</span>
      <span data-summary="meta">${escapeHtml(creator.category || "未分类")} · ${escapeHtml(creator.language || "中文")} · ${escapeHtml(creator.content_type || "口播")}</span>
      <span data-summary="capability" class="badge ${meta.runnable ? "ok" : "warn"}">${meta.runnable ? "可抓取" : "待接入"}</span>
      <span data-summary="status" class="badge ${creator.enabled === false ? "" : "ok"}">${creator.enabled === false ? "停用" : "启用"}</span>
    </summary>
    <div class="creator-fields">
    <input data-field="sec_user_id" type="hidden" value="${escapeHtml(creator.sec_user_id || "")}">
    <input data-field="key" type="hidden" value="${escapeHtml(creator.key || "")}">
    <input data-field="bio" type="hidden" value="${escapeHtml(creator.bio || "")}">
    <label>平台
      <select data-field="platform">
        ${Object.entries(PLATFORM_META).map(([key, item]) => `<option value="${key}" ${key === platform ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
      </select>
    </label>
    <label>名称<input data-field="name" value="${escapeHtml(creator.name || "")}"></label>
    <label>主页 URL<input data-field="url" value="${escapeHtml(creator.url || "")}"></label>
    <label>分类<input data-field="category" value="${escapeHtml(creator.category || "")}" placeholder="自动识别"></label>
    <label>语言<input data-field="language" value="${escapeHtml(creator.language || "")}" placeholder="中文"></label>
    <label>内容类型<input data-field="content_type" value="${escapeHtml(creator.content_type || "")}" placeholder="口播"></label>
    <label>Cookie<input data-field="cookie_profile" value="${escapeHtml(creator.cookie_profile || "")}" placeholder="默认"></label>
    <label class="check"><input data-field="enabled" type="checkbox" ${creator.enabled !== false ? "checked" : ""}><span>启用</span></label>
    <label>标签<input data-field="tags" value="${escapeHtml(tags.join(", "))}"></label>
    <button type="button" class="secondary" data-action="resolve">URL 补全</button>
    <button type="button" class="secondary" data-action="remove">删除</button>
    </div>
  `;
  row.querySelector('[data-action="remove"]').addEventListener("click", () => {
    row.remove();
    renderCreatorSelect();
    applyCreatorFilter();
  });
  row.querySelector('[data-action="resolve"]').addEventListener("click", () => resolveCreator(row).catch((error) => toast(error.message)));
  row.addEventListener("input", () => updateCreatorSummary(row));
  row.addEventListener("change", () => updateCreatorSummary(row));
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
  applyCreatorFilter();
}

function updateCreatorSummary(row) {
  const platformInput = row.querySelector('[data-field="platform"]');
  const platform = normalizePlatform(platformInput.value, row.querySelector('[data-field="url"]').value);
  if (platformInput.value !== platform) {
    platformInput.value = platform;
  }
  const tagsInput = row.querySelector('[data-field="tags"]');
  if (tagsInput && (!tagsInput.value.trim() || isDefaultTagText(tagsInput.value))) {
    tagsInput.value = defaultTagsForPlatform(platform).join(", ");
  }
  const meta = platformMeta(platform);
  const name = row.querySelector('[data-field="name"]').value.trim() || "未命名来源";
  const category = row.querySelector('[data-field="category"]').value.trim() || "未分类";
  const language = row.querySelector('[data-field="language"]').value.trim() || "中文";
  const contentType = row.querySelector('[data-field="content_type"]').value.trim() || "口播";
  const enabled = row.querySelector('[data-field="enabled"]').checked;
  const platformEl = row.querySelector('[data-summary="platform"]');
  platformEl.textContent = meta.label;
  platformEl.className = `platform-pill ${platform}`;
  row.querySelector('[data-summary="name"]').textContent = name;
  row.querySelector('[data-summary="meta"]').textContent = `${category} · ${language} · ${contentType}`;
  const capability = row.querySelector('[data-summary="capability"]');
  capability.textContent = meta.runnable ? "可抓取" : "待接入";
  capability.className = `badge ${meta.runnable ? "ok" : "warn"}`;
  const status = row.querySelector('[data-summary="status"]');
  status.textContent = enabled ? "启用" : "停用";
  status.className = `badge ${enabled ? "ok" : ""}`.trim();
}

function creatorSearchText(row) {
  return [
    row.querySelector('[data-field="platform"]').value,
    platformLabel(row.querySelector('[data-field="platform"]').value),
    row.querySelector('[data-field="name"]').value,
    row.querySelector('[data-field="url"]').value,
    row.querySelector('[data-field="category"]').value,
    row.querySelector('[data-field="language"]').value,
    row.querySelector('[data-field="content_type"]').value,
    row.querySelector('[data-field="cookie_profile"]').value,
    row.querySelector('[data-field="tags"]').value,
  ].join(" ").toLowerCase();
}

function applyCreatorFilter() {
  const input = $("creatorSearch");
  const query = input ? input.value.trim().toLowerCase() : "";
  const platformFilter = $("platformFilter")?.value || "all";
  const rows = Array.from(document.querySelectorAll(".creator-row"));
  let visible = 0;
  for (const row of rows) {
    const platform = normalizePlatform(row.querySelector('[data-field="platform"]').value, row.querySelector('[data-field="url"]').value);
    const matchedPlatform = platformFilter === "all" || platform === platformFilter;
    const matchedText = !query || creatorSearchText(row).includes(query);
    const matched = matchedPlatform && matchedText;
    row.hidden = !matched;
    if (matched) visible += 1;
  }
  const enabled = rows.filter((row) => row.querySelector('[data-field="enabled"]').checked).length;
  const runnable = rows.filter((row) => row.querySelector('[data-field="enabled"]').checked && isRunnablePlatform(row.querySelector('[data-field="platform"]').value)).length;
  $("creatorCount").textContent = `${visible}/${rows.length} 个来源 · 启用 ${enabled} · 可抓取 ${runnable}`;
}

function readCreators() {
  return Array.from(document.querySelectorAll(".creator-row")).map((row) => {
    const platform = normalizePlatform(row.querySelector('[data-field="platform"]').value, row.querySelector('[data-field="url"]').value);
    const tags = row.querySelector('[data-field="tags"]').value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    return {
      key: row.querySelector('[data-field="key"]').value.trim(),
      platform,
      name: row.querySelector('[data-field="name"]').value.trim(),
      url: row.querySelector('[data-field="url"]').value.trim(),
      sec_user_id: row.querySelector('[data-field="sec_user_id"]').value.trim(),
      bio: row.querySelector('[data-field="bio"]').value.trim(),
      category: row.querySelector('[data-field="category"]').value.trim(),
      language: row.querySelector('[data-field="language"]').value.trim(),
      content_type: row.querySelector('[data-field="content_type"]').value.trim(),
      cookie_profile: row.querySelector('[data-field="cookie_profile"]').value.trim(),
      enabled: row.querySelector('[data-field="enabled"]').checked,
      tags: tags.length ? tags : defaultTagsForPlatform(platform),
    };
  });
}

function renderCreatorSelect() {
  const select = $("runCreator");
  const current = select.value;
  const creators = readCreators().filter((creator) => creator.enabled && isRunnablePlatform(creator.platform));
  select.innerHTML = `<option value="">全部可抓取来源</option>`;
  for (const creator of creators) {
    const option = document.createElement("option");
    option.value = creator.key;
    const category = creator.category ? ` · ${creator.category}` : "";
    option.textContent = `[${platformLabel(creator.platform)}] ${creator.name}${category}`;
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
  const platform = normalizePlatform(row.querySelector('[data-field="platform"]').value, url);
  row.querySelector('[data-field="platform"]').value = platform;
  if (platform !== "douyin") {
    throw new Error(`${platformLabel(platform)} URL 补全和抓取适配器下一阶段接入。现在可以先手动保存为内容源。`);
  }
  const button = row.querySelector('[data-action="resolve"]');
  button.disabled = true;
  button.textContent = "补全中";
  try {
    const result = await api("/api/creator/resolve", {
      method: "POST",
      body: JSON.stringify({ url, platform }),
    });
    row.querySelector('[data-field="key"]').value = result.creator.key || "";
    row.querySelector('[data-field="platform"]').value = normalizePlatform(result.creator.platform, result.creator.url || url);
    row.querySelector('[data-field="name"]').value = result.creator.name || "";
    row.querySelector('[data-field="url"]').value = result.creator.url || url;
    row.querySelector('[data-field="sec_user_id"]').value = result.creator.sec_user_id || "";
    row.querySelector('[data-field="bio"]').value = result.creator.bio || "";
    row.querySelector('[data-field="category"]').value = result.creator.category || "";
    row.querySelector('[data-field="language"]').value = result.creator.language || "";
    row.querySelector('[data-field="content_type"]').value = result.creator.content_type || "";
    row.querySelector('[data-field="enabled"]').checked = result.creator.enabled !== false;
    row.querySelector('[data-field="cookie_profile"]').value = result.creator.cookie_profile || "";
    row.querySelector('[data-field="tags"]').value = (result.creator.tags || defaultTagsForPlatform(platform)).join(", ");
    updateCreatorSummary(row);
    renderCreatorSelect();
    applyCreatorFilter();
    toast("内容源信息已补全");
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
    throw new Error("请先选择一个可抓取的内容源");
  }
  const creatorLabel = $("runCreator").selectedOptions[0]?.textContent || creator;
  const confirmed = window.confirm(`将正式抓取 ${creatorLabel} 的所有可扫描内容，并生成 Markdown。已成功处理过的内容会自动跳过。继续吗？`);
  if (!confirmed) return;
  await startRun();
}

async function runAllEnabledCreators() {
  $("runCreator").value = "";
  const confirmed = window.confirm("将按内容源库顺序串行运行所有可抓取且已启用的来源。已成功处理过的内容会自动跳过。继续吗？");
  if (!confirmed) return;
  await startRun();
}

async function stopRun() {
  const result = await api("/api/run/stop", { method: "POST", body: "{}" });
  renderLogs(result.worker);
  toast("已发送停止信号");
  await refreshStatus();
}

async function retryFailedRun(runId) {
  if (!runId) {
    throw new Error("缺少运行编号，无法重爬失败项");
  }
  const confirmed = window.confirm("将只重爬这次运行中失败的视频；成功和跳过的视频不会进入本轮处理。继续吗？");
  if (!confirmed) return;
  const result = await api("/api/run/retry-failed", {
    method: "POST",
    body: JSON.stringify({ run_id: runId }),
  });
  renderLogs(result.worker);
  const retry = result.worker.retry;
  toast(`已启动失败重爬：${retry?.video_count || 0} 条`);
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
    platform: "douyin",
    name: "",
    url: "",
    category: "",
    language: "",
    content_type: "",
    bio: "",
    enabled: true,
    tags: defaultTagsForPlatform("douyin"),
  }));
  renderCreatorSelect();
  applyCreatorFilter();
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
$("creatorList").addEventListener("input", () => {
  renderCreatorSelect();
  applyCreatorFilter();
});
$("creatorList").addEventListener("change", () => {
  renderCreatorSelect();
  applyCreatorFilter();
});
$("creatorSearch").addEventListener("input", applyCreatorFilter);
$("platformFilter").addEventListener("change", applyCreatorFilter);
$("historyList").addEventListener("click", (event) => {
  const target = event.target;
  const button = target && target.closest ? target.closest('[data-action="retry-failed"]') : null;
  if (!button) return;
  retryFailedRun(button.dataset.runId || "").catch((error) => toast(error.message));
});

loadAll().catch((error) => toast(error.message));
state.statusTimer = setInterval(() => {
  refreshStatus().catch(() => {});
}, 3000);
