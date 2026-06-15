const state = {
  config: null,
  statusTimer: null,
  lastRunning: false,
  lastReturnCode: null,
  candidateSelections: {},
};

const $ = (id) => document.getElementById(id);

const PLATFORM_META = {
  douyin: { label: "抖音", runnable: true, defaultTags: ["douyin", "口播"] },
  weibo: { label: "微博", runnable: true, defaultTags: ["weibo", "文字"] },
  xiaoyuzhou: { label: "小宇宙", runnable: true, defaultTags: ["xiaoyuzhou", "播客"] },
  wechat: { label: "公众号", runnable: true, defaultTags: ["wechat", "公众号"] },
  youtube: { label: "YouTube", runnable: false, defaultTags: ["youtube", "视频"] },
  bilibili: { label: "B站", runnable: false, defaultTags: ["bilibili", "视频"] },
  tiktok: { label: "TikTok", runnable: false, defaultTags: ["tiktok", "视频"] },
};

function inferPlatformFromUrl(url) {
  const text = String(url || "").toLowerCase();
  if (text.includes("mp.weixin.qq.com") || text.includes("weixin.qq.com")) return "wechat";
  if (text.includes("xiaoyuzhoufm.com") || text.includes("feed.xyzfm.space") || text.includes("podcast.xyz")) return "xiaoyuzhou";
  if (text.includes("weibo.com") || text.includes("weibo.cn")) return "weibo";
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
  badge($("weiboCookieBadge"), weiboCookie.ready ? "微博 Cookie OK" : "微博 Cookie 缺失", weiboCookie.ready ? "ok" : "");
  $("douyinAccountText").textContent = douyinCookie.ready ? "Cookie 已导入，可用于抖音抓取" : "Cookie 缺失，请用 Chrome 插件导入";
  $("weiboAccountText").textContent = weiboCookie.ready ? "Cookie 已保存，可用于微博 URL 补全和文本抓取" : "未导入 Cookie；微博抓取需要先导入";
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
  setRunControlsRunning(Boolean(status.worker.running));
  $("subtitle").textContent = `${status.output.path}`;
}

function setRunControlsRunning(running) {
  $("startRun").disabled = running;
  $("scanCandidates").disabled = running;
  $("crawlCreatorAll").disabled = running;
  $("stopRun").disabled = !running;
  state.lastRunning = running;
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
  if (status === "dry_run") return "候选";
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
  const candidateItems = items.filter((item) => item.status === "dry_run");
  const attentionItems = items.filter(needsAttention);
  const foldedItems = items.filter((item) => !needsAttention(item) && item.status !== "dry_run");
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
        ${candidateItems.length ? `<button type="button" class="secondary small" data-action="run-selected" data-run-id="${escapeHtml(run.run_id || "")}">抓取选中内容</button>` : ""}
        ${failedItems.length ? `<button type="button" class="secondary small" data-action="retry-failed" data-run-id="${escapeHtml(run.run_id || "")}">重爬失败项 ${failedItems.length}</button>` : ""}
      </div>
      ${candidateItems.length ? `
      <details class="run-items candidates" open>
        <summary>候选内容 ${candidateItems.length}</summary>
        <div class="item-tools">
          <button type="button" class="secondary small" data-action="select-candidates" data-run-id="${escapeHtml(run.run_id || "")}">全选</button>
          <button type="button" class="secondary small" data-action="clear-candidates" data-run-id="${escapeHtml(run.run_id || "")}">清空</button>
        </div>
        <div class="item-table">${candidateItems.map((item) => renderRunItem(item, { selectable: true, runId: run.run_id || "" })).join("")}</div>
      </details>
      ` : ""}
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

function renderRunItem(item, options = {}) {
  const runSelections = state.candidateSelections[options.runId || ""] || new Set();
  const checked = runSelections.has(String(item.video_id || ""));
  const checkbox = options.selectable ? `
      <label class="candidate-check">
        <input type="checkbox" data-role="candidate" data-run-id="${escapeHtml(options.runId || "")}" data-video-id="${escapeHtml(item.video_id || "")}" ${checked ? "checked" : ""}>
      </label>
    ` : "";
  return `
    <div class="run-item ${escapeHtml(item.status || "")} ${options.selectable ? "selectable" : ""}">
      ${checkbox}
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
    <input data-field="platform_id" type="hidden" value="${escapeHtml(creator.platform_id || "")}">
    <input data-field="weibo_uid" type="hidden" value="${escapeHtml(creator.weibo_uid || "")}">
    <input data-field="weibo_custom" type="hidden" value="${escapeHtml(creator.weibo_custom || "")}">
    <input data-field="xiaoyuzhou_pid" type="hidden" value="${escapeHtml(creator.xiaoyuzhou_pid || "")}">
    <input data-field="xiaoyuzhou_eid" type="hidden" value="${escapeHtml(creator.xiaoyuzhou_eid || "")}">
    <input data-field="wechat_biz" type="hidden" value="${escapeHtml(creator.wechat_biz || "")}">
    <label>平台
      <select data-field="platform">
        ${Object.entries(PLATFORM_META).map(([key, item]) => `<option value="${key}" ${key === platform ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
      </select>
    </label>
    <label>名称<input data-field="name" value="${escapeHtml(creator.name || "")}"></label>
    <label>主页 URL<input data-field="url" value="${escapeHtml(creator.url || "")}"></label>
    <label>RSS URL<input data-field="rss_url" value="${escapeHtml(creator.rss_url || creator.feed_url || "")}" placeholder="小宇宙/公众号可选，用于完整历史"></label>
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

function outputSubdirInputs() {
  return {
    douyin: $("outputSubdirDouyin"),
    weibo: $("outputSubdirWeibo"),
    xiaoyuzhou: $("outputSubdirXiaoyuzhou"),
    wechat: $("outputSubdirWechat"),
    youtube: $("outputSubdirYoutube"),
    bilibili: $("outputSubdirBilibili"),
    tiktok: $("outputSubdirTiktok"),
  };
}

function renderConfig(config) {
  state.config = config;
  $("vaultPath").value = config.vault_path || "";
  const outputSubdirs = config.output_subdirs || {};
  const outputInputs = outputSubdirInputs();
  outputInputs.douyin.value = outputSubdirs.douyin || config.output_subdir || "Douyin/口播博主";
  outputInputs.weibo.value = outputSubdirs.weibo || "Weibo/内容源";
  outputInputs.xiaoyuzhou.value = outputSubdirs.xiaoyuzhou || "Podcast/小宇宙";
  outputInputs.wechat.value = outputSubdirs.wechat || "WeChat/公众号";
  outputInputs.youtube.value = outputSubdirs.youtube || "YouTube/视频博主";
  outputInputs.bilibili.value = outputSubdirs.bilibili || "Bilibili/视频博主";
  outputInputs.tiktok.value = outputSubdirs.tiktok || "TikTok/视频博主";
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
    row.querySelector('[data-field="rss_url"]').value,
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
      platform_id: row.querySelector('[data-field="platform_id"]').value.trim(),
      weibo_uid: row.querySelector('[data-field="weibo_uid"]').value.trim(),
      weibo_custom: row.querySelector('[data-field="weibo_custom"]').value.trim(),
      xiaoyuzhou_pid: row.querySelector('[data-field="xiaoyuzhou_pid"]').value.trim(),
      xiaoyuzhou_eid: row.querySelector('[data-field="xiaoyuzhou_eid"]').value.trim(),
      wechat_biz: row.querySelector('[data-field="wechat_biz"]').value.trim(),
      rss_url: row.querySelector('[data-field="rss_url"]').value.trim(),
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
  const creators = readCreators().filter((creator) => creator.enabled);
  const runnable = creators.filter((creator) => isRunnablePlatform(creator.platform));
  const pending = creators.filter((creator) => !isRunnablePlatform(creator.platform));
  select.innerHTML = `<option value="">全部可抓取来源</option>`;
  const appendCreatorOption = (group, creator, disabled = false) => {
    const option = document.createElement("option");
    option.value = creator.key;
    option.disabled = disabled;
    const category = creator.category ? ` · ${creator.category}` : "";
    option.textContent = `[${platformLabel(creator.platform)}] ${creator.name}${category}${disabled ? " · 待接入抓取" : ""}`;
    group.appendChild(option);
  };
  if (runnable.length) {
    const group = document.createElement("optgroup");
    group.label = "可抓取";
    for (const creator of runnable) appendCreatorOption(group, creator);
    select.appendChild(group);
  }
  if (pending.length) {
    const group = document.createElement("optgroup");
    group.label = "已入库，待接入抓取";
    for (const creator of pending) appendCreatorOption(group, creator, true);
    select.appendChild(group);
  }
  if (current && runnable.some((creator) => creator.key === current)) {
    select.value = current;
  }
}

async function resolveCreator(row) {
  const urlInput = row.querySelector('[data-field="url"]');
  const url = urlInput.value.trim();
  if (!url) {
    throw new Error("请先填写主页 URL");
  }
  const platform = normalizePlatform(row.querySelector('[data-field="platform"]').value, url);
  row.querySelector('[data-field="platform"]').value = platform;
  if (!["douyin", "weibo", "xiaoyuzhou", "wechat"].includes(platform)) {
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
    row.querySelector('[data-field="platform_id"]').value = result.creator.platform_id || "";
    row.querySelector('[data-field="weibo_uid"]').value = result.creator.weibo_uid || "";
    row.querySelector('[data-field="weibo_custom"]').value = result.creator.weibo_custom || "";
    row.querySelector('[data-field="xiaoyuzhou_pid"]').value = result.creator.xiaoyuzhou_pid || "";
    row.querySelector('[data-field="xiaoyuzhou_eid"]').value = result.creator.xiaoyuzhou_eid || "";
    row.querySelector('[data-field="wechat_biz"]').value = result.creator.wechat_biz || "";
    row.querySelector('[data-field="rss_url"]').value = result.creator.rss_url || result.creator.feed_url || "";
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
  const outputInputs = outputSubdirInputs();
  const payload = {
    vault_path: $("vaultPath").value.trim(),
    output_subdirs: {
      douyin: outputInputs.douyin.value.trim(),
      weibo: outputInputs.weibo.value.trim(),
      xiaoyuzhou: outputInputs.xiaoyuzhou.value.trim(),
      wechat: outputInputs.wechat.value.trim(),
      youtube: outputInputs.youtube.value.trim(),
      bilibili: outputInputs.bilibili.value.trim(),
      tiktok: outputInputs.tiktok.value.trim(),
    },
  };
  payload.output_subdir = payload.output_subdirs.douyin;
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
    weibo_cookie: $("weiboCookieInput").value.trim(),
    deepseek_api_key: $("apiKeyInput").value.trim(),
  };
  const result = await api("/api/secrets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  $("cookieInput").value = "";
  $("weiboCookieInput").value = "";
  $("apiKeyInput").value = "";
  renderStatus(result.status);
  toast("密钥已保存");
}

async function startRun(options = {}) {
  if (state.lastRunning) {
    throw new Error("已有同步任务正在运行。请等当前任务完成，或先点击“停止任务”后再启动新的任务。");
  }
  await saveConfig(true);
  const payload = {
    creator: $("runCreator").value,
    force: $("forceRun").checked,
    skip_summary: !$("aiSummaryRun").checked,
    full_history: Boolean(options.fullHistory),
    dry_run: Boolean(options.dryRun),
  };
  const result = await api("/api/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  renderLogs(result.worker);
  toast(options.dryRun ? "候选内容嗅探已启动" : "任务已启动");
  await refreshStatus();
}

async function scanCandidateItems() {
  const creator = $("runCreator").value;
  if (!creator) {
    throw new Error("请先选择一个可抓取的内容源");
  }
  await startRun({ dryRun: true });
}

async function crawlSelectedCreatorAll() {
  const creator = $("runCreator").value;
  if (!creator) {
    throw new Error("请先选择一个可抓取的内容源");
  }
  const creatorLabel = $("runCreator").selectedOptions[0]?.textContent || creator;
  const confirmed = window.confirm(`将深度回溯 ${creatorLabel} 的历史内容，并生成 Markdown。微博会默认最多扫描约 350 页；已成功处理过的内容会自动跳过。继续吗？`);
  if (!confirmed) return;
  await startRun({ fullHistory: true });
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
    body: JSON.stringify({ run_id: runId, skip_summary: !$("aiSummaryRun").checked }),
  });
  renderLogs(result.worker);
  const retry = result.worker.retry;
  toast(`已启动失败重爬：${retry?.video_count || 0} 条`);
  await refreshStatus();
}

function rememberCandidateCheck(input) {
  const runId = input.dataset.runId || "";
  const videoId = input.dataset.videoId || "";
  if (!runId || !videoId) return;
  if (!state.candidateSelections[runId]) {
    state.candidateSelections[runId] = new Set();
  }
  if (input.checked) {
    state.candidateSelections[runId].add(videoId);
  } else {
    state.candidateSelections[runId].delete(videoId);
  }
}

async function runSelectedItems(button) {
  const runId = button.dataset.runId || "";
  if (!runId) {
    throw new Error("缺少运行编号，无法抓取选中内容");
  }
  const container = button.closest(".history-item") || document;
  const videoIds = Array.from(container.querySelectorAll('[data-role="candidate"]:checked'))
    .map((input) => input.dataset.videoId || "")
    .filter(Boolean);
  if (!videoIds.length) {
    throw new Error("请先勾选至少一条候选内容");
  }
  const confirmed = window.confirm(`将只抓取已勾选的 ${videoIds.length} 条内容。继续吗？`);
  if (!confirmed) return;
  const result = await api("/api/run/selected", {
    method: "POST",
    body: JSON.stringify({
      run_id: runId,
      video_ids: videoIds,
      force: $("forceRun").checked,
      skip_summary: !$("aiSummaryRun").checked,
    }),
  });
  renderLogs(result.worker);
  toast(`已启动选中内容抓取：${result.worker.selected?.video_count || videoIds.length} 条`);
  await refreshStatus();
}

function setCandidateChecks(button, checked) {
  const container = button.closest(".history-item") || document;
  for (const input of container.querySelectorAll('[data-role="candidate"]')) {
    input.checked = checked;
    rememberCandidateCheck(input);
  }
}

async function openTarget(target) {
  const result = await api("/api/open", {
    method: "POST",
    body: JSON.stringify({ target }),
  });
  toast(`已打开：${result.opened.path}`);
}

function renderCreativeResult(draft) {
  const sources = draft.sources || [];
  $("creativeResult").className = "creative-result";
  $("creativeResult").innerHTML = `
    <div class="result-meta">
      <strong>已生成：${escapeHtml(draft.format_label || "")}</strong>
      <span>文件：${escapeHtml(draft.path || "")}</span>
      <span>模型：${escapeHtml(draft.model || "")} · 素材 ${formatCount(draft.source_count)} 条</span>
      <span>来源：${sources.map((source) => `${source.platform_label} · ${source.title}`).map(escapeHtml).join("；") || "无"}</span>
    </div>
    <pre>${escapeHtml(draft.preview || "")}</pre>
  `;
}

async function generateCreativeDraft() {
  const button = $("generateCreative");
  button.disabled = true;
  button.textContent = "生成中";
  try {
    const payload = {
      topic: $("creativeTopic").value.trim(),
      platform: $("creativePlatform").value,
      format: $("creativeFormat").value,
      limit: Number($("creativeLimit").value || 8),
    };
    const result = await api("/api/creative/generate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderCreativeResult(result.draft);
    toast("二创草稿已生成");
  } finally {
    button.disabled = false;
    button.textContent = "生成二创草稿";
  }
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
$("scanCandidates").addEventListener("click", () => scanCandidateItems().catch((error) => toast(error.message)));
$("crawlCreatorAll").addEventListener("click", () => crawlSelectedCreatorAll().catch((error) => toast(error.message)));
$("stopRun").addEventListener("click", () => stopRun().catch((error) => toast(error.message)));
$("openOutput").addEventListener("click", () => openTarget("output").catch((error) => toast(error.message)));
$("openCreative").addEventListener("click", () => openTarget("creative").catch((error) => toast(error.message)));
$("generateCreative").addEventListener("click", () => generateCreativeDraft().catch((error) => toast(error.message)));
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
$("historyList").addEventListener("change", (event) => {
  const target = event.target;
  if (target && target.matches && target.matches('[data-role="candidate"]')) {
    rememberCandidateCheck(target);
  }
});
$("historyList").addEventListener("click", (event) => {
  const target = event.target;
  const button = target && target.closest ? target.closest("[data-action]") : null;
  if (button) {
    const action = button.dataset.action || "";
    if (action === "retry-failed") {
      retryFailedRun(button.dataset.runId || "").catch((error) => toast(error.message));
    } else if (action === "run-selected") {
      runSelectedItems(button).catch((error) => toast(error.message));
    } else if (action === "select-candidates") {
      setCandidateChecks(button, true);
    } else if (action === "clear-candidates") {
      setCandidateChecks(button, false);
    }
    return;
  }
  const row = target && target.closest ? target.closest(".run-item.selectable") : null;
  if (row && !target.closest("input, label, button, a, summary")) {
    const input = row.querySelector('[data-role="candidate"]');
    if (input) {
      input.checked = !input.checked;
      rememberCandidateCheck(input);
    }
  }
});

loadAll().catch((error) => toast(error.message));
state.statusTimer = setInterval(() => {
  refreshStatus().catch(() => {});
}, 3000);
