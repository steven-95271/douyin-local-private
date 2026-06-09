const DOUYIN_PATTERNS = [
  /https?:\/\/www\.douyin\.com\/video\/\d+/i,
  /https?:\/\/www\.douyin\.com\/note\/\d+/i,
  /https?:\/\/www\.douyin\.com\/[^"'<> ]*[?&]modal_id=\d+/i,
  /https?:\/\/www\.douyin\.com\/[^"'<> ]*[?&]vid=\d+/i,
  /https?:\/\/v\.douyin\.com\/[A-Za-z0-9_-]+\/?/i
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
  return null;
}

function collectFromPage() {
  const urls = new Set();

  const current = cleanUrl(location.href);
  if (current) urls.add(current);

  for (const selector of ["a[href]", "link[rel='canonical']", "meta[property='og:url']", "meta[name='twitter:url']"]) {
    for (const node of document.querySelectorAll(selector)) {
      const value = node.href || node.content || node.getAttribute("href") || node.getAttribute("content");
      const url = cleanUrl(value);
      if (url) urls.add(url);
    }
  }

  return Array.from(urls);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function scrollAndCollect(rounds, delayMs) {
  const found = new Set(collectFromPage());
  let lastY = window.scrollY;

  for (let index = 0; index < rounds; index += 1) {
    window.scrollBy({ top: Math.max(600, Math.floor(window.innerHeight * 0.9)), behavior: "smooth" });
    await sleep(delayMs);
    for (const url of collectFromPage()) found.add(url);

    if (Math.abs(window.scrollY - lastY) < 20) break;
    lastY = window.scrollY;
  }

  return Array.from(found);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "collect") return false;

  if (message.scroll) {
    scrollAndCollect(message.rounds || 8, message.delayMs || 1800)
      .then((urls) => sendResponse({ ok: true, urls, page: location.href, title: document.title }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  sendResponse({ ok: true, urls: collectFromPage(), page: location.href, title: document.title });
  return false;
});
