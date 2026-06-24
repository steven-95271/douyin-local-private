const els = {
  count: document.getElementById("count"),
  collect: document.getElementById("collect"),
  scrollCollect: document.getElementById("scrollCollect"),
  send: document.getElementById("send"),
  export: document.getElementById("export"),
  importCookie: document.getElementById("importCookie"),
  importWeiboCookie: document.getElementById("importWeiboCookie"),
  importWechatCookie: document.getElementById("importWechatCookie"),
  importXiaohongshuCookie: document.getElementById("importXiaohongshuCookie"),
  importXiaohongshuProfile: document.getElementById("importXiaohongshuProfile"),
  openDashboard: document.getElementById("openDashboard"),
  urls: document.getElementById("urls"),
  status: document.getElementById("status"),
  rounds: document.getElementById("rounds"),
  watermark: document.getElementById("watermark"),
  start: document.getElementById("start"),
  includeImages: document.getElementById("includeImages")
};

let currentUrls = [];
let currentItemsByUrl = new Map();

function setStatus(text) {
  els.status.textContent = text;
}

function uniq(values) {
  return Array.from(new Set(values.filter(Boolean)));
}

function render(urls, items = []) {
  for (const item of items) {
    if (!item || !item.url) continue;
    const existing = currentItemsByUrl.get(item.url) || {};
    currentItemsByUrl.set(item.url, {
      url: item.url,
      title: item.title || existing.title || "",
    });
  }
  currentUrls = uniq(urls);
  els.count.textContent = String(currentUrls.length);
  els.urls.value = currentUrls.join("\n");
  chrome.storage.local.set({ urls: currentUrls, items: Array.from(currentItemsByUrl.values()) });
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) throw new Error("没有可用的当前标签页");
  return tab;
}

async function collect(scroll = false) {
  setStatus(scroll ? "滚动收集中..." : "收集中...");
  const tab = await activeTab();
  const response = await chrome.tabs.sendMessage(tab.id, {
    type: "collect",
    scroll,
    rounds: Number(els.rounds.value || 12),
    delayMs: 1800
  });

  if (!response || !response.ok) {
    throw new Error(response && response.error ? response.error : "收集失败，请确认当前页是 douyin.com");
  }

  render(uniq([...currentUrls, ...(response.urls || [])]), response.items || []);
  setStatus(`已收集 ${currentUrls.length} 条`);
}

async function sendToLocal() {
  const urls = uniq(els.urls.value.split(/\r?\n/).map((line) => line.trim()));
  if (!urls.length) {
    setStatus("没有可发送的链接");
    return;
  }
  const xiaohongshuUrls = urls.filter((url) => /xiaohongshu\.com|xhslink\.com/i.test(url));
  const xiaohongshuItems = xiaohongshuUrls.map((url) => ({
    url,
    title: currentItemsByUrl.get(url)?.title || "",
  }));
  if (xiaohongshuUrls.length) {
    setStatus("写入小红书候选链接到同步面板...");
    const tab = await activeTab();
    if (!tab.url || !/xiaohongshu\.com/i.test(tab.url)) {
      setStatus("请先切到小红书博主主页，再发送小红书候选链接");
      return;
    }
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: scrapeXiaohongshuProfileFromPage,
    });
    if (!result || !result.name) {
      setStatus("没有读到当前小红书博主名称，无法归档候选链接");
      return;
    }
    const response = await fetch("http://127.0.0.1:8787/api/creator/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ creator: { ...result, manual_urls: xiaohongshuUrls, manual_items: xiaohongshuItems } }),
    });
    if (!response.ok) {
      throw new Error(`同步面板返回 HTTP ${response.status}，请确认 8787 面板已启动`);
    }
    const data = await response.json();
    const titledCount = xiaohongshuItems.filter((item) => item.title).length;
    setStatus(`${data.updated ? "已更新" : "已导入"}小红书博主：${data.creator?.name || result.name}；候选 ${xiaohongshuUrls.length} 条，带标题 ${titledCount} 条`);
    return;
  }

  setStatus("发送到本地服务...");
  const response = await fetch("http://127.0.0.1:8765/api/enqueue", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      urls,
      start: els.start.checked,
      watermark: els.watermark.value,
      include_images: els.includeImages.checked,
      concurrency: 1,
      delay: 2
    })
  });

  if (!response.ok) {
    throw new Error(`本地服务返回 HTTP ${response.status}`);
  }

  const data = await response.json();
  setStatus(`已接收 ${data.accepted} 条，新增 ${data.added} 条${data.running ? "，下载任务运行中" : ""}`);
}

function exportTxt() {
  const text = els.urls.value.trim();
  if (!text) {
    setStatus("没有可导出的链接");
    return;
  }

  const blob = new Blob([text + "\n"], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  chrome.downloads.download({
    url,
    filename: `douyin_urls_${Date.now()}.txt`,
    saveAs: true
  }, () => {
    setTimeout(() => URL.revokeObjectURL(url), 3000);
  });
}

function getCookieStores() {
  return new Promise((resolve, reject) => {
    chrome.cookies.getAllCookieStores((stores) => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
        return;
      }
      resolve((stores || []).map((store) => store.id).filter(Boolean));
    });
  });
}

function getCookies(details) {
  return new Promise((resolve, reject) => {
    chrome.cookies.getAll(details, (cookies) => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
        return;
      }
      resolve(cookies || []);
    });
  });
}

async function getCookiesAcrossStores(details) {
  const storeIds = await getCookieStores().catch(() => []);
  if (!storeIds.length) {
    return getCookies(details);
  }
  const batches = await Promise.all(storeIds.map((storeId) => getCookies({ ...details, storeId }).catch(() => [])));
  return batches.flat();
}

function getCookiesForUrl(url) {
  return getCookiesAcrossStores({ url });
}

function getCookiesForDomain(domain) {
  return getCookiesAcrossStores({ domain });
}

function parseDocumentCookie(cookieText, domain) {
  return String(cookieText || "")
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const index = part.indexOf("=");
      if (index <= 0) return null;
      return {
        name: part.slice(0, index).trim(),
        value: part.slice(index + 1).trim(),
        domain,
      };
    })
    .filter(Boolean);
}

function tabsForUrlPatterns(patterns) {
  return new Promise((resolve) => {
    chrome.tabs.query({ url: patterns }, (tabs) => {
      const error = chrome.runtime.lastError;
      if (error) {
        resolve([]);
        return;
      }
      resolve(tabs || []);
    });
  });
}

async function getDocumentCookiesFromTabs(patterns) {
  const tabs = await tabsForUrlPatterns(patterns);
  const cookies = [];
  for (const tab of tabs) {
    if (!tab.id || !tab.url) continue;
    try {
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => document.cookie,
      });
      const hostname = new URL(tab.url).hostname;
      cookies.push(...parseDocumentCookie(result, hostname));
    } catch (_) {
      // Some tabs cannot be scripted, for example Chrome internal pages or restricted states.
    }
  }
  return cookies;
}

async function getDouyinCookies() {
  const batches = await Promise.all([
    getCookiesForUrl("https://www.douyin.com/"),
    getCookiesForUrl("https://douyin.com/"),
    getCookiesForUrl("https://www.douyin.com/user/"),
    getCookiesForUrl("https://sso.douyin.com/"),
    getCookiesForUrl("https://passport.douyin.com/"),
    getCookiesForDomain(".douyin.com"),
    getCookiesForDomain("douyin.com"),
  ]);
  const byName = new Map();
  for (const cookie of batches.flat()) {
    if (!cookie.name || !cookie.value) continue;
    byName.set(cookie.name, cookie.value);
  }
  const cookies = Array.from(byName.entries())
    .sort(([nameA], [nameB]) => nameA.localeCompare(nameB))
    .map(([name, value]) => `${name}=${value}`);
  return {
    header: cookies.join("; "),
    count: cookies.length,
    names: Array.from(byName.keys()).sort(),
    hasLogin: byName.has("sessionid") || byName.has("sessionid_ss") || byName.has("sid_guard") || byName.has("passport_csrf_token"),
  };
}

async function getWeiboCookies() {
  const batches = await Promise.all([
    getCookiesForUrl("https://weibo.com/"),
    getCookiesForUrl("https://www.weibo.com/"),
    getCookiesForUrl("https://passport.weibo.com/"),
    getCookiesForDomain(".weibo.com"),
    getCookiesForDomain("weibo.com"),
    getCookiesForUrl("https://m.weibo.cn/"),
    getCookiesForDomain(".weibo.cn"),
    getCookiesForDomain("m.weibo.cn"),
    getCookiesForUrl("https://passport.sina.com.cn/"),
    getCookiesForDomain(".sina.com.cn"),
  ]);

  const cookiePriority = (cookie) => {
    const domain = String(cookie.domain || "").toLowerCase();
    const normalized = domain.replace(/^\./, "");
    if (normalized === "weibo.com") return 5;
    if (normalized === "www.weibo.com") return 4;
    if (normalized === "m.weibo.cn" || normalized === "weibo.cn") return 3;
    if (normalized === "passport.weibo.com") return 2;
    if (domain.endsWith("sina.com.cn") || domain.endsWith("sina.cn")) return 1;
    return 0;
  };
  const byName = new Map();
  for (const cookie of batches.flat()) {
    if (!cookie.name || !cookie.value) continue;
    const existing = byName.get(cookie.name);
    const candidate = {
      value: cookie.value,
      domain: cookie.domain || "",
      priority: cookiePriority(cookie),
    };
    if (!existing || candidate.priority > existing.priority) {
      byName.set(cookie.name, candidate);
    }
  }
  const cookies = Array.from(byName.entries())
    .sort(([nameA], [nameB]) => nameA.localeCompare(nameB))
    .map(([name, item]) => `${name}=${item.value}`);
  const names = Array.from(byName.keys()).sort();
  const domains = uniq(Array.from(byName.values()).map((item) => item.domain).filter(Boolean)).sort();
  return {
    header: cookies.join("; "),
    count: cookies.length,
    names,
    domains,
    hasLogin: byName.has("SUB") || byName.has("SUBP") || byName.has("SSOLoginState") || byName.has("ALF"),
  };
}

async function getWechatMpCookies() {
  const batches = await Promise.all([
    getCookiesForUrl("https://mp.weixin.qq.com/"),
    getCookiesForUrl("https://mp.weixin.qq.com/cgi-bin/home"),
    getCookiesForUrl("https://mp.weixin.qq.com/cgi-bin/searchbiz"),
    getCookiesForDomain(".mp.weixin.qq.com"),
    getCookiesForDomain("mp.weixin.qq.com"),
    getCookiesForDomain(".weixin.qq.com"),
  ]);

  const byName = new Map();
  for (const cookie of batches.flat()) {
    if (!cookie.name || !cookie.value) continue;
    byName.set(cookie.name, {
      value: cookie.value,
      domain: cookie.domain || "",
    });
  }
  const cookies = Array.from(byName.entries())
    .sort(([nameA], [nameB]) => nameA.localeCompare(nameB))
    .map(([name, item]) => `${name}=${item.value}`);
  const names = Array.from(byName.keys()).sort();
  const domains = uniq(Array.from(byName.values()).map((item) => item.domain).filter(Boolean)).sort();

  let token = "";
  try {
    const tab = await activeTab();
    const url = new URL(tab.url || "");
    if (url.hostname === "mp.weixin.qq.com") {
      token = url.searchParams.get("token") || "";
    }
  } catch (_) {
    token = "";
  }

  return {
    header: cookies.join("; "),
    token,
    count: cookies.length,
    names,
    domains,
    hasLogin: Boolean(token) && (byName.has("slave_sid") || byName.has("data_bizuin") || byName.has("bizuin")),
  };
}

async function getXiaohongshuCookies() {
  const batches = await Promise.all([
    getCookiesForUrl("https://www.xiaohongshu.com/"),
    getCookiesForUrl("https://xiaohongshu.com/"),
    getCookiesForUrl("https://edith.xiaohongshu.com/"),
    getCookiesForUrl("https://creator.xiaohongshu.com/"),
    getCookiesForUrl("https://www.xiaohongshu.com/explore/"),
    getCookiesForDomain(".xiaohongshu.com"),
    getCookiesForDomain("xiaohongshu.com"),
    getCookiesForDomain("www.xiaohongshu.com"),
    getCookiesForDomain("edith.xiaohongshu.com"),
    getCookiesForDomain("creator.xiaohongshu.com"),
    getCookiesForDomain(".xhslink.com"),
    getDocumentCookiesFromTabs([
      "https://xiaohongshu.com/*",
      "https://*.xiaohongshu.com/*",
      "https://xhslink.com/*",
      "https://*.xhslink.com/*",
    ]),
  ]);
  const byName = new Map();
  for (const cookie of batches.flat()) {
    if (!cookie.name || !cookie.value) continue;
    byName.set(cookie.name, {
      value: cookie.value,
      domain: cookie.domain || "",
    });
  }
  const cookies = Array.from(byName.entries())
    .sort(([nameA], [nameB]) => nameA.localeCompare(nameB))
    .map(([name, item]) => `${name}=${item.value}`);
  const names = Array.from(byName.keys()).sort();
  const domains = uniq(Array.from(byName.values()).map((item) => item.domain).filter(Boolean)).sort();
  return {
    header: cookies.join("; "),
    count: cookies.length,
    names,
    domains,
    hasLogin: byName.has("web_session") || byName.has("webId") || byName.has("webId.sig") || byName.has("a1") || byName.has("access-token"),
  };
}

async function importCookieToDashboard(platform = "douyin") {
  const label = platform === "weibo" ? "微博" : platform === "wechat" ? "公众号后台" : platform === "xiaohongshu" ? "小红书" : "抖音";
  setStatus(`读取${label} Cookie...`);
  const cookie = platform === "weibo"
    ? await getWeiboCookies()
    : platform === "wechat"
      ? await getWechatMpCookies()
      : platform === "xiaohongshu"
        ? await getXiaohongshuCookies()
        : await getDouyinCookies();
  if (!cookie.header) {
    if (platform === "xiaohongshu") {
      setStatus("没有读到小红书 Cookie。请在同一个 Chrome 用户里打开 xiaohongshu.com 并确认已登录；如果刚更新插件，请先到扩展管理页点“重新加载”。");
      return;
    }
    setStatus(`没有读到 Cookie，请先在 Chrome 登录 ${platform === "weibo" ? "weibo.com" : platform === "wechat" ? "mp.weixin.qq.com" : "douyin.com"}`);
    return;
  }
  if (platform === "wechat" && !cookie.token) {
    setStatus("已读到公众号后台 Cookie，但没有读到 token。请先打开 mp.weixin.qq.com 后台首页，再点导入。");
    return;
  }

  setStatus("写入本地同步面板...");
  const body = platform === "weibo"
    ? { weibo_cookie: cookie.header }
    : platform === "wechat"
      ? { wechat_mp_cookie: cookie.header, wechat_mp_token: cookie.token }
      : platform === "xiaohongshu"
        ? { xiaohongshu_cookie: cookie.header }
        : { douyin_cookie: cookie.header };
  const response = await fetch("http://127.0.0.1:8787/api/secrets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`同步面板返回 HTTP ${response.status}，请确认 8787 面板已启动`);
  }
  const names = cookie.names.slice(0, 8).join(", ");
  const domains = cookie.domains && cookie.domains.length ? `；域：${cookie.domains.slice(0, 4).join(", ")}` : "";
  const tokenText = platform === "wechat" ? `；token ${cookie.token ? "已保存" : "缺失"}` : "";
  setStatus(`${label} Cookie 已导入本地：${cookie.count} 项${cookie.hasLogin ? "，含登录态" : "，未见登录态"}${tokenText}。${names}${domains}`);
}

function scrapeXiaohongshuProfileFromPage() {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const lineClean = (value) => String(value || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const visibleText = (node) => {
    if (!node) return "";
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return "";
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return "";
    return clean(node.innerText || node.textContent || "");
  };
  const pickVisible = (root, selectors) => {
    for (const selector of selectors) {
      for (const node of root.querySelectorAll(selector)) {
        const text = visibleText(node);
        if (text && text.length <= 80 && !/[：:]/.test(text) && !/(关注|粉丝|获赞|收藏|小红书号|IP属地)/.test(text)) {
          return text;
        }
      }
    }
    return "";
  };
  const url = location.href;
  if (!/xiaohongshu\.com/i.test(location.hostname)) {
    throw new Error("当前标签页不是小红书页面");
  }
  const platformId = (location.pathname.match(/\/user\/profile\/([^/?#]+)/) || [])[1] || "";
  const candidates = Array.from(document.querySelectorAll("main, section, div"))
    .map((node) => ({ node, text: clean(node.innerText || "") }))
    .filter((item) => item.text.includes("小红书号") && item.text.includes("粉丝") && item.text.length < 2500)
    .sort((a, b) => a.text.length - b.text.length);
  const root = candidates[0]?.node || document.querySelector("main") || document.body;
  const rootLines = lineClean(root.innerText || "");
  let name = pickVisible(root, [
    ".user-name",
    ".nickname",
    "[class*='user-name']",
    "[class*='nickname']",
    "[class*='userName']",
    "[class*='Nickname']",
  ]);
  if (!name) {
    const redIndex = rootLines.findIndex((line) => /小红书号/.test(line));
    const beforeRed = redIndex >= 0 ? rootLines.slice(Math.max(0, redIndex - 4), redIndex).reverse() : rootLines.slice(0, 8);
    name = beforeRed.find((line) => line.length <= 80 && !/(首页|搜索|已关注|关注|粉丝|获赞|收藏|小红书号|IP属地|发布|消息|直播)/.test(line)) || "";
  }
  if (!name) {
    name = clean(document.title.replace(/[-_｜|].*$/, ""));
  }

  const bioLines = [];
  const redIndex = rootLines.findIndex((line) => /小红书号/.test(line));
  if (redIndex >= 0) {
    for (const line of rootLines.slice(redIndex + 1)) {
      if (/(关注|粉丝|获赞|收藏|笔记|已关注|小红书号|IP属地)/.test(line)) break;
      if (/^[♀♂]|^[\w\u4e00-\u9fa5]+[省市区县]?$/.test(line) && line.length <= 12) continue;
      if (line && line !== name) bioLines.push(line);
      if (bioLines.join(" ").length > 260) break;
    }
  }
  const recentTitles = Array.from(document.querySelectorAll("a, [class*='title'], [class*='Title']"))
    .map((node) => clean(node.innerText || node.textContent || ""))
    .filter((text) => text.length >= 3 && text.length <= 80 && !/(首页|搜索|关注|粉丝|获赞|收藏|小红书号|IP属地|发布|消息|直播)/.test(text))
    .slice(0, 12);
  return {
    platform: "xiaohongshu",
    platform_id: platformId,
    name,
    url,
    bio: bioLines.join("\n"),
    recent_titles: Array.from(new Set(recentTitles)),
  };
}

async function importXiaohongshuProfileToDashboard() {
  setStatus("读取当前小红书主页...");
  const tab = await activeTab();
  if (!tab.url || !/xiaohongshu\.com/i.test(tab.url)) {
    setStatus("请先切到小红书博主主页，再点导入当前小红书博主");
    return;
  }
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: scrapeXiaohongshuProfileFromPage,
  });
  if (!result || !result.name) {
    setStatus("没有读到博主名称。请确认当前页面已经加载出博主资料区。");
    return;
  }
  setStatus(`写入本地同步面板：${result.name}`);
  const response = await fetch("http://127.0.0.1:8787/api/creator/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ creator: result }),
  });
  if (!response.ok) {
    throw new Error(`同步面板返回 HTTP ${response.status}，请确认 8787 面板已启动`);
  }
  const data = await response.json();
  const creator = data.creator || result;
  setStatus(`${data.updated ? "已更新" : "已导入"}小红书博主：${creator.name}。回到同步面板刷新即可看到。`);
}

function openDashboard() {
  chrome.tabs.create({ url: "http://127.0.0.1:8787" });
}

els.collect.addEventListener("click", () => collect(false).catch((error) => setStatus(String(error.message || error))));
els.scrollCollect.addEventListener("click", () => collect(true).catch((error) => setStatus(String(error.message || error))));
els.send.addEventListener("click", () => sendToLocal().catch((error) => setStatus(String(error.message || error))));
els.export.addEventListener("click", exportTxt);
els.importCookie.addEventListener("click", () => importCookieToDashboard("douyin").catch((error) => setStatus(String(error.message || error))));
els.importWeiboCookie.addEventListener("click", () => importCookieToDashboard("weibo").catch((error) => setStatus(String(error.message || error))));
els.importWechatCookie.addEventListener("click", () => importCookieToDashboard("wechat").catch((error) => setStatus(String(error.message || error))));
els.importXiaohongshuCookie.addEventListener("click", () => importCookieToDashboard("xiaohongshu").catch((error) => setStatus(String(error.message || error))));
els.importXiaohongshuProfile.addEventListener("click", () => importXiaohongshuProfileToDashboard().catch((error) => setStatus(String(error.message || error))));
els.openDashboard.addEventListener("click", openDashboard);

chrome.storage.local.get({ urls: [], items: [] }, (data) => {
  currentItemsByUrl = new Map((data.items || []).filter((item) => item && item.url).map((item) => [item.url, item]));
  render(data.urls || [], data.items || []);
});
