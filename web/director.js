import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_TYPE = "H3LongVideoDirector";
const ACTIVE_POLLERS = new Map();

function ensureStyles() {
  if (document.getElementById("h3-long-video-director-style")) return;
  const style = document.createElement("style");
  style.id = "h3-long-video-director-style";
  style.textContent = `
    .h3-director-panel { width: 100%; height: 560px; box-sizing: border-box; padding: 8px; color: #ddd; background: #181818; border-top: 1px solid #333; font-family: sans-serif; display: flex; flex-direction: column; overflow: hidden; }
    .h3-director-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex: 0 0 auto; }
    .h3-director-tabs { display: flex; gap: 4px; border-bottom: 1px solid #3a3a3a; flex: 0 0 auto; }
    .h3-director-tab { border: 1px solid #3a3a3a; border-bottom: none; background: #1e1e1e; color: #9aa4ae; border-radius: 5px 5px 0 0; padding: 4px 12px; font-size: 12px; cursor: pointer; margin-bottom: -1px; }
    .h3-director-tab:hover { background: #282828; }
    .h3-director-tab[aria-selected="true"] { background: #262626; color: #eee; border-color: #55585c; }
    .h3-director-tab-count { color: #7d868f; margin-left: 5px; }
    .h3-director-tab[aria-selected="true"] .h3-director-tab-count { color: #c7d0da; }
    .h3-director-panes { flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; }
    .h3-director-pane { display: none; padding: 8px 2px 4px; }
    .h3-director-pane[data-active="true"] { display: block; }
    .h3-director-empty { color: #7a828c; font-size: 12px; padding: 14px 2px; }
    .h3-director-state { flex: 1; font-size: 13px; font-weight: 600; }
    .h3-director-mode { color: #aaa; font-size: 12px; }
    .h3-director-refresh { border: 1px solid #555; background: #2a2a2a; color: #eee; border-radius: 4px; padding: 3px 8px; cursor: pointer; }
    .h3-director-refresh:hover { background: #393939; }
    .h3-director-section-title { margin: 8px 0 5px; color: #c7d0da; font-size: 12px; font-weight: 700; }
    .h3-director-keyframes { display: grid; grid-template-columns: repeat(auto-fill, minmax(188px, 1fr)); align-items: start; gap: 8px; margin-bottom: 10px; }
    .h3-director-keyframe-card { min-width: 0; border: 1px solid #5b4a2a; background: #19150f; border-radius: 6px; padding: 6px; }
    .h3-director-kf-head { display: flex; align-items: center; justify-content: space-between; gap: 4px; margin-bottom: 4px; }
    .h3-director-kf-kind { font-size: 10px; padding: 0 4px; border-radius: 3px; border: 1px solid #3d4b5c; color: #9fb4cc; }
    .h3-director-kf-kind[data-kind="cut"] { border-color: #8a5a2a; color: #ffbe7a; background: #241a0d; }
    .h3-director-kf-kind[data-kind="open"] { border-color: #4a5b3d; color: #b8d59f; }
    .h3-director-keyframe-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
    .h3-director-keyframe-pair figure { margin: 0; min-width: 0; }
    .h3-director-keyframe-pair a { display: block; }
    .h3-director-keyframe-pair img { display: block; width: 100%; height: auto; max-height: 240px; object-fit: contain; border-radius: 4px; background: #000; cursor: zoom-in; }
    .h3-director-keyframe-pair figcaption { color: #9fa8b3; text-align: center; font-size: 11px; margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .h3-director-kf-tag { font-size: 10px; padding: 0 4px; border-radius: 3px; border: 1px solid #5b4a2a; color: #d8bd7e; background: #241d0e; }
    .h3-director-kf-tag[data-source="manual"] { border-color: #3b6f55; color: #8fe0b4; background: #10251a; }
    .h3-director-kf-tag[data-source="pending"] { border-color: #a06a20; color: #ffd479; background: #2a1e08; }
    .h3-director-kf-tag[data-source="chain"] { border-color: #3d4b5c; color: #9fb4cc; background: #141a21; }
    .h3-director-kf-tag[data-source="stale"] { border-color: #8a5a1e; color: #f0be7a; background: #2a1c0a; }
    .h3-director-keyframe-card[data-pending="true"] { border-color: #a06a20; box-shadow: 0 0 0 1px #a06a20; }
    .h3-director-keyframe-pair figure[data-pending="true"] img { opacity: 0.4; }
    .h3-director-notice { margin-top: 6px; color: #9edcff; font-size: 12px; white-space: pre-wrap; flex: 0 0 auto; }
    .h3-director-notice[data-kind="warn"] { color: #ffd479; }
    .h3-director-notice[data-kind="error"] { color: #ff9a9a; }
    .h3-director-notice[data-kind="ok"] { color: #9ef0b8; }
    .h3-director-backend { margin: 0 0 8px; padding: 6px 9px; border: 1px solid #5a4a2a; background: #221c12; color: #d8bd7e; border-radius: 4px; font-size: 11px; line-height: 1.5; }
    .h3-director-kf-actions { display: flex; gap: 3px; margin-top: 4px; }
    .h3-director-kf-empty { display: flex; align-items: center; justify-content: center; width: 100%; aspect-ratio: 9 / 16; max-height: 148px; border: 1px dashed #4a4a52; border-radius: 4px; background: #1b1b20; color: #8a8a96; font-size: 11px; text-align: center; padding: 4px; }
    .h3-director-kf-actions button { flex: 1 1 0; min-width: 0; border: 1px solid #555; background: #2a2a2a; color: #eee; border-radius: 3px; padding: 2px 0; font-size: 11px; line-height: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
    .h3-director-kf-actions button:hover { background: #3a3a3a; }
    .h3-director-kf-actions button:disabled { opacity: 0.45; cursor: default; }
    .h3-director-review { display: none; margin: 6px 0; border: 1px solid #7a6a2a; background: #221c0c; border-radius: 6px; padding: 6px 10px; align-items: center; gap: 10px; flex-wrap: wrap; }
    .h3-director-review-title { color: #ffd479; font-size: 13px; font-weight: 700; white-space: nowrap; }
    .h3-director-review-hint { color: #c7b183; font-size: 12px; line-height: 1.4; flex: 1 1 220px; min-width: 0; }
    .h3-director-review-actions { display: flex; gap: 8px; align-items: center; flex: 0 0 auto; }
    .h3-director-refimg { height: 84px; width: auto; max-width: 120px; border-radius: 4px; background: #000; cursor: zoom-in; flex: 0 0 auto; }
    .h3-director-go { border: 1px solid #3b6f55; background: #1d3a2a; color: #b9f2d3; border-radius: 4px; padding: 5px 12px; font-size: 13px; font-weight: 600; cursor: pointer; }
    .h3-director-go:hover { background: #265039; }
    .h3-director-go:disabled { opacity: 0.5; cursor: default; }
    .h3-director-segments { display: grid; grid-template-columns: repeat(auto-fill, minmax(214px, 1fr)); align-items: start; gap: 8px; }
    .h3-director-card { min-width: 0; border: 1px solid #34404c; background: #11161c; border-radius: 6px; padding: 6px; }
    .h3-director-card-head { display: flex; justify-content: space-between; gap: 6px; margin-bottom: 5px; color: #cfd8e3; font-size: 12px; }
    .h3-director-card video { display: block; width: 100%; height: 220px; object-fit: contain; border-radius: 4px; background: #000; }
    .h3-director-final { border: 1px solid #3b6f55; background: #101b15; border-radius: 6px; padding: 8px; }
    .h3-director-final video { display: block; width: 100%; height: 300px; object-fit: contain; border-radius: 4px; background: #000; margin-bottom: 6px; }
    .h3-director-download { display: inline-block; color: #9edcff; text-decoration: none; font-size: 13px; }
    .h3-director-download:hover { text-decoration: underline; }
    .h3-director-meta { color: #87919c; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .h3-director-jobs { max-width: 280px; border: 1px solid #555; background: #2a2a2a; color: #eee; border-radius: 4px; padding: 3px 6px; font-size: 12px; }
    .h3-director-kf-script { display: flex; align-items: center; gap: 6px; margin-top: 5px; padding-top: 5px; border-top: 1px dashed #3c352a; font-size: 11px; color: #9fa8b3; }
    .h3-director-kf-secs { flex: 0 0 auto; color: #d8bd7e; }
    .h3-director-kf-prompt { flex: 1 1 0; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
    .h3-director-kf-prompt:hover { color: #cfe0f2; }
    .h3-director-kf-edit { flex: 0 0 auto; border: 1px solid #4a5b6b; background: #1a2028; color: #9fb4cc; border-radius: 3px; padding: 1px 6px; font-size: 11px; cursor: pointer; }
    .h3-director-kf-edit:hover { background: #243040; color: #cfe0f2; }
    .h3-director-editor { display: none; border: 1px solid #4a5b6b; background: #12181f; border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; box-sizing: border-box; max-height: 366px; overflow-y: auto; }
    .h3-director-panel[data-fullscreen="true"] .h3-director-editor { max-height: 64vh; }
    .h3-director-editor[data-open="true"] { display: block; }
    .h3-director-editor-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
    .h3-director-editor-title { flex: 1; color: #cfe0f2; font-size: 13px; font-weight: 600; }
    .h3-director-editor textarea { width: 100%; box-sizing: border-box; min-height: 130px; resize: vertical; border: 1px solid #3d4b5c; background: #0d1117; color: #dbe4ee; border-radius: 4px; padding: 6px 8px; font-size: 12px; line-height: 1.55; font-family: inherit; }
    .h3-director-editor textarea:focus { outline: 1px solid #4a7ba8; }
    .h3-director-editor-row { display: flex; align-items: center; gap: 10px; margin: 6px 0; color: #9fb4cc; font-size: 12px; flex-wrap: wrap; }
    .h3-director-editor-row input[type="number"] { width: 78px; box-sizing: border-box; border: 1px solid #3d4b5c; background: #0d1117; color: #dbe4ee; border-radius: 4px; padding: 3px 6px; font-size: 12px; }
    .h3-director-editor-actions { display: flex; gap: 8px; margin-top: 6px; align-items: center; flex-wrap: wrap; position: sticky; bottom: 0; background: #12181f; padding: 6px 0 2px; }
    .h3-director-editor-actions button { border: 1px solid #555; background: #262626; color: #eee; border-radius: 4px; padding: 4px 12px; cursor: pointer; font-size: 12px; }
    .h3-director-editor-actions button:hover { background: #363636; }
    .h3-director-editor-actions button[data-primary="true"] { border-color: #3b6f55; background: #1d3a2a; color: #b9f2d3; }
    .h3-director-editor-hint { color: #8a949f; font-size: 11px; flex: 1 1 220px; }
    .h3-director-editor-force { display: flex; align-items: center; gap: 4px; color: #d8b26a; font-size: 11px; cursor: pointer; }
    .h3-director-editor-body { display: flex; gap: 10px; align-items: stretch; flex-wrap: wrap; }
    .h3-director-editor-texts { flex: 1 1 340px; min-width: 240px; display: flex; flex-direction: column; gap: 8px; }
    .h3-director-editor-texts label { display: flex; align-items: baseline; gap: 6px; color: #9fb4cc; font-size: 11px; }
    .h3-director-editor-texts label b { color: #cfe0f2; font-weight: 600; font-size: 11px; }
    .h3-director-editor-folds summary { display: flex; align-items: baseline; gap: 6px; color: #9fb4cc; font-size: 11px; cursor: pointer; list-style: none; padding: 1px 0; }
    .h3-director-editor-folds summary::-webkit-details-marker { display: none; }
    .h3-director-editor-folds summary::before { content: "▸"; color: #7d8a99; }
    .h3-director-editor-folds[open] summary::before { content: "▾"; }
    .h3-director-editor-folds summary b { color: #cfe0f2; font-weight: 600; font-size: 11px; }
    .h3-director-editor-folds summary em { font-style: normal; color: #6f7d8b; }
    .h3-director-editor-folds textarea { margin-top: 3px; }
    .h3-director-editor-texts label em { font-style: normal; color: #6f7d8b; }
    .h3-director-editor-texts textarea { height: 76px; min-height: 76px; }
    .h3-director-editor-texts textarea[data-main="true"] { height: 96px; min-height: 96px; }
    .h3-director-editor-frames { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; flex: 0 0 200px; align-content: start; }
    .h3-director-editor-frames figure { margin: 0; min-width: 0; }
    .h3-director-editor-frames img { display: block; width: 100%; max-height: 210px; object-fit: contain; border-radius: 4px; background: #000; }
    .h3-director-editor-frames figcaption { color: #87919c; font-size: 11px; text-align: center; margin-top: 2px; }
    .h3-director-editor-body textarea { flex: 0 0 auto; width: 100%; box-sizing: border-box; min-width: 0; }
    .h3-director-kf-tail { border: 1px solid #4a5b6b; background: #1b2836; color: #9ec5e8; border-radius: 3px; padding: 0 4px; font-size: 10px; line-height: 15px; white-space: nowrap; }
    .h3-director-kf-head { border: 1px solid #4a6b57; background: #17281f; color: #9fe0b8; border-radius: 3px; padding: 0 4px; font-size: 10px; line-height: 15px; white-space: nowrap; }
    .h3-director-error { margin-top: 6px; color: #ffb1b1; font-size: 12px; white-space: pre-wrap; flex: 0 0 auto; }

    /* 全屏：面板整体搬到一个 fixed 遮罩里，脱离画布的尺寸限制 */
    .h3-director-overlay { position: fixed; inset: 0; z-index: 99990; display: none; padding: 20px; box-sizing: border-box; background: rgba(6, 7, 9, 0.88); }
    .h3-director-overlay[data-open="true"] { display: block; }
    .h3-director-overlay > .h3-director-panel { box-shadow: 0 24px 70px rgba(0, 0, 0, 0.65); }
    .h3-director-panel[data-fullscreen="true"] { height: 100% !important; width: 100% !important; border: 1px solid #3a3a3a; border-radius: 10px; padding: 12px; }
    .h3-director-panel[data-fullscreen="true"] .h3-director-keyframes { grid-template-columns: repeat(auto-fill, minmax(288px, 1fr)); gap: 12px; }
    .h3-director-panel[data-fullscreen="true"] .h3-director-segments { grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
    .h3-director-panel[data-fullscreen="true"] .h3-director-keyframe-pair img { height: auto; aspect-ratio: 9 / 16; max-height: 44vh; }
    .h3-director-panel[data-fullscreen="true"] .h3-director-card video { height: auto; aspect-ratio: 9 / 16; max-height: 46vh; }
    .h3-director-panel[data-fullscreen="true"] .h3-director-final video { height: auto; aspect-ratio: 9 / 16; max-height: 66vh; margin-left: auto; margin-right: auto; width: auto; max-width: 100%; }
    .h3-director-panel[data-fullscreen="true"] .h3-director-resize { display: none; }
    .h3-director-placeholder { width: 100%; box-sizing: border-box; border: 1px dashed #3c3c3c; border-radius: 6px; background: repeating-linear-gradient(45deg, #151515, #151515 10px, #191919 10px, #191919 20px); }

    /* 底部拖柄：改面板高度 */
    .h3-director-resize { flex: 0 0 auto; height: 9px; margin: 4px -8px -8px; cursor: ns-resize; border-top: 1px solid #2f2f2f; background: linear-gradient(#232323, #1a1a1a); display: flex; align-items: center; justify-content: center; }
    .h3-director-resize::after { content: ""; width: 46px; height: 3px; border-radius: 2px; background: #4a4a4a; }
    .h3-director-resize:hover::after { background: #6d6d6d; }

    /* 灯箱：单张图/视频拉满整屏，左右键翻页 */
    .h3-director-lightbox { position: fixed; inset: 0; z-index: 99999; display: none; flex-direction: column; background: rgba(0, 0, 0, 0.94); }
    .h3-director-lightbox[data-open="true"] { display: flex; }
    .h3-director-lb-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; color: #dedede; font-size: 13px; flex: 0 0 auto; }
    .h3-director-lb-title { flex: 1; font-weight: 600; }
    .h3-director-lb-close { border: 1px solid #4a4a4a; background: #232323; color: #ddd; border-radius: 4px; padding: 3px 10px; cursor: pointer; font-size: 12px; }
    .h3-director-lb-close:hover { background: #353535; }
    .h3-director-lb-body { position: relative; flex: 1 1 auto; min-height: 0; display: flex; align-items: center; justify-content: center; padding: 0 58px; }
    .h3-director-lb-body img, .h3-director-lb-body video { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 6px; background: #000; }
    .h3-director-lb-body video { display: none; }
    .h3-director-card-head { align-items: center; }
    .h3-director-zoom { border: 1px solid #4a5b6b; background: #1a2028; color: #9fb4cc; border-radius: 3px; padding: 0 5px; font-size: 11px; line-height: 16px; cursor: pointer; flex: 0 0 auto; }
    .h3-director-zoom:hover { background: #243040; color: #cfe0f2; }
    .h3-director-final-tools { display: flex; align-items: center; gap: 10px; }
    .h3-director-lb-nav { position: absolute; top: 50%; transform: translateY(-50%); width: 42px; height: 84px; border: none; border-radius: 6px; background: rgba(255, 255, 255, 0.08); color: #fff; font-size: 22px; cursor: pointer; }
    .h3-director-lb-nav:hover:not(:disabled) { background: rgba(255, 255, 255, 0.18); }
    .h3-director-lb-nav:disabled { opacity: 0.25; cursor: default; }
    .h3-director-lb-nav[data-dir="prev"] { left: 8px; }
    .h3-director-lb-nav[data-dir="next"] { right: 8px; }
    .h3-director-lb-foot { display: flex; gap: 8px; padding: 10px 14px; justify-content: center; flex: 0 0 auto; }
    .h3-director-lb-foot button { border: 1px solid #555; background: #262626; color: #eee; border-radius: 4px; padding: 5px 14px; cursor: pointer; font-size: 12px; }
    .h3-director-lb-foot button:hover:not(:disabled) { background: #363636; }
    .h3-director-lb-foot button:disabled { opacity: 0.4; cursor: default; }
    .h3-director-lb-hint { color: #7f8994; font-size: 11px; }
  `;
  document.head.appendChild(style);
}

// 面板可能被搬进独立窗口（另一个 document），样式得跟着复制一份
function copyPanelStyles(doc) {
  if (!doc || doc === document) return;
  if (!doc.getElementById("h3-long-video-director-style")) {
    const source = document.getElementById("h3-long-video-director-style");
    if (source) doc.head.appendChild(source.cloneNode(true));
  }
  if (!doc.getElementById("h3-director-host-base")) {
    const base = doc.createElement("style");
    base.id = "h3-director-host-base";
    base.textContent =
      "html,body{margin:0;height:100%;background:#181818;overflow:hidden;}";
    doc.head.appendChild(base);
  }
}

function makeElement(tag, className, text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function makeVideoCard(data, handlers = {}) {
  const card = makeElement("div", "h3-director-card");
  const head = makeElement("div", "h3-director-card-head");
  const title = makeElement("strong", "", `第 ${data.index} 段`);
  const details = makeElement(
    "span",
    "",
    `${data.seconds ?? "?"} 秒 · ${data.two_pass ? "二采" : "一采"}`,
  );
  const zoom = makeElement("button", "h3-director-zoom", "⤢");
  zoom.title = "全屏看这段（← → 在分段之间翻页）";
  zoom.addEventListener("click", () => handlers.onZoomSegment?.(data));
  head.append(title, details, zoom);
  const video = document.createElement("video");
  video.controls = true;
  video.preload = "metadata";
  video.src = data.url;
  card.append(head, video);
  return card;
}

const KEYFRAME_SOURCE_LABELS = {
  ai: "AI 生成",
  manual: "已替换",
  explicit: "指定首帧",
  hold: "定格",
  "opening-draft": "开场草案",
  free: "自由尾帧",
  "from-video": "接视频末帧",
  "pending-video": "待定首帧",
};

function keyframeSourceLabel(source) {
  return KEYFRAME_SOURCE_LABELS[source] || "AI 生成";
}

// ---------------------------------------------------------------------------
// 灯箱：把单张关键帧拉到整屏看，左右键在同一部片子的所有帧之间翻页
// ---------------------------------------------------------------------------
const LIGHTBOX = {
  root: null,
  image: null,
  video: null,
  title: null,
  tag: null,
  detail: null,
  counter: null,
  prev: null,
  next: null,
  foot: null,
  replace: null,
  reroll: null,
  reset: null,
  items: [],
  pos: 0,
  ui: null,
  kind: "keyframes",
};

function flattenKeyframes(keyframes) {
  const items = [];
  for (const item of keyframes || []) {
    if (item.start_url) items.push({
      key: `kf-${item.index}-start`,
      kind: "image",
      index: item.index,
      side: "start",
      label: `第 ${item.index} 段 · 开始`,
      url: item.start_url,
      source: item.start_source,
      overridden: item.start_overridden,
      hasOriginal: item.start_has_original,
      pending: item.start_pending,
      attempt: item.start_attempt,
      inherited: item.start_inherited,
      rerollable: !item.chain,
    });
    if (item.end_url) items.push({
      key: `kf-${item.index}-end`,
      kind: "image",
      index: item.index,
      side: "end",
      label: `第 ${item.index} 段 · 结束`,
      url: item.end_url,
      source: item.end_source,
      overridden: item.end_overridden,
      hasOriginal: item.end_has_original,
      pending: item.end_pending,
      attempt: item.end_attempt,
      inherited: false,
      rerollable: item.end_source !== "hold",
    });
  }
  return items;
}

function flattenSegments(segments) {
  return (segments || []).map((item) => ({
    key: `seg-${item.index}`,
    kind: "video",
    index: item.index,
    label: `第 ${item.index} 段 · 视频`,
    detail: `${item.seconds ?? "?"} 秒 · ${item.two_pass ? "二采" : "一采"}`,
    url: item.url,
  }));
}

function flattenTransitions(transitions) {
  return (transitions || []).map((item) => ({
    key: `tr-${item.index}`,
    kind: "video",
    index: item.index,
    label: `转场 · 进入第 ${item.index} 段`,
    detail: `${item.seconds ?? "?"} 秒`,
    url: item.url,
  }));
}

const LIGHTBOX_SETS = {
  keyframes: (ui) => flattenKeyframes(ui.lastKeyframes),
  segments: (ui) => flattenSegments(ui.lastSegments),
  transitions: (ui) => flattenTransitions(ui.lastTransitions),
  final: (ui) =>
    ui.lastFinal?.url
      ? [
          {
            key: "final",
            kind: "video",
            label: "成片",
            detail: ui.lastFinal.filename || "",
            url: ui.lastFinal.url,
          },
        ]
      : [],
};

// 灯箱要开在「面板所在的 document」里，否则面板被搬进独立窗口后点了没反应
let LIGHTBOX_DOC = null;
const lightboxDoc = () => LIGHTBOX_DOC || document;

function bindLightboxKeys(doc) {
  if (!doc || doc.__h3LightboxKeys) return;
  doc.__h3LightboxKeys = true;
  doc.addEventListener("keydown", (event) => {
    if (!LIGHTBOX.root || LIGHTBOX.root.dataset.open !== "true") return;
    if (event.key === "Escape") {
      closeLightbox();
      event.preventDefault();
    } else if (event.key === "ArrowLeft") {
      moveLightbox(-1);
      event.preventDefault();
    } else if (event.key === "ArrowRight") {
      moveLightbox(1);
      event.preventDefault();
    }
  });
}

function moveLightboxToDoc(doc) {
  LIGHTBOX_DOC = doc || document;
  copyPanelStyles(LIGHTBOX_DOC);
  bindLightboxKeys(LIGHTBOX_DOC);
  if (LIGHTBOX.root && LIGHTBOX.root.ownerDocument !== LIGHTBOX_DOC) {
    LIGHTBOX_DOC.body.appendChild(LIGHTBOX.root);
  }
}

function ensureLightbox() {
  if (LIGHTBOX.root) return LIGHTBOX;
  const root = makeElement("div", "h3-director-lightbox");
  const head = makeElement("div", "h3-director-lb-head");
  const title = makeElement("div", "h3-director-lb-title", "");
  const tag = makeElement("span", "h3-director-kf-tag", "");
  const detail = makeElement("span", "h3-director-lb-hint", "");
  const counter = makeElement("span", "h3-director-lb-hint", "");
  const close = makeElement("button", "h3-director-lb-close", "关闭 (Esc)");
  head.append(title, tag, detail, counter, close);

  const body = makeElement("div", "h3-director-lb-body");
  const image = document.createElement("img");
  const video = document.createElement("video");
  video.controls = true;
  video.autoplay = true;
  video.playsInline = true;
  const prev = makeElement("button", "h3-director-lb-nav", "‹");
  prev.dataset.dir = "prev";
  const next = makeElement("button", "h3-director-lb-nav", "›");
  next.dataset.dir = "next";
  body.append(image, video, prev, next);

  const foot = makeElement("div", "h3-director-lb-foot");
  const replace = makeElement("button", "", "替换");
  const reroll = makeElement("button", "", "重生成");
  const reset = makeElement("button", "", "还原");
  foot.append(replace, reroll, reset);

  root.append(head, body, foot);
  const doc = lightboxDoc();
  copyPanelStyles(doc);
  doc.body.appendChild(root);
  bindLightboxKeys(doc);

  close.addEventListener("click", () => closeLightbox());
  prev.addEventListener("click", () => moveLightbox(-1));
  next.addEventListener("click", () => moveLightbox(1));
  replace.addEventListener("click", () => {
    const item = LIGHTBOX.items[LIGHTBOX.pos];
    if (item) LIGHTBOX.ui?.keyframeHandlers?.onReplace?.(item.index, item.side);
  });
  reroll.addEventListener("click", () => {
    const item = LIGHTBOX.items[LIGHTBOX.pos];
    if (item) LIGHTBOX.ui?.keyframeHandlers?.onReroll?.(item.index, item.side);
  });
  reset.addEventListener("click", () => {
    const item = LIGHTBOX.items[LIGHTBOX.pos];
    if (item) LIGHTBOX.ui?.keyframeHandlers?.onReset?.(item.index, item.side);
  });

  Object.assign(LIGHTBOX, {
    root,
    image,
    video,
    title,
    tag,
    detail,
    counter,
    prev,
    next,
    foot,
    replace,
    reroll,
    reset,
  });
  return LIGHTBOX;
}

function renderLightbox() {
  const item = LIGHTBOX.items[LIGHTBOX.pos];
  if (!item) return;
  const isVideo = item.kind === "video";
  LIGHTBOX.title.textContent = item.label;

  if (isVideo) {
    LIGHTBOX.image.style.display = "none";
    LIGHTBOX.image.removeAttribute("src");
    LIGHTBOX.video.style.display = "block";
    if (LIGHTBOX.video.getAttribute("src") !== item.url) {
      LIGHTBOX.video.src = item.url;
      LIGHTBOX.video.play?.().catch(() => {});
    }
  } else {
    LIGHTBOX.video.pause?.();
    LIGHTBOX.video.style.display = "none";
    LIGHTBOX.video.removeAttribute("src");
    LIGHTBOX.image.style.display = "block";
    LIGHTBOX.image.src = item.url;
    LIGHTBOX.image.alt = item.label;
  }

  LIGHTBOX.tag.style.display = isVideo ? "none" : "";
  LIGHTBOX.detail.textContent = item.detail || "";
  if (!isVideo) {
    LIGHTBOX.tag.textContent = item.pending
      ? `待重画 #${item.attempt || 1}`
      : item.inherited
        ? "继承上段尾帧"
        : keyframeSourceLabel(item.source);
    LIGHTBOX.tag.dataset.source = item.pending
      ? "pending"
      : item.inherited
        ? "chain"
        : item.source || "ai";
  }

  LIGHTBOX.counter.textContent = `${LIGHTBOX.pos + 1} / ${LIGHTBOX.items.length}`;
  LIGHTBOX.prev.disabled = LIGHTBOX.pos <= 0;
  LIGHTBOX.next.disabled = LIGHTBOX.pos >= LIGHTBOX.items.length - 1;

  // 只有关键帧才谈得上「替换 / 重画 / 还原」
  LIGHTBOX.foot.style.display = isVideo ? "none" : "flex";
  if (!isVideo) {
    LIGHTBOX.replace.disabled = false;
    LIGHTBOX.reroll.disabled = !item.rerollable || Boolean(item.pending);
    LIGHTBOX.reroll.title = item.rerollable
      ? "让 AI 换一个 seed 重画这一帧"
      : "这一帧是继承/定格得来的，无法单独重生成";
    LIGHTBOX.reset.disabled = !item.overridden || !item.hasOriginal;
    LIGHTBOX.reset.title = item.hasOriginal
      ? "还原成 AI 原本生成的那张图（不重画）"
      : "这张图没有可还原的原图（外部灌进来的）；要重出用「重画」";
  }
}

function moveLightbox(delta) {
  const pos = LIGHTBOX.pos + delta;
  if (pos < 0 || pos >= LIGHTBOX.items.length) return;
  LIGHTBOX.pos = pos;
  renderLightbox();
}

function openLightbox(ui, kind, key) {
  ensureLightbox();
  const build = LIGHTBOX_SETS[kind] || LIGHTBOX_SETS.keyframes;
  const items = build(ui);
  if (!items.length) return;
  LIGHTBOX.ui = ui;
  LIGHTBOX.kind = kind;
  LIGHTBOX.items = items;
  const pos = items.findIndex((item) => item.key === key);
  LIGHTBOX.pos = pos >= 0 ? pos : 0;
  LIGHTBOX.root.dataset.open = "true";
  renderLightbox();
}

function syncLightbox(ui) {
  if (!LIGHTBOX.root || LIGHTBOX.root.dataset.open !== "true") return;
  const current = LIGHTBOX.items[LIGHTBOX.pos];
  const build = LIGHTBOX_SETS[LIGHTBOX.kind] || LIGHTBOX_SETS.keyframes;
  const items = build(ui);
  if (!items.length) {
    closeLightbox();
    return;
  }
  LIGHTBOX.items = items;
  const pos = current
    ? items.findIndex((item) => item.key === current.key)
    : -1;
  LIGHTBOX.pos = pos >= 0 ? pos : Math.min(LIGHTBOX.pos, items.length - 1);
  renderLightbox();
}

function closeLightbox() {
  if (!LIGHTBOX.root) return;
  LIGHTBOX.root.dataset.open = "false";
  LIGHTBOX.video?.pause?.();
}

bindLightboxKeys(document);

async function postJson(url, body) {
  const response = await api.fetchApi(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function postFormData(url, formData) {
  const response = await api.fetchApi(url, { method: "POST", body: formData });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function makeKeyframeCard(data, handlers = {}) {
  const card = makeElement("div", "h3-director-keyframe-card");
  const head = makeElement("div", "h3-director-kf-head");
  head.appendChild(makeElement("strong", "", `第 ${data.index} 段关键帧`));
  const kind = makeElement(
    "span",
    "h3-director-kf-kind",
    data.index === 1 ? "开场" : data.chain ? "连续" : "切点",
  );
  kind.dataset.kind =
    data.index === 1 ? "open" : data.chain ? "chain" : "cut";
  kind.title =
    data.index === 1
      ? "全片第一帧"
      : data.chain
        ? "接着上一段的尾帧继续拍，首帧继承上一段尾帧"
        : "切点：这一段的开头是独立画面，跟上一段尾帧无关";
  head.appendChild(kind);
  card.appendChild(head);
  const pair = makeElement("div", "h3-director-keyframe-pair");
  const sides = [
    {
      side: "start",
      label: "开始",
      url: data.start_url,
      source: data.start_source,
      overridden: data.start_overridden,
      hasOriginal: data.start_has_original,
      pending: Boolean(data.start_pending),
      attempt: Number(data.start_attempt || 0),
      inherited: Boolean(data.start_inherited),
      // 上一段尾帧刚重画、这一段还没轮到：还按上一段展示，但标记待同步
      stale: Boolean(data.start_stale),
      // A chained start frame is inherited from the previous segment, so it
      // has no independent draft that could be re-rolled.
      rerollable: !data.chain,
    },
    {
      side: "end",
      label: "结束",
      url: data.end_url,
      source: data.end_source,
      overridden: data.end_overridden,
      hasOriginal: data.end_has_original,
      pending: Boolean(data.end_pending),
      attempt: Number(data.end_attempt || 0),
      inherited: false,
      // A free / pending end has no image to re-roll: the segment simply ends
      // wherever the model takes it.
      rerollable: !["hold", "free", "pending-video"].includes(data.end_source),
    },
  ];
  if (sides.some((item) => item.pending)) card.dataset.pending = "true";
  for (const { side, label, url, source, overridden, hasOriginal, pending, attempt, inherited, stale, rerollable } of sides) {
    if (!url) {
      // Placeholder slot: free end (no anchor at all) or pending start (the
      // previous segment ends freely, so this frame only exists after that
      // render finishes).
      const figure = document.createElement("figure");
      figure.dataset.empty = "true";
      const slot = makeElement(
        "div",
        "h3-director-kf-empty",
        side === "end" ? "自由尾帧" : "待定首帧",
      );
      slot.title =
        side === "end"
          ? "这一段没有尾帧锚点：模型自由收尾，下一段会用它真实的最后一帧当首帧"
          : "上一段是自由尾帧，这一帧要等那段渲染完成后才确定";
      const caption = makeElement("figcaption", "", label);
      const tag = makeElement(
        "span",
        "h3-director-kf-tag",
        side === "end" ? "自由" : "待定",
      );
      tag.dataset.source = side === "end" ? "free" : "pending-video";
      tag.title = slot.title;
      caption.append(" ", tag);
      const actions = makeElement("div", "h3-director-kf-actions");
      const replaceButton = makeElement("button", "", "替换");
      replaceButton.title = "上传一张图，把这一侧改成固定锚点";
      replaceButton.addEventListener("click", () =>
        handlers.onReplace?.(data.index, side),
      );
      actions.append(replaceButton);
      figure.append(slot, caption, actions);
      pair.appendChild(figure);
      continue;
    }
    const figure = document.createElement("figure");
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = "点击全屏查看（← → 翻页，Esc 关闭）";
    const image = document.createElement("img");
    image.loading = "lazy";
    image.src = url;
    link.appendChild(image);
    link.addEventListener("click", (event) => {
      if (!handlers.onZoom) return;
      event.preventDefault();
      handlers.onZoom(data.index, side);
    });
    if (pending) figure.dataset.pending = "true";

    const caption = makeElement("figcaption", "", label);
    // 卡片一格只有 ~90px，标签最多四个字，否则会被省略号吃掉；
    // 完整说法（含第几次重画）放到 title 和灯箱里
    const tag = makeElement(
      "span",
      "h3-director-kf-tag",
      pending
        ? "待重画"
        : stale
          ? "待同步"
          : inherited
            ? "继承"
            : keyframeSourceLabel(source),
    );
    tag.dataset.source = pending
      ? "pending"
      : stale
        ? "stale"
        : inherited
          ? "chain"
          : source || "ai";
    if (stale) {
      tag.title =
        "上一段的尾帧刚变过，这一帧还等着按新尾帧重出。点「确认关键帧」或再跑一次关键帧阶段就会跟上";
    } else if (inherited) {
      tag.title = "这一帧就是上一段的尾帧，上一段重画后会自动跟着变";
    } else if (pending) {
      tag.title = `已标记重画（第 ${attempt || 1} 次）`;
    }
    caption.append(" ", tag);

    const actions = makeElement("div", "h3-director-kf-actions");
    const replaceButton = makeElement("button", "", "替换");
    // 卡片一格只有 ~90px，三个按钮必须都是两个字，否则会被截成「重…」
    const rerollButton = makeElement("button", "", pending ? "重画中" : "重画");
    const resetButton = makeElement("button", "", "还原");
    resetButton.disabled = !overridden || !hasOriginal;
    resetButton.title = hasOriginal
      ? "还原成 AI 原本生成的那张图（不重画，也不排队重画）"
      : "这张图是从外部灌进来的，没有原图可还原；要按提示词重出用「重画」，要换图用「替换」";
    rerollButton.disabled = !rerollable;
    rerollButton.title = pending
      ? "这一帧已标记重画，点一下立即开始（会换一个新的 seed）"
      : rerollable
        ? "让 AI 换一个 seed 立即重画这一帧"
        : "这一帧是继承/定格得来的，无法单独重生成";
    replaceButton.addEventListener("click", () => handlers.onReplace?.(data.index, side));
    rerollButton.addEventListener("click", () => handlers.onReroll?.(data.index, side));
    resetButton.addEventListener("click", () => handlers.onReset?.(data.index, side));
    actions.append(replaceButton, rerollButton, resetButton);

    figure.append(link, caption, actions);
    pair.appendChild(figure);
  }
  card.appendChild(pair);

  // 这一段的秒数和提示词：直接摊在关键帧下面，点一下就能改，
  // 不用再去翻 storyboard 那一大段 JSON。
  const script = data.script || null;
  const scriptRow = makeElement("div", "h3-director-kf-script");
  const seconds = script?.seconds ?? data.seconds;
  const secsLabel = makeElement(
    "span",
    "h3-director-kf-secs",
    seconds === undefined || seconds === null ? "? 秒" : `${seconds} 秒`,
  );
  const promptText = String(script?.prompt || "");
  const promptPreview = makeElement(
    "span",
    "h3-director-kf-prompt",
    promptText || "（这段还没有提示词）",
  );
  promptPreview.title = promptText;
  const editButton = makeElement("button", "h3-director-kf-edit", "改词");
  editButton.title = "改这一段的秒数和提示词（关键帧就在上面，边看边改）";
  promptPreview.addEventListener("click", () =>
    handlers.onEditScript?.(data.index),
  );
  editButton.addEventListener("click", () => handlers.onEditScript?.(data.index));
  scriptRow.append(secsLabel, promptPreview);
  const headPromptText = String(script?.start_frame_prompt || "").trim();
  if (headPromptText) {
    const headBadge = makeElement("span", "h3-director-kf-head", "首帧词");
    headBadge.title = `首帧单独用这句：\n${headPromptText}`;
    headBadge.addEventListener("click", () =>
      handlers.onEditScript?.(data.index),
    );
    scriptRow.append(headBadge);
  }
  const endPromptText = String(script?.end_frame_prompt || "").trim();
  if (endPromptText) {
    const tailBadge = makeElement("span", "h3-director-kf-tail", "尾帧词");
    tailBadge.title = `尾帧单独用这句：\n${endPromptText}`;
    tailBadge.addEventListener("click", () =>
      handlers.onEditScript?.(data.index),
    );
    scriptRow.append(tailBadge);
  }
  scriptRow.append(editButton);
  card.appendChild(scriptRow);
  return card;
}

function makeTransitionCard(data, handlers = {}) {
  const card = makeElement("div", "h3-director-card");
  const head = makeElement("div", "h3-director-card-head");
  const title = makeElement("strong", "", `进入第 ${data.index} 段`);
  const details = makeElement("span", "", `${data.seconds ?? "?"} 秒`);
  const zoom = makeElement("button", "h3-director-zoom", "⤢");
  zoom.title = "全屏看这段转场";
  zoom.addEventListener("click", () => handlers.onZoomTransition?.(data));
  head.append(title, details, zoom);
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
    interrupted: "已中断",
    unknown: "等待状态",
  };
  const phaseLabels = {
    keyframes: `关键帧 ${data.completed_keyframes || 0}/${total || "?"}`,
    videos: `视频 ${done}/${total || "?"}`,
  };
  if (status === "awaiting_review") {
    ui.state.textContent = `关键帧待确认 ${data.completed_keyframes || 0}/${total || "?"}`;
  } else if (status === "interrupted") {
    ui.state.textContent = `已中断 · ${
      phaseLabels[data.phase] || countText.trim()
    }`;
  } else {
    ui.state.textContent =
      phaseLabels[data.phase] || `${labels[status] || status}${countText}`;
  }
  ui.mode.textContent = [node.lastTwoPassMode, node.samplingSummary]
    .filter(Boolean)
    .join(" · ");
  ui.error.textContent = data.error || "";

  // 关键帧来源提示：选了 Qwen / 只用手动图时，H3 不生成关键帧，得自己放图。
  const backend = data.keyframe_backend || "h3";
  const externalBackend = backend !== "h3";
  if (ui.backendNote) {
    if (externalBackend && data.keyframe_backend_label) {
      ui.backendNote.style.display = "block";
      ui.backendNote.textContent =
        `关键帧来源：${data.keyframe_backend_label} · ` +
        `${data.keyframe_backend_hint || "自己出图"} · ` +
        "出图后点对应卡片上的「替换」，全部放好再确认生成视频";
    } else {
      ui.backendNote.style.display = "none";
    }
  }

  const scriptList = Array.isArray(data.script) ? data.script : [];
  ui.scriptMap = new Map(scriptList.map((entry) => [Number(entry.index), entry]));
  const keyframeSignature = JSON.stringify(
    (data.keyframes || []).map((item) => {
      const script = ui.scriptMap.get(Number(item.index)) || null;
      return [
        item.index,
        item.start_url,
        item.end_url,
        item.start_source,
        item.end_source,
        item.start_overridden,
        item.end_overridden,
        item.start_has_original,
        item.end_has_original,
        item.start_pending,
        item.end_pending,
        item.start_attempt,
        item.end_attempt,
        item.start_inherited,
        item.start_stale,
        script?.seconds,
        (script?.prompt || "").slice(0, 80),
        (script?.start_frame_prompt || "").slice(0, 80),
        (script?.end_frame_prompt || "").slice(0, 80),
      ];
    }),
  );
  if (ui.keyframeSignature !== keyframeSignature) {
    ui.keyframeSignature = keyframeSignature;
    ui.keyframes.replaceChildren(
      ...(data.keyframes || []).map((item) =>
        makeKeyframeCard(
          { ...item, script: ui.scriptMap.get(Number(item.index)) || null },
          ui.keyframeHandlers,
        ),
      ),
    );
  }

  ui.lastKeyframes = data.keyframes || [];
  ui.lastReference = data.reference_expand || null;
  // The backend decides which action the banner should offer: the review gate
  // ("gate"), a job whose keyframes are complete but whose videos are not
  // ("resume"), or a job that died before the keyframe set was finished
  // ("keyframes"). Relying on awaiting_review alone left reloaded jobs with no
  // button at all.
  const totalSegments = Number(data.segment_total || total || 0) || 0;
  const ratio = (value) => (totalSegments ? `${value}/${totalSegments}` : value);
  // Fallback for a backend that predates ``review_state`` (a stale Python
  // process keeps serving the old payload until ComfyUI restarts): derive the
  // same decision from fields that were already there.
  const idle =
    status === "awaiting_review" ||
    status === "completed" ||
    status === "failed" ||
    status === "interrupted";
  const frames = data.keyframes || [];
  const keyframesReady =
    data.keyframes_ready !== undefined
      ? Boolean(data.keyframes_ready)
      : totalSegments > 0 &&
        frames.length >= totalSegments &&
        frames.every((item) => !item.start_pending && !item.end_pending);
  const reviewState =
    data.review_state !== undefined
      ? data.review_state
      : data.awaiting_review
        ? "gate"
        : idle && keyframesReady && done < totalSegments
          ? "resume"
          : null;
  const awaiting = reviewState === "gate";
  ui.reviewState = reviewState;
  ui.review.style.display = reviewState ? "flex" : "none";
  const busy = status === "running" || status === "scheduled";
  ui.go.disabled = !reviewState || busy;
  if (ui.rerollAll) {
    ui.rerollAll.style.display = reviewState === "keyframes" ? "none" : "";
  }
  const referenceMode = reviewState === "reference";
  if (ui.rerollRef) {
    ui.rerollRef.style.display = referenceMode ? "" : "none";
    ui.replaceRef.style.display = referenceMode ? "" : "none";
    ui.refImage.style.display =
      referenceMode && ui.lastReference?.url ? "" : "none";
    if (referenceMode && ui.lastReference?.url) {
      ui.refImage.src = ui.lastReference.url;
    }
  }
  if (reviewState === "reference") {
    const attempt = ui.lastReference?.attempts
      ? `（第 ${ui.lastReference.attempts + 1} 次尝试）`
      : "";
    ui.reviewTitle.textContent = "参考图已扩好，等你确认";
    ui.go.textContent = "没问题，生成关键帧";
    ui.reviewHint.textContent =
      "这是参考图1扩到成片比例后的效果，之后所有关键帧都用它当画布" +
      `${attempt}。` +
      "背景延伸没问题就点继续；不满意可以重扩一次，或换成你自己处理的图。";
  } else if (reviewState === "gate") {
    ui.reviewTitle.textContent = "关键帧已生成，等待你确认";
    ui.go.textContent = "确认关键帧，生成视频";
    ui.reviewHint.textContent =
      `已生成 ${data.completed_keyframes || 0} 段首尾帧 · ` +
      "点图看大图，可替换 / 重画，满意后确认生成视频";
  } else if (reviewState === "resume") {
    ui.reviewTitle.textContent = "关键帧已就位，可以继续生成视频";
    ui.go.textContent = "继续生成视频";
    ui.reviewHint.textContent =
      `首尾帧 ${ratio(data.completed_keyframes || 0)} 齐全 · ` +
      `视频 ${ratio(done)} · 点继续渲染剩下的分段，关键帧不重画`;
  } else if (reviewState === "keyframes") {
    if (backend === "manual") {
      ui.reviewTitle.textContent = "等你放关键帧";
      ui.go.textContent = "关键帧还没放齐";
      ui.reviewHint.textContent =
        `已有 ${ratio(data.completed_keyframes || 0)} · ` +
        `${data.keyframe_backend_label || "外部图"} 出图后逐张「替换」，放齐后才能生成视频`;
      ui.go.disabled = true;
    } else {
      // h3 和两个 Qwen 后端都是 runner 自动出图，缺帧只是还没跑完。
      ui.reviewTitle.textContent = "关键帧还没生成完";
      ui.go.textContent = "继续生成关键帧";
      ui.reviewHint.textContent =
        `只有 ${ratio(data.completed_keyframes || 0)} 有首尾帧 · ` +
        "点继续补齐缺的帧，已画好的不会重画";
    }
  }
  // Jump to the keyframe tab the moment the gate opens, but never fight the
  // user's manual tab choice afterwards.
  if (busy) {
    ui.rewriting = true;
  }
  if (awaiting && ui.rewriting) {
    ui.rewriting = false;
    ui.setNotice?.("重画完成，请重新确认关键帧。");
  }
  if (awaiting && !ui.awaitingReview) {
    ui.setActiveTab("keyframes");
  }
  ui.awaitingReview = awaiting;

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
      ...(data.transitions || []).map((item) =>
        makeTransitionCard(item, ui.keyframeHandlers),
      ),
    );
  }

  const segmentSignature = JSON.stringify(
    (data.segments || []).map((item) => [item.index, item.url, item.seconds, item.two_pass]),
  );
  if (ui.segmentSignature !== segmentSignature) {
    ui.segmentSignature = segmentSignature;
    ui.segments.replaceChildren(
      ...(data.segments || []).map((item) =>
        makeVideoCard(item, ui.keyframeHandlers),
      ),
    );
  }

  ui.lastSegments = data.segments || [];
  ui.lastTransitions = data.transitions || [];
  ui.lastFinal = data.final_url
    ? { url: data.final_url, filename: data.final_filename }
    : null;
  syncLightbox(ui);

  if (data.final_url) {
    if (ui.finalUrl !== data.final_url) {
      ui.finalUrl = data.final_url;
      ui.finalVideo.src = data.final_url;
      ui.download.href = data.final_url;
      ui.download.download = data.final_filename || "long_video.mp4";
      ui.download.textContent = `下载完整视频：${data.final_filename || "long_video.mp4"}`;
    }
    ui.final.style.display = "block";
    ui.finalEmpty.style.display = "none";
  } else {
    ui.final.style.display = "none";
    ui.finalEmpty.style.display = "block";
  }

  const counts = {
    keyframes: (data.keyframes || []).length,
    transitions: (data.transitions || []).length,
    segments: (data.segments || []).length,
    final: data.final_url ? 1 : 0,
  };
  for (const tab of ui.tabs) {
    tab.count.textContent = String(counts[tab.key] ?? 0);
    if (tab.key !== "final") {
      tab.empty.style.display = counts[tab.key] ? "none" : "block";
    }
  }

  const metaParts = [];
  if (data.seed !== null && data.seed !== undefined) {
    metaParts.push(`本次 Seed：${data.seed}`);
  }
  if (data.log) metaParts.push(`日志：${data.log}`);
  ui.meta.textContent = metaParts.join("  |  ");
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
    if (
      data.status === "completed" ||
      data.status === "failed" ||
      data.status === "interrupted" ||
      data.status === "awaiting_review"
    ) {
      stopPolling(node);
    }
  } catch (error) {
    ui.error.textContent = `读取任务状态失败：${error.message}`;
  }
}

const JOB_STATUS_LABELS = {
  scheduled: "排队中",
  running: "生成中",
  awaiting_review: "待确认",
  completed: "已完成",
  failed: "失败",
  interrupted: "中断",
  unknown: "未知",
};

async function loadJobList() {
  try {
    const response = await api.fetchApi("/h3-long-video/jobs", {
      cache: "no-store",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return Array.isArray(data.jobs) ? data.jobs : [];
  } catch (error) {
    // 老版本后端没有 /jobs 路由：不报错，面板照旧用 node.properties 里的任务号
    return null;
  }
}

function formatBytes(bytes) {
  const total = Number(bytes) || 0;
  if (total <= 0) return "0";
  const units = ["B", "KB", "MB", "GB"];
  let value = total;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const text = value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1);
  return `${text}${units[index]}`;
}

function renderJobOptions(node, ui, jobs) {
  if (!ui.jobSelect) return;
  ui.jobs = jobs;
  const current = ui.jobId || node.properties?.h3_job_id || "";
  const options = [
    new Option(jobs.length ? "选择任务…" : "还没有任务", "", false, !current),
  ];
  for (const job of jobs) {
    const size = Number(job.bytes) > 0 ? ` · ${formatBytes(job.bytes)}` : "";
    const label =
      `${job.label || job.job_id} · ${JOB_STATUS_LABELS[job.status] || job.status} · ` +
      `${job.completed_segments}/${job.segments} 段 · ${job.seconds}s${size}`;
    options.push(new Option(label, job.job_id, false, job.job_id === current));
  }
  ui.jobSelect.replaceChildren(...options);
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

// 2026-11-04：keyframe_steps 控件删了（关键帧直接用 steps）。老工作流
// widgets_values 是按顺序存的，少一格就整排前移一位——步数压到
// advanced_overrides、seed 压到 stage，界面上看是「一堆莫名其妙的数字」。
// 特征很好认：advanced_overrides（第 12 个控件）拿到了一个纯数字。
// 把那一格删掉再整体回填，用户重新保存一次就干净了。
function migrateLegacyKeyframeSteps(node) {
  const widgets = node.widgets;
  const values = node.widgets_values;
  if (!Array.isArray(widgets) || !Array.isArray(values)) return false;
  const overrides = findWidget(node, "advanced_overrides");
  if (!overrides) return false;
  if (!/^-?\d+(\.\d+)?$/.test(String(overrides.value ?? "").trim())) {
    return false;
  }
  if (values.length < 13 || typeof values[11] !== "number") return false;
  const fixed = values.slice(0, 11).concat(values.slice(12));
  node.widgets_values = fixed;
  widgets.forEach((widget, index) => {
    if (index < fixed.length) widget.value = fixed[index];
  });
  return true;
}

// 下拉控件拿到非法值（空串 / 旧版遗留的错位值）时落回第一项，别让它带着
// 空值去排队——后端只认白名单，会直接抛错。
function sanitizeCombo(node, name) {
  const widget = findWidget(node, name);
  if (!widget || !Array.isArray(widget.options?.values)) return;
  const options = widget.options.values;
  if (!options.length) return;
  if (!options.includes(widget.value)) widget.value = options[0];
}

function migrateWidgetValues(node) {
  migrateLegacyKeyframeSteps(node);
  for (const name of [
    "stage",
    "keyframe_backend",
    "resolution",
    "video_backend",
  ]) {
    sanitizeCombo(node, name);
  }
  const seed = findWidget(node, "seed");
  if (seed && !Number.isFinite(Number(seed.value))) {
    seed.value = 0;
  }
  const randomizeSeed = findWidget(node, "randomize_seed");
  if (randomizeSeed && typeof randomizeSeed.value !== "boolean") {
    randomizeSeed.value = true;
  }
  const megapixels = findWidget(node, "megapixels");
  if (megapixels && typeof megapixels.value === "string") {
    const parsed = Number(megapixels.value);
    if (!Number.isFinite(parsed)) {
      megapixels.value = 0.2;
    }
  }
  // 704x1216（0.5789）跟 9:16 差 2.9%，选「自定义」不动数字就会报错，所以兜底值
  // 也换成和 resolution 默认档一致的 832x1472（= 一采 416x736 的 2 倍）。
  const finalWidth = findWidget(node, "final_width");
  if (finalWidth && !Number.isFinite(Number(finalWidth.value))) {
    finalWidth.value = 832;
  }
  const finalHeight = findWidget(node, "final_height");
  if (finalHeight && !Number.isFinite(Number(finalHeight.value))) {
    finalHeight.value = 1472;
  }
  // 「每处定格剪完留多久」：ComfyUI 直接拿控件 key 当标签，这里是英文，补个中文名。
  const holdSeconds = findWidget(node, "static_compact_hold_seconds");
  if (holdSeconds) {
    holdSeconds.label = "定格保留(秒)";
    if (!Number.isFinite(Number(holdSeconds.value))) {
      holdSeconds.value = 0.12;
    }
  }
}

// 分辨率档位表在 config.json 的 resolution_presets 里，前端不复制一份。
// 标签约定成「宽x高（说明）」，这里靠正则把标签解析回尺寸——后端加一档，
// 面板不用改代码就能认出来。「自定义（用 final_width / final_height）」不含
// "数字x数字"，正则匹配不到，自然回落到两个 INT 控件。
const RESOLUTION_LABEL_PATTERN = /^\s*(\d+)\s*[x×]\s*(\d+)/;

function parseResolutionLabel(value) {
  const match = RESOLUTION_LABEL_PATTERN.exec(String(value ?? ""));
  if (!match) {
    return null;
  }
  const width = Number(match[1]);
  const height = Number(match[2]);
  return width > 0 && height > 0 ? [width, height] : null;
}

function effectiveResolution(node) {
  const preset = parseResolutionLabel(findWidget(node, "resolution")?.value);
  if (preset) {
    return preset;
  }
  const finalWidth = findWidget(node, "final_width");
  const finalHeight = findWidget(node, "final_height");
  return [finalWidth?.value ?? "?", finalHeight?.value ?? "?"];
}

function countSegments(node, ui) {
  const storyboard = findWidget(node, "storyboard");
  const mode = findWidget(node, "two_pass_mode");
  const steps = findWidget(node, "steps");
  const twoPassSteps = findWidget(node, "two_pass_steps");
  const sampler = findWidget(node, "sampler");
  const scheduler = findWidget(node, "scheduler");
  const megapixels = findWidget(node, "megapixels");
  const videoBackend = findWidget(node, "video_backend");
  const seed = findWidget(node, "seed");
  const randomizeSeed = findWidget(node, "randomize_seed");
  node.lastTwoPassMode = mode?.value || "";
  const [effectiveWidth, effectiveHeight] = effectiveResolution(node);
  node.samplingSummary = [
    `${steps?.value ?? "?"}+${twoPassSteps?.value ?? "?"} 步`,
    sampler?.value,
    scheduler?.value,
    randomizeSeed?.value ? "随机 Seed" : `Seed ${seed?.value ?? "?"}`,
    `${megapixels?.value ?? "?"} MP`,
    `${effectiveWidth}×${effectiveHeight}`,
    videoBackend
      ? String(videoBackend.value).includes("云")
        ? "云端出片"
        : "本地出片"
      : "",  ]
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

const SELFLIFT_DEFAULT_CFG =
  "D:/Comfy-Desktop/output/rooftop-dusk-v2/segments.json";

/** SelfLift 逐段人在回路（2026-11-11）：面板里的第 5 个 tab。
 *
 *  一段段跑 → 看每段首/尾帧 → 换 seed 重出 / 指定任意图作首帧 → 改提示词 → 继续。
 *  后端是 selflift_panel.py 的 /h3-long-video/selflift/* 路由，
 *  生成实现仍是 skill/scripts/run_selflift.py（唯一入口），这里只做 UI。
 */
function buildSelfLiftPane(host) {
  const KEY = "h3_selflift_cfg";
  const style = document.createElement("style");
  style.textContent = `
    .h3-sl-body { display:flex; flex-direction:column; gap:8px; color:#ddd; font-size:12px; }
    .h3-sl-bar { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
    .h3-sl-input { background:#20232a; color:#e8ecf2; border:1px solid #3a404c; border-radius:4px; padding:4px 6px; font-size:12px; min-width:0; }
    .h3-sl-bar .h3-sl-input:first-child { flex:1 1 300px; }
    .h3-sl-btn { background:#263244; color:#e8ecf2; border:1px solid #3f5573; border-radius:4px; padding:5px 9px; font-size:12px; cursor:pointer; }
    .h3-sl-btn:hover { background:#33465f; }
    .h3-sl-btn.primary { background:#2f5d8a; border-color:#4a7fb5; }
    .h3-sl-status { padding:5px 8px; border-radius:4px; background:#1d222b; border-left:3px solid #4a7fb5; }
    .h3-sl-status[data-kind="error"] { border-left-color:#c85a5a; color:#ffb4b4; }
    .h3-sl-status[data-kind="ok"] { border-left-color:#4caf7d; }
    .h3-sl-status[data-kind="running"] { border-left-color:#e0a44a; }
    .h3-sl-task { display:none; max-height:140px; overflow:auto; background:#11141a; color:#9fb0c4; padding:6px; font-size:11px; white-space:pre-wrap; margin:0; }
    .h3-sl-list { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:10px; }
    .h3-sl-card { background:#1d2129; border:1px solid #343b47; border-radius:6px; padding:8px; display:flex; flex-direction:column; gap:6px; }
    .h3-sl-card[data-stale="true"] { border-color:#b07a3a; }
    .h3-sl-head { display:flex; justify-content:space-between; align-items:center; gap:6px; }
    .h3-sl-badge { color:#9fb0c4; font-size:11px; }
    .h3-sl-card[data-stale="true"] .h3-sl-badge { color:#e8b26a; }
    .h3-sl-imgs { display:flex; gap:6px; }
    .h3-sl-imgbox { flex:1 1 0; min-width:0; }
    .h3-sl-imgbox img { width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:3px; cursor:zoom-in; background:#000; }
    .h3-sl-label { color:#98a4b3; font-size:11px; }
    .h3-sl-noimg { background:#11141a; aspect-ratio:16/9; display:flex; align-items:center; justify-content:center; color:#666; border-radius:3px; }
    .h3-sl-row { display:flex; flex-wrap:wrap; gap:5px; align-items:center; }
    .h3-sl-row .h3-sl-input { width:100px; }
    .h3-sl-textarea { width:100%; box-sizing:border-box; background:#161a21; color:#e8ecf2; border:1px solid #343b47; border-radius:4px; padding:6px; font-size:11px; line-height:1.5; resize:vertical; }
    .h3-sl-finals { display:flex; flex-wrap:wrap; gap:8px; }
    .h3-sl-link { color:#8fb8e8; font-size:11px; }
    .h3-sl-hint { font-size:11px; line-height:1.5; color:#f0c47a; background:#2a2318;
      border:1px solid #6b5228; border-radius:4px; padding:5px 8px; }
  `;
  host.appendChild(style);

  const bar = makeElement("div", "h3-sl-bar");
  const cfgInput = makeElement("input", "h3-sl-input");
  cfgInput.type = "text";
  cfgInput.placeholder = "分镜 json 绝对路径（产物写到同目录）";
  cfgInput.value = localStorage.getItem(KEY) || SELFLIFT_DEFAULT_CFG;
  const openBtn = makeElement("button", "h3-sl-btn", "打开 / 刷新");
  const newTitle = makeElement("input", "h3-sl-input");
  newTitle.placeholder = "新片标题";
  newTitle.style.width = "110px";
  const newBtn = makeElement("button", "h3-sl-btn", "新建片");
  const joinSel = makeElement("select", "h3-sl-input");
  [["auto", "按每段转场"], ["dissolve", "全片叠化"], ["cut", "全片硬切"], ["black", "全片黑场"]]
    .forEach(([value, text]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      joinSel.appendChild(option);
    });
  const mergeBtn = makeElement("button", "h3-sl-btn", "拼接成片");
  const runAllBtn = makeElement("button", "h3-sl-btn primary", "一键跑全片");
  runAllBtn.title = "按顺序跑完所有未完成 / 已失效的段，跑完自动拼接成片；跑片过程中再点一次=取消";
  bar.append(cfgInput, openBtn, newTitle, newBtn, runAllBtn, joinSel, mergeBtn);

  const status = makeElement("div", "h3-sl-status", "未加载");
  const taskBox = makeElement("pre", "h3-sl-task");
  const finalsBox = makeElement("div", "h3-sl-finals");
  const list = makeElement("div", "h3-sl-list");

  // 参考图 / 空镜板就地替换：写回 cfg，跑片时由 ensure_ref_in_input 自动拷进 ComfyUI input
  const refRow = makeElement("div", "h3-sl-bar");
  const refFile = document.createElement("input");
  refFile.type = "file"; refFile.accept = "image/*"; refFile.className = "h3-sl-label";
  const refUpBtn = makeElement("button", "h3-sl-btn", "上传人物参考");
  const plateFile = document.createElement("input");
  plateFile.type = "file"; plateFile.accept = "image/*"; plateFile.className = "h3-sl-label";
  const plateUpBtn = makeElement("button", "h3-sl-btn", "上传空镜板");
  refRow.append(makeElement("span", "h3-sl-label", "参考图："),
    refFile, refUpBtn, plateFile, plateUpBtn);
  const uploadRef = async (kind, input, label) => {
    if (!input.files?.length) { say(`先选一张${label}`, "error"); return; }
    try {
      const form = new FormData();
      form.append("cfg", cfgPath());
      form.append("kind", kind);
      form.append("file", input.files[0]);
      await postFormData("/h3-long-video/selflift/refimg", form);
      say(`${label}已更新（写回 cfg，跑片时自动拷进 ComfyUI input）`, "ok");
      input.value = "";
      refresh();
    } catch (error) {
      say(`上传${label}失败：${error.message}`, "error");
    }
  };
  refUpBtn.addEventListener("click", () => uploadRef("ref", refFile, "人物参考图"));
  plateUpBtn.addEventListener("click", () => uploadRef("plate", plateFile, "空镜板"));

  const hint = makeElement("div", "h3-sl-hint",
    "出片点这里：左边「一键跑全片」（按序补跑缺段并自动拼接），或每段卡片里的「运行」。"
    + "画布上方的 Run 走旧 Director 链路，和这个面板无关。");
  host.append(bar, refRow, hint, status, taskBox, finalsBox, list);

  let timer = null;
  let signature = "";
  let autoQueue = null;   // 一键跑全片：待跑段号队列（null=未启用）
  let autoBusy = false;   // 防止 3s 轮询重复提交同一段
  let autoMerged = false;
  const cfgPath = () => cfgInput.value.trim();
  const say = (text, kind = "info") => {
    status.textContent = text;
    status.dataset.kind = kind;
  };

  async function refresh() {
    const cfg = cfgPath();
    if (!cfg) return null;
    try {
      const res = await api.fetchApi(
        `/h3-long-video/selflift/status?cfg=${encodeURIComponent(cfg)}`,
        { cache: "no-store" },
      );
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "状态读取失败");
      render(data);
      return data;
    } catch (error) {
      say(`读取失败：${error.message}`, "error");
      return null;
    }
  }

  /** 一键跑全片：一次只提交一段，靠 3s 状态轮询推进队列，全部跑完自动拼接。 */
  async function pumpQueue(data) {
    if (!autoQueue || autoBusy) return;
    const task = data.task || {};
    if (task.running) return;
    if (!autoQueue.length) {
      if (autoMerged) return;
      autoMerged = true;
      autoQueue = null;
      runAllBtn.textContent = "一键跑全片";
      say("全部段已完成，正在拼接成片…", "running");
      try {
        await postJson("/h3-long-video/selflift/merge",
          { cfg: cfgPath(), join: joinSel.value });
      } catch (error) {
        say(`拼接失败：${error.message}`, "error");
      }
      return;
    }
    const seg = autoQueue[0];
    autoBusy = true;
    say(`一键跑全片：正在跑第 ${seg} 段（剩 ${autoQueue.length - 1} 段）`, "running");
    try {
      await postJson("/h3-long-video/selflift/run", { cfg: cfgPath(), seg });
      autoQueue.shift();
      // 等后端把 TASKS.running 落地，避免下一轮轮询误判"没在跑"而重复提交
      await new Promise((r) => setTimeout(r, 1200));
    } catch (error) {
      autoQueue = null;
      runAllBtn.textContent = "一键跑全片";
      say(`一键跑全片中断：${error.message}`, "error");
    } finally {
      setTimeout(() => { autoBusy = false; }, 2500);
    }
  }

  async function post(url, body) {
    try {
      await postJson(url, body);
      signature = ""; // 强制重建卡片，显示刚写回的 seed / 提示词 / 首帧
      await refresh();
    } catch (error) {
      say(`操作失败：${error.message}`, "error");
    }
  }

  function makeCard(seg, total) {
    const card = makeElement("div", "h3-sl-card");
    if (seg.stale) card.dataset.stale = "true";
    const head = makeElement("div", "h3-sl-head");
    head.appendChild(makeElement("strong", "", `第 ${seg.index} 段 · ${seg.title || seg.id}`));
    head.appendChild(makeElement("span", "h3-sl-badge",
      seg.stale ? "需重出（上游已变）" : seg.done ? "已生成" : "待生成"));

    const imgs = makeElement("div", "h3-sl-imgs");
    [["首帧", seg.first_url], ["尾帧", seg.last_url]].forEach(([label, url]) => {
      const box = makeElement("div", "h3-sl-imgbox");
      box.appendChild(makeElement("span", "h3-sl-label", label));
      if (label === "尾帧" && url) {
        const download = makeElement("a", "h3-sl-link", "↓ 下载");
        download.href = url;
        download.setAttribute("download", "");
        box.appendChild(download);
      }
      if (url) {
        const img = document.createElement("img");
        img.src = url;
        img.loading = "lazy";
        img.title = "点开看大图";
        img.addEventListener("click", () => window.open(url, "_blank"));
        box.appendChild(img);
      } else {
        box.appendChild(makeElement("div", "h3-sl-noimg", "暂无"));
      }
      imgs.appendChild(box);
    });

    const row = makeElement("div", "h3-sl-row");
    const seedInput = makeElement("input", "h3-sl-input");
    seedInput.type = "number";
    seedInput.value = seg.seed ?? "";
    row.append(makeElement("span", "h3-sl-label", "seed"), seedInput);
    const runBtn = makeElement("button", "h3-sl-btn primary",
      seg.done ? "按此 seed 重出" : "生成本段");
    const rerollBtn = makeElement("button", "h3-sl-btn", "换随机 seed 重出");
    const clearBtn = makeElement("button", "h3-sl-btn", "恢复自动承接");
    row.append(runBtn, rerollBtn, clearBtn);

    const prompt = makeElement("textarea", "h3-sl-textarea");
    prompt.value = seg.body || "";
    prompt.rows = 5;
    prompt.placeholder = "本段提示词正文（head / tail 全片共用锚不变）";
    const saveBtn = makeElement("button", "h3-sl-btn", "保存提示词");

    const fileRow = makeElement("div", "h3-sl-row");
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/*";
    fileInput.className = "h3-sl-label";
    const uploadBtn = makeElement("button", "h3-sl-btn", "设为本段首帧图");
    const overrideNote = makeElement("span", "h3-sl-label",
      seg.first_frame_override
        ? `首帧覆盖：${seg.first_frame_override}`
        : "首帧：自动承接上段尾帧");
    fileRow.append(fileInput, uploadBtn, overrideNote);

    runBtn.addEventListener("click", () => post("/h3-long-video/selflift/run", {
      cfg: cfgPath(), seg: seg.index, seed: Number(seedInput.value) || undefined,
    }));
    rerollBtn.addEventListener("click", () => post("/h3-long-video/selflift/run", {
      cfg: cfgPath(), seg: seg.index,
      seed: Math.floor(Math.random() * 1000000000),
    }));
    clearBtn.addEventListener("click", () => post("/h3-long-video/selflift/set", {
      cfg: cfgPath(), seg: seg.index, first_frame: "auto",
    }));
    saveBtn.addEventListener("click", () => post("/h3-long-video/selflift/set", {
      cfg: cfgPath(), seg: seg.index, body: prompt.value,
    }));
    uploadBtn.addEventListener("click", async () => {
      if (!fileInput.files?.length) { say("先选一张图片", "error"); return; }
      const form = new FormData();
      form.append("cfg", cfgPath());
      form.append("seg", String(seg.index));
      form.append("file", fileInput.files[0]);
      try {
        await postFormData("/h3-long-video/selflift/upload", form);
        signature = "";
        await refresh();
      } catch (error) {
        say(`上传失败：${error.message}`, "error");
      }
    });

    // 尾帧：替换供下段接续 / 锁定本段（节点原生有 last_frame 输入，默认关闭）
    const tailRow = makeElement("div", "h3-sl-row");
    const tailFile = document.createElement("input");
    tailFile.type = "file";
    tailFile.accept = "image/*";
    tailFile.className = "h3-sl-label";
    const tailNextBtn = makeElement("button", "h3-sl-btn", "替换尾帧→下段首帧");
    const tailLockBtn = makeElement("button", "h3-sl-btn", "锁定本段尾帧");
    const tailClearBtn = makeElement("button", "h3-sl-btn", "解除尾帧锁");
    if (seg.index >= total) {
      tailNextBtn.disabled = true;
      tailNextBtn.title = "末段没有下一段";
    }
    tailNextBtn.title = "只改喂给下一段的帧图，不重出本段；后续段自动标 stale";
    tailLockBtn.title = "把这张图接到本段 last_frame 输入，需重出本段；会切 FL2VA/Hybrid";
    const tailNote = makeElement("span", "h3-sl-label",
      seg.last_frame_override
        ? `尾帧锁：${seg.last_frame_override}（需重出本段）`
        : "尾帧替换只改下段接续；要改本段画面请重出本段");
    tailRow.append(tailFile, tailNextBtn, tailLockBtn, tailClearBtn, tailNote);

    const postTail = async (mode) => {
      if (!tailFile.files?.length) { say("先选一张图片", "error"); return; }
      const form = new FormData();
      form.append("cfg", cfgPath());
      form.append("seg", String(seg.index));
      form.append("mode", mode);
      form.append("file", tailFile.files[0]);
      try {
        const result = await postFormData("/h3-long-video/selflift/frame", form);
        say(result.note || "已提交", "ok");
        signature = "";
        await refresh();
      } catch (error) {
        say(`尾帧操作失败：${error.message}`, "error");
      }
    };
    tailNextBtn.addEventListener("click", () => postTail("next_first"));
    tailLockBtn.addEventListener("click", () => postTail("last_lock"));
    tailClearBtn.addEventListener("click", () => post("/h3-long-video/selflift/set", {
      cfg: cfgPath(), seg: seg.index, last_frame: "auto",
    }));

    // 承接方式 + 转场类型 + 在此段后插入（首段没有上一段，不显示承接）
    const linkRow = makeElement("div", "h3-sl-row");
    let hasLink = false;
    if (seg.index > 1) {
      hasLink = true;
      const link = seg.link || { mode: "chain", transition: "dissolve" };
      const modeSel = makeElement("select", "h3-sl-input");
      [["chain", "链式承接"], ["transition", "转场"]].forEach(([v, t]) => {
        const o = document.createElement("option");
        o.value = v; o.textContent = t;
        if (link.mode === v) o.selected = true;
        modeSel.appendChild(o);
      });
      const transSel = makeElement("select", "h3-sl-input");
      [["dissolve", "叠化 0.25s"], ["black", "黑屏 0.8s"], ["cut", "硬切"]].forEach(([v, t]) => {
        const o = document.createElement("option");
        o.value = v; o.textContent = t;
        if ((link.transition || "dissolve") === v) o.selected = true;
        transSel.appendChild(o);
      });
      transSel.disabled = link.mode !== "transition";
      modeSel.title = "链式=本段首帧承接上段尾帧（上游变了本段会需重出）；"
        + "转场=本段独立生成，只靠转场接缝（上游变了不用重出）";
      transSel.title = "仅「转场」模式下生效：叠化 / 黑屏 / 硬切";
      const applyLink = () => post("/h3-long-video/selflift/set", {
        cfg: cfgPath(), seg: seg.index,
        link_mode: modeSel.value, link_transition: transSel.value,
      });
      modeSel.addEventListener("change", () => {
        transSel.disabled = modeSel.value !== "transition";
        applyLink();
      });
      transSel.addEventListener("change", applyLink);
      linkRow.append(makeElement("span", "h3-sl-label", "承接"), modeSel, transSel);
    }
    // 插入按钮：除末段外每段都有，首段也能插到 1↔2 之间（后端 insert --after 1 已支持）
    if (seg.index < total) {
      hasLink = true;
      const insertBtn = makeElement("button", "h3-sl-btn", "＋ 在此段后插入");
      insertBtn.title = "在下一段之前插入一个新段（默认链式承接），后续段自动重编号";
      insertBtn.addEventListener("click", () => post("/h3-long-video/selflift/insert", {
        cfg: cfgPath(), after: seg.index,
      }));
      linkRow.append(insertBtn);
    }
    card.append(head);
    if (hasLink) card.append(linkRow);
    card.append(imgs, row, prompt, saveBtn, fileRow, tailRow);
    return card;
  }

  function render(data) {
    const task = data.task || {};
    pumpQueue(data);
    if (task.running) {
      say(`正在跑：${task.kind}（约 3~4 分钟，可继续编辑其它段）`, "running");
      taskBox.style.display = "block";
      taskBox.textContent = (task.log || "").slice(-1500);
    } else {
      taskBox.style.display = task.error ? "block" : "none";
      if (task.error) taskBox.textContent = `${task.error}\n${(task.log || "").slice(-1200)}`;
      const ready = data.segments.filter((s) => s.done && !s.stale).length;
      const links = data.segments.filter((s) => s.index > 1)
        .map((s) => (s.link && s.link.mode) || "chain");
      const transN = links.filter((m) => m === "transition").length;
      const chainN = links.length - transN;
      say(`${data.title || "未命名"} · ${ready}/${data.segments.length} 段就绪 · `
        + `接续 链式 ${chainN} / 转场 ${transN}`,
      task.error ? "error" : "info");
    }
    finalsBox.replaceChildren(...(data.finals || []).map((item) => {
      const link = makeElement("a", "h3-sl-link", `▶ ${item.name}`);
      link.href = item.url;
      link.target = "_blank";
      return link;
    }));
    // body 不入签名：轮询不会重建卡片、不会冲掉正在输入的提示词
    const sig = JSON.stringify([
      data.segments.map((s) => [s.index, s.id, s.done, s.stale, s.seed,
        s.first_frame_override, s.last_frame_override,
        s.first_url, s.last_url, s.video_url,
        (s.link && s.link.mode) || "chain", (s.link && s.link.transition) || "dissolve"]),
      data.finals,
    ]);
    if (sig === signature) return;
    signature = sig;
    list.replaceChildren(...data.segments.map((s) => makeCard(s, data.segments.length)));
  }

  function start() {
    stop();
    signature = "";
    refresh();
    timer = setInterval(refresh, 3000);
  }
  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  openBtn.addEventListener("click", () => {
    localStorage.setItem(KEY, cfgPath());
    start();
  });
  newBtn.addEventListener("click", async () => {
    try {
      const data = await postJson("/h3-long-video/selflift/init", {
        title: newTitle.value.trim() || "未命名长片",
      });
      cfgInput.value = data.cfg;
      localStorage.setItem(KEY, data.cfg);
      say(`新片已建立：${data.cfg} — ${data.note}`, "ok");
      start();
    } catch (error) {
      say(`新建失败：${error.message}`, "error");
    }
  });
  mergeBtn.addEventListener("click", () => post("/h3-long-video/selflift/merge", {
    cfg: cfgPath(), join: joinSel.value,
  }));
  runAllBtn.addEventListener("click", async () => {
    if (autoQueue) {           // 再点一次 = 取消
      autoQueue = null;
      runAllBtn.textContent = "一键跑全片";
      say("已取消一键跑全片（当前正在跑的那段不会中断）", "info");
      return;
    }
    const data = await refresh();
    if (!data) return;
    const task = data.task || {};
    if (task.running) { say("已有任务在跑，等它结束再点", "error"); return; }
    const pending = (data.segments || [])
      .filter((s) => !s.done || s.stale).map((s) => s.index);
    if (!pending.length) {
      say("所有段都已完成，直接拼接成片…", "running");
      post("/h3-long-video/selflift/merge", { cfg: cfgPath(), join: joinSel.value });
      return;
    }
    autoQueue = pending;
    autoBusy = false;
    autoMerged = false;
    runAllBtn.textContent = "取消全片";
    say(`一键跑全片：共 ${pending.length} 段（第 ${pending.join(" / ")} 段）`, "running");
    pumpQueue(data);
  });

  start();
  return { start, stop, refresh };
}

function installPanel(node) {
  ensureStyles();
  const root = makeElement("div", "h3-director-panel");

  const head = makeElement("div", "h3-director-head");
  const state = makeElement("div", "h3-director-state", "等待运行");
  const mode = makeElement("div", "h3-director-mode");
  const refresh = makeElement("button", "h3-director-refresh", "刷新预览");
  const fullscreenButton = makeElement(
    "button",
    "h3-director-refresh",
    "⛶ 全屏",
  );
  fullscreenButton.title = "把预览铺满浏览器窗口（Esc 或再次点击退出），画布太小的时候用这个";
  const popupButton = makeElement(
    "button",
    "h3-director-refresh",
    "⧉ 独立窗口",
  );
  popupButton.title = "把预览搬到单独的浏览器窗口，可以拖到第二块屏幕，画布完全不遮挡";
  const jobSelect = makeElement("select", "h3-director-jobs");
  jobSelect.title = "切换要查看的任务；列表里没有就点「刷新预览」重新拉一次";
  const renameButton = makeElement("button", "h3-director-refresh", "✎ 改名");
  renameButton.title = "改当前任务的名字：文件夹会一起变成「日期-名称」";
  const deleteButton = makeElement("button", "h3-director-refresh", "🗑 删除");
  deleteButton.title = "删掉当前任务的整个文件夹（首尾帧、日志、中间产物），不可恢复";
  head.append(
    state,
    mode,
    jobSelect,
    renameButton,
    deleteButton,
    refresh,
    popupButton,
    fullscreenButton,
  );

  const review = makeElement("div", "h3-director-review");
  const refImage = makeElement("img", "h3-director-refimg", "");
  refImage.style.display = "none";
  refImage.title = "参考图1扩到成片比例后的效果（点击放大）";
  refImage.addEventListener("click", () => {
    if (refImage.src) window.open(refImage.src, "_blank");
  });
  const reviewTitle = makeElement(
    "div",
    "h3-director-review-title",
    "关键帧已生成，等待你确认",
  );
  const reviewHint = makeElement("div", "h3-director-review-hint", "");
  const reviewActions = makeElement("div", "h3-director-review-actions");
  const go = makeElement("button", "h3-director-go", "确认关键帧，生成视频");
  const rerollAll = makeElement("button", "h3-director-refresh", "全部重生成");
  const rerollRef = makeElement("button", "h3-director-refresh", "重扩参考图");
  rerollRef.style.display = "none";
  const replaceRef = makeElement("button", "h3-director-refresh", "换参考图");
  replaceRef.style.display = "none";
  reviewActions.append(go, rerollAll, rerollRef, replaceRef);
  review.append(refImage, reviewTitle, reviewHint, reviewActions);

  const keyframes = makeElement("div", "h3-director-keyframes");
  // 关键帧来源提示条：选了 Qwen / 只用手动图 时告诉用户该开哪张工作流。
  const backendNote = makeElement("div", "h3-director-backend");
  backendNote.style.display = "none";
  const transitions = makeElement("div", "h3-director-segments");
  const segments = makeElement("div", "h3-director-segments");
  const error = makeElement("div", "h3-director-error");
  const notice = makeElement("div", "h3-director-notice");

  // ---- 分段脚本编辑器：在关键帧上方展开，改完直接写回该任务的 config.json ----
  const editor = makeElement("div", "h3-director-editor");
  const editorHead = makeElement("div", "h3-director-editor-head");
  const editorTitle = makeElement("div", "h3-director-editor-title", "第 ? 段脚本");
  const editorClose = makeElement("button", "h3-director-kf-edit", "收起");
  editorHead.append(editorTitle, editorClose);
  const editorRow = makeElement("div", "h3-director-editor-row");
  const secsInput = document.createElement("input");
  secsInput.type = "number";
  secsInput.min = "1";
  secsInput.max = "30";
  secsInput.step = "0.5";
  secsInput.title = "这一段视频的时长（秒）";
  const secsLabel = makeElement("span", "", "秒数");
  const editorHintRow = makeElement(
    "span",
    "h3-director-kf-prompt",
    "改哪个框就只重画对应的那帧；留空表示跟主提示词一样",
  );
  editorRow.append(secsLabel, secsInput, editorHintRow);
  const makeScriptField = (main, title, hint, placeholder) => {
    const wrap = document.createElement("div");
    const label = makeElement("label", "");
    label.append(makeElement("b", "", title));
    if (hint) label.append(makeElement("em", "", hint));
    const area = document.createElement("textarea");
    area.placeholder = placeholder;
    if (main) area.dataset.main = "true";
    wrap.append(label, area);
    return { wrap, area };
  };
  const mainField = makeScriptField(
    true,
    "主提示词",
    "整段视频用这句；首尾帧没单独写时也用它",
    "这一段要发生什么：动作、机位、光线、服装…",
  );
  // 首帧一般跟着主提示词走，折叠起来省地方；有单独写就自动展开。
  const headDetails = document.createElement("details");
  headDetails.className = "h3-director-editor-folds";
  const headSummary = document.createElement("summary");
  headSummary.append(
    makeElement("b", "", "首帧提示词"),
    makeElement("em", "", "留空 = 跟主提示词一样（点开可单独改）"),
  );
  const startArea = document.createElement("textarea");
  startArea.placeholder = "留空 = 跟主提示词一样";
  startArea.title = "这段开头那一帧用哪句提示词；留空就用主提示词";
  headDetails.append(headSummary, startArea);
  const endField = makeScriptField(
    false,
    "尾帧提示词",
    "留空 = 跟主提示词一样；填了会单独出尾帧",
    "这段结尾那一帧长什么样（可不填）",
  );
  const promptArea = mainField.area;
  const endArea = endField.area;
  const editorTexts = makeElement("div", "h3-director-editor-texts");
  editorTexts.append(mainField.wrap, headDetails, endField.wrap);
  const editorBody = makeElement("div", "h3-director-editor-body");
  const editorFrames = makeElement("div", "h3-director-editor-frames");
  const frameSlots = {};
  for (const side of ["start", "end"]) {
    const figure = document.createElement("figure");
    const image = document.createElement("img");
    image.alt = side === "start" ? "这一段的首帧" : "这一段的尾帧";
    const caption = makeElement(
      "figcaption",
      "",
      side === "start" ? "首帧" : "尾帧",
    );
    figure.append(image, caption);
    editorFrames.appendChild(figure);
    frameSlots[side] = image;
  }
  editorBody.append(editorFrames, editorTexts);
  const editorActions = makeElement("div", "h3-director-editor-actions");
  const saveButton = makeElement("button", "", "只保存（不动关键帧）");
  const saveRedrawButton = makeElement("button", "", "保存并重画关键帧");
  saveRedrawButton.dataset.primary = "true";
  const forceLabel = makeElement("label", "h3-director-editor-force");
  const forceCheck = document.createElement("input");
  forceCheck.type = "checkbox";
  forceLabel.append(forceCheck, document.createTextNode(" 连我自己放的图一起重出"));
  forceLabel.title =
    "你替换/灌进来的图默认不会被覆盖。勾上这个再点「保存并重画关键帧」，" +
    "就会撤掉那段手动图，按新提示词让 AI 重出（撤掉不可恢复）";
  forceLabel.style.display = "none";
  const editorState = makeElement("span", "h3-director-editor-hint", "");
  editorActions.append(saveButton, saveRedrawButton, forceLabel, editorState);
  editor.append(editorHead, editorRow, editorBody, editorActions);

  const final = makeElement("div", "h3-director-final");
  final.style.display = "none";
  const finalVideo = document.createElement("video");
  finalVideo.controls = true;
  finalVideo.preload = "metadata";
  const finalZoom = makeElement("button", "h3-director-zoom", "⤢ 全屏看成片");
  finalZoom.addEventListener("click", () => openLightbox(ui, "final", "final"));
  const download = makeElement("a", "h3-director-download", "下载完整视频");
  const finalTools = makeElement("div", "h3-director-final-tools");
  finalTools.append(finalZoom, download);
  final.append(finalVideo, finalTools);
  const finalEmpty = makeElement(
    "div",
    "h3-director-empty",
    "成片还没生成。所有分段跑完并拼接后会出现在这里。",
  );

  const tabsBar = makeElement("div", "h3-director-tabs");
  const panes = makeElement("div", "h3-director-panes");
  const selfliftBody = makeElement("div", "h3-sl-body");
  const selfliftCtl = buildSelfLiftPane(selfliftBody);

  const tabSpecs = [
    {
      key: "keyframes",
      label: "关键帧",
      body: keyframes,
      emptyText: "还没有关键帧。点运行后这里会先出首尾帧，出完会停下来等你确认。",
    },
    {
      key: "transitions",
      label: "转场",
      body: transitions,
      emptyText: "这一段没有转场。只有「切点」段之间才会生成转场。",
    },
    {
      key: "segments",
      label: "分段视频",
      body: segments,
      emptyText: "还没有分段视频。确认关键帧之后才会开始渲染。",
    },
    {
      key: "final",
      label: "成片",
      body: final,
      emptyText: "",
    },
  ];

  // 第 5 个 tab：SelfLift 逐段人在回路（长视频新默认，旧链路 4 个 tab 原样保留）
  tabSpecs.push({
    key: "selflift",
    label: "SelfLift 逐段",
    body: selfliftBody,
    emptyText: "填分镜 json 路径，点「打开 / 刷新」即可逐段生成。",
  });

  const tabs = [];
  for (const spec of tabSpecs) {
    const button = makeElement("button", "h3-director-tab", spec.label);
    button.type = "button";
    const count = makeElement("span", "h3-director-tab-count", "0");
    button.appendChild(count);
    button.setAttribute("aria-selected", "false");
    const pane = makeElement("div", "h3-director-pane");
    pane.dataset.active = "false";
    const empty =
      spec.key === "final"
        ? finalEmpty
        : makeElement("div", "h3-director-empty", spec.emptyText);
    pane.append(empty, spec.body);
    tabsBar.appendChild(button);
    panes.appendChild(pane);
    tabs.push({ ...spec, button, count, pane, empty });
  }

  const setActiveTab = (key) => {
    for (const tab of tabs) {
      const active = tab.key === key;
      tab.pane.dataset.active = active ? "true" : "false";
      tab.button.setAttribute("aria-selected", active ? "true" : "false");
    }
    node.properties ||= {};
    node.properties.h3_tab = key;
  };

  for (const tab of tabs) {
    tab.button.addEventListener("click", () => setActiveTab(tab.key));
  }
  const selfliftTab = tabs.find((tab) => tab.key === "selflift");
  if (selfliftTab) selfliftTab.empty.style.display = "none";

  const keyframeTab = tabs.find((tab) => tab.key === "keyframes");
  if (keyframeTab) keyframeTab.pane.insertBefore(editor, keyframes);
  if (keyframeTab) keyframeTab.pane.insertBefore(backendNote, editor);

  const meta = makeElement("div", "h3-director-meta");
  const resizeBar = makeElement("div", "h3-director-resize");
  resizeBar.title = "上下拖动调整面板高度，双击恢复默认";
  root.append(head, review, tabsBar, panes, notice, error, meta, resizeBar);

  // ``notice`` survives status refreshes (``error`` gets rewritten by
  // updatePanel on every poll, which used to swallow the reroll confirmation
  // within a few hundred milliseconds).
  const setNotice = (text = "", kind = "info") => {
    notice.textContent = text;
    notice.dataset.kind = kind;
  };

  const ui = {
    state,
    mode,
    jobs: [],
    jobSelect,
    scriptMap: new Map(),
    editor,
    review,
    reviewTitle,
    reviewHint,
    refImage,
    rerollRef,
    replaceRef,
    go,
    rerollAll,
    reviewState: "",
    lastReference: null,
    keyframes,
    backendNote,
    transitions,
    segments,
    final,
    finalVideo,
    finalEmpty,
    download,
    notice,
    error,
    meta,
    tabs,
    selflift: selfliftCtl,
    setActiveTab,
    setNotice,
    rewriting: false,
    segmentSignature: "",
    keyframeSignature: "",
    transitionSignature: "",
    finalUrl: "",
    jobId: "",
    awaitingReview: false,
    keyframeHandlers: {},
  };

  setActiveTab(node.properties?.h3_tab || "selflift");

  const currentJobId = () => ui.jobId || node.properties?.h3_job_id || "";
  const refreshOnce = () => {
    const jobId = currentJobId();
    if (jobId) pollJob(node, ui, jobId);
  };

  // ---- 分段脚本：边看关键帧边改秒数/提示词 --------------------------------
  let editingIndex = 0;
  const closeEditor = () => {
    editingIndex = 0;
    editor.dataset.open = "false";
    editorState.textContent = "";
  };
  const openEditor = (index) => {
    const segIndex = Number(index);
    if (!segIndex) return;
    editingIndex = segIndex;
    const script = ui.scriptMap?.get(segIndex) || null;
    editorTitle.textContent = `第 ${segIndex} 段脚本`;
    secsInput.value =
      script?.seconds === undefined || script?.seconds === null
        ? ""
        : String(script.seconds);
    promptArea.value = script?.prompt || "";
    startArea.value = script?.start_frame_prompt || "";
    endArea.value = script?.end_frame_prompt || "";
    // 首帧写了专属词就摊开给用户看，没写就收起（省地方给尾帧）。
    headDetails.open = !!startArea.value.trim();
    startArea.placeholder = "留空 = 跟主提示词一样";
    endArea.placeholder = "留空 = 跟主提示词一样";
    startArea.title = "这段开头那一帧用哪句提示词；留空就用主提示词";
    endArea.title = "这段结尾那一帧用哪句提示词；留空就用主提示词";
    const frames = (ui.lastKeyframes || []).find(
      (item) => Number(item.index) === segIndex,
    );
    for (const side of ["start", "end"]) {
      const url = side === "start" ? frames?.start_url : frames?.end_url;
      const image = frameSlots[side];
      if (url) image.src = url;
      else image.removeAttribute("src");
    }
    // 这一段有你手动放的图时，才给「连手动图一起重出」这个开关。
    const hasManual =
      !!frames &&
      (String(frames.start_source) === "manual" ||
        String(frames.end_source) === "manual");
    forceLabel.style.display = hasManual ? "" : "none";
    forceCheck.checked = false;
    editor.dataset.open = "true";
    editorState.textContent = script
      ? ""
      : "这一段还没写进任务（先跑一次关键帧，或刷新预览）";
    setActiveTab("keyframes");
    editor.scrollIntoView?.({ block: "nearest" });
    promptArea.focus();
  };
  const saveScript = async (redraw) => {
    const jobId = currentJobId();
    if (!jobId) {
      setNotice("还没有选中任务，先在右上角选一个", "warn");
      return;
    }
    if (!editingIndex) return;
    if (!promptArea.value.trim()) {
      setNotice("提示词不能空着", "warn");
      return;
    }
    saveButton.disabled = true;
    saveRedrawButton.disabled = true;
    editorState.textContent = redraw ? "正在保存并排队重画…" : "正在保存…";
    try {
      const result = await postJson(
        `/h3-long-video/script/${encodeURIComponent(jobId)}/${editingIndex}`,
        {
          seconds: secsInput.value === "" ? null : Number(secsInput.value),
          prompt: promptArea.value,
          start_frame_prompt: startArea.value,
          end_frame_prompt: endArea.value,
          force: forceLabel.style.display !== "none" && forceCheck.checked,
          redraw,
        },
      );
      ui.keyframeSignature = "";
      const changed = (result.changed || []).join("、") || "只动了帧的来源";
      if (result.queued) {
        const label =
          (result.sides || [])
            .map((side) => (side === "start" ? "首帧" : "尾帧"))
            .join("、") || "关键帧";
        const cascade = result.cascade || [];
        const tail = cascade.length
          ? `；后面第 ${cascade.join("、")} 段的首帧要跟着它走，会顺着一条链一起重出（每段约 1–2 分钟）`
          : "";
        setNotice(`第 ${editingIndex} 段已保存（${changed}），正在重出${label}${tail}…`);
        startPolling(node, ui, jobId);
      } else {
        setNotice(
          `第 ${editingIndex} 段已保存（${changed}）` +
            (result.note ? `。${result.note}` : "") +
            (redraw && !result.queued
              ? "。"
              : "。改完提示词要点「确认关键帧，生成视频」重跑这一段。"),
        );
        refreshOnce();
      }
      editorState.textContent = "已保存";
    } catch (error) {
      editorState.textContent = "";
      setNotice(`保存失败：${error.message}`, "warn");
    } finally {
      saveButton.disabled = false;
      saveRedrawButton.disabled = false;
    }
  };
  saveButton.addEventListener("click", () => saveScript(false));
  saveRedrawButton.addEventListener("click", () => saveScript(true));
  editorClose.addEventListener("click", closeEditor);

  ui.keyframeHandlers = {
    onEditScript(index) {
      openEditor(index);
    },
    onZoom(index, side) {
      openLightbox(ui, "keyframes", `kf-${index}-${side}`);
    },
    onZoomSegment(item) {
      openLightbox(ui, "segments", `seg-${item.index}`);
    },
    onZoomTransition(item) {
      openLightbox(ui, "transitions", `tr-${item.index}`);
    },
    async onReplace(index, side) {
      const jobId = currentJobId();
      if (!jobId) return;
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/*";
      input.addEventListener("change", async () => {
        const file = input.files?.[0];
        if (!file) return;
        const formData = new FormData();
        formData.append("image", file, file.name);
        const sideLabel = side === "start" ? "开始" : "结束";
        setNotice(`正在替换第 ${index} 段${sideLabel}帧…`);
        try {
          const result = await postFormData(
            `/h3-long-video/keyframe/${encodeURIComponent(jobId)}/${index}/${side}/replace?run=1`,
            formData,
          );
          ui.keyframeSignature = "";
          if (result.queued) {
            setNotice(
              `第 ${index} 段${sideLabel}帧已替换，正在同步下游关键帧…`,
            );
            startPolling(node, ui, jobId);
          } else if (result.note) {
            setNotice(
              `第 ${index} 段${sideLabel}帧已替换（${result.note}）。` +
                "下游帧会在点「确认关键帧，生成视频」时同步。",
              "warn",
            );
            refreshOnce();
          } else {
            setNotice(`第 ${index} 段${sideLabel}帧已替换为你上传的图片。`);
            refreshOnce();
          }
        } catch (error) {
          setNotice(`替换失败：${error.message}`, "warn");
        }
      });
      input.click();
    },
    async onReroll(index, side) {
      const jobId = currentJobId();
      if (!jobId) return;
      const sideLabel = side === "start" ? "开始" : "结束";
      setNotice(`正在重画第 ${index} 段${sideLabel}帧…`);
      try {
        const result = await postJson(
          `/h3-long-video/keyframe/${encodeURIComponent(jobId)}/${index}/${side}/reroll`,
          { run: true },
        );
        ui.keyframeSignature = "";
        if (result.queued) {
          setNotice(
            `已开始重画第 ${index} 段${sideLabel}帧（第 ${result.attempt} 次），` +
              "下游关键帧会一起更新。画完会自动回到待确认状态。",
          );
          startPolling(node, ui, jobId);
        } else {
          setNotice(
            `第 ${index} 段${sideLabel}帧已标记重画` +
              (result.note ? `（${result.note}）` : "") +
              "，点「确认关键帧，生成视频」时会重画。",
            "warn",
          );
          refreshOnce();
        }
      } catch (error) {
        setNotice(`重生成失败：${error.message}`, "warn");
      }
    },
    async onReset(index, side) {
      const jobId = currentJobId();
      if (!jobId) return;
      try {
        await postJson(
          `/h3-long-video/keyframe/${encodeURIComponent(jobId)}/${index}/${side}/reset`,
        );
        setNotice("");
        ui.keyframeSignature = "";
        refreshOnce();
      } catch (error) {
        setNotice(`还原失败：${error.message}`, "warn");
      }
    },
  };

  go.addEventListener("click", async () => {
    const jobId = currentJobId();
    if (!jobId) return;
    const mode = ui.reviewState || "gate";
    go.disabled = true;
    if (mode === "reference") {
      setNotice("已确认扩图，开始生成关键帧…");
      try {
        await postJson(
          `/h3-long-video/reference/${encodeURIComponent(jobId)}/confirm`,
          {},
        );
        startPolling(node, ui, jobId);
      } catch (error) {
        go.disabled = false;
        setNotice(`确认失败：${error.message}`, "warn");
      }
      return;
    }
    if (mode === "keyframes") {
      setNotice("正在补齐关键帧…");
      try {
        await postJson(`/h3-long-video/keyframes/${encodeURIComponent(jobId)}`, {});
        startPolling(node, ui, jobId);
      } catch (error) {
        go.disabled = false;
        setNotice(`继续生成关键帧失败：${error.message}`, "warn");
      }
      return;
    }
    setNotice("正在开始生成分段视频…");
    try {
      await postJson(`/h3-long-video/resume/${encodeURIComponent(jobId)}`, {});
      ui.review.style.display = "none";
      startPolling(node, ui, jobId);
    } catch (error) {
      go.disabled = false;
      setNotice(`继续生成失败：${error.message}`, "warn");
    }
  });

  rerollRef?.addEventListener("click", async () => {
    const jobId = currentJobId();
    if (!jobId) return;
    rerollRef.disabled = true;
    setNotice("正在重新扩参考图（换一个 seed）…");
    try {
      await postJson(
        `/h3-long-video/reference/${encodeURIComponent(jobId)}/reroll`,
        {},
      );
      ui.lastReference = null;
      startPolling(node, ui, jobId);
    } catch (error) {
      setNotice(`重新扩图失败：${error.message}`, "warn");
    } finally {
      rerollRef.disabled = false;
    }
  });

  replaceRef?.addEventListener("click", async () => {
    const jobId = currentJobId();
    if (!jobId) return;
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.addEventListener("change", async () => {
      const file = input.files?.[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("image", file, file.name);
      setNotice("正在替换参考扩图（会自动裁到成片比例）…");
      try {
        const result = await postFormData(
          `/h3-long-video/reference/${encodeURIComponent(jobId)}/replace`,
          formData,
        );
        ui.lastReference = {
          ...(ui.lastReference || {}),
          status: "ready",
          source: "user",
          url: result.url,
        };
        setNotice("参考图已替换，请确认后再生成关键帧。");
        refreshOnce();
      } catch (error) {
        setNotice(`替换失败：${error.message}`, "warn");
      }
    });
    input.click();
  });

  rerollAll.addEventListener("click", async () => {
    const jobId = currentJobId();
    if (!jobId) return;
    const items = node.h3DirectorUI?.lastKeyframes || [];
    const targets = [];
    for (const item of items) {
      for (const side of ["start", "end"]) {
        if (side === "start" && item.chain) continue;
        if (side === "end" && item.end_source === "hold") continue;
        // Free ends and pending starts have no image to re-roll.
        if (!(side === "start" ? item.start_url : item.end_url)) continue;
        if (side === "end" && item.end_source === "free") continue;
        targets.push([item.index, side]);
      }
    }
    if (!targets.length) {
      setNotice("没有可以重画的关键帧（链式开始帧和定格尾帧无法单独重画）。", "warn");
      return;
    }
    setNotice(`正在重画 ${targets.length} 张关键帧…`);
    let queued = false;
    for (let i = 0; i < targets.length; i += 1) {
      const [index, side] = targets[i];
      const last = i === targets.length - 1;
      try {
        const result = await postJson(
          `/h3-long-video/keyframe/${encodeURIComponent(jobId)}/${index}/${side}/reroll`,
          { run: last },
        );
        if (result.queued) queued = true;
      } catch (error) {
        setNotice(`重生成失败：${error.message}`, "warn");
        ui.keyframeSignature = "";
        refreshOnce();
        return;
      }
    }
    ui.keyframeSignature = "";
    if (queued) {
      setNotice(`已开始重画 ${targets.length} 张关键帧，画完后回到待确认状态。`);
      startPolling(node, ui, jobId);
    } else {
      setNotice(`已标记 ${targets.length} 张关键帧待重画。`);
      refreshOnce();
    }
  });

  node.h3DirectorUI = ui;
  const panelWidget = node.addDOMWidget(
    "h3_director_preview",
    "H3DirectorPreview",
    root,
    { serialize: false },
  );
  node.setSize([
    Math.max(node.size[0], 840),
    Math.min(1200, Math.max(node.size[1], 900)),
  ]);

  // ---- 面板高度：底部拖柄 + 记忆 -------------------------------------------
  const DEFAULT_PANEL_HEIGHT = 560;
  let panelHeight = Number(node.properties?.h3_panel_height) || DEFAULT_PANEL_HEIGHT;

  const applyPanelHeight = (height, { resizeNode = true } = {}) => {
    panelHeight = Math.max(300, Math.min(2600, Math.round(height)));
    node.properties ||= {};
    node.properties.h3_panel_height = panelHeight;
    if (!node.h3DirectorMode) {
      root.style.height = `${panelHeight}px`;
    }
    if (panelWidget) {
      panelWidget.computeSize = (width) => [width, panelHeight];
    }
    if (resizeNode) {
      node.setSize([
        node.size[0],
        Math.max(node.size[1], panelHeight + 300),
      ]);
      node.setDirtyCanvas?.(true, true);
    }
  };
  applyPanelHeight(panelHeight, { resizeNode: false });

  let dragStartY = 0;
  let dragStartHeight = 0;
  resizeBar.addEventListener("pointerdown", (event) => {
    if (node.h3DirectorMode) return;
    event.preventDefault();
    event.stopPropagation();
    dragStartY = event.clientY;
    dragStartHeight = panelHeight;
    resizeBar.setPointerCapture?.(event.pointerId);
    const onMove = (moveEvent) => {
      applyPanelHeight(dragStartHeight + (moveEvent.clientY - dragStartY));
    };
    const onUp = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  });
  resizeBar.addEventListener("dblclick", () => {
    applyPanelHeight(DEFAULT_PANEL_HEIGHT);
  });

  // ---- 全屏 / 独立窗口：把面板搬出画布 ------------------------------------
  const overlay = makeElement("div", "h3-director-overlay");
  document.body.appendChild(overlay);
  overlay.addEventListener("wheel", (event) => event.stopPropagation(), {
    passive: true,
  });
  overlay.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
    if (event.target === overlay) setPanelMode(null);
  });

  let detachHome = null;
  let popupWindow = null;
  let popupTimer = null;

  function detachRoot(host, doc) {
    const parent = root.parentElement;
    const placeholder = makeElement("div", "h3-director-placeholder");
    placeholder.style.height = `${Math.max(120, root.offsetHeight || panelHeight)}px`;
    if (parent) {
      parent.insertBefore(placeholder, root);
    } else {
      document.body.appendChild(placeholder);
    }
    detachHome = { parent, placeholder };
    copyPanelStyles(doc);
    host.appendChild(root);
  }

  function reattachRoot() {
    const target = detachHome?.placeholder;
    if (target?.parentNode) {
      target.parentNode.replaceChild(root, target);
    } else if (detachHome?.parent) {
      detachHome.parent.appendChild(root);
    }
    detachHome = null;
  }

  function closePopup() {
    if (popupTimer) {
      clearInterval(popupTimer);
      popupTimer = null;
    }
    if (popupWindow && !popupWindow.closed) popupWindow.close();
    popupWindow = null;
  }

  function syncModeButtons() {
    const mode = node.h3DirectorMode || null;
    fullscreenButton.textContent = mode === "fullscreen" ? "退出全屏" : "⛶ 全屏";
    popupButton.textContent = mode === "popup" ? "关闭独立窗口" : "⧉ 独立窗口";
    root.dataset.fullscreen = mode ? "true" : "false";
    root.style.height = mode ? "100%" : `${panelHeight}px`;
  }

  function setPanelMode(mode) {
    const current = node.h3DirectorMode || null;
    if (mode === current) return;

    // 先清状态再关窗口：window.close() 会同步触发 beforeunload -> setPanelMode(null)，
    // 不清状态就会递归进来把节点搬两次
    node.h3DirectorMode = null;
    if (current === "fullscreen") {
      overlay.dataset.open = "false";
    } else if (current === "popup") {
      closePopup();
    }
    if (current) {
      reattachRoot();
      moveLightboxToDoc(document);
    }

    node.h3DirectorMode = mode;
    if (mode === "fullscreen") {
      if (!overlay.isConnected) document.body.appendChild(overlay);
      detachRoot(overlay, document);
      overlay.dataset.open = "true";
    } else if (mode === "popup") {
      const popup = window.open(
        "",
        `h3director-${node.id ?? "node"}`,
        "width=1280,height=960,menubar=no,toolbar=no,location=no,status=no",
      );
      if (!popup) {
        node.h3DirectorMode = null;
        syncModeButtons();
        setNotice("浏览器拦截了弹出窗口，允许弹窗后再点一次（或先用全屏）", "warn");
        return;
      }
      popupWindow = popup;
      try {
        popup.document.title = "H3 Director 预览";
      } catch (error) {
        /* 某些浏览器刚 open 时还拿不到 document，忽略 */
      }
      copyPanelStyles(popup.document);
      detachRoot(popup.document.body, popup.document);
      moveLightboxToDoc(popup.document);
      popup.document.addEventListener("keydown", (event) => {
        if (LIGHTBOX.root?.dataset.open === "true") return;
        if (event.key === "Escape") setPanelMode(null);
      });
      popup.addEventListener("beforeunload", () => {
        if (node.h3DirectorMode === "popup") setPanelMode(null);
      });
      popupTimer = setInterval(() => {
        if (!popupWindow || popupWindow.closed) setPanelMode(null);
      }, 700);
    }

    syncModeButtons();
    countSegments(node, ui);
    node.setDirtyCanvas?.(true, true);
  }

  node.h3DirectorSetFullscreen = (on) => setPanelMode(on ? "fullscreen" : null);

  fullscreenButton.addEventListener("click", () => {
    setPanelMode(node.h3DirectorMode === "fullscreen" ? null : "fullscreen");
  });
  popupButton.addEventListener("click", () => {
    setPanelMode(node.h3DirectorMode === "popup" ? null : "popup");
  });

  document.addEventListener("keydown", (event) => {
    // 灯箱开着的时候 Esc 先关灯箱，别把全屏一起退了
    if (LIGHTBOX.root?.dataset.open === "true") return;
    if (event.key === "Escape" && node.h3DirectorMode) {
      setPanelMode(null);
      event.preventDefault();
    }
  });

  const originalOnRemoved = node.onRemoved;
  node.onRemoved = function () {
    if (node.h3DirectorMode) setPanelMode(null);
    overlay.remove();
    return originalOnRemoved?.apply(this, arguments);
  };

  // 任务被删掉之后得把面板清干净，否则还挂着已经不存在的那套图。
  const resetPanel = () => {
    ui.keyframeSignature = "";
    ui.segmentSignature = "";
    ui.transitionSignature = "";
    ui.finalUrl = "";
    ui.scriptMap = new Map();
    ui.lastKeyframes = [];
    ui.lastSegments = [];
    ui.lastTransitions = [];
    ui.lastFinal = null;
    ui.awaitingReview = false;
    ui.reviewState = "";
    ui.keyframes.replaceChildren();
    ui.transitions.replaceChildren();
    ui.segments.replaceChildren();
    if (ui.backendNote) ui.backendNote.style.display = "none";
    ui.review.style.display = "none";
    ui.final.style.display = "none";
    ui.finalEmpty.style.display = "block";
    ui.meta.textContent = "";
    ui.meta.title = "";
    ui.download.textContent = "";
    ui.download.removeAttribute("href");
    state.textContent = "等待运行";
    mode.textContent = "";
    closeEditor();
    for (const tab of ui.tabs) {
      tab.count.textContent = "0";
      if (tab.key !== "final") tab.empty.style.display = "block";
    }
    syncLightbox(ui);
  };

  renameButton.addEventListener("click", async () => {
    const jobId = currentJobId();
    if (!jobId) {
      setNotice("先在上面的任务列表里选一个任务，再改名", "warn");
      return;
    }
    const info = (ui.jobs || []).find((job) => job.job_id === jobId);
    const suggestion = info?.name || jobId.replace(/^\d{8}-/, "");
    const name = window.prompt(
      `给这个任务起个名字（会变成「日期-名称」）：`,
      suggestion,
    );
    if (name === null) return;
    if (!name.trim()) {
      setNotice("名字不能为空", "error");
      return;
    }
    renameButton.disabled = true;
    try {
      const response = await api.fetchApi(
        `/h3-long-video/rename/${encodeURIComponent(jobId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        },
      );
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      const newId = data.job_id || jobId;
      if (newId !== jobId) {
        ui.jobId = newId;
        node.properties ||= {};
        node.properties.h3_job_id = newId;
        const resumeWidget = findWidget(node, "resume_job_id");
        if (resumeWidget && resumeWidget.value === jobId) {
          resumeWidget.value = newId;
        }
        ui.keyframeSignature = "";
        startPolling(node, ui, newId);
      }
      const jobs = await loadJobList();
      if (jobs) renderJobOptions(node, ui, jobs);
      setNotice(`已改名为 ${newId}`, "ok");
    } catch (error) {
      setNotice(`改名失败：${error.message}`, "error");
    } finally {
      renameButton.disabled = false;
    }
  });

  deleteButton.addEventListener("click", async () => {
    const jobId = currentJobId();
    if (!jobId) {
      setNotice("先在上面的任务列表里选一个任务，再删除", "warn");
      return;
    }
    const info = (ui.jobs || []).find((job) => job.job_id === jobId);
    const size = info ? `，约 ${formatBytes(info.bytes)}` : "";
    const confirmed = window.confirm(
      `删除任务 ${jobId}${size}？\n\n` +
        "会删掉：任务文件夹（首尾帧、日志、中间产物）+ 这个任务自己生成的段视频/转场。\n" +
        "成片 long_video.mp4 和别的任务共用的文件不动。不可恢复。",
    );
    if (!confirmed) return;
    deleteButton.disabled = true;
    try {
      const response = await api.fetchApi(
        `/h3-long-video/delete/${encodeURIComponent(jobId)}`,
        { method: "POST" },
      );
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      if ((ui.jobId || node.properties?.h3_job_id) === jobId) {
        stopPolling(node);
        ui.jobId = "";
        node.properties ||= {};
        node.properties.h3_job_id = "";
        const resumeWidget = findWidget(node, "resume_job_id");
        if (resumeWidget && resumeWidget.value === jobId) {
          resumeWidget.value = "";
        }
        resetPanel();
      }
      const jobs = (await loadJobList()) || [];
      if (!ui.jobId && jobs.length) {
        startPolling(node, ui, jobs[0].job_id);
      }
      renderJobOptions(node, ui, jobs);
      setNotice(
        `已删除 ${jobId}：任务目录 + ${data.outputs || 0} 个产物，` +
          `释放 ${formatBytes(data.freed || 0)}`,
        "ok",
      );
    } catch (error) {
      setNotice(`删除失败：${error.message}`, "error");
    } finally {
      deleteButton.disabled = false;
    }
  });

  refresh.addEventListener("click", () => {
    countSegments(node, ui);
    const jobId = ui.jobId || node.properties?.h3_job_id;
    if (jobId) pollJob(node, ui, jobId);
    loadJobList().then((jobs) => {
      if (jobs) renderJobOptions(node, ui, jobs);
    });
  });

  jobSelect.addEventListener("change", () => {
    const jobId = jobSelect.value;
    if (!jobId) return;
    ui.jobId = jobId;
    node.properties ||= {};
    node.properties.h3_job_id = jobId;
    ui.keyframeSignature = "";
    closeEditor();
    startPolling(node, ui, jobId);
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
    "resolution",
    "final_width",
    "final_height",
    "video_backend",
    "seed",
    "randomize_seed",
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
  // 没有任务号的时候（比如直接从旧工作流打开），自动挂到最近的一个任务上，
  // 这样关键帧不用手填 id 就能看到。
  loadJobList().then((jobs) => {
    if (!jobs) return;
    ui.jobs = jobs;
    if (!(ui.jobId || node.properties?.h3_job_id) && jobs.length) {
      startPolling(node, ui, jobs[0].job_id);
    }
    renderJobOptions(node, ui, jobs);
  });
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
        const resumeWidget = findWidget(node, "resume_job_id");
        if (resumeWidget) resumeWidget.value = payload.job_id;
        loadJobList().then((jobs) => {
          if (jobs) renderJobOptions(node, node.h3DirectorUI, jobs);
        });
      }
      return result;
    };
    const originalRemoved = node.onRemoved;
    node.onRemoved = function () {
      stopPolling(node);
      this.h3DirectorUI?.selflift?.stop?.();
      return originalRemoved?.apply(this, arguments);
    };
    const originalConfigure = node.onConfigure;
    node.onConfigure = function () {
      const result = originalConfigure?.apply(this, arguments);
      setTimeout(() => {
        migrateWidgetValues(this);
        countSegments(this, this.h3DirectorUI);
        app.graph.setDirtyCanvas(true, true);
        const ui = this.h3DirectorUI;
        if (ui) {
          const jobId = ui.jobId || this.properties?.h3_job_id;
          if (jobId && !ui.jobId) startPolling(this, ui, jobId);
          loadJobList().then((jobs) => {
            if (jobs) renderJobOptions(this, ui, jobs);
          });
        }
      }, 0);
      return result;
    };
  },
});
