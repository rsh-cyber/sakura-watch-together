/* Sakura watch bridge: read the playing <video>, keep local frames, and build
 * a storyboard only when the plugin asks for one. */

(() => {
  if (window.__sakuraWatchBridgeLoaded) return;
  window.__sakuraWatchBridgeLoaded = true;

  let lastFrameAt = 0;
  let captureBusy = false;
  let storyboardBusy = false;
  let lastStoryboardToken = "";
  let lastBufferUrl = "";
  let lastBufferSrc = "";
  let lastVideo = null;

  const frameBuffer = [];
  const captionTimeline = [];
  const FRAME_BUFFER_MAX = 120;
  const FRAME_BUFFER_MS = 10 * 60 * 1000;
  const CAPTION_MAX = 300;
  const CAPTION_MS = 10 * 60 * 1000;
  let lastCaptionValue = "";

  function clock(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  function pruneFrames(now) {
    while (
      frameBuffer.length > FRAME_BUFFER_MAX ||
      (frameBuffer.length > 0 && now - frameBuffer[0].at > FRAME_BUFFER_MS)
    ) {
      frameBuffer.shift();
    }
  }

  function noteVideo(video) {
    const currentSrc = video?.currentSrc || video?.src || "";
    const changed =
      (lastBufferUrl && lastBufferUrl !== location.href) ||
      (lastBufferSrc && currentSrc && lastBufferSrc !== currentSrc) ||
      (lastVideo && lastVideo !== video);
    if (changed) {
      // New page or new video element: never reuse the previous video's
      // frames or captions for the first comment on the new one.
      frameBuffer.length = 0;
      resetCaptionTimeline();
      lastStoryboardToken = "";
    }
    lastBufferUrl = location.href;
    if (currentSrc) lastBufferSrc = currentSrc;
    lastVideo = video;
  }

  function visibleBox(video) {
    if (!video || !video.isConnected) return null;
    const r = video.getBoundingClientRect();
    if (!(r.width > 2 && r.height > 2)) return null;
    let style;
    try {
      style = getComputedStyle(video);
    } catch {
      return null;
    }
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return null;
    }
    const left = Math.max(0, r.left);
    const top = Math.max(0, r.top);
    const right = Math.min(window.innerWidth, r.right);
    const bottom = Math.min(window.innerHeight, r.bottom);
    if (right - left <= 2 || bottom - top <= 2) return null;
    return {
      x: left,
      y: top,
      width: right - left,
      height: bottom - top,
      area: (right - left) * (bottom - top),
    };
  }

  function pickVideo() {
    const all = [...document.querySelectorAll("video")];
    if (all.length === 0) return null;
    if (all.length === 1) return all[0];

    const viewport = Math.max(1, window.innerWidth * window.innerHeight);
    const info = all.map((video) => ({ video, box: visibleBox(video) }));
    const visible = info.filter((item) => item.box);
    const pool = visible.length > 0 ? visible : info;
    const live = pool.filter((item) => !item.video.ended);
    const candidates = live.length > 0 ? live : pool;

    function score({ video, box }) {
      let value = box ? (box.area / viewport) * 1e7 : 0;
      if (!video.paused && !video.ended) value += 1e5;
      if (video.readyState >= 2) value += 1e3;
      if (video.currentTime > 0) value += 1e3;
      if (video === document.pictureInPictureElement) value += 1e9;
      const fs = document.fullscreenElement;
      if (fs && fs !== document.documentElement && fs.contains(video)) value += 5e8;
      return value;
    }

    return candidates.reduce((best, current) => (score(current) > score(best) ? current : best)).video;
  }

  function usefulCaption(text) {
    const value = String(text || "").trim();
    if (!value || value.length < 2) return false;
    return !/^(关闭|开启|字幕|设置|自动|中文|简体|繁体|繁體|english|cc|字幕关闭|字幕开启|日本語|日语|日文|英语|英文)$/i.test(
      value,
    );
  }

  function trackCaption(video) {
    const parts = [];
    try {
      for (const track of video?.textTracks || []) {
        if (track.mode !== "showing" && track.mode !== "hidden") continue;
        for (const cue of track.activeCues || []) {
          const text = String(cue.text || "")
            .replace(/<[^>]+>/g, " ")
            .replace(/\s+/g, " ")
            .trim();
          if (usefulCaption(text)) parts.push(text);
        }
      }
    } catch {
      return "";
    }
    return parts.join(" ").slice(0, 300);
  }

  function captionText(video) {
    const fromTracks = trackCaption(video);
    if (fromTracks) return fromTracks;
    const selectors = [
      ".bpx-player-subtitle-panel-text",
      ".bilibili-player-video-subtitle",
      ".art-subtitle",
      ".shaka-subtitle",
      ".video-subtitle",
      "[class*='subtitle'][class*='text']",
    ];
    for (const selector of selectors) {
      const nodes = [...document.querySelectorAll(selector)];
      for (const node of nodes) {
        if (!(node instanceof HTMLElement)) continue;
        const text = (node.innerText || "").trim().replace(/\s+/g, " ");
        if (text && text.length <= 300 && usefulCaption(text)) return text;
      }
    }
    return "";
  }

  function pruneCaptions(now) {
    while (
      captionTimeline.length > CAPTION_MAX ||
      (captionTimeline.length > 0 && now - captionTimeline[0].at > CAPTION_MS)
    ) {
      captionTimeline.shift();
    }
  }

  function rememberCaption(video) {
    const text = captionText(video);
    if (!text || text === lastCaptionValue) return;
    lastCaptionValue = text;
    captionTimeline.push({
      pos: Number(video?.currentTime) || 0,
      at: Date.now(),
      text: text.slice(0, 240),
    });
    pruneCaptions(Date.now());
  }

  function resetCaptionTimeline() {
    captionTimeline.length = 0;
    lastCaptionValue = "";
  }

  function send(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (response) => {
          const error = chrome.runtime.lastError;
          resolve(error ? { ok: false, error: error.message } : response || { ok: false });
        });
      } catch (error) {
        resolve({ ok: false, error: String(error) });
      }
    });
  }

  function loadImage(dataUrl) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = reject;
      image.src = dataUrl;
    });
  }

  function grabVideoFrame(video) {
    const width = Number(video?.videoWidth) || 0;
    const height = Number(video?.videoHeight) || 0;
    if (width < 2 || height < 2) return "";
    try {
      const maxWidth = 960;
      const scale = Math.min(1, maxWidth / width);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(width * scale));
      canvas.height = Math.max(1, Math.round(height * scale));
      const ctx = canvas.getContext("2d", { alpha: false });
      if (!ctx) return "";
      ctx.drawImage(video, 0, 0, width, height, 0, 0, canvas.width, canvas.height);
      return canvas.toDataURL("image/png");
    } catch {
      // Cross-origin/DRM video taints the canvas; the tab screenshot below is
      // the fallback for those pages.
      return "";
    }
  }

  async function reportPosition() {
    const video = pickVideo();
    if (!video || !Number.isFinite(video.currentTime)) return;
    noteVideo(video);
    rememberCaption(video);
    await send({
      type: "position",
      payload: {
        pos: video.currentTime,
        dur: Number.isFinite(video.duration) ? video.duration : null,
        title: document.title || "",
        url: location.href,
        paused: Boolean(video.paused),
        ended: Boolean(video.ended),
      },
    });
  }

  async function captureFrame() {
    if (captureBusy) return;
    const now = Date.now();
    if (now - lastFrameAt < 4500) return;
    const video = pickVideo();
    if (!video || video.paused || video.ended || video.readyState < 2) return;
    noteVideo(video);

    captureBusy = true;
    lastFrameAt = now;
    try {
      let cropped = grabVideoFrame(video);
      let source = "video";

      if (!cropped) {
        const box = visibleBox(video);
        if (!box || box.width < 160 || box.height < 90) return;
        source = "screen";
        const response = await send({
          type: "capture",
          payload: {
            viewport: { width: window.innerWidth, height: window.innerHeight },
          },
        });
        if (!response?.ok || !response.dataUrl) return;

        const image = await loadImage(response.dataUrl);
        const scaleX = image.naturalWidth / Math.max(1, window.innerWidth);
        const scaleY = image.naturalHeight / Math.max(1, window.innerHeight);
        const sx = Math.max(0, Math.round(box.x * scaleX));
        const sy = Math.max(0, Math.round(box.y * scaleY));
        const sw = Math.min(image.naturalWidth - sx, Math.round(box.width * scaleX));
        const sh = Math.min(image.naturalHeight - sy, Math.round(box.height * scaleY));
        if (sw < 160 || sh < 90) return;

        const maxWidth = 960;
        const scale = Math.min(1, maxWidth / sw);
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(sw * scale));
        canvas.height = Math.max(1, Math.round(sh * scale));
        const ctx = canvas.getContext("2d", { alpha: false });
        if (!ctx) return;
        ctx.drawImage(image, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
        cropped = canvas.toDataURL("image/png");
      }

      rememberCaption(video);
      const caption = captionText(video);
      frameBuffer.push({
        pos: video.currentTime,
        caption,
        dataUrl: cropped,
        at: now,
        source,
        url: location.href,
      });
      pruneFrames(now);

      await send({
        type: "frame",
        payload: {
          pos: video.currentTime,
          dur: Number.isFinite(video.duration) ? video.duration : null,
          title: document.title || "",
          url: location.href,
          paused: Boolean(video.paused),
          ended: Boolean(video.ended),
          caption,
          image: cropped,
          source,
        },
      });
    } catch {
      /* extension context can disappear while the page stays open */
    } finally {
      captureBusy = false;
    }
  }

  function pickStoryboardFrames(from, to, limit) {
    const sameVideo = (frame) => !frame.url || frame.url === location.href;
    let pool = frameBuffer.filter(
      (frame) =>
        sameVideo(frame) &&
        frame.pos >= Number(from) - 2 &&
        frame.pos <= Number(to) + 2,
    );
    if (pool.length < 3) {
      pool = frameBuffer.filter(sameVideo).slice(-Math.max(limit, 6));
    }
    if (pool.length === 0) return [];
    if (pool.length <= limit) return pool.slice();

    const picked = [];
    for (let index = 0; index < limit; index += 1) {
      const at = Math.round((index * (pool.length - 1)) / Math.max(1, limit - 1));
      picked.push(pool[at]);
    }
    return [...new Map(picked.map((frame) => [frame.at, frame])).values()];
  }

  function drawCover(ctx, image, x, y, width, height) {
    const imageRatio = image.naturalWidth / Math.max(1, image.naturalHeight);
    const cellRatio = width / height;
    let sx = 0;
    let sy = 0;
    let sw = image.naturalWidth;
    let sh = image.naturalHeight;
    if (imageRatio > cellRatio) {
      sw = image.naturalHeight * cellRatio;
      sx = (image.naturalWidth - sw) / 2;
    } else {
      sh = image.naturalWidth / cellRatio;
      sy = (image.naturalHeight - sh) / 2;
    }
    ctx.drawImage(image, sx, sy, sw, sh, x, y, width, height);
  }

  async function buildStoryboard(request) {
    const limit = Math.max(3, Math.min(6, Number(request.limit) || 4));
    const frames = pickStoryboardFrames(request.from, request.to, limit);
    if (frames.length === 0) return;

    const images = await Promise.all(frames.map((frame) => loadImage(frame.dataUrl)));
    const columns = images.length <= 2 ? images.length : 2;
    const rows = Math.ceil(images.length / columns);
    const cellWidth = 480;
    const cellHeight = 270;
    const canvas = document.createElement("canvas");
    canvas.width = columns * cellWidth;
    canvas.height = rows * cellHeight;
    const ctx = canvas.getContext("2d", { alpha: false });
    if (!ctx) return;
    ctx.fillStyle = "#111";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    images.forEach((image, index) => {
      const x = (index % columns) * cellWidth;
      const y = Math.floor(index / columns) * cellHeight;
      drawCover(ctx, image, x, y, cellWidth, cellHeight);
      ctx.fillStyle = "rgba(0,0,0,0.62)";
      ctx.fillRect(x, y + cellHeight - 28, cellWidth, 28);
      ctx.fillStyle = "#fff";
      ctx.font = "16px sans-serif";
      ctx.textBaseline = "middle";
      ctx.fillText(
        `${clock(frames[index].pos)}  ${index + 1}/${images.length}`,
        x + 10,
        y + cellHeight - 14,
      );
      const caption = String(frames[index].caption || "").slice(0, 52);
      if (caption) {
        ctx.fillStyle = "rgba(0,0,0,0.62)";
        ctx.fillRect(x, y + cellHeight - 56, cellWidth, 28);
        ctx.fillStyle = "#ffe9a8";
        ctx.font = "14px sans-serif";
        ctx.textBaseline = "middle";
        ctx.fillText(caption, x + 10, y + cellHeight - 42);
      }
    });

    await fetch("http://127.0.0.1:9377/api/storyboard", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        token: request.token,
        image: canvas.toDataURL("image/png"),
        frames: frames.map((frame) => ({ pos: frame.pos, caption: frame.caption || "" })),
        captions: captionTimeline
          .filter(
            (item) =>
              item.pos >= Number(request.from) - 3 &&
              item.pos <= Number(request.to) + 3,
          )
          .slice(-80),
        from: request.from,
        to: request.to,
      }),
    });
  }

  async function pollStoryboardRequest() {
    if (storyboardBusy) return;
    storyboardBusy = true;
    try {
      const response = await fetch("http://127.0.0.1:9377/api/state", {
        cache: "no-store",
      });
      if (!response.ok) return;
      const state = await response.json().catch(() => ({}));
      const request = state?.storyboard_request;
      if (!request?.token || request.token === lastStoryboardToken) return;
      lastStoryboardToken = request.token;
      await buildStoryboard(request);
    } catch {
      /* bridge not running yet */
    } finally {
      storyboardBusy = false;
    }
  }

  setInterval(reportPosition, 1000);
  setInterval(captureFrame, 5000);
  setInterval(pollStoryboardRequest, 1200);

  for (const event of ["play", "pause", "seeked", "ratechange", "ended", "loadedmetadata"]) {
    document.addEventListener(event, reportPosition, true);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      reportPosition();
      void captureFrame();
      void pollStoryboardRequest();
    }
  });
  document.addEventListener("DOMContentLoaded", reportPosition, true);
  reportPosition();
  setTimeout(captureFrame, 1200);
})();
