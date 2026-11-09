/* Sakura watch bridge: position + cropped video frames -> 127.0.0.1:9377 */

const BASE = "http://127.0.0.1:9377";
let helloDone = false;

async function post(path, payload) {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result?.ok === false) {
    throw new Error(result?.error || `HTTP ${response.status}`);
  }
  return result;
}

async function hello(force = false) {
  if (helloDone && !force) return;
  helloDone = true;
  try {
    await post("/api/hello", { version: chrome.runtime.getManifest().version });
  } catch {
    helloDone = false;
  }
}

void hello();

chrome.runtime.onInstalled.addListener(() => void hello(true));
chrome.runtime.onStartup?.addListener(() => void hello(true));

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "position") {
    post("/api/position", message.payload)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "frame") {
    post("/api/frame", message.payload)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "capture") {
    chrome.tabs.captureVisibleTab(null, { format: "jpeg", quality: 72 }, (dataUrl) => {
      const error = chrome.runtime.lastError;
      if (error || !dataUrl) {
        sendResponse({ ok: false, error: error?.message || "capture failed" });
        return;
      }
      sendResponse({
        ok: true,
        dataUrl,
        viewport: message.payload?.viewport || null,
      });
    });
    return true;
  }

  return false;
});
