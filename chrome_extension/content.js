const DOUYIN_PATTERNS = [
  /https?:\/\/www\.douyin\.com\/video\/\d+/i,
  /https?:\/\/www\.douyin\.com\/note\/\d+/i,
  /https?:\/\/www\.douyin\.com\/[^"'<> ]*[?&]modal_id=\d+/i,
  /https?:\/\/www\.douyin\.com\/[^"'<> ]*[?&]vid=\d+/i,
  /https?:\/\/v\.douyin\.com\/[A-Za-z0-9_-]+\/?/i
];
const XIAOHONGSHU_PATTERNS = [
  /https?:\/\/www\.xiaohongshu\.com\/explore\/[A-Za-z0-9]+[^"'<> ]*/i,
  /https?:\/\/www\.xiaohongshu\.com\/discovery\/item\/[A-Za-z0-9]+[^"'<> ]*/i,
  /https?:\/\/www\.xiaohongshu\.com\/user\/profile\/[A-Za-z0-9]+\/[A-Za-z0-9]+[^"'<> ]*/i,
  /https?:\/\/xhslink\.com\/[A-Za-z0-9_-]+\/?/i
];

function cleanUrl(value) {
  if (!value || typeof value !== "string") return null;
  const text = value.trim();
  for (const pattern of DOUYIN_PATTERNS) {
    const match = text.match(pattern);
    if (!match) continue;
    try {
      const url = new URL(match[0], location.href);
      url.hash = "";
      if (url.hostname === "www.douyin.com") {
        const video = url.pathname.match(/^\/video\/(\d+)/);
        if (video) return `https://www.douyin.com/video/${video[1]}`;
        const note = url.pathname.match(/^\/note\/(\d+)/);
        if (note) return `https://www.douyin.com/note/${note[1]}`;
        const modalId = url.searchParams.get("modal_id");
        if (modalId) return `https://www.douyin.com/video/${modalId}`;
        const vid = url.searchParams.get("vid");
        if (vid) return `https://www.douyin.com/video/${vid}`;
      }
      return url.toString();
    } catch (_error) {
      return match[0];
    }
  }
  for (const pattern of XIAOHONGSHU_PATTERNS) {
    const match = text.match(pattern);
    if (!match) continue;
    try {
      const url = new URL(match[0], location.href);
      url.hash = "";
      if (url.hostname === "www.xiaohongshu.com") {
        const explore = url.pathname.match(/^\/explore\/([A-Za-z0-9]+)/);
        const discovery = url.pathname.match(/^\/discovery\/item\/([A-Za-z0-9]+)/);
        const profileNote = url.pathname.match(/^\/user\/profile\/[A-Za-z0-9]+\/([A-Za-z0-9]+)/);
        const noteId = explore?.[1] || discovery?.[1] || profileNote?.[1] || "";
        if (noteId) {
          const clean = new URL(`https://www.xiaohongshu.com/explore/${noteId}`);
          for (const key of ["xsec_token", "xsec_source"]) {
            const value = url.searchParams.get(key);
            if (value) clean.searchParams.set(key, value);
          }
          return clean.toString();
        }
      }
      return url.toString();
    } catch (_error) {
      return match[0];
    }
  }
  return null;
}

function collectFromPage() {
  return collectItemsFromPage().map((item) => item.url);
}

function cleanTitle(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/^(置顶|视频|图文)\s*/g, "")
    .trim();
}

function titleForLink(node) {
  const candidates = [];
  if (node) {
    candidates.push(node.getAttribute("title"), node.getAttribute("aria-label"), node.innerText, node.textContent);
    const card = node.closest("section, article, div");
    if (card) {
      candidates.push(card.getAttribute("title"), card.getAttribute("aria-label"));
      const titleNode = card.querySelector("[class*='title'], [class*='Title'], [class*='footer'], [class*='desc']");
      candidates.push(titleNode?.innerText, titleNode?.textContent);
      for (const line of String(card.innerText || "").split(/\r?\n/)) {
        candidates.push(line);
      }
    }
  }
  for (const candidate of candidates) {
    const title = cleanTitle(candidate);
    if (title && title.length >= 2 && title.length <= 120 && !/(点赞|评论|收藏|关注|粉丝|小红书号|IP属地)/.test(title)) {
      return title;
    }
  }
  return "";
}

function collectItemsFromPage() {
  const byUrl = new Map();

  const current = cleanUrl(location.href);
  if (current) byUrl.set(current, { url: current, title: cleanTitle(document.title) });

  for (const selector of ["a[href]", "link[rel='canonical']", "meta[property='og:url']", "meta[name='twitter:url']"]) {
    for (const node of document.querySelectorAll(selector)) {
      const value = node.href || node.content || node.getAttribute("href") || node.getAttribute("content");
      const url = cleanUrl(value);
      if (!url) continue;
      const existing = byUrl.get(url);
      const title = titleForLink(node);
      byUrl.set(url, {
        url,
        title: title || existing?.title || "",
      });
    }
  }

  return Array.from(byUrl.values());
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function scrollAndCollect(rounds, delayMs) {
  const found = new Map(collectItemsFromPage().map((item) => [item.url, item]));
  let lastY = window.scrollY;

  for (let index = 0; index < rounds; index += 1) {
    window.scrollBy({ top: Math.max(600, Math.floor(window.innerHeight * 0.9)), behavior: "smooth" });
    await sleep(delayMs);
    for (const item of collectItemsFromPage()) {
      const existing = found.get(item.url);
      found.set(item.url, { ...item, title: item.title || existing?.title || "" });
    }

    if (Math.abs(window.scrollY - lastY) < 20) break;
    lastY = window.scrollY;
  }

  return Array.from(found.values());
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "collect") return false;

  if (message.scroll) {
    scrollAndCollect(message.rounds || 8, message.delayMs || 1800)
      .then((items) => sendResponse({ ok: true, urls: items.map((item) => item.url), items, page: location.href, title: document.title }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  const items = collectItemsFromPage();
  sendResponse({ ok: true, urls: items.map((item) => item.url), items, page: location.href, title: document.title });
  return false;
});
