const ENDPOINT = "http://127.0.0.1:47653/capture";
let lastImage = null;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "capture-prism",
    title: "保存图片到 Prism",
    contexts: ["image"]
  });
});

async function capture(url, tab) {
  try {
    const response = await fetch(ENDPOINT, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Prism-Capture": "1"},
      body: JSON.stringify({url, title: tab?.title || "", tags: []})
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    chrome.action.setBadgeText({text: "OK"});
    chrome.action.setBadgeBackgroundColor({color: "#238636"});
    chrome.action.setTitle({title: "图片已发送到 Prism"});
  } catch (error) {
    chrome.action.setBadgeText({text: "!"});
    chrome.action.setBadgeBackgroundColor({color: "#c03030"});
    chrome.action.setTitle({title: "采集失败，请先启动 Prism 再重试。"});
  }
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "capture-prism" && info.srcUrl) {
    lastImage = {url: info.srcUrl, tab};
    capture(info.srcUrl, tab);
  }
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "capture-image" && message.url) {
    lastImage = {url: message.url, tab: sender.tab};
    capture(message.url, sender.tab);
  }
});

chrome.commands.onCommand.addListener(command => {
  if (command === "capture-hovered-image" && lastImage) {
    capture(lastImage.url, lastImage.tab);
  }
});
