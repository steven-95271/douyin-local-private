const els = {
  count: document.getElementById("count"),
  collect: document.getElementById("collect"),
  scrollCollect: document.getElementById("scrollCollect"),
  send: document.getElementById("send"),
  export: document.getElementById("export"),
  urls: document.getElementById("urls"),
  status: document.getElementById("status"),
  rounds: document.getElementById("rounds"),
  watermark: document.getElementById("watermark"),
  start: document.getElementById("start"),
  includeImages: document.getElementById("includeImages")
};

let currentUrls = [];

function setStatus(text) {
  els.status.textContent = text;
}

function uniq(values) {
  return Array.from(new Set(values.filter(Boolean)));
}

function render(urls) {
  currentUrls = uniq(urls);
  els.count.textContent = String(currentUrls.length);
  els.urls.value = currentUrls.join("\n");
  chrome.storage.local.set({ urls: currentUrls });
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

  render(uniq([...currentUrls, ...response.urls]));
  setStatus(`已收集 ${currentUrls.length} 条`);
}

async function sendToLocal() {
  const urls = uniq(els.urls.value.split(/\r?\n/).map((line) => line.trim()));
  if (!urls.length) {
    setStatus("没有可发送的链接");
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

els.collect.addEventListener("click", () => collect(false).catch((error) => setStatus(String(error.message || error))));
els.scrollCollect.addEventListener("click", () => collect(true).catch((error) => setStatus(String(error.message || error))));
els.send.addEventListener("click", () => sendToLocal().catch((error) => setStatus(String(error.message || error))));
els.export.addEventListener("click", exportTxt);

chrome.storage.local.get({ urls: [] }, (data) => render(data.urls || []));
