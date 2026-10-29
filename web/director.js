import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_TYPE = "H3LongVideoDirector";
const ACTIVE_POLLERS = new Map();

function ensureStyles() {
  if (document.getElementById("h3-long-video-director-style")) return;
  const style = document.createElement("style");
  style.id = "h3-long-video-director-style";
  style.textContent = `
    .h3-director-panel { width: 100%; height: 560px; box-sizing: border-box; padding: 8px; color: #ddd; background: #181818; border-top: 1px solid #333; font-family: sans-serif; overflow-y: auto; }
    .h3-director-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .h3-director-state { flex: 1; font-size: 13px; font-weight: 600; }
    .h3-director-mode { color: #aaa; font-size: 12px; }
    .h3-director-refresh { border: 1px solid #555; background: #2a2a2a; color: #eee; border-radius: 4px; padding: 3px 8px; cursor: pointer; }
    .h3-director-refresh:hover { background: #393939; }
    .h3-director-section-title { margin: 8px 0 5px; color: #c7d0da; font-size: 12px; font-weight: 700; }
    .h3-director-keyframes { display: flex; align-items: stretch; gap: 8px; margin-bottom: 10px; overflow-x: auto; padding-bottom: 5px; }
    .h3-director-keyframe-card { flex: 0 0 210px; min-width: 0; border: 1px solid #5b4a2a; background: #19150f; border-radius: 6px; padding: 6px; }
    .h3-director-keyframe-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
    .h3-director-keyframe-pair figure { margin: 0; min-width: 0; }
    .h3-director-keyframe-pair a { display: block; }
    .h3-director-keyframe-pair img { display: block; width: 100%; height: 150px; object-fit: contain; border-radius: 4px; background: #000; cursor: zoom-in; }
    .h3-director-keyframe-pair figcaption { color: #9fa8b3; text-align: center; font-size: 11px; margin-top: 3px; }
    .h3-director-segments { display: flex; align-items: stretch; gap: 8px; overflow-x: auto; padding-bottom: 5px; }
    .h3-director-card { flex: 0 0 230px; min-width: 0; border: 1px solid #34404c; background: #11161c; border-radius: 6px; padding: 6px; }
    .h3-director-card-head { display: flex; justify-content: space-between; gap: 6px; margin-bottom: 5px; color: #cfd8e3; font-size: 12px; }
    .h3-director-card video { display: block; width: 100%; height: 220px; object-fit: contain; border-radius: 4px; background: #000; }
    .h3-director-final { margin-top: 10px; border: 1px solid #3b6f55; background: #101b15; border-radius: 6px; padding: 8px; }
    .h3-director-final video { display: block; width: 100%; height: 360px; object-fit: contain; border-radius: 4px; background: #000; margin: 6px 0; }
    .h3-director-download { display: inline-block; color: #9edcff; text-decoration: none; font-size: 13px; }
    .h3-director-download:hover { text-decoration: underline; }
    .h3-director-meta { color: #87919c; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .h3-director-error { margin-top: 8px; color: #ffb1b1; font-size: 12px; white-space: pre-wrap; }
  `;
  document.head.appendChild(style);
}

function makeElement(tag, className, text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function makeVideoCard(data) {
  const card = makeElement("div", "h3-director-card");
  const head = makeElement("div", "h3-director-card-head");
  const title = makeElement("strong", "", `第 ${data.index} 段`);
  const details = makeElement(
    "span",
    "",
    `${data.seconds ?? "?"} 秒 · ${data.two_pass ? "二采" : "一采"}`,
  );
  head.append(title, details);
  const video = document.createElement("video");
  video.controls = true;
  video.preload = "metadata";
  video.src = data.url;
  card.append(head, video);
  return card;
}

function makeKeyframeCard(data) {
  const card = makeElement("div", "h3-director-keyframe-card");
  card.append(makeElement("strong", "", `第 ${data.index} 段关键帧`));
  const pair = makeElement("div", "h3-director-keyframe-pair");
  for (const [label, url] of [
    ["开始", data.start_url],
    ["结束", data.end_url],
  ]) {
    const figure = document.createElement("figure");
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = "点击查看原始尺寸";
    const image = document.createElement("img");
    image.loading = "lazy";
    image.src = url;
    link.appendChild(image);
    figure.append(link, makeElement("figcaption", "", label));
    pair.appendChild(figure);
  }
  card.appendChild(pair);
  return card;
}

function makeTransitionCard(data) {
  const card = makeElement("div", "h3-director-card");
  const head = makeElement("div", "h3-director-card-head");
  const title = makeElement("strong", "", `进入第 ${data.index} 段`);
  const details = makeElement("span", "", `${data.seconds ?? "?"} 秒`);
  head.append(title, details);
  const video = document.createElement("video");
  video.controls = true;
  video.preload = "metadata";
  video.src = data.url;
  card.append(head, video);
  return card;
}

function updatePanel(ui, node, data) {
  const status = data.status || "unknown";
  const total = Number(node.storyboardCount || 0);
  const done = Number(data.completed_segments || 0);
  const countText = total > 0 ? ` ${done}/${total}` : ` ${done}`;
  const labels = {
    scheduled: "已排队",
    running: "正在生成",
    completed: "生成完成",
    failed: "生成失败",
    unknown: "等待状态",
  };
  const phaseLabels = {
    keyframes: `关键帧 ${data.completed_keyframes || 0}/${total || "?"}`,
    videos: `视频 ${done}/${total || "?"}`,
  };
  ui.state.textContent =
    phaseLabels[data.phase] || `${labels[status] || status}${countText}`;
  ui.mode.textContent = [node.lastTwoPassMode, node.samplingSummary]
    .filter(Boolean)
    .join(" · ");
  ui.error.textContent = data.error || "";

  const keyframeSignature = JSON.stringify(
    (data.keyframes || []).map((item) => [
      item.index,
      item.start_url,
      item.end_url,
    ]),
  );
  if (ui.keyframeSignature !== keyframeSignature) {
    ui.keyframeSignature = keyframeSignature;
    ui.keyframes.replaceChildren(
      ...(data.keyframes || []).map((item) => makeKeyframeCard(item)),
    );
  }

  const transitionSignature = JSON.stringify(
    (data.transitions || []).map((item) => [
      item.index,
      item.url,
      item.seconds,
    ]),
  );
  if (ui.transitionSignature !== transitionSignature) {
    ui.transitionSignature = transitionSignature;
    ui.transitions.replaceChildren(
      ...(data.transitions || []).map((item) => makeTransitionCard(item)),
    );
    ui.transitionTitle.style.display = data.transitions?.length ? "block" : "none";
    ui.transitions.style.display = data.transitions?.length ? "flex" : "none";
  }

  const segmentSignature = JSON.stringify(
    (data.segments || []).map((item) => [item.index, item.url, item.seconds, item.two_pass]),
  );
  if (ui.segmentSignature !== segmentSignature) {
    ui.segmentSignature = segmentSignature;
    ui.segments.replaceChildren(
      ...(data.segments || []).map((item) => makeVideoCard(item)),
    );
  }

  if (data.final_url) {
    if (ui.finalUrl !== data.final_url) {
      ui.finalUrl = data.final_url;
      ui.finalVideo.src = data.final_url;
      ui.download.href = data.final_url;
      ui.download.download = data.final_filename || "long_video.mp4";
      ui.download.textContent = `下载完整视频：${data.final_filename || "long_video.mp4"}`;
    }
    ui.final.style.display = "block";
  } else {
    ui.final.style.display = "none";
  }

  ui.meta.textContent = data.log ? `日志：${data.log}` : "";
  ui.meta.title = data.log || "";
}

async function pollJob(node, ui, jobId) {
  if (!jobId) return;
  try {
    const response = await api.fetchApi(
      `/h3-long-video/status/${encodeURIComponent(jobId)}`,
      { cache: "no-store" },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    updatePanel(ui, node, data);
    if (data.status === "completed" || data.status === "failed") {
      stopPolling(node);
    }
  } catch (error) {
    ui.error.textContent = `读取任务状态失败：${error.message}`;
  }
}

function startPolling(node, ui, jobId) {
  stopPolling(node);
  node.properties ||= {};
  node.properties.h3_job_id = jobId;
  ui.jobId = jobId;
  const run = () => pollJob(node, ui, jobId);
  run();
  const timer = setInterval(run, 3000);
  ACTIVE_POLLERS.set(node.id, timer);
}

function stopPolling(node) {
  const timer = ACTIVE_POLLERS.get(node.id);
  if (timer) clearInterval(timer);
  ACTIVE_POLLERS.delete(node.id);
}

function findWidget(node, name) {
  return node.widgets?.find((widget) => widget.name === name);
}

function migrateWidgetValues(node) {
  const megapixels = findWidget(node, "megapixels");
  if (megapixels && typeof megapixels.value === "string") {
    const parsed = Number(megapixels.value);
    if (!Number.isFinite(parsed)) {
      megapixels.value = 0.2;
    }
  }
  const finalWidth = findWidget(node, "final_width");
  if (finalWidth && !Number.isFinite(Number(finalWidth.value))) {
    finalWidth.value = 704;
  }
  const finalHeight = findWidget(node, "final_height");
  if (finalHeight && !Number.isFinite(Number(finalHeight.value))) {
    finalHeight.value = 1216;
  }
}

function countSegments(node, ui) {
  const storyboard = findWidget(node, "storyboard");
  const mode = findWidget(node, "two_pass_mode");
  const steps = findWidget(node, "steps");
  const twoPassSteps = findWidget(node, "two_pass_steps");
  const sampler = findWidget(node, "sampler");
  const scheduler = findWidget(node, "scheduler");
  const megapixels = findWidget(node, "megapixels");
  const finalWidth = findWidget(node, "final_width");
  const finalHeight = findWidget(node, "final_height");
  node.lastTwoPassMode = mode?.value || "";
  node.samplingSummary = [
    `${steps?.value ?? "?"}+${twoPassSteps?.value ?? "?"} 步`,
    sampler?.value,
    scheduler?.value,
    `${megapixels?.value ?? "?"} MP`,
    `${finalWidth?.value ?? "?"}×${finalHeight?.value ?? "?"}`,
  ]
    .filter(Boolean)
    .join(" / ");
  try {
    const parsed = JSON.parse(storyboard?.value || "{}");
    const segments = Array.isArray(parsed) ? parsed : parsed.segments;
    node.storyboardCount = Array.isArray(segments) ? segments.length : 0;
    if (!Array.isArray(segments)) {
      ui.error.textContent = "Storyboard 里没有有效的 segments 数组。";
    } else {
      ui.error.textContent = "";
    }
  } catch (error) {
    node.storyboardCount = 0;
    ui.error.textContent = `Storyboard JSON 错误：${error.message}`;
  }
}

function installPanel(node) {
  ensureStyles();
  const root = makeElement("div", "h3-director-panel");

  const head = makeElement("div", "h3-director-head");
  const state = makeElement("div", "h3-director-state", "等待运行");
  const mode = makeElement("div", "h3-director-mode");
  const refresh = makeElement("button", "h3-director-refresh", "刷新预览");
  head.append(state, mode, refresh);

  const keyframeTitle = makeElement("div", "h3-director-section-title", "首尾关键帧（横向滚动）");
  const transitionTitle = makeElement(
    "div",
    "h3-director-section-title",
    "分镜转场（横向滚动）",
  );
  transitionTitle.style.display = "none";
  const transitions = makeElement("div", "h3-director-segments");
  transitions.style.display = "none";
  const segmentsTitle = makeElement("div", "h3-director-section-title", "分段视频（横向滚动）");
  const segments = makeElement("div", "h3-director-segments");
  const keyframes = makeElement("div", "h3-director-keyframes");
  const error = makeElement("div", "h3-director-error");

  const final = makeElement("div", "h3-director-final");
  final.style.display = "none";
  const finalTitle = makeElement("strong", "", "完整视频");
  const finalVideo = document.createElement("video");
  finalVideo.controls = true;
  finalVideo.preload = "metadata";
  const download = makeElement("a", "h3-director-download", "下载完整视频");
  final.append(finalTitle, finalVideo, download);

  const meta = makeElement("div", "h3-director-meta");
  root.append(
    head,
    keyframeTitle,
    keyframes,
    transitionTitle,
    transitions,
    segmentsTitle,
    segments,
    final,
    error,
    meta,
  );

  const ui = {
    state,
    mode,
    keyframes,
    transitionTitle,
    transitions,
    segments,
    final,
    finalVideo,
    download,
    error,
    meta,
    segmentSignature: "",
    keyframeSignature: "",
    transitionSignature: "",
    finalUrl: "",
    jobId: "",
  };
  node.h3DirectorUI = ui;
  node.addDOMWidget("h3_director_preview", "H3DirectorPreview", root, {
    serialize: false,
  });
  node.setSize([
    Math.max(node.size[0], 840),
    Math.min(1200, Math.max(node.size[1], 900)),
  ]);

  refresh.addEventListener("click", () => {
    countSegments(node, ui);
    const jobId = ui.jobId || node.properties?.h3_job_id;
    if (jobId) pollJob(node, ui, jobId);
  });

  const storyboard = findWidget(node, "storyboard");
  if (storyboard) {
    const original = storyboard.callback;
    storyboard.callback = function () {
      const result = original?.apply(this, arguments);
      countSegments(node, ui);
      return result;
    };
  }
  const modeWidget = findWidget(node, "two_pass_mode");
  if (modeWidget) {
    const original = modeWidget.callback;
    modeWidget.callback = function () {
      const result = original?.apply(this, arguments);
      countSegments(node, ui);
      return result;
    };
  }
  for (const name of [
    "steps",
    "two_pass_steps",
    "sampler",
    "scheduler",
    "megapixels",
    "final_width",
    "final_height",
  ]) {
    const widget = findWidget(node, name);
    if (!widget) continue;
    const original = widget.callback;
    widget.callback = function () {
      const result = original?.apply(this, arguments);
      countSegments(node, ui);
      return result;
    };
  }

  countSegments(node, ui);
  migrateWidgetValues(node);
  countSegments(node, ui);
  if (node.properties?.h3_job_id) {
    startPolling(node, ui, node.properties.h3_job_id);
  }
}

app.registerExtension({
  name: "H3LongVideo.DirectorPreview",
  nodeCreated(node) {
    if (node.comfyClass !== NODE_TYPE && node.type !== NODE_TYPE) return;
    installPanel(node);
    const originalExecuted = node.onExecuted;
    node.onExecuted = function (message) {
      const result = originalExecuted?.apply(this, arguments);
      const payload = message?.h3_director?.[0];
      if (payload?.job_id && node.h3DirectorUI) {
        startPolling(node, node.h3DirectorUI, payload.job_id);
      }
      return result;
    };
    const originalRemoved = node.onRemoved;
    node.onRemoved = function () {
      stopPolling(node);
      return originalRemoved?.apply(this, arguments);
    };
    const originalConfigure = node.onConfigure;
    node.onConfigure = function () {
      const result = originalConfigure?.apply(this, arguments);
      setTimeout(() => {
        migrateWidgetValues(this);
        countSegments(this, this.h3DirectorUI);
        app.graph.setDirtyCanvas(true, true);
      }, 0);
      return result;
    };
  },
});
