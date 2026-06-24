const state = {
  config: null,
  statusTimer: null,
  lastRunning: false,
  lastReturnCode: null,
  candidateSelections: {},
  runCreatorQuery: "",
  runItemPages: {},
  runItemOpen: {},
  inlineRunItems: {},
};

const $ = (id) => document.getElementById(id);

const PLATFORM_META = {
  douyin: { label: "抖音", runnable: true, defaultTags: ["douyin", "口播"] },
  weibo: { label: "微博", runnable: true, defaultTags: ["weibo", "文字"] },
  xiaoyuzhou: { label: "小宇宙", runnable: true, defaultTags: ["xiaoyuzhou", "播客"] },
  wechat: { label: "公众号", runnable: true, defaultTags: ["wechat", "公众号"] },
  xiaohongshu: { label: "小红书", runnable: true, defaultTags: ["xiaohongshu", "图文"] },
  youtube: { label: "YouTube", runnable: false, defaultTags: ["youtube", "视频"] },
  bilibili: { label: "B站", runnable: false, defaultTags: ["bilibili", "视频"] },
  tiktok: { label: "TikTok", runnable: false, defaultTags: ["tiktok", "视频"] },
  kuaishou: { label: "快手", runnable: false, defaultTags: ["kuaishou", "视频"] },
  tieba: { label: "贴吧", runnable: false, defaultTags: ["tieba", "帖子"] },
  zhihu: { label: "知乎", runnable: false, defaultTags: ["zhihu", "问答"] },
};

function inferPlatformFromUrl(url) {
  const text = String(url || "").toLowerCase();
  if (text.includes("mp.weixin.qq.com") || text.includes("weixin.qq.com")) return "wechat";
  if (text.includes("xiaohongshu.com") || text.includes("xhslink.com")) return "xiaohongshu";
  if (text.includes("xiaoyuzhoufm.com") || text.includes("feed.xyzfm.space") || text.includes("podcast.xyz")) return "xiaoyuzhou";
  if (text.includes("weibo.com") || text.includes("weibo.cn")) return "weibo";
  if (text.includes("kuaishou.com") || text.includes("gifshow.com")) return "kuaishou";
  if (text.includes("tieba.baidu.com")) return "tieba";
  if (text.includes("zhihu.com")) return "zhihu";
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

function clampPercent(value) {
  return Math.max(0, Math.min(100, Number(value || 0)));
}

function inferPercentFromLabel(label) {
  const match = String(label || "").match(/(\d+)\s*\/\s*(\d+)/);
  if (!match) return 0;
  const current = Number(match[1] || 0);
  const total = Number(match[2] || 0);
  if (!total) return 0;
  return Math.max(0, Math.min(99, Math.floor((current / total) * 100)));
}

function progressPercent(progress) {
  const explicit = clampPercent(progress?.percent);
  return explicit || inferPercentFromLabel(progress?.label);
}

function shortTaskLabel(progress) {
  if (!progress) return "";
  const creator = String(progress.current_creator || "").trim();
  const label = String(progress.label || "").trim();
  if (creator) return creator;
  return label
    .replace(/^正在运行[:：]\s*/, "")
    .replace(/^正在扫描\s*/, "")
    .replace(/^正在处理\s*/, "")
    .replace(/^最近任务[:：]\s*/, "")
    .split("：")[0]
    .trim();
}

function renderWorkerBadge(worker) {
  const progress = worker.progress || {};
  const percent = progressPercent(progress);
  const task = shortTaskLabel(progress);
  const suffix = task ? ` · ${task}` : "";
  const el = $("workerBadge");
  el.title = progress.label || "";
  if (worker.running) {
    badge(el, `运行中 ${percent}%${suffix}`, "warn");
  } else if (worker.returncode === 0) {
    badge(el, `完成 ${percent || 100}%${suffix}`, "ok");
  } else if (worker.returncode === null || worker.returncode === undefined) {
    badge(el, "空闲", "");
  } else {
    badge(el, `失败${suffix}`, "bad");
  }
}

function browserLoginText(label, cookie, login) {
  if (login?.running) return `${label}登录窗口已打开，请在 Chrome 中完成扫码或验证`;
  if (cookie?.ready) return `${label} Cookie 已保存；抓取时会使用当前 Chrome 插件导入的登录态`;
  if (login?.status === "opened_system_chrome") return login.message || `已打开${label}登录页；登录后请用 Chrome 插件导入 Cookie`;
  if (login?.status === "timeout") return login.message || `${label}扫码登录超时，请重试`;
  if (login?.status === "missing_dependency") return login.message || "Playwright 未安装";
  if (login?.profile_exists) return `${label}浏览器资料已保存，但还没有可用登录态`;
  return `建议点击“扫码登录”完成${label}登录`;
}

function renderStatus(status) {
  const douyinCookie = status.accounts?.douyin?.cookie || status.cookie || {};
  const weiboCookie = status.accounts?.weibo?.cookie || {};
  const wechatCookie = status.accounts?.wechat?.cookie || {};
  const xiaohongshuCookie = status.accounts?.xiaohongshu?.cookie || {};
  const douyinLogin = status.accounts?.douyin?.browser_login || {};
  const xiaohongshuLogin = status.accounts?.xiaohongshu?.browser_login || {};
  badge($("douyinCookieBadge"), douyinCookie.ready ? "抖音 Cookie 已保存" : "抖音未登录", douyinCookie.ready ? "ok" : "bad");
  badge($("weiboCookieBadge"), weiboCookie.ready ? "微博 Cookie OK" : "微博 Cookie 缺失", weiboCookie.ready ? "ok" : "");
  badge($("wechatCookieBadge"), wechatCookie.ready ? "公众号后台 OK" : "公众号后台缺失", wechatCookie.ready ? "ok" : "");
  badge($("xiaohongshuCookieBadge"), xiaohongshuCookie.ready ? "小红书 Cookie 已保存" : "小红书未登录", xiaohongshuCookie.ready ? "ok" : "");
  $("douyinAccountText").textContent = browserLoginText("抖音", douyinCookie, douyinLogin);
  $("weiboAccountText").textContent = weiboCookie.ready ? "Cookie 已保存，可用于微博 URL 补全和文本抓取" : "未导入 Cookie；微博抓取需要先导入";
  $("wechatAccountText").textContent = wechatCookie.ready ? "公众号后台登录态已保存，可按名称搜索并同步历史文章" : "未导入公众号后台登录态；按名称抓取需要先导入";
  $("xiaohongshuAccountText").textContent = browserLoginText("小红书", xiaohongshuCookie, xiaohongshuLogin);
  badge($("keyBadge"), status.deepseek.ready ? "DeepSeek OK" : "DeepSeek 缺失", status.deepseek.ready ? "ok" : "bad");
  renderWorkerBadge(status.worker || {});
  setRunControlsRunning(Boolean(status.worker.running));
  $("subtitle").textContent = `${status.output.path}`;
}

function setRunControlsRunning(running) {
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

function runItemPanelStateKey(runId, status) {
  return `${runId || ""}:${status || "dry_run"}`;
}

function candidateCountForRun(run) {
  if (!run) return 0;
  if (run.candidate_cache?.candidate_count) return Number(run.candidate_cache.candidate_count || 0);
  if (run.dry_count) return Number(run.dry_count || 0);
  return Array.isArray(run.items) ? run.items.filter((item) => item.status === "dry_run").length : 0;
}

function sameCreatorRun(a, b) {
  const aKey = String(a?.creator_filter || "").trim();
  const bKey = String(b?.creator_filter || "").trim();
  if (aKey && bKey && aKey === bKey) return true;
  const aCreator = String(a?.current_creator || "").trim();
  const bCreator = String(b?.current_creator || "").trim();
  return Boolean(aCreator && bCreator && aCreator === bCreator);
}

function candidateCacheFromHistory(run, history) {
  if (run?.candidate_cache?.run_id) return run.candidate_cache;
  const found = (history || []).find((item) => sameCreatorRun(run, item) && candidateCountForRun(item) > 0);
  if (!found) return {};
  return {
    run_id: found.run_id || "",
    started_at: found.started_at || "",
    candidate_count: candidateCountForRun(found),
  };
}

function renderRunMeta(worker) {
  const history = worker.history || [];
  for (const run of history) {
    if (run.run_id && Array.isArray(run.items) && run.items.length) {
      state.inlineRunItems[run.run_id] = run.items;
    }
  }
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
    ${renderLatestRun(latest, planned, failed, history)}
    <details class="history-archive">
      <summary>历史任务 ${older.length}</summary>
      <div class="archive-list">
        ${older.length ? older.map(renderArchiveRun).join("") : `<p class="empty">暂无更早任务</p>`}
      </div>
    </details>
  `;
  loadOpenRunItemPanels();
}

function renderLatestRun(run, planned, failed, history = []) {
  const runId = run.run_id || "";
  if (runId && Array.isArray(run.items) && run.items.length) {
    state.inlineRunItems[runId] = run.items;
  }
  const candidateCache = candidateCacheFromHistory(run, history);
  const candidateRunId = candidateCache.run_id || runId;
  const candidateCount = Number(candidateCache.candidate_count || run.dry_count || 0);
  const candidateDate = candidateCache.started_at ? ` · 保存于 ${candidateCache.started_at}` : "";
  const attentionCount = Number(run.failed_count || 0) + Number(run.pending_count || 0) + Number(run.running_count || 0);
  const foldedCount = Number(run.success_count || 0) + Number(run.skipped_count || 0);
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
        ${candidateCount ? `<button type="button" class="secondary small" data-action="run-selected" data-run-id="${escapeHtml(candidateRunId)}">抓取已勾选内容</button>` : ""}
        ${failed ? `<button type="button" class="secondary small" data-action="retry-failed" data-run-id="${escapeHtml(runId)}">重爬失败项 ${failed}</button>` : ""}
      </div>
      ${candidateCount ? renderRunItemsPanel(candidateRunId, "dry_run", `已保存候选 ${candidateCount}${candidateDate}`, { selectable: true }) : ""}
      ${renderRunItemsPanel(runId, "attention", `需要关注 ${attentionCount}`, { open: true, empty: "暂无失败、等待或进行中的内容" })}
      ${renderRunItemsPanel(runId, "done", `已成功 / 已跳过 ${foldedCount}`, { folded: true, empty: "暂无已完成或跳过的内容" })}
    </div>
  `;
}

function renderRunItemsPanel(runId, status, title, options = {}) {
  const stateKey = runItemPanelStateKey(runId, status);
  const saved = state.runItemPages[stateKey] || {};
  const isOpen = state.runItemOpen[stateKey] ?? Boolean(options.open);
  const query = saved.query || "";
  const page = saved.page || 1;
  return `
    <details class="run-items ${options.folded ? "folded" : ""} ${status}" data-run-id="${escapeHtml(runId)}" data-status="${escapeHtml(status)}" ${isOpen ? "open" : ""}>
      <summary>${escapeHtml(title)}</summary>
      <div class="item-tools paged">
        <input data-role="item-search" type="search" value="${escapeHtml(query)}" placeholder="搜索标题 / ID / 来源">
        <button type="button" class="secondary small" data-action="load-items" data-page="1">搜索</button>
        ${options.selectable ? `<button type="button" class="secondary small" data-action="select-candidates">选择本页</button>
        <button type="button" class="secondary small" data-action="select-all-candidates">选择全部候选</button>
        <button type="button" class="secondary small" data-action="clear-candidates">清空本页</button>` : ""}
      </div>
      <div class="item-page-meta" data-role="item-page-meta">第 ${page} 页</div>
      <div class="item-table" data-role="run-items-page">打开后加载明细...</div>
      <div class="item-pager">
        <button type="button" class="secondary small" data-action="prev-items">上一页</button>
        <button type="button" class="secondary small" data-action="next-items">下一页</button>
      </div>
    </details>
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

async function loadRunItemsPanel(panel, page = null) {
  const runId = panel.dataset.runId || "";
  const status = panel.dataset.status || "dry_run";
  if (!runId) return;
  const stateKey = runItemPanelStateKey(runId, status);
  const saved = state.runItemPages[stateKey] || {};
  const queryInput = panel.querySelector('[data-role="item-search"]');
  const query = (queryInput?.value || saved.query || "").trim();
  const nextPage = Math.max(1, Number(page || saved.page || 1));
  state.runItemPages[stateKey] = { page: nextPage, query };
  const table = panel.querySelector('[data-role="run-items-page"]');
  const meta = panel.querySelector('[data-role="item-page-meta"]');
  table.textContent = "加载中...";
  const params = new URLSearchParams({
    run_id: runId,
    status,
    page: String(nextPage),
    page_size: "50",
    query,
  });
  let pageData = null;
  let fallbackError = null;
  try {
    const result = await api(`/api/run/items?${params.toString()}`);
    pageData = result.items || {};
  } catch (error) {
    fallbackError = error;
    pageData = inlineRunItemsPage(runId, status, nextPage, 50, query);
    if (!pageData && String(error.message || "") === "not_found") {
      pageData = { run_id: runId, status, query, page: nextPage, page_size: 50, total: 0, items: [] };
    }
    if (!pageData) throw fallbackError;
  }
  const items = pageData.items || [];
  const total = Number(pageData.total || 0);
  const pageSize = Number(pageData.page_size || 50);
  const currentPage = Number(pageData.page || nextPage);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  state.runItemPages[stateKey] = { page: currentPage, query };
  const selectable = status === "dry_run";
  meta.textContent = `共 ${total} 条 · 第 ${currentPage}/${totalPages} 页 · 每页 ${pageSize} 条`;
  if (!items.length) {
    table.innerHTML = `<p class="empty">没有匹配的内容</p>`;
  } else {
    table.innerHTML = items.map((item) => renderRunItem(item, { selectable, runId })).join("");
  }
  const prev = panel.querySelector('[data-action="prev-items"]');
  const next = panel.querySelector('[data-action="next-items"]');
  if (prev) prev.disabled = currentPage <= 1;
  if (next) next.disabled = currentPage >= totalPages;
}

function inlineRunItemsPage(runId, status, page, pageSize, query) {
  const source = state.inlineRunItems[runId] || [];
  if (!source.length) return null;
  const normalizedQuery = String(query || "").trim().toLowerCase();
  const filtered = source.filter((item) => {
    const itemStatus = String(item.status || "");
    const matchedStatus = status === "attention"
      ? ["failed", "pending", "running"].includes(itemStatus)
      : status === "done"
        ? ["success", "skipped"].includes(itemStatus)
        : status === "all" || itemStatus === status;
    if (!matchedStatus) return false;
    if (!normalizedQuery) return true;
    return [
      item.title,
      item.video_id,
      item.creator_name,
    ].join(" ").toLowerCase().includes(normalizedQuery);
  });
  const start = (page - 1) * pageSize;
  return {
    run_id: runId,
    status,
    query,
    page,
    page_size: pageSize,
    total: filtered.length,
    items: filtered.slice(start, start + pageSize),
  };
}

async function selectAllCandidateItems(button) {
  const panel = button.closest(".run-items");
  if (!panel) return;
  const runId = panel.dataset.runId || "";
  const query = (panel.querySelector('[data-role="item-search"]')?.value || "").trim();
  if (!runId) throw new Error("缺少运行编号");
  const params = new URLSearchParams({ run_id: runId, status: "dry_run", query });
  let ids = [];
  try {
    const result = await api(`/api/run/item-ids?${params.toString()}`);
    ids = result.items?.video_ids || [];
  } catch (error) {
    const page = inlineRunItemsPage(runId, "dry_run", 1, 100000, query);
    if (!page) throw error;
    ids = page.items.map((item) => item.video_id).filter(Boolean);
  }
  if (!state.candidateSelections[runId]) {
    state.candidateSelections[runId] = new Set();
  }
  for (const id of ids) {
    state.candidateSelections[runId].add(String(id));
  }
  for (const input of panel.querySelectorAll('[data-role="candidate"]')) {
    input.checked = state.candidateSelections[runId].has(String(input.dataset.videoId || ""));
  }
  toast(`已选择 ${ids.length} 条候选内容`);
}

function loadOpenRunItemPanels() {
  for (const panel of document.querySelectorAll(".run-items[open]")) {
    const table = panel.querySelector('[data-role="run-items-page"]');
    if (table && table.textContent.includes("加载")) {
      loadRunItemsPanel(panel).catch((error) => toast(error.message));
    }
  }
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
  const percent = progressPercent(progress);
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
    <input data-field="manual_urls" type="hidden" value="${escapeHtml((creator.manual_urls || []).join("\n"))}">
    <input data-field="manual_items" type="hidden" value="${escapeHtml(JSON.stringify(creator.manual_items || []))}">
    <input data-field="weibo_uid" type="hidden" value="${escapeHtml(creator.weibo_uid || "")}">
    <input data-field="weibo_custom" type="hidden" value="${escapeHtml(creator.weibo_custom || "")}">
    <input data-field="xiaoyuzhou_pid" type="hidden" value="${escapeHtml(creator.xiaoyuzhou_pid || "")}">
    <input data-field="xiaoyuzhou_eid" type="hidden" value="${escapeHtml(creator.xiaoyuzhou_eid || "")}">
    <input data-field="wechat_biz" type="hidden" value="${escapeHtml(creator.wechat_biz || "")}">
    <input data-field="wechat_fakeid" type="hidden" value="${escapeHtml(creator.wechat_fakeid || "")}">
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
    xiaohongshu: $("outputSubdirXiaohongshu"),
    youtube: $("outputSubdirYoutube"),
    bilibili: $("outputSubdirBilibili"),
    tiktok: $("outputSubdirTiktok"),
    kuaishou: $("outputSubdirKuaishou"),
    tieba: $("outputSubdirTieba"),
    zhihu: $("outputSubdirZhihu"),
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
  outputInputs.xiaohongshu.value = outputSubdirs.xiaohongshu || "Xiaohongshu/内容源";
  outputInputs.youtube.value = outputSubdirs.youtube || "YouTube/视频博主";
  outputInputs.bilibili.value = outputSubdirs.bilibili || "Bilibili/视频博主";
  outputInputs.tiktok.value = outputSubdirs.tiktok || "TikTok/视频博主";
  outputInputs.kuaishou.value = outputSubdirs.kuaishou || "Kuaishou/视频博主";
  outputInputs.tieba.value = outputSubdirs.tieba || "Tieba/贴吧";
  outputInputs.zhihu.value = outputSubdirs.zhihu || "Zhihu/内容源";
  const retention = config.retention || {};
  $("keepVideo").checked = Boolean(retention.keep_video);
  $("keepAudio").checked = Boolean(retention.keep_audio);
  $("saveTranscriptTxt").checked = Boolean(retention.save_transcript_txt);
  $("saveSourceRaw").checked = Boolean(retention.save_source_raw);
  $("includeTranscriptInMarkdown").checked = retention.include_transcript_in_markdown !== false;
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
      manual_urls: row.querySelector('[data-field="manual_urls"]').value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
      manual_items: JSON.parse(row.querySelector('[data-field="manual_items"]').value || "[]"),
      weibo_uid: row.querySelector('[data-field="weibo_uid"]').value.trim(),
      weibo_custom: row.querySelector('[data-field="weibo_custom"]').value.trim(),
      xiaoyuzhou_pid: row.querySelector('[data-field="xiaoyuzhou_pid"]').value.trim(),
      xiaoyuzhou_eid: row.querySelector('[data-field="xiaoyuzhou_eid"]').value.trim(),
      wechat_biz: row.querySelector('[data-field="wechat_biz"]').value.trim(),
      wechat_fakeid: row.querySelector('[data-field="wechat_fakeid"]').value.trim(),
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

function creatorRunSearchText(creator) {
  return [
    creator.key,
    creator.platform,
    platformLabel(creator.platform),
    creator.name,
    creator.url,
    creator.category,
    creator.language,
    creator.content_type,
    creator.cookie_profile,
    ...(creator.tags || []),
  ].join(" ").toLowerCase();
}

function creatorDisplayLabel(creator) {
  if (!creator) return "";
  const category = creator.category ? ` · ${creator.category}` : "";
  return `[${platformLabel(creator.platform)}] ${creator.name || creator.key}${category}`;
}

function selectedRunCreator() {
  const selectedKey = $("runCreator").value;
  if (!selectedKey) return null;
  return readCreators().find((creator) => creator.enabled && creator.key === selectedKey) || null;
}

function renderCreatorSelect() {
  const hidden = $("runCreator");
  const search = $("runCreatorSearch");
  const picker = $("runCreatorPicker");
  const selected = $("runCreatorSelected");
  const current = hidden.value;
  const query = (search?.value || state.runCreatorQuery || "").trim().toLowerCase();
  state.runCreatorQuery = query;
  const creators = readCreators().filter((creator) => creator.enabled && creator.key);
  const runnable = creators.filter((creator) => isRunnablePlatform(creator.platform));
  const selectedCreator = runnable.find((creator) => creator.key === current) || null;
  if (current && !selectedCreator) {
    hidden.value = "";
  }
  selected.textContent = selectedCreator ? `已选择：${creatorDisplayLabel(selectedCreator)}` : "未选择内容源";
  selected.className = `run-source-selected ${selectedCreator ? "ok" : ""}`.trim();
  const filtered = creators.filter((creator) => !query || creatorRunSearchText(creator).includes(query));
  picker.innerHTML = "";
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "run-source-empty";
    empty.textContent = creators.length ? "没有匹配的内容源" : "暂无已启用内容源";
    picker.appendChild(empty);
    return;
  }
  for (const creator of filtered) {
    const runnablePlatform = isRunnablePlatform(creator.platform);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-source-item ${creator.key === hidden.value ? "active" : ""} ${runnablePlatform ? "" : "disabled"}`.trim();
    button.disabled = !runnablePlatform;
    button.dataset.key = creator.key;
    const tags = (creator.tags || []).slice(0, 4).join(" / ");
    const meta = [
      creator.category || "未分类",
      creator.content_type || "内容",
      tags,
    ].filter(Boolean).join(" · ");
    button.innerHTML = `
      <span class="platform-pill ${escapeHtml(normalizePlatform(creator.platform))}">${escapeHtml(platformLabel(creator.platform))}</span>
      <span class="run-source-main">
        <strong>${escapeHtml(creator.name || creator.key)}</strong>
        <small>${escapeHtml(meta)}</small>
      </span>
      <span class="badge ${runnablePlatform ? "ok" : "warn"}">${runnablePlatform ? "可抓取" : "待接入"}</span>
    `;
    button.addEventListener("click", () => {
      hidden.value = creator.key;
      if (normalizePlatform(creator.platform) === "xiaohongshu") {
        $("aiSummaryRun").checked = false;
      }
      renderCreatorSelect();
    });
    picker.appendChild(button);
  }
}

async function resolveCreator(row) {
  const urlInput = row.querySelector('[data-field="url"]');
  const nameInput = row.querySelector('[data-field="name"]');
  const url = urlInput.value.trim();
  const platform = normalizePlatform(row.querySelector('[data-field="platform"]').value, url);
  row.querySelector('[data-field="platform"]').value = platform;
  const query = platform === "wechat" ? (nameInput.value.trim() || url) : url;
  if (!query) {
    throw new Error(platform === "wechat" ? "请先在名称栏填写公众号名称" : "请先填写主页 URL");
  }
  if (!["douyin", "weibo", "xiaoyuzhou", "wechat", "xiaohongshu"].includes(platform)) {
    throw new Error(`${platformLabel(platform)} URL 补全和抓取适配器下一阶段接入。现在可以先手动保存为内容源。`);
  }
  const button = row.querySelector('[data-action="resolve"]');
  button.disabled = true;
  button.textContent = "补全中";
  try {
    const result = await api("/api/creator/resolve", {
      method: "POST",
      body: JSON.stringify({ url: query, platform }),
    });
    row.querySelector('[data-field="key"]').value = result.creator.key || "";
    row.querySelector('[data-field="platform"]').value = normalizePlatform(result.creator.platform, result.creator.url || query);
    row.querySelector('[data-field="name"]').value = result.creator.name || "";
    row.querySelector('[data-field="url"]').value = result.creator.url || url;
    row.querySelector('[data-field="sec_user_id"]').value = result.creator.sec_user_id || "";
    row.querySelector('[data-field="platform_id"]').value = result.creator.platform_id || "";
    row.querySelector('[data-field="manual_urls"]').value = (result.creator.manual_urls || []).join("\n");
    row.querySelector('[data-field="manual_items"]').value = JSON.stringify(result.creator.manual_items || []);
    row.querySelector('[data-field="weibo_uid"]').value = result.creator.weibo_uid || "";
    row.querySelector('[data-field="weibo_custom"]').value = result.creator.weibo_custom || "";
    row.querySelector('[data-field="xiaoyuzhou_pid"]').value = result.creator.xiaoyuzhou_pid || "";
    row.querySelector('[data-field="xiaoyuzhou_eid"]').value = result.creator.xiaoyuzhou_eid || "";
    row.querySelector('[data-field="wechat_biz"]').value = result.creator.wechat_biz || "";
    row.querySelector('[data-field="wechat_fakeid"]').value = result.creator.wechat_fakeid || "";
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
    toast(`内容源信息已补全：${result.creator.name || "未命名来源"}`);
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
      xiaohongshu: outputInputs.xiaohongshu.value.trim(),
      youtube: outputInputs.youtube.value.trim(),
      bilibili: outputInputs.bilibili.value.trim(),
      tiktok: outputInputs.tiktok.value.trim(),
      kuaishou: outputInputs.kuaishou.value.trim(),
      tieba: outputInputs.tieba.value.trim(),
      zhihu: outputInputs.zhihu.value.trim(),
    },
    retention: {
      keep_video: $("keepVideo").checked,
      keep_audio: $("keepAudio").checked,
      save_transcript_txt: $("saveTranscriptTxt").checked,
      save_source_raw: $("saveSourceRaw").checked,
      include_transcript_in_markdown: $("includeTranscriptInMarkdown").checked,
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

async function startBrowserLogin(platform) {
  const result = await api("/api/browser-login/start", {
    method: "POST",
    body: JSON.stringify({ platform }),
  });
  toast(result.started ? "已打开扫码登录窗口" : "扫码登录窗口已在运行");
  const status = await api("/api/status");
  renderStatus(status.status);
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
  toast(options.dryRun ? "全量候选内容嗅探已启动" : "任务已启动");
  await refreshStatus();
}

async function scanCandidateItems() {
  const creator = $("runCreator").value;
  if (!creator) {
    throw new Error("请先选择一个可抓取的内容源");
  }
  await startRun({ dryRun: true, fullHistory: true });
}

function findSavedCandidateCacheForCreator(history, creator) {
  if (!creator) return {};
  const target = {
    creator_filter: creator.key,
    current_creator: creator.name,
  };
  for (const run of history || []) {
    const cache = candidateCacheFromHistory(run, history);
    if (!cache.run_id || !Number(cache.candidate_count || 0)) continue;
    if (
      sameCreatorRun(target, run)
      || sameCreatorRun(target, cache)
      || String(cache.creator_filter || "") === creator.key
      || String(cache.current_creator || "") === creator.name
    ) {
      return cache;
    }
  }
  return {};
}

async function latestSavedCandidateCacheForCreator(history, creator) {
  const fallback = findSavedCandidateCacheForCreator(history, creator);
  if (!creator) return fallback;
  const params = new URLSearchParams({
    creator: creator.key || "",
    name: creator.name || "",
  });
  try {
    const result = await api(`/api/run/candidate-cache?${params.toString()}`);
    return result.cache?.run_id ? result.cache : fallback;
  } catch {
    return fallback;
  }
}

async function allCandidateIdsForRun(runId) {
  const params = new URLSearchParams({ run_id: runId, status: "dry_run" });
  const result = await api(`/api/run/item-ids?${params.toString()}`);
  return (result.items?.video_ids || []).map((item) => String(item)).filter(Boolean);
}

async function startSavedCandidateRun(cache, creatorLabel) {
  const runId = cache.run_id || "";
  if (!runId) return false;
  const videoIds = await allCandidateIdsForRun(runId);
  if (!videoIds.length) return false;
  const confirmed = window.confirm(`已找到 ${creatorLabel} 上次保存的候选内容 ${videoIds.length} 条。本次会直接抓取这些候选，不重新全量嗅探。继续吗？`);
  if (!confirmed) return true;
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
  toast(`已按已保存候选启动抓取：${result.worker.selected?.video_count || videoIds.length} 条`);
  await refreshStatus();
  return true;
}

async function crawlSelectedCreatorAll() {
  const creator = $("runCreator").value;
  if (!creator) {
    throw new Error("请先选择一个可抓取的内容源");
  }
  const selectedCreator = selectedRunCreator();
  const creatorLabel = creatorDisplayLabel(selectedCreator) || creator;
  await saveConfig(true);
  const status = await api("/api/status");
  const cache = await latestSavedCandidateCacheForCreator(status.status.worker?.history || [], selectedCreator);
  if (await startSavedCandidateRun(cache, creatorLabel)) {
    return;
  }
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
  const videoIds = Array.from(state.candidateSelections[runId] || new Set()).filter(Boolean);
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
$("douyinQrLogin").addEventListener("click", () => startBrowserLogin("douyin").catch((error) => toast(error.message)));
$("xiaohongshuQrLogin").addEventListener("click", () => startBrowserLogin("xiaohongshu").catch((error) => toast(error.message)));
$("refresh").addEventListener("click", () => refreshStatus().catch((error) => toast(error.message)));
$("scanCandidates").addEventListener("click", () => scanCandidateItems().catch((error) => toast(error.message)));
$("crawlCreatorAll").addEventListener("click", () => crawlSelectedCreatorAll().catch((error) => toast(error.message)));
$("stopRun").addEventListener("click", () => stopRun().catch((error) => toast(error.message)));
$("openOutput").addEventListener("click", () => openTarget("output").catch((error) => toast(error.message)));
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
$("runCreatorSearch").addEventListener("input", renderCreatorSelect);
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
    } else if (action === "select-all-candidates") {
      selectAllCandidateItems(button).catch((error) => toast(error.message));
    } else if (action === "clear-candidates") {
      setCandidateChecks(button, false);
    } else if (action === "load-items") {
      const panel = button.closest(".run-items");
      if (panel) loadRunItemsPanel(panel, Number(button.dataset.page || 1)).catch((error) => toast(error.message));
    } else if (action === "prev-items" || action === "next-items") {
      const panel = button.closest(".run-items");
      if (panel) {
        const stateKey = runItemPanelStateKey(panel.dataset.runId || "", panel.dataset.status || "");
        const current = state.runItemPages[stateKey]?.page || 1;
        loadRunItemsPanel(panel, action === "prev-items" ? current - 1 : current + 1).catch((error) => toast(error.message));
      }
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
$("historyList").addEventListener("toggle", (event) => {
  const panel = event.target;
  if (panel && panel.matches && panel.matches(".run-items")) {
    const stateKey = runItemPanelStateKey(panel.dataset.runId || "", panel.dataset.status || "");
    state.runItemOpen[stateKey] = panel.open;
    if (panel.open) {
      loadRunItemsPanel(panel).catch((error) => toast(error.message));
    }
  }
}, true);
$("historyList").addEventListener("keydown", (event) => {
  const target = event.target;
  if (event.key === "Enter" && target && target.matches && target.matches('[data-role="item-search"]')) {
    const panel = target.closest(".run-items");
    if (panel) {
      event.preventDefault();
      loadRunItemsPanel(panel, 1).catch((error) => toast(error.message));
    }
  }
});

loadAll().catch((error) => toast(error.message));
state.statusTimer = setInterval(() => {
  refreshStatus().catch(() => {});
}, 3000);
