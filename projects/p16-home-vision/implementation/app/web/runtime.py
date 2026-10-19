from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["web"])

BASELINE_PATH = Path(__file__).with_name("r11-approved.html")
RUNTIME_SCRIPT_MARKER = "<!-- HOME_VISION_RUNTIME_SCRIPT -->"

RUNTIME_STYLE = r'''
<style id="homeVisionRuntimeStyle">
  .content > .page { width: 100%; min-width: 0; }
  .overview > *, .storage > *, .telemetry > *, .page > * { min-width: 0; }
  .overview { grid-template-columns: minmax(0, 1fr) minmax(320px, 380px); grid-template-rows: auto; align-items: start; }
  /* Keep the dashboard sections in deliberate bands.  Auto-placement made the
     tall telemetry column stretch the row and leave a large empty area beside it. */
  .overview > .calendar { grid-column: 1; grid-row: 1; }
  .overview > .summary { grid-column: 2; grid-row: 1; }
  .overview > .storage { grid-column: 1 / -1; grid-row: 2; }
  .overview > .telemetry { grid-column: 1; grid-row: 3; }
  .overview > .activity { grid-column: 2; grid-row: 3; }
  .overview > .pad { grid-column: 1 / -1; grid-row: 4; }
  .head, .calhead, .date-title, .row { min-width: 0; }
  .head > div, .calhead > div, .date-title > div, .row > div { min-width: 0; }
  .head h1, .date-title b { overflow-wrap: anywhere; }
  .calgrid { grid-template-columns: repeat(7, minmax(0, 1fr)); }
  .day { min-width: 0; overflow: hidden; }
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); min-width: 0; }
  .summary .metric { min-width: 0; overflow: hidden; }
  .summary .metric .k, .summary .metric .v { overflow-wrap: anywhere; }
  .runtime-empty { border: 1px dashed var(--l); border-radius: 12px; background: #fbfcfd; }
  .runtime-empty strong { display: block; color: var(--t); margin-bottom: 4px; }
  .runtime-section-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; padding: 14px 14px 10px; }
  .runtime-section-head small { color: var(--m); }
  .runtime-state { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
  .runtime-state div { background: #f8fafc; border: 1px solid #edf0f3; border-radius: 9px; padding: 9px; }
  .runtime-state b { display: block; margin-top: 2px; }
  .runtime-identity { display: grid; gap: 4px; margin-top: 10px; color: var(--m); font-size: 12px; }
  .runtime-identity b { color: var(--t); font-size: 13px; }
  .thumb { min-height: 122px; flex-direction: column; align-items: flex-start; justify-content: flex-end; gap: 2px; }
  .thumb small { font-size: 10px; color: rgba(255,255,255,.88); }
  .thumb-media { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: #dbe1e8; }
  .thumb-shell { position:relative; width:100%; aspect-ratio:16 / 9; overflow:hidden; background:linear-gradient(135deg,#274357,#183247); }
  .thumb-shell .thumb-media { position:absolute; inset:0; width:100%; height:100%; opacity:0; transition:opacity .18s ease; }
  .thumb-loading { position:absolute; inset:0; display:grid; place-items:center; align-content:center; gap:8px; color:#b9d2df; background:linear-gradient(110deg,rgba(255,255,255,.035) 8%,rgba(255,255,255,.09) 18%,rgba(255,255,255,.035) 33%); background-size:220% 100%; animation:thumbShimmer 1.35s linear infinite; }
  .thumb-loading small { font-size:11px; }
  .thumb-spinner { width:20px; height:20px; border:2px solid rgba(185,210,223,.28); border-top-color:#66d9ff; border-radius:50%; animation:thumbSpin .8s linear infinite; }
  .thumb-shell.loaded .thumb-media { opacity:1; }
  .thumb-shell.loaded .thumb-loading { opacity:0; pointer-events:none; animation:none; }
  .thumb-shell.error .thumb-loading { animation:none; background:linear-gradient(135deg,#294357,#1b3042); color:#d7e4eb; }
  .thumb-shell.error .thumb-spinner { display:none; }
  @keyframes thumbSpin { to { transform:rotate(360deg); } }
  @keyframes thumbShimmer { to { background-position:-220% 0; } }
  .day.unknown { background: #fff; border-style: dashed; color: var(--m); }
  .day.scanned { background: #f7f8fa; }
  .day.partial { background: #fff8e8; border-color: #f0d29f; }
  .day.failed { background: #fff0f0; border-color: #efbcbc; color: #873636; }
  .day.recorded { background: var(--gs); border-color: #b9dfcc; }
  .activityrow { grid-template-columns: 92px minmax(0, 1fr) auto; }
  .activityrow span:nth-child(2) { overflow-wrap: anywhere; }
  .task-stages { display: grid; gap: 5px; min-width: 0; }
  .task-stage { display: grid; grid-template-columns: 34px minmax(54px, 1fr) 58px; gap: 6px; align-items: center; font-size: 11px; }
  .task-stage .progress { height: 6px; }
  .task-stage b { text-align: right; white-space: nowrap; }
  .task-stage.unavailable { color: var(--m); }
  .task-stage.unavailable .progress span { background: #cbd5e1; }
  .task-action .btn { white-space: nowrap; }
  .task-actions { display: flex; gap: 6px; flex-wrap: wrap; }
  .task-group-head { padding: 12px 14px; font-weight: 700; background: #f8fafc; border-bottom: 1px solid var(--l); }
  .task-history { border-top: 1px solid var(--l); }
  .task-history summary { cursor: pointer; padding: 12px 14px; font-weight: 700; }
  .card-progress { display: grid; gap: 4px; margin-top: 8px; }
  .card-progress .progress { height: 6px; }
  .card-progress small { color: var(--m); font-size: 10px; }
  .archive-availability { margin: -2px 0 12px; padding: 10px 12px; border: 1px solid #dce5f8; border-radius: 11px; background: #f7f9ff; color: #38527a; }
  .archive-tools { min-width: 0; overflow-x: auto; scrollbar-width: thin; }
  #archive { overflow-anchor: none; }
  .archive-tools > * { flex: 0 0 auto; }
  .date-title { gap: 12px; flex-wrap: wrap; }
  .date-title > .btn { flex: 0 0 auto; }
  .lane, .segments { min-width: 0; }
  .segments { padding-bottom: 4px; scrollbar-width: thin; }
  .analysisitem .runtime-empty { padding: 22px; }
  .player { width: min(760px, calc(100vw - 32px)); max-height: calc(100vh - 32px); right: 16px; bottom: 16px; }
  .player.large { width: min(920px, calc(100vw - 32px)); }
  .phead b { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .phead, .pfoot { gap: 12px; }
  .phead > div { display: flex; gap: 6px; flex: 0 0 auto; }
  .phead button, .pfoot button { min-width: 36px; min-height: 32px; }
  .phead #playerClose { background: #b42318; }
  .screen { position: relative; overflow: hidden; }
  .screen { width: 100%; min-height: 0; aspect-ratio: 16 / 9; }
  .screen video { display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }
  .screen video { max-width: 100%; max-height: 100%; }
  .screen-status { position: absolute; inset: auto 12px 10px; z-index: 1; padding: 7px 9px; border-radius: 8px; background: rgba(15,23,42,.78); color: #fff; font-size: 12px; pointer-events: none; }
  .screen-status.ready { display: none; }
  .player.full { inset: 16px; width: auto; max-height: none; }
  .player.full .screen { height: calc(100vh - 132px); aspect-ratio: auto; }
  @media(max-width:850px) {
    .overview { grid-template-columns: 1fr; }
    .overview > * { grid-column: 1; grid-row: auto; }
    .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .head { flex-wrap: wrap; }
    .activityrow { grid-template-columns: 1fr; gap: 4px; }
    .taskrow { grid-template-columns: 1fr 90px; }
    .taskrow > .task-stages, .taskrow > .task-action { grid-column: 1 / -1; }
    .player { left: 8px; right: 8px; bottom: 62px; width: auto; max-height: calc(100vh - 78px); }
    .player.large { width: auto; }
    .player.full { inset: 8px 8px 62px; }
    .player.full .screen { height: auto; aspect-ratio: 16 / 9; }
  }


  /* P16R1 B — Balanced Dark Glass (Stage 4 Human-selected) */
  :root {
    --bg:#1b2a38; --s:rgba(50,67,82,.86); --t:#edf4f8; --m:#a9bac5;
    --l:rgba(190,219,234,.13); --b:#68b9ff; --bs:rgba(104,185,255,.11);
    --g:#62d99a; --gs:rgba(98,217,154,.11); --r:#ff8490; --rs:rgba(255,132,144,.12);
    --o:#efbd69; --os:rgba(239,189,105,.12); --p:#b6a0e8; --ps:rgba(182,160,232,.12);
    --sh:0 20px 54px rgba(5,14,22,.28);
  }
  body {
    background:radial-gradient(circle at 78% 10%,rgba(104,185,255,.075),transparent 30%),
               radial-gradient(circle at 16% 82%,rgba(98,217,154,.035),transparent 36%),
               linear-gradient(145deg,#182633,#213446 48%,#1a2b39);
    color:var(--t);
  }
  body::before { content:""; position:fixed; inset:0; pointer-events:none; background-image:linear-gradient(rgba(255,255,255,.010) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.010) 1px,transparent 1px); background-size:42px 42px; mask-image:linear-gradient(to bottom,rgba(0,0,0,.45),transparent 72%); }
  .app { grid-template-columns:94px 1fr; }
  .side { background:linear-gradient(180deg,rgba(20,34,47,.97),rgba(27,44,58,.95)); border-right:1px solid rgba(220,238,247,.08); padding:16px 10px; backdrop-filter:blur(20px); }
  .brand { padding:8px 5px 18px; text-align:center; font-size:12px; color:#e2f0f6; }
  .brand small { font-size:9px; color:#91a7b5; }
  .nav button { text-align:center; color:#9eb1bd; border:1px solid transparent; border-radius:13px; padding:10px 4px; font-size:11px; }
  .nav button.active,.nav button:hover { color:#dcecff; background:rgba(104,185,255,.12); border-color:rgba(135,194,238,.18); }
  .top { background:rgba(27,43,56,.90); border-bottom:1px solid rgba(220,238,247,.08); backdrop-filter:blur(18px); color:var(--t); }
  .content { max-width:1600px; }
  .surface { background:linear-gradient(180deg,rgba(52,70,85,.90),rgba(42,59,73,.84)); border-color:var(--l); border-radius:20px; box-shadow:var(--sh),inset 0 1px 0 rgba(255,255,255,.045); backdrop-filter:blur(20px); }
  .btn { background:rgba(255,255,255,.055); border-color:rgba(190,219,234,.13); color:#c5d6df; }
  .btn.primary { color:#10202b; background:linear-gradient(135deg,#75c0ff,#73dda7); border-color:transparent; font-weight:800; }
  .btn.danger { background:rgba(255,116,131,.12); border-color:rgba(255,116,131,.22); color:#ffacb5; }
  .calendar { background:linear-gradient(145deg,#edf3f5,#dce7eb); border-color:rgba(183,213,226,.42); color:#263e4d; box-shadow:0 14px 34px rgba(7,19,28,.16),inset 0 1px 0 rgba(255,255,255,.72); }
  .calendar .muted,.calendar .meta,.calendar .dow { color:#647b89; }
  .overview > .summary .surface, .overview > .storage .surface, .overview > .telemetry .surface, .overview > .activity, .overview > .pad { background:linear-gradient(180deg,rgba(58,75,90,.91),rgba(47,64,79,.86)); }
  .calendar .btn { color:#496777; background:rgba(255,255,255,.56); border-color:rgba(60,94,112,.16); }
  .day { background:rgba(255,255,255,.54); border-color:rgba(57,88,104,.13); color:#607a89; }
  .day b { color:#233f52; }
  .day.recorded { background:linear-gradient(180deg,#effcf6,#e5f8ef); border-color:rgba(35,184,112,.22); }
  .day.scanned { background:linear-gradient(180deg,#f4fbff,#eaf6fc); border-color:rgba(44,157,211,.18); }
  .day.partial { background:linear-gradient(180deg,#fffaf0,#fff3d9); border-color:rgba(226,157,42,.24); }
  .day.failed { background:linear-gradient(180deg,#fff7f8,#ffebee); border-color:rgba(223,80,96,.22); color:#8e414a; }
  .day.unknown { background:rgba(234,240,243,.62); border-style:dashed; color:#80939e; }
  .metric,.volume,.telecard,.activityrow,.date,.lane,.seg,.analysisitem,.reviewcard,.taskrow,.event { color:var(--t); }
  .metric .k,.muted,.meta,.runtime-identity { color:var(--m); }
  .runtime-empty,.runtime-state div,.mini div,.event,.task-group-head { background:rgba(255,255,255,.055); border-color:rgba(222,238,246,.085); }
  .archive-tools { background:rgba(37,54,68,.94); border-color:rgba(190,219,234,.12); backdrop-filter:blur(18px); }
  .archive-availability { background:rgba(104,185,255,.07); border-color:rgba(135,194,238,.15); color:#bdd5e1; }
  .date-title { background:rgba(255,255,255,.070); }
  .seg { background:rgba(45,62,76,.89); border-color:rgba(222,238,246,.085); }
  .seg.selected { outline-color:rgba(42,203,255,.45); }
  .selectbar { background:#2a3f50; border:1px solid rgba(135,194,238,.20); }
  .progress,.bar { background:rgba(255,255,255,.07); }
  .progress span,.bar span { background:linear-gradient(90deg,#2acbff,#36ec98); }
  .player { background:#223646; border:1px solid rgba(135,194,238,.18); box-shadow:0 24px 60px rgba(4,13,20,.38); }
  .detail { background:#293f50; border-left-color:rgba(135,194,238,.15); color:var(--t); }
  .prov { background:rgba(255,255,255,.025); border-color:rgba(255,255,255,.06); color:#b9d6e6; }
  .modalback { background:rgba(9,18,25,.62); backdrop-filter:blur(8px); }
  .modal { background:#243746; color:var(--t); border:1px solid rgba(165,207,229,.16); }
  .impact { border-color:rgba(255,255,255,.08); }
  .mobile { background:#1b2a37; border-top-color:rgba(222,238,246,.09); }
  .mobile button { background:transparent; color:#a4b6c1; }
  .mobile button.active { color:var(--b); }
  @media(max-width:850px) { .app { grid-template-columns:1fr; } .content { padding:14px 10px 72px; } }

</style>
'''

RUNTIME_SUPPORT_SCRIPT = r'''
<script id="homeVisionRuntimeSupport">
(() => {
  const titles = {overview:'总览', archive:'归档', analysis:'分析', tasks:'任务', review:'待复核', history:'记录'};
  window.go = (id, element, updateLocation = true) => {
    if (!document.getElementById(id)) id = 'overview';
    if (updateLocation && window.location.hash !== `#${id}`) window.history.replaceState(null, '', `#${id}`);
    document.querySelectorAll('.page').forEach((page) => page.classList.toggle('active', page.id === id));
    const title = document.getElementById('title');
    if (title) title.textContent = titles[id] || id;
    document.querySelectorAll('.nav button, .mobile button').forEach((button) => button.classList.remove('active'));
    if (element) element.classList.add('active');
    document.querySelectorAll(`[data-page="${id}"]`).forEach((button) => button.classList.add('active'));
    window.homeVisionRuntime?.renderTruthfulSurfaces?.();
    window.homeVisionRuntime?.ensurePageData?.(id);
  };
  window.toast = (message) => {
    const node = document.getElementById('toast');
    if (!node) return;
    node.textContent = message;
    node.classList.add('show');
    window.setTimeout(() => node.classList.remove('show'), 2400);
  };
  window.openPlayer = (title, focusPlayer = false) => {
    const player = document.getElementById('player');
    const heading = document.getElementById('ptitle');
    if (heading) heading.textContent = title;
    player?.classList.add('show');
    if (focusPlayer) window.setTimeout(() => document.getElementById('playerClose')?.focus({preventScroll: true}), 0);
  };
  window.closePlayer = () => {
    window.homeVisionRuntime?.invalidateMediaRequest?.();
    const player = document.getElementById('player');
    const video = player?.querySelector('video');
    if (video) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
    const screen = document.getElementById('screen');
    if (screen) screen.innerHTML = '▶';
    player?.classList.remove('show');
    window.homeVisionRuntime?.flushArchiveRender?.();
  };
  window.toggleMin = () => {
    const screen = document.getElementById('screen');
    if (screen) screen.style.display = screen.style.display === 'none' ? 'grid' : 'none';
  };
  window.toggleLarge = () => document.getElementById('player')?.classList.toggle('large');
  window.toggleFull = () => document.getElementById('player')?.classList.toggle('full');
  window.closeAnalysis = () => document.getElementById('analysisDetail')?.classList.remove('show');
  window.closeModal = (id) => document.getElementById(id)?.classList.remove('show');
  window.openAnalysis = () => window.toast('当前没有可用的真实分析运行');
  window.toggleSelection = () => window.homeVisionArchiveSelection?.toggle?.();
  window.clearSel = () => window.homeVisionArchiveSelection?.clear?.();
  window.queueSelected = () => window.homeVisionArchiveSelection?.queue?.();
  window.openPullAll = () => window.toast('正在读取真实归档清单');
  window.openDelete = () => window.toast('删除必须通过真实待复核记录发起');
  window.openCancelAll = () => window.toast('请从任务页使用真实取消操作');
  window.confirmPullAll = () => {};
  window.confirmDelete = () => {};
  window.confirmCancelAll = () => {};
  document.querySelectorAll('.nav button').forEach((button) => {
    button.addEventListener('click', () => window.go(button.dataset.page, button));
  });
  const routeFromHash = () => window.go(window.location.hash.slice(1), null, false);
  window.addEventListener('hashchange', routeFromHash);
  document.addEventListener('keydown', (event) => {
    const dialog = document.querySelector('.modalback.show .modal');
    if (!dialog) return;
    if (event.key === 'Escape') {
      const back = dialog.closest('.modalback'); back?.classList.remove('show');
      window.homeVisionModalTrigger?.focus?.({preventScroll: true});
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = [...dialog.querySelectorAll('button:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')];
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  routeFromHash();
})();
</script>
'''

SEMANTIC_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("Home Vision Gate B v11", "Home Vision"),
    ('<button class="btn" onclick="toggleLab()">状态演示 · 原型</button>', ""),
    ("● NVR 在线 · Scene 已同步", "● Home Vision · 正在读取 API"),
    ("AIBOX Connector · Scene sync", "Integration / Consumer API"),
    ("已连接 · 12 Scene · 4 分钟前", "只读查询 · 外部消费者未接入"),
    ("归档事实独立于 AIBOX。", "只读接口不会下发任务或分析策略。"),
    ("A8 Host", "运行环境"),
    ("scene_snapshot_hash", "profile_snapshot_hash"),
    ("Scene", "分析策略"),
    ("AIBOX 同步失败 · 使用缓存 分析策略 r7；Archive truth 独立。", "外部消费者不可用：Home Vision 本地归档与分析继续工作。"),
    ('任务 <span id="taskCount">7</span>', '任务 <span id="taskCount">读取中</span>'),
    ('<option>客厅</option><option>卧室</option>', '<option>读取中</option>'),
    ('<b id="ptitle">客厅</b>', '<b id="ptitle">未选择录像</b>'),
)

RUNTIME_SCRIPT = r'''
<script id="homeVisionRuntime">
(() => {
  const state = { capabilities: null, health: null, runtime: null, archiveStatus: null, cameras: [], recordings: [], scans: [], assets: [], jobs: [], volumes: [], profiles: [], runs: [], events: [], reviews: [], audits: [], calendarMonth: null, selectedArchiveDate: null, archiveStatusFilter: 'all', archiveCameraFilter: 'all' };
  const jobSamples = new Map();
  let archiveScrollTop = 0;
  let archiveRenderKey = null;
  let archiveRenderDeferred = false;
  let thumbnailObserver = null;
  const THUMBNAIL_CONCURRENCY = 4;
  let thumbnailDomObserver = null;
  let thumbnailQueue = [];
  const thumbnailActive = new Map();
  const thumbnailInFlight = new Map();
  let playerItems = [];
  let playerIndex = -1;
  let mediaRequestId = 0;
  let archiveSelectionMode = false;
  const selectedArchiveIds = new Set();
  let hydrateInFlight = false;
  let pendingHydrateIntent = null;
  let jobsLoaded = false;
  let lastJobRefreshAt = 0;
  let lastCatalogRefreshAt = 0;
  const CATALOG_REFRESH_MS = 15000;
  const JOB_REFRESH_MS = 15000;
  const secondaryLoaded = new Set();
  const secondaryLoadedAt = new Map();
  const secondaryInFlight = new Set();
  const SECONDARY_REFRESH_MS = 15000;
  let activeRuntimeRun = null;
  let analysisEntry = null;

  const safeJson = async (path) => {
    const response = await fetch(path, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };

  const fetchAll = async (path, limit = 500, initialAfterId = null) => {
    const items = [];
    let afterId = initialAfterId;
    do {
      const separator = path.includes('?') ? '&' : '?';
      const query = `${path}${separator}limit=${limit}${afterId ? `&after_id=${encodeURIComponent(afterId)}` : ''}`;
      const page = await safeJson(query);
      const pageItems = Array.isArray(page.items) ? page.items : [];
      items.push(...pageItems);
      const last = pageItems[pageItems.length - 1] || {};
      afterId = page.next_after_id || (pageItems.length === limit
        ? last.source_recording_id || last.scan_run_id || last.archive_job_id || last.storage_volume_id
          || last.analysis_run_id || last.event_id || last.review_flag_id || last.audit_event_id || last.artifact_id || last.id || null
        : null);
      if (!pageItems.length || pageItems.length < limit) break;
    } while (afterId);
    return {items};
  };

  const postJson = async (path, payload) => {
    const response = await fetch(path, {method: 'POST', headers: {'Accept': 'application/json', 'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    return body;
  };

  const armThumbnail = (image) => {
    if (image.dataset.thumbArmed === 'true') return;
    image.dataset.thumbArmed = 'true';
    const shell = image.closest('.thumb-shell');
    const settle = (ok) => {
      if (!shell) return;
      shell.classList.toggle('loaded', ok);
      shell.classList.toggle('error', !ok);
      const copy = shell.querySelector('.thumb-loading small');
      if (copy) copy.textContent = ok ? '封面已载入' : '封面暂不可用';
    };
    image.addEventListener('load', () => settle(true), {once: true});
    image.addEventListener('error', () => settle(false), {once: true});
    if (image.complete && image.currentSrc) settle(image.naturalWidth > 0);
  };

  const pumpThumbnails = () => {
    thumbnailQueue = thumbnailQueue.filter((image) => {
      const keep = image.isConnected && image.dataset.thumbSrc && !image.dataset.thumbLoading && !image.dataset.thumbFailed;
      if (!keep) delete image.dataset.thumbQueued;
      return keep;
    });
    thumbnailActive.forEach((_request, image) => {
      if (!image.isConnected) {
        thumbnailActive.delete(image);
        thumbnailInFlight.delete(image);
        delete image.dataset.thumbLoading;
        delete image.dataset.thumbQueued;
      }
    });
    while (thumbnailActive.size < THUMBNAIL_CONCURRENCY && thumbnailInFlight.size < THUMBNAIL_CONCURRENCY && thumbnailQueue.length) {
      const image = thumbnailQueue.shift();
      if (!image?.isConnected || !image.dataset.thumbSrc || image.dataset.thumbLoading || image.dataset.thumbFailed || thumbnailInFlight.has(image)) {
        if (image) delete image.dataset.thumbQueued;
        continue;
      }
      const source = image.dataset.thumbSrc;
      delete image.dataset.thumbQueued;
      image.dataset.thumbLoading = 'true';
      const request = {image, source};
      thumbnailActive.set(image, request);
      thumbnailInFlight.set(image, request);
      const finish = (success) => {
        if (thumbnailInFlight.get(image) !== request) return;
        thumbnailInFlight.delete(image);
        thumbnailActive.delete(image);
        delete image.dataset.thumbLoading;
        delete image.dataset.thumbQueued;
        if (success) image.removeAttribute('data-thumb-src');
        else image.dataset.thumbFailed = 'true';
        pumpThumbnails();
      };
      image.addEventListener('load', () => finish(true), {once: true});
      image.addEventListener('error', () => finish(false), {once: true});
      image.src = source;
    }
  };

  const queueThumbnail = (image) => {
    if (!image.isConnected || !image.dataset.thumbSrc || image.dataset.thumbQueued || image.dataset.thumbLoading || image.dataset.thumbFailed || thumbnailInFlight.has(image)) return;
    image.dataset.thumbQueued = 'true';
    thumbnailQueue.push(image);
    pumpThumbnails();
  };

  const revealThumbnail = (image) => queueThumbnail(image);

  const observeThumbnails = (root) => {
    if (!root) return;
    root.querySelectorAll('img.thumb-media').forEach(armThumbnail);
    thumbnailDomObserver?.disconnect();
    if ('MutationObserver' in window) {
      thumbnailDomObserver = new MutationObserver(() => pumpThumbnails());
      thumbnailDomObserver.observe(root, {childList: true, subtree: true});
    }
    const pending = [...root.querySelectorAll('img[data-thumb-src]:not([data-thumb-failed])')]
      .sort((a, b) => Number(b.dataset.thumbPriority === 'high') - Number(a.dataset.thumbPriority === 'high'));
    if (!pending.length) return;
    if (!('IntersectionObserver' in window)) {
      pending.forEach(queueThumbnail);
      return;
    }
    thumbnailObserver?.disconnect();
    thumbnailObserver = new IntersectionObserver((entries, observer) => {
      entries.filter((entry) => entry.isIntersecting).forEach((entry) => {
        revealThumbnail(entry.target);
        observer.unobserve(entry.target);
      });
    }, {rootMargin: '700px 0px'});
    pending.forEach((image) => thumbnailObserver.observe(image));
  };

  const humanBytes = (value) => {
    if (!Number.isFinite(value) || value <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = value, index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
  };

  const setMetric = (sectionSelector, label, value) => {
    document.querySelectorAll(`${sectionSelector} .metric`).forEach((metric) => {
      const key = metric.querySelector('.k');
      const target = metric.querySelector('.v');
      if (key && target && key.textContent.trim() === label) target.textContent = value;
    });
  };

  const runEvents = (runId) => state.events
    .filter((event) => event.analysis_run_id === runId)
    .sort((a, b) => (a.start_offset_ms || 0) - (b.start_offset_ms || 0));

  const formatOffset = (milliseconds) => {
    const total = Math.max(0, Math.floor((milliseconds || 0) / 1000));
    const h = String(Math.floor(total / 3600)).padStart(2, '0');
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
    const s = String(total % 60).padStart(2, '0');
    return `${h}:${m}:${s}`;
  };

  const formatShortDay = (key) => {
    const date = new Date(`${key}T00:00:00Z`);
    return Number.isNaN(date.getTime()) ? key : new Intl.DateTimeFormat('zh-CN', {month:'numeric', day:'numeric', timeZone:'UTC'}).format(date);
  };

  const showRuntimeAnalysis = (runId, entry) => {
    const run = state.runs.find((item) => item.analysis_run_id === runId);
    if (!run) return;
    const events = runEvents(runId);
    activeRuntimeRun = run;
    analysisEntry = entry || document.activeElement;
    document.getElementById('adTitle').textContent = `分析 · ${run.asset_id}`;
    document.getElementById('adRunMeta').textContent = `asset ${run.asset_id} · run ${run.analysis_run_id}`;
    document.getElementById('adScene').textContent = `${run.analysis_profile_id} @ r${run.analysis_profile_revision}`;
    const eventRoot = document.getElementById('adEvents');
    eventRoot.innerHTML = '';
    events.forEach((event) => {
      const row = document.createElement('div');
      row.className = 'event';
      const text = document.createElement('span');
      const end = event.end_offset_ms == null ? '' : `–${formatOffset(event.end_offset_ms)}`;
      text.textContent = `${formatOffset(event.start_offset_ms)}${end} · ${event.summary}`;
      const button = document.createElement('button');
      button.className = 'btn';
      button.textContent = '定位播放';
      button.addEventListener('click', () => {
        document.getElementById('analysisDetail').classList.remove('show');
        openPlayer(`${run.asset_id} · ${formatOffset(event.start_offset_ms)}`, true);
      });
      row.append(text, button);
      eventRoot.append(row);
    });
    document.getElementById('adProv').innerHTML = [
      `asset_id: ${run.asset_id}`,
      `analysis_run_id: ${run.analysis_run_id}`,
      `analysis_profile: ${run.analysis_profile_id}@r${run.analysis_profile_revision}`,
      `profile_snapshot_hash: ${run.analysis_profile_snapshot_hash}`,
      `pipeline: ${run.pipeline_version}`,
      `model: ${run.model_id || '未选择/未记录'}`,
      `model_version: ${run.model_version || '—'}`,
      `runtime_backend: ${run.runtime_backend || '—'}`,
      `state: ${run.state}`,
      `completed_at: ${run.completed_at || '—'}`,
    ].join('<br>');
    document.getElementById('analysisDetail').classList.add('show');
    setTimeout(() => document.getElementById('adClose').focus(), 0);
  };

  const hydrateAnalysis = () => {
    const completed = state.runs.filter((run) => run.state === 'completed').length;
    const pending = state.runs.filter((run) => run.state === 'pending').length;
    const running = state.runs.filter((run) => ['running', 'processing'].includes(run.state)).length;
    const failed = state.runs.filter((run) => run.state === 'failed').length;
    setMetric('#analysis', '今天已分析', String(completed));
    setMetric('#analysis', '待分析', String(pending));
    setMetric('#analysis', '分析中', String(running));
    setMetric('#analysis', '失败', String(failed));
    setMetric('#analysis', '发现事件', String(state.events.length));

    const analysisTools = document.querySelector('#analysis .analysis-tools');
    if (analysisTools) {
      const unavailable = !state.runs.length;
      analysisTools.querySelectorAll('select').forEach((select) => {
        select.disabled = unavailable;
        select.title = unavailable ? '当前没有真实分析运行记录，筛选暂不可用' : '';
      });
      let note = analysisTools.parentElement?.querySelector('.analysis-availability');
      if (!note) {
        analysisTools.parentElement?.insertAdjacentHTML('beforeend', '<div class="analysis-availability"></div>');
        note = analysisTools.parentElement?.querySelector('.analysis-availability');
      }
      if (note) note.textContent = unavailable
        ? `当前没有真实分析运行记录；未接入分析 worker，${state.profiles.length ? `已登记 ${state.profiles.length} 个分析策略但没有任务在执行` : '也没有已登记的分析策略'}。`
        : `筛选基于 ${state.runs.length} 条真实分析运行记录。`;
    }

    const item = document.querySelector('#analysis .analysisitem');
    const run = state.runs.find((candidate) => candidate.state === 'completed') || state.runs[0];
    if (!item) return;
    if (!run) {
      item.innerHTML = '<div class="runtime-empty"><strong>暂无分析结果</strong><p class="muted">当前 API 没有已回流的分析运行记录，因此这里不会显示演示事件或虚构指标。</p><p class="muted">先完成归档并接入分析 worker 后，分析策略、事件和定位播放会出现在这里。</p></div>';
      return;
    }
    const events = runEvents(run.analysis_run_id);
    item.innerHTML = '';
    const header = document.createElement('div');
    header.className = 'row';
    const info = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = `资产 ${run.asset_id}`;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = `分析策略：${run.analysis_profile_id} @ r${run.analysis_profile_revision} · ${run.model_id || '模型待 benchmark'} · ${run.pipeline_version}`;
    info.append(title, meta);
    const actions = document.createElement('div');
    actions.className = 'toolbar';
    const pill = document.createElement('span');
    pill.className = run.state === 'completed' ? 'pill ok' : (run.state === 'failed' ? 'pill fail' : 'pill wait');
    pill.textContent = `${run.state} · ${events.length} 事件`;
    const detail = document.createElement('button');
    detail.className = 'btn primary';
    detail.textContent = '完整分析';
    detail.addEventListener('click', () => showRuntimeAnalysis(run.analysis_run_id, detail));
    actions.append(pill, detail);
    header.append(info, actions);
    const eventRoot = document.createElement('div');
    eventRoot.className = 'events';
    events.slice(0, 3).forEach((event) => {
      const row = document.createElement('div');
      row.className = 'event';
      const text = document.createElement('span');
      text.textContent = `${formatOffset(event.start_offset_ms)} ${event.summary}`;
      const button = document.createElement('button');
      button.className = 'btn';
      button.textContent = '定位播放';
      button.addEventListener('click', () => openPlayer(`${run.asset_id} · ${formatOffset(event.start_offset_ms)}`));
      row.append(text, button);
      eventRoot.append(row);
    });
    item.append(header, eventRoot);
  };

  const hydrateOverview = () => {
    const verified = state.assets.filter((asset) => asset.verification_state === 'verified' && asset.lifecycle_state === 'present');
    const verifiedBytes = verified.reduce((sum, asset) => sum + (asset.size_bytes || 0), 0);
    setMetric('#overview', '已验证', `${verified.length} · ${humanBytes(verifiedBytes)}`);
    const recordingDays = new Set(state.recordings.map((recording) => String(recording.source_start || '').slice(0, 10)).filter(Boolean));
    const incompleteDays = [...recordingDays].filter((day) => state.recordings.some((recording) => String(recording.source_start || '').startsWith(day) && !state.assets.some((asset) => asset.source_recording_id === recording.source_recording_id && asset.verification_state === 'verified' && asset.lifecycle_state === 'present')));
    setMetric('#overview', '未完成日期', `${incompleteDays.length} 天`);
    setMetric('#overview', '最旧未完成', incompleteDays.sort()[0] ? formatShortDay(incompleteDays.sort()[0]) : '无');
    const meta = document.querySelector('.top .meta');
    const completedRuns = state.runs.filter((run) => run.state === 'completed').length;
    if (meta) meta.textContent = `● Home Vision API 就绪 · ${verified.length} 个已验证资产 · ${completedRuns} 次分析完成`;
  };

  const renderTruthfulSurfaces = () => {
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
    const empty = (message) => `<div class="pad runtime-empty"><p class="muted">${esc(message)}</p></div>`;
    const cameraNames = new Map(state.cameras.map((camera) => [camera.track_id, camera.name && !/^Track\s/i.test(camera.name) ? camera.name : `摄像头 ${camera.track_id}`]));
    const cameraLabel = (trackId) => cameraNames.get(trackId) || `摄像头 ${trackId || '未知'}`;
    const assetsBySource = new Map(state.assets.map((asset) => [asset.source_recording_id, asset]));
    const formatDate = (value, options = {}) => {
      if (!value) return '时间未知';
      const date = new Date(value.endsWith('Z') ? value : `${value}Z`);
      return Number.isNaN(date.getTime()) ? '时间未知' : new Intl.DateTimeFormat('zh-CN', {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', timeZone:'UTC', ...options}).format(date);
    };
    const parseUtcDate = (value) => new Date(String(value || '').endsWith('Z') ? value : `${value}Z`);
    const formatDay = (key) => {
      const date = new Date(`${key}T00:00:00Z`);
      return Number.isNaN(date.getTime()) ? key : new Intl.DateTimeFormat('zh-CN', {month:'long', day:'numeric', weekday:'short', timeZone:'UTC'}).format(date);
    };
    const formatShortDay = (key) => {
      const date = new Date(`${key}T00:00:00Z`);
      return Number.isNaN(date.getTime()) ? key : new Intl.DateTimeFormat('zh-CN', {month:'numeric', day:'numeric', timeZone:'UTC'}).format(date);
    };
    const formatDuration = (milliseconds) => {
      if (!Number.isFinite(milliseconds)) return '时长未知';
      const seconds = Math.max(0, Math.round(milliseconds / 1000));
      if (seconds < 60) return `${seconds} 秒`;
      return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    };
    const stateLabel = (value) => ({available:'可用', expired:'源已失效', unknown:'未确认', completed:'已完成', pending:'等待中', running:'进行中', processing:'处理中', failed:'失败', verified:'已验证', present:'在线'}[value] || '状态未知');
    const recordings = [...state.recordings].sort((a, b) => String(b.source_start || '').localeCompare(String(a.source_start || '')));
    const recordingsById = new Map(recordings.map((recording) => [recording.source_recording_id, recording]));
    const grouped = new Map();
    recordings.forEach((recording) => {
      const day = String(recording.source_start || '').slice(0, 10) || 'unknown';
      if (!grouped.has(day)) grouped.set(day, []);
      grouped.get(day).push(recording);
    });

    const jobsBySource = new Map();
    state.jobs.forEach((job) => {
      const existing = jobsBySource.get(job.source_recording_id);
      if (!existing || String(job.requested_at || '').localeCompare(String(existing.requested_at || '')) > 0) jobsBySource.set(job.source_recording_id, job);
    });
    const archiveTools = document.querySelector('#archive .archive-tools');
    const statusSelect = archiveTools?.querySelector('select');
    const cameraSelect = archiveTools?.querySelectorAll('select')?.[1];
    if (statusSelect) {
      statusSelect.innerHTML = '<option value="all">全部状态</option><option value="unarchived">未归档</option><option value="archived">已归档</option><option value="failed">失败</option>';
      statusSelect.value = state.archiveStatusFilter;
      statusSelect.onchange = () => { state.archiveStatusFilter = statusSelect.value; renderTruthfulSurfaces(); };
    }
    if (cameraSelect) {
      const cameraOptions = [...state.cameras].sort((a, b) => String(a.track_id).localeCompare(String(b.track_id), undefined, {numeric: true})).map((camera) => `<option value="${esc(camera.track_id)}">${esc(cameraLabel(camera.track_id))}</option>`).join('');
      cameraSelect.innerHTML = `<option value="all">全部摄像头</option>${cameraOptions}`;
      cameraSelect.value = state.archiveCameraFilter;
      cameraSelect.onchange = () => { state.archiveCameraFilter = cameraSelect.value; renderTruthfulSurfaces(); };
    }
    const archiveRecordings = recordings.filter((recording) => {
      const cameraMatches = state.archiveCameraFilter === 'all' || String(recording.track_id) === String(state.archiveCameraFilter);
      const asset = assetsBySource.get(recording.source_recording_id);
      const job = jobsBySource.get(recording.source_recording_id);
      const statusMatches = state.archiveStatusFilter === 'all'
        || (state.archiveStatusFilter === 'unarchived' && ['available', 'expired'].includes(recording.source_state) && recording.archive_status !== 'archived_verified')
        || (state.archiveStatusFilter === 'archived' && recording.archive_status === 'archived_verified')
        || (state.archiveStatusFilter === 'failed' && job?.state === 'failed');
      return cameraMatches && statusMatches;
    });
    const archiveGrouped = new Map();
    archiveRecordings.forEach((recording) => {
      const day = String(recording.source_start || '').slice(0, 10) || 'unknown';
      if (!archiveGrouped.has(day)) archiveGrouped.set(day, []);
      archiveGrouped.get(day).push(recording);
    });
    const activeJobStates = new Set(['queued', 'paused', 'resolving_source', 'downloading', 'verifying']);
    const archiveDisposition = (recording) => {
      const asset = assetsBySource.get(recording.source_recording_id);
      const job = jobsBySource.get(recording.source_recording_id);
      if (activeJobStates.has(job?.state)) return 'active';
      if (asset?.asset_role === 'source_archive' && asset?.verification_state === 'verified' && asset?.lifecycle_state === 'present') return 'archived';
      if (job?.state === 'failed' && recording.source_state === 'available') return 'retryable';
      if (recording.archive_status === 'archived_verified') return 'archived';
      if (recording.source_state === 'expired') return 'revalidate';
      if (recording.source_state === 'available') return 'eligible';
      return 'unavailable';
    };
    const archiveCandidates = archiveRecordings.filter((recording) => ['eligible', 'retryable', 'revalidate'].includes(archiveDisposition(recording)));
    const selectableArchiveIds = new Set(archiveCandidates.map((recording) => recording.source_recording_id));
    [...selectedArchiveIds].forEach((id) => { if (!selectableArchiveIds.has(id)) selectedArchiveIds.delete(id); });
    let pendingArchiveIds = [];
    const archiveConfigured = Boolean(state.archiveStatus?.configured ?? state.archiveStatus?.enqueue_enabled);
    const archiveReady = Boolean(state.archiveStatus?.enqueue_ready);
    const archiveControlActions = new Set(state.archiveStatus?.control_actions || []);
    const jobStateLabel = (value) => ({queued:'等待中', resolving_source:'定位源录像', downloading:'拉取中', verifying:'校验中', paused:'已暂停', completed:'已完成', failed:'失败', cancelled:'已取消'}[value] || '状态未知');
    const normalizationLabel = (value, errorCode) => {
      const errors = {WORKER_TIMEOUT:'worker 超时', NETWORK_ERROR:'网络连接失败', SOURCE_FILE_MISSING:'找不到原片', NORMALIZED_DURATION_MISMATCH:'转码后时长不符', NORMALIZED_VIDEO_PROFILE_INVALID:'视频格式不符'};
      if (value === 'failed' && errors[errorCode]) return `转码失败 · ${errors[errorCode]}`;
      return ({pending:'转码排队中', initializing:'初始化中', transcoding:'转码中', completed:'转码完成', failed:'转码失败'}[value] || '未归档');
    };
    const normalizationState = (asset, job) => {
      if (asset?.normalization_state) return asset.normalization_state;
      if (job && activeJobStates.has(job.state)) return 'initializing';
      return null;
    };
    const jobSpeed = (job) => {
      if (job.state !== 'downloading') return 0;
      const now = performance.now();
      const previous = jobSamples.get(job.archive_job_id);
      jobSamples.set(job.archive_job_id, {bytes: job.bytes_received || 0, at: now});
      if (!previous || now <= previous.at || (job.bytes_received || 0) < previous.bytes) return job.throughput_bytes_per_second || 0;
      const deltaSeconds = (now - previous.at) / 1000;
      const deltaBytes = (job.bytes_received || 0) - previous.bytes;
      return deltaBytes > 0 ? deltaBytes / deltaSeconds : (job.throughput_bytes_per_second || 0);
    };
    const humanRate = (value) => value > 0 ? `${humanBytes(value)}/秒` : '—';
    const openArchiveModal = (ids, trigger, title = '批量拉取？') => {
      pendingArchiveIds = [...new Set(ids)];
      const modal = document.getElementById('pullModal');
      if (!modal) return;
      const selected = pendingArchiveIds.map((id) => recordingsById.get(id)).filter(Boolean);
      const counts = selected.reduce((result, recording) => { const key = archiveDisposition(recording); result[key] = (result[key] || 0) + 1; return result; }, {});
      modal.innerHTML = `<div class="modal" role="dialog" aria-modal="true" tabindex="-1"><h2>${title}</h2><div class="impact"><span>可加入</span><b>${counts.eligible || 0} 段</b><span>失败可重试</span><b>${counts.retryable || 0} 段</b><span>需重新查询 NVR</span><b>${counts.revalidate || 0} 段</b><span>已排队/进行中</span><b>${counts.active || 0} 段</b><span>已归档</span><b>${counts.archived || 0} 段</b></div><p class="muted">“需重新查询 NVR”的录像会再次验证源是否仍可取；如果 NVR 仍找不到，任务会保留失败原因，不会伪造归档成功。下载前由 worker 按实际可写空间选择归档卷。</p><div class="actions"><button class="btn" data-archive-cancel>取消</button><button class="btn primary" data-archive-confirm ${(counts.eligible || counts.retryable || counts.revalidate) && archiveReady ? '' : 'disabled'}>确认加入任务</button></div></div>`;
      modal.classList.add('show');
      modal.querySelector('.modal')?.focus();
      window.homeVisionModalTrigger = trigger || null;
      modal.querySelector('[data-archive-cancel]')?.addEventListener('click', () => { modal.classList.remove('show'); trigger?.focus?.({preventScroll: true}); });
      modal.querySelector('[data-archive-confirm]')?.addEventListener('click', async () => {
        const confirm = modal.querySelector('[data-archive-confirm]');
        if (!confirm || confirm.disabled) return;
        confirm.disabled = true;
        try {
          const queueIds = selected.filter((recording) => ['eligible', 'retryable', 'revalidate'].includes(archiveDisposition(recording))).map((recording) => recording.source_recording_id);
          const result = await postJson('/internal/archive/batch', {source_recording_ids: queueIds});
          modal.classList.remove('show');
          toast(`${result.queued_count || 0} 段已加入归档任务${result.already_archived_source_ids?.length ? `，${result.already_archived_source_ids.length} 段已归档` : ''}`);
          selectedArchiveIds.clear(); archiveSelectionMode = false;
          window.homeVisionArchiveSelection?.sync?.();
          await hydrate({refreshJobs: true, deferCatalogRefresh: true});
          trigger?.focus?.({preventScroll: true});
        } catch (error) {
          confirm.disabled = false;
          toast(`加入任务失败：${error.message}`);
        }
      });
    };

    const syncArchiveSelectionUi = () => {
      const archive = document.getElementById('archive');
      const entry = document.getElementById('selectEntry');
      const bar = document.getElementById('selectbar');
      const selectedText = document.getElementById('selectedText');
      const selectedSize = document.getElementById('selectedSize');
      archive?.classList.toggle('selection-mode', archiveSelectionMode);
      if (entry) entry.textContent = archiveSelectionMode ? '退出选择' : '选择录像';
      if (selectedText) selectedText.textContent = `已选 ${selectedArchiveIds.size} 段`;
      if (selectedSize) selectedSize.textContent = '确认前不会创建任务';
      bar?.classList.toggle('show', archiveSelectionMode && selectedArchiveIds.size > 0);
    };
    const clearArchiveSelection = () => {
      selectedArchiveIds.clear();
      document.querySelectorAll('#archive .seg.selected').forEach((card) => card.classList.remove('selected'));
      document.querySelectorAll('#archive [data-select-recording]').forEach((button) => { button.textContent = '□'; button.setAttribute('aria-pressed', 'false'); });
      syncArchiveSelectionUi();
    };
    const toggleArchiveSelectionMode = () => {
      archiveSelectionMode = !archiveSelectionMode;
      if (!archiveSelectionMode) clearArchiveSelection();
      else syncArchiveSelectionUi();
    };
    const queueArchiveSelection = () => {
      if (!selectedArchiveIds.size) return;
      openArchiveModal([...selectedArchiveIds], document.getElementById('selectbar'), `把已选 ${selectedArchiveIds.size} 段加入归档任务？`);
    };
    window.homeVisionArchiveSelection = {toggle: toggleArchiveSelectionMode, clear: clearArchiveSelection, queue: queueArchiveSelection, sync: syncArchiveSelectionUi};

    const bulkPull = document.querySelector('#archive .archive-tools .btn.primary');
    if (bulkPull) {
      bulkPull.disabled = !archiveReady || !archiveCandidates.length;
      bulkPull.removeAttribute('onclick');
      bulkPull.textContent = archiveReady ? '拉取全部当前可拉取' : archiveConfigured ? '归档 worker 暂不可用' : '归档 worker 未配置';
      bulkPull.title = archiveReady ? `先预览当前 ${archiveCandidates.length} 段可加入、失败重试或重新查询 NVR 的影响，再确认创建任务` : archiveConfigured ? 'worker 已配置但没有 fresh 心跳；创建、继续和重试已停用' : '当前运行实例没有配置归档 worker';
      bulkPull.onclick = () => openArchiveModal(archiveRecordings.map((recording) => recording.source_recording_id), bulkPull, '把当前可归档录像加入任务？');
    }
    const selectEntry = document.getElementById('selectEntry');
    if (selectEntry) {
      selectEntry.style.display = '';
      selectEntry.disabled = !archiveRecordings.length;
      selectEntry.removeAttribute('onclick');
      selectEntry.onclick = toggleArchiveSelectionMode;
    }
    syncArchiveSelectionUi();
    const heartbeat = state.archiveStatus?.heartbeat || {};
    const workerLabel = !archiveConfigured ? '归档 worker 未配置' : archiveReady ? '归档 worker 在线' : heartbeat.status === 'stale' ? '归档 worker 心跳过期' : '归档 worker 尚未报告心跳';
    const workerNote = !archiveConfigured
      ? '当前只能查看 NVR 清单和已验证副本，不能创建或控制归档任务。'
      : archiveReady
        ? `创建、继续和重试可用；最近心跳 ${heartbeat.age_seconds} 秒前。批量操作先 Preview 再 Confirm。`
        : `worker 已配置但当前没有 fresh 心跳；创建、继续和重试已停用。${archiveControlActions.has('pause_queued') || archiveControlActions.has('cancel_waiting') ? '等待任务仍可按服务端能力安全暂停/取消。' : ''}`;
    let availability = archiveTools?.nextElementSibling?.classList.contains('archive-availability') ? archiveTools.nextElementSibling : null;
    if (archiveTools && !availability) {
      archiveTools.insertAdjacentHTML('afterend', '<div class="archive-availability"></div>');
      availability = archiveTools.nextElementSibling;
    }
    if (availability) availability.innerHTML = `<b>${workerLabel}</b>：${workerNote}`;
    // A date is only "no recording" after a successful/partial NVR scan covers it.
    const scanCoverage = new Map();
    const markScanDay = (key, trackId, scanState) => {
      if (!scanCoverage.has(key)) scanCoverage.set(key, new Map());
      const dayCoverage = scanCoverage.get(key);
      const current = dayCoverage.get(trackId);
      const rank = {failed: 0, partial: 1, success: 2};
      if (!current || (rank[scanState] ?? -1) > (rank[current] ?? -1)) dayCoverage.set(trackId, scanState);
    };
    state.scans.filter((scan) => ['success', 'partial'].includes(scan.state)).forEach((scan) => {
      const start = parseUtcDate(scan.query_start);
      const end = parseUtcDate(scan.query_end);
      if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return;
      for (let cursor = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate())); cursor < end; cursor.setUTCDate(cursor.getUTCDate() + 1)) markScanDay(cursor.toISOString().slice(0, 10), String(scan.track_id), scan.state);
    });
    state.scans.filter((scan) => scan.state === 'failed').forEach((scan) => {
      const start = parseUtcDate(scan.query_start);
      const end = parseUtcDate(scan.query_end);
      if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return;
      for (let cursor = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate())); cursor < end; cursor.setUTCDate(cursor.getUTCDate() + 1)) markScanDay(cursor.toISOString().slice(0, 10), String(scan.track_id), scan.state);
    });
    const requiredTrackIds = new Set(state.cameras.filter((camera) => camera.enabled !== false).map((camera) => String(camera.track_id)));
    const scanStateForDay = (key) => {
      const coverage = scanCoverage.get(key);
      if (!coverage?.size) return 'unknown';
      const required = requiredTrackIds.size ? [...requiredTrackIds] : [...coverage.keys()];
      const states = required.map((trackId) => coverage.get(trackId));
      if (states.every((scanState) => scanState === 'success')) return 'scanned';
      if (states.some((scanState) => ['success', 'partial'].includes(scanState))) return 'partial';
      return 'failed';
    };

    // Keep the approved Calendar-first Overview skeleton, but never call an unscanned day empty.
    const calendar = document.querySelector('#overview .calendar');
    if (calendar) {
      const recordingDates = [...grouped.keys()].filter((key) => key !== 'unknown').sort();
      const knownScanDays = [...scanCoverage.keys()].filter((key) => ['scanned', 'partial'].includes(scanStateForDay(key))).sort();
      const knownDates = [...new Set([...recordingDates, ...knownScanDays])].sort();
      const earliestKnownDate = knownDates[0] || null;
      const latestKnownDate = knownDates[knownDates.length - 1] || null;
      const latestMonth = latestKnownDate ? latestKnownDate.slice(0, 7) : new Date().toISOString().slice(0, 7);
      if (!state.calendarMonth || (earliestKnownDate && state.calendarMonth < earliestKnownDate.slice(0, 7)) || state.calendarMonth > latestMonth) state.calendarMonth = latestMonth;
      const anchor = new Date(`${state.calendarMonth}-01T00:00:00Z`);
      const year = anchor.getUTCFullYear();
      const month = anchor.getUTCMonth();
      const monthLabel = new Intl.DateTimeFormat('zh-CN', {year:'numeric', month:'long', timeZone:'UTC'}).format(anchor);
      const firstDay = new Date(Date.UTC(year, month, 1));
      const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
      const cells = Array.from({length: firstDay.getUTCDay() === 0 ? 6 : firstDay.getUTCDay() - 1}, () => '<span></span>');
      for (let day = 1; day <= daysInMonth; day += 1) {
        const key = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const count = grouped.get(key)?.length || 0;
        const scanState = scanStateForDay(key);
        const beforeBoundary = Boolean(earliestKnownDate && key < earliestKnownDate);
        const status = beforeBoundary ? 'outside' : (count ? 'recorded' : scanState);
        const archived = (grouped.get(key) || []).filter((recording) => {
          const asset = assetsBySource.get(recording.source_recording_id);
          return asset?.verification_state === 'verified' && asset?.lifecycle_state === 'present';
        }).length;
        const coverageNote = scanState === 'partial' ? ' · 部分摄像头未查询' : scanState === 'failed' ? ' · 查询失败' : '';
        const copy = beforeBoundary ? '早于已知数据边界' : (count ? `${count} 段已发现 · ${archived} 段已归档${coverageNote}` : (scanState === 'scanned' ? '已查询 · 无录像' : scanState === 'partial' ? '部分摄像头已查询' : scanState === 'failed' ? '查询失败 · 尚未确认' : '尚未查询 NVR'));
        cells.push(`<button class="day ${status}" ${count ? `data-date="${key}"` : 'disabled'}><b>${day}</b><small>${copy}</small></button>`);
      }
      const monthKeys = Array.from({length: daysInMonth}, (_, index) => `${year}-${String(month + 1).padStart(2, '0')}-${String(index + 1).padStart(2, '0')}`).filter((key) => !earliestKnownDate || key >= earliestKnownDate);
      const monthUnknown = monthKeys.filter((key) => !grouped.has(key) && scanStateForDay(key) === 'unknown').length;
      const monthPartialDays = monthKeys.filter((key) => scanStateForDay(key) === 'partial').length;
      const monthFailedDays = monthKeys.filter((key) => scanStateForDay(key) === 'failed').length;
      const monthPrefix = `${state.calendarMonth}-`;
      const monthRecordedDays = recordingDates.filter((key) => key.startsWith(monthPrefix)).length;
      const monthScannedDays = monthKeys.filter((key) => scanStateForDay(key) === 'scanned').length;
      const monthDate = new Date(Date.UTC(year, month, 1));
      const previousMonth = new Date(Date.UTC(year, month - 1, 1)).toISOString().slice(0, 7);
      const nextMonth = new Date(Date.UTC(year, month + 1, 1)).toISOString().slice(0, 7);
      const previousDisabled = Boolean(earliestKnownDate && previousMonth < earliestKnownDate.slice(0, 7));
      const nextDisabled = nextMonth > latestMonth;
      const boundaryText = earliestKnownDate ? `已知数据边界：${formatShortDay(earliestKnownDate)} 起；录像日和已查询无录像日都参与边界` : '尚未确定已知数据边界';
      calendar.querySelector('.calhead').innerHTML = `<div><b>${monthLabel}</b><div class="muted">${monthRecordedDays} 天有真实录像 · ${monthScannedDays} 天全摄像头已查询 · ${monthPartialDays} 天部分查询 · ${monthFailedDays} 天查询失败 · ${monthUnknown} 天尚未查询 NVR</div><div class="meta">${boundaryText}</div></div><div class="toolbar"><button class="btn" data-calendar-prev ${previousDisabled ? 'disabled' : ''}>上月</button><button class="btn" data-calendar-next ${nextDisabled ? 'disabled' : ''}>下月</button></div>`;
      calendar.querySelector('.calgrid').innerHTML = `<div class="dow">一</div><div class="dow">二</div><div class="dow">三</div><div class="dow">四</div><div class="dow">五</div><div class="dow">六</div><div class="dow">日</div>${cells.join('')}`;
      calendar.querySelector('[data-calendar-prev]')?.addEventListener('click', () => { state.calendarMonth = previousMonth; renderTruthfulSurfaces(); });
      calendar.querySelector('[data-calendar-next]')?.addEventListener('click', () => { state.calendarMonth = nextMonth; renderTruthfulSurfaces(); });
      calendar.querySelectorAll('[data-date]').forEach((button) => button.addEventListener('click', () => { state.selectedArchiveDate = button.dataset.date; go('archive', document.querySelector('[data-page="archive"]')); renderTruthfulSurfaces(); }));
    }
    setMetric('#overview', '可用空间', state.volumes.length ? humanBytes(state.volumes.reduce((sum, volume) => sum + (volume.free_bytes || 0), 0)) : '未登记');

    // Preserve separate A/B cards and capacity bars. Missing volumes are explicitly unavailable.
    const storage = document.querySelector('#overview .storage');
    if (storage) {
      const volumes = [...state.volumes].sort((a, b) => String(a.storage_volume_id).localeCompare(String(b.storage_volume_id)));
      storage.querySelectorAll('.volume').forEach((card, index) => {
        const volume = volumes[index];
        const label = `归档盘 ${index === 0 ? 'A' : 'B'}`;
        if (!volume) { card.innerHTML = `<div class="row"><b>${label}</b><span class="pill gray">暂无真实数据</span></div><p class="muted">尚未发现对应存储卷。</p>`; return; }
        const total = Number(volume.total_bytes) || 0;
        const applicationUsed = Number(volume.application_used_bytes) || 0;
        const percent = total ? Math.min(100, Math.round((applicationUsed / total) * 100)) : 0;
        const volumeState = volume.online ? (volume.writable ? '在线 · 可写' : '在线 · 不可写') : '离线';
        const identity = volume.storage_identity || `OMV 归档卷 ${volume.storage_volume_id}`;
        const physical = volume.physical_device_id || '未登记';
        card.innerHTML = `<div class="row"><b>${label}</b><span class="pill ${volume.online && volume.writable ? 'ok' : 'fail'}">${volumeState}</span></div><div class="runtime-identity"><b>存储卷 ${esc(volume.storage_volume_id)}</b><span>存储身份：${esc(identity)} · 节点：${esc(volume.node_id)}</span><span>物理设备：${esc(physical)}</span><span>目录：recordings/YYYY/MM/DD/camera-{track_id}/ · 文件：HHMMSS-{source_id}.mp4（下载中为 .part）</span></div><div class="bar"><span style="width:${percent}%"></span></div><div class="row"><span>Home Vision 已占用 ${humanBytes(applicationUsed)} / ${humanBytes(total)}</span><b>${humanBytes(volume.free_bytes)} 可供归档</b></div>`;
      });
    }
    const telemetry = document.querySelector('#overview .telemetry');
    if (telemetry) {
      const apiStatus = state.health?.status === 'ok' ? '在线' : '不可用';
      const dbStatus = state.health?.database === 'ok' ? '正常' : '不可用';
      const runtime = state.runtime || {};
      const cpu = runtime.cpu || {};
      const memory = runtime.memory || {};
      const network = runtime.network || {};
      const archiveWriteBytes = state.assets.filter((asset) => asset.verification_state === 'verified' && asset.lifecycle_state === 'present').reduce((sum, asset) => sum + (asset.size_bytes || 0), 0) + state.jobs.filter((job) => ['downloading', 'verifying'].includes(job.state)).reduce((sum, job) => sum + (job.bytes_received || 0), 0);
      const percent = memory.limit_bytes ? `${Math.round((memory.used_bytes || 0) / memory.limit_bytes * 100)}%` : '未知';
      const host = runtime.host || {};
      const hostText = host.status === 'available' ? '已接入' : '未接入';
      telemetry.innerHTML = `<div class="surface telecard"><b>Home Vision 容器</b><div class="runtime-state"><div>API<b>${apiStatus}</b></div><div>数据库<b>${dbStatus}</b></div><div>运行环境<b>${runtime.runtime === 'container' ? 'OMV 容器' : '未知'}</b></div><div>CPU 负载<b>${cpu.load_1m == null ? '未知' : `${Number(cpu.load_1m).toFixed(2)} / ${cpu.logical_cores || '?'} 核`}</b></div><div>内存<b>${memory.used_bytes == null ? '未知' : `${humanBytes(memory.used_bytes)} · ${percent}`}</b></div><div>归档写入<b>${humanBytes(archiveWriteBytes)}</b></div><div>网络接收<b>${humanBytes(network.received_bytes || 0)}</b></div><div>网络发送<b>${humanBytes(network.transmitted_bytes || 0)}</b></div></div><div class="muted" style="margin-top:10px">每 2 秒刷新；归档写入是本程序已验证并正在写入的应用字节数，网络接收/发送是容器启动以来累计值。${runtime.collected_at ? ` 最近采样：${esc(formatDate(runtime.collected_at, {second:'2-digit'}))}` : ''}</div></div><div class="surface telecard"><b>OMV Edge 主机</b><div class="runtime-state"><div>主机级遥测<b>${hostText}</b></div><div>CPU / 内存<b>${host.status === 'available' ? '由主机代理提供' : '未采集'}</b></div><div>网络 / Load<b>${host.status === 'available' ? '由主机代理提供' : '未采集'}</b></div></div><div class="muted" style="margin-top:10px">${esc(host.reason || '主机级遥测未接入；这里不会把容器指标冒充主机指标。')}</div></div>`;
    }
    const activity = document.querySelector('#overview .activity');
    if (activity) activity.innerHTML = recordings.length ? `<div class="runtime-section-head"><b>最近真实录像</b><small>来源状态 · 本地副本</small></div>${recordings.slice(0, 5).map((recording) => { const asset = assetsBySource.get(recording.source_recording_id); const hasAsset = asset?.verification_state === 'verified' && asset?.lifecycle_state === 'present'; const localState = hasAsset ? '已归档' : '未归档'; return `<div class="activityrow"><span>${esc(formatDate(recording.source_start))}</span><span>${esc(cameraLabel(recording.track_id))} · NVR 源${esc(stateLabel(recording.source_state))} · ${localState}</span><span class="pill ${hasAsset ? 'ok' : 'gray'}">${esc(formatDuration(recording.duration_ms))}</span></div>`; }).join('')}` : empty('当前没有真实录像记录。');

    // Archive remains date-grouped → camera lanes → media cards, with real actions only.
    const archive = document.querySelector('#archive');
    if (archive) {
      const nextArchiveRenderKey = JSON.stringify({
        date: state.selectedArchiveDate,
        status: state.archiveStatusFilter,
        camera: state.archiveCameraFilter,
        recordings: archiveRecordings.map((recording) => [recording.source_recording_id, recording.track_id, recording.source_start,
          recording.source_end, recording.duration_ms, recording.source_state, recording.archive_status]),
        assets: archiveRecordings.map((recording) => {
          const asset = assetsBySource.get(recording.source_recording_id);
          return [recording.source_recording_id, asset?.asset_id, asset?.verification_state, asset?.lifecycle_state, asset?.size_bytes, asset?.normalization_state];
        }),
        thumbnails: archiveRecordings.map((recording) => [recording.source_recording_id, recording.thumbnail_available]),
        jobs: archiveRecordings.map((recording) => {
          const job = jobsBySource.get(recording.source_recording_id);
          return [recording.source_recording_id, job?.archive_job_id, job?.state, job?.error_code];
        }),
      });
      if (nextArchiveRenderKey === archiveRenderKey) {
        // Keep the existing DOM so lazy thumbnails and the user's scroll position survive polling.
      } else {
        archiveRenderKey = nextArchiveRenderKey;
        const preserveArchiveScroll = document.getElementById('archive')?.classList.contains('active') && !state.selectedArchiveDate;
        if (preserveArchiveScroll) archiveScrollTop = window.scrollY;
        const scrollAnchor = preserveArchiveScroll
          ? [...archive.querySelectorAll('.seg[data-source-recording-id]')].find((card) => {
              const rect = card.getBoundingClientRect();
              return rect.bottom > 0 && rect.top < window.innerHeight;
            })
          : null;
        const scrollAnchorId = scrollAnchor?.dataset.sourceRecordingId;
        const scrollAnchorDate = scrollAnchor?.closest('.date');
        const scrollAnchorDay = scrollAnchorDate?.dataset.archiveDay;
        const scrollAnchorTop = scrollAnchor?.getBoundingClientRect().top;
        const scrollAnchorDayTop = scrollAnchorDate?.getBoundingClientRect().top;
        const existingDates = new Map([...archive.querySelectorAll('.date[data-archive-day]')].map((date) => [date.dataset.archiveDay, date]));
      const dateFilter = archive.querySelector('.archive-tools > .btn');
      if (dateFilter) {
        dateFilter.textContent = state.selectedArchiveDate ? `${formatShortDay(state.selectedArchiveDate)} ×` : '日期筛选';
        dateFilter.title = state.selectedArchiveDate ? '清除日期筛选' : '前往总览日历选择日期';
        dateFilter.onclick = () => {
          if (state.selectedArchiveDate) { state.selectedArchiveDate = null; renderTruthfulSurfaces(); return; }
          go('overview', document.querySelector('[data-page="overview"]'));
          renderTruthfulSurfaces();
        };
      }
      archive.querySelectorAll('.archive-load-more').forEach((node) => node.remove());
      archive.querySelectorAll('.runtime-empty').forEach((node) => node.remove());
      const visibleGrouped = state.selectedArchiveDate ? new Map([[state.selectedArchiveDate, archiveGrouped.get(state.selectedArchiveDate) || []]]) : archiveGrouped;
      if (!visibleGrouped.size || (state.selectedArchiveDate && !visibleGrouped.get(state.selectedArchiveDate)?.length)) archive.insertAdjacentHTML('beforeend', empty(state.selectedArchiveDate ? `${formatShortDay(state.selectedArchiveDate)} 没有已发现的录像。` : '当前没有真实归档记录。'));
      const groupedEntries = [...visibleGrouped.entries()];
      const renderedDates = groupedEntries.map(([day, dayRecordings]) => {
        const dayRenderKey = JSON.stringify({
          ready: archiveReady,
          configured: archiveConfigured,
          cameras: [...new Set(dayRecordings.map((recording) => recording.track_id))].map((trackId) => [trackId, cameraLabel(trackId)]),
          recordings: dayRecordings.map((recording) => {
            const asset = assetsBySource.get(recording.source_recording_id);
            const job = jobsBySource.get(recording.source_recording_id);
            return [recording.source_recording_id, recording.track_id, recording.source_start, recording.source_end,
              recording.duration_ms, recording.source_state, recording.archive_status, recording.thumbnail_available,
              asset?.asset_id, asset?.verification_state, asset?.lifecycle_state, asset?.size_bytes, asset?.normalization_state,
              job?.archive_job_id, job?.state, job?.error_code];
          }),
        });
        const existingDate = existingDates.get(day);
        if (existingDate?.dataset.renderKey === dayRenderKey) return existingDate;
        const date = document.createElement('div'); date.className = 'surface date';
        date.dataset.archiveDay = day;
        date.dataset.renderKey = dayRenderKey;
        const lanes = new Map(); dayRecordings.forEach((recording) => { if (!lanes.has(recording.track_id)) lanes.set(recording.track_id, []); lanes.get(recording.track_id).push(recording); });
        const dayCandidates = dayRecordings.filter((recording) => ['eligible', 'retryable', 'revalidate'].includes(archiveDisposition(recording)));
        const dayHasActive = dayRecordings.some((recording) => archiveDisposition(recording) === 'active');
        const dayHasUnavailable = dayRecordings.some((recording) => archiveDisposition(recording) === 'unavailable');
        const dayComplete = dayRecordings.length > 0 && dayRecordings.every((recording) => archiveDisposition(recording) === 'archived');
        const dayButtonLabel = !archiveReady ? (archiveConfigured ? '归档 worker 暂不可用' : '归档 worker 未配置') : dayComplete ? '当天已全部归档' : dayHasActive ? '当天拉取中' : dayCandidates.length ? '批量拉取当天' : dayHasUnavailable ? '源待确认' : '暂无可归档录像';
        date.innerHTML = `<div class="date-title"><div><b>${esc(day === 'unknown' ? '日期未知' : formatDay(day))}</b><div class="muted">${dayRecordings.length} 段真实录像 · ${dayCandidates.length} 段可加入/重试/重新查询</div></div><button class="btn primary" data-archive-day ${!archiveReady || !dayCandidates.length ? 'disabled' : ''}>${dayButtonLabel}</button></div>`;
        date.querySelector('[data-archive-day]')?.addEventListener('click', (event) => openArchiveModal(dayRecordings.map((recording) => recording.source_recording_id), event.currentTarget, `${esc(day === 'unknown' ? '日期未知' : formatDay(day))}加入归档任务？`));
        lanes.forEach((laneRecordings, trackId) => {
          const lane = document.createElement('div'); lane.className = 'lane';
          lane.innerHTML = `<h3>${esc(cameraLabel(trackId))}</h3><div class="segments"></div>`;
          const segments = lane.querySelector('.segments');
          laneRecordings.forEach((recording) => {
            const asset = assetsBySource.get(recording.source_recording_id);
            const job = jobsBySource.get(recording.source_recording_id);
            const card = document.createElement('article'); card.className = 'seg';
            card.dataset.sourceRecordingId = recording.source_recording_id;
            const normalizedState = normalizationState(asset, job);
            const canPlay = normalizedState === 'completed';
            const disposition = archiveDisposition(recording);
            let cardState = stateLabel(recording.source_state);
            if (canPlay) cardState = '✓ 转码完成';
            else if (asset?.verification_state === 'verified' && asset?.lifecycle_state === 'present' && recording.archive_status === 'archived_older_snapshot') cardState = `已归档 · NVR 信息有更新${normalizedState ? ` · ${normalizationLabel(normalizedState, asset?.normalization_error_code)}` : ''}`;
            else if (normalizedState) cardState = normalizationLabel(normalizedState, asset?.normalization_error_code);
            else if (job?.state && activeJobStates.has(job.state)) cardState = jobStateLabel(job.state);
            else if (disposition === 'revalidate') cardState = '源待重新查询';
            else if (disposition === 'unavailable') cardState = '源状态待确认';
            else if (job?.state === 'failed') cardState = `失败 · ${job.error_code === 'NVR_SOURCE_SEGMENT_NOT_FOUND' ? '源已失效' : '可重试'}`;
            const targetDate = state.selectedArchiveDate === day;
            const sourceThumb = `/api/v1/source-recordings/${encodeURIComponent(recording.source_recording_id)}/thumbnail`;
            const thumb = canPlay ? `<div class="thumb-shell"><div class="thumb-loading" aria-live="polite"><span class="thumb-spinner" aria-hidden="true"></span><small>正在载入封面…</small></div><img class="thumb-media" data-thumb-src="/api/v1/media/${encodeURIComponent(asset.asset_id)}/thumbnail" ${targetDate ? 'data-thumb-priority="high"' : ''} alt="${esc(cameraLabel(trackId))} 首帧" loading="lazy" fetchpriority="${targetDate ? 'high' : 'auto'}" decoding="async"></div>` : recording.thumbnail_available ? `<img class="thumb-media" data-thumb-src="${sourceThumb}" ${targetDate ? 'data-thumb-priority="high"' : ''} alt="${esc(cameraLabel(trackId))} 录像封面" loading="lazy" fetchpriority="${targetDate ? 'high' : 'auto'}" decoding="async">` : `<div class="thumb"><b>${esc(cameraLabel(trackId))}</b><small>${job?.state === 'failed' ? '拉取失败 · 暂无首图' : 'NVR 源 · 尚未归档'}</small></div>`;
            const rawLivePercent = job?.progress_percent == null ? (job?.state === 'verifying' ? 100 : 0) : Math.min(100, Math.max(0, job.progress_percent));
            const livePercent = job?.state === 'downloading' ? Math.min(99, rawLivePercent) : rawLivePercent;
            const liveProgress = job && activeJobStates.has(job.state) ? `<div class="card-progress"><div class="progress"><span style="width:${livePercent}%"></span></div><small>${esc(jobStateLabel(job.state))} · ${livePercent}%${job.throughput_bytes_per_second ? ` · ${esc(humanRate(job.throughput_bytes_per_second))}` : ''}</small></div>` : '';
            const canRevalidate = archiveReady && disposition === 'revalidate';
            const canQueue = archiveReady && !normalizedState && ['eligible', 'retryable'].includes(disposition);
            const actionLabel = canPlay ? '定位播放' : normalizedState ? normalizationLabel(normalizedState) : canRevalidate ? '重新查询 NVR' : canQueue ? '加入归档' : disposition === 'unavailable' ? '源待确认' : '暂无本地副本';
            const selectable = ['eligible', 'retryable', 'revalidate'].includes(disposition);
            if (selectable) card.classList.add('selectable');
            const selected = selectedArchiveIds.has(recording.source_recording_id);
            if (selected) card.classList.add('selected');
            const selector = selectable ? `<button class="check" data-select-recording="${esc(recording.source_recording_id)}" aria-label="选择 ${esc(cameraLabel(trackId))} ${esc(formatDate(recording.source_start))}" aria-pressed="${selected ? 'true' : 'false'}">${selected ? '✓' : '□'}</button>` : '';
            card.innerHTML = `${selector}${thumb}<div class="segbody"><div class="time">${esc(formatDate(recording.source_start, {hour:'2-digit', minute:'2-digit'}))}–${esc(recording.source_end ? formatDate(recording.source_end, {hour:'2-digit', minute:'2-digit'}) : '结束时间未知')}</div><div class="meta">${esc(formatDuration(recording.duration_ms))} · ${asset ? esc(humanBytes(asset.size_bytes)) : '尚未归档'}</div>${liveProgress}<span class="pill state ${canPlay ? 'ok' : normalizedState === 'failed' ? 'fail' : normalizedState ? 'wait' : disposition === 'revalidate' ? 'exp' : job?.state === 'failed' ? 'fail' : 'gray'}">${esc(cardState)}</span><div class="actions"><button class="btn" ${canPlay ? `data-asset-id="${esc(asset.asset_id)}"` : (canQueue || canRevalidate ? 'data-archive-one' : 'disabled')}>${actionLabel}</button></div></div>`;
            card.querySelector('[data-select-recording]')?.addEventListener('click', (event) => {
              event.stopPropagation();
              const id = event.currentTarget.dataset.selectRecording;
              if (selectedArchiveIds.has(id)) selectedArchiveIds.delete(id); else selectedArchiveIds.add(id);
              const nowSelected = selectedArchiveIds.has(id);
              card.classList.toggle('selected', nowSelected);
              event.currentTarget.textContent = nowSelected ? '✓' : '□';
              event.currentTarget.setAttribute('aria-pressed', nowSelected ? 'true' : 'false');
              syncArchiveSelectionUi();
            });
            if (canPlay) card.querySelector('[data-asset-id]').addEventListener('click', () => openRuntimeMedia(asset.asset_id, card.querySelector('[data-asset-id]')));
            card.querySelector('[data-archive-one]')?.addEventListener('click', async (event) => {
              const button = event.currentTarget;
              button.disabled = true;
              try {
                const result = await postJson('/internal/archive/batch', {source_recording_ids: [recording.source_recording_id]});
                if (!result.queued_count) {
                  const message = result.already_archived_source_ids?.length ? '这段录像已有本地归档，不会重复加入' : result.already_queued_source_ids?.length ? '这段录像已在归档队列中' : '这段录像当前不能加入归档';
                  toast(message);
                  await hydrate({refreshCatalog: true, refreshJobs: true});
                  return;
                }
                toast(`${result.queued_count || 0} 段已初始化并进入归档队列`);
                await hydrate({refreshJobs: true, deferCatalogRefresh: true});
              } catch (error) {
                button.disabled = false;
                toast(`加入任务失败：${error.message}`);
              }
            });
            segments.append(card);
          });
          date.append(lane);
        });
        return date;
      });
      const retainedDates = new Set(renderedDates);
      existingDates.forEach((date) => { if (!retainedDates.has(date)) date.remove(); });
      renderedDates.forEach((date, index) => {
        const currentDates = [...archive.children].filter((child) => child.classList.contains('date'));
        if (currentDates[index] !== date) archive.insertBefore(date, currentDates[index] || null);
      });
      observeThumbnails(archive);
      if (preserveArchiveScroll) window.requestAnimationFrame(() => {
        const anchor = scrollAnchorId
          ? [...archive.querySelectorAll('.seg[data-source-recording-id]')].find((card) => card.dataset.sourceRecordingId === scrollAnchorId)
          : null;
        const fallback = scrollAnchorDay
          ? [...archive.querySelectorAll('.date[data-archive-day]')].find((date) => date.dataset.archiveDay === scrollAnchorDay)
          : null;
        const target = anchor || fallback;
        const expectedTop = anchor ? scrollAnchorTop : scrollAnchorDayTop;
        if (target && Number.isFinite(expectedTop)) {
          const delta = target.getBoundingClientRect().top - expectedTop;
          if (Math.abs(delta) > 1) window.scrollBy(0, delta);
        } else if (!target) {
          const maxScrollTop = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
          window.scrollTo({top: Math.min(archiveScrollTop, maxScrollTop), behavior: 'auto'});
        }
      });
      }
    }

    const activeJobs = state.jobs.filter((job) => !['completed', 'failed', 'cancelled'].includes(job.state));
    const jobOrder = {resolving_source: 0, downloading: 0, verifying: 0, failed: 1, queued: 2, paused: 2, completed: 3, cancelled: 4};
    const jobs = [...state.jobs].sort((a, b) => {
      const stateDelta = (jobOrder[a.state] ?? 5) - (jobOrder[b.state] ?? 5);
      if (stateDelta) return stateDelta;
      const sourceDelta = String(recordingsById.get(a.source_recording_id)?.source_start || '').localeCompare(String(recordingsById.get(b.source_recording_id)?.source_start || ''));
      return -sourceDelta || -String(a.requested_at || '').localeCompare(String(b.requested_at || ''));
    });
    const failedJobs = state.jobs.filter((job) => job.state === 'failed');
    const retryableJob = (job) => job.state === 'failed' && job.error_code !== 'NVR_SOURCE_SEGMENT_NOT_FOUND';
    const taskCount = document.getElementById('taskCount'); if (taskCount) { taskCount.textContent = String(activeJobs.length); taskCount.title = `${activeJobs.length} 条待处理任务；任务页保留 ${jobs.length} 条完整历史记录`; }
    const taskStats = {'拉取中': state.jobs.filter((job) => ['resolving_source','downloading'].includes(job.state)).length, '等待': state.jobs.filter((job) => ['queued','paused'].includes(job.state)).length, '校验中': state.jobs.filter((job) => job.state === 'verifying').length, '失败': state.jobs.filter((job) => job.state === 'failed').length, '已完成': state.jobs.filter((job) => job.state === 'completed').reduce((sum, job) => sum + (job.bytes_received || 0), 0)};
    Object.entries(taskStats).forEach(([label, value]) => setMetric('#tasks', label, label === '已完成' ? humanBytes(value) : String(value)));
    const tasks = document.querySelector('#tasks');
    const jobErrorLabel = (value) => ({MEDIA_DURATION_MISMATCH:'媒体时长与 NVR 记录不一致',ARCHIVE_WORKER_FAILED:'归档 worker 异常中断',NVR_SOURCE_SEGMENT_NOT_UNIQUE:'NVR 返回的录像段不唯一',NVR_SOURCE_SEGMENT_NOT_FOUND:'NVR 已没有这段源录像，可能已过期'}[value] || '归档失败，需检查');
    if (tasks) {
      const subtitle = tasks.querySelector('.head .muted');
      if (subtitle) subtitle.textContent = `归档任务与校验状态 · 共 ${jobs.length} 条记录，其中 ${activeJobs.length} 条待处理，${failedJobs.length} 条失败`;
      const panel = tasks.querySelector('.surface:not(.metric):not(.pad)');
      if (panel && tasks.classList.contains('active')) {
        const renderTaskRow = (job) => {
          const source = recordingsById.get(job.source_recording_id);
          const asset = assetsBySource.get(job.source_recording_id);
          const verified = asset?.verification_state === 'verified' && asset?.lifecycle_state === 'present';
          const normalizedState = normalizationState(asset, job);
          const subject = source ? `${cameraLabel(source.track_id)} · ${formatDate(source.source_start)}–${formatDate(source.source_end)}` : `归档任务 · ${job.archive_job_id}`;
          const speed = jobSpeed(job);
          const percent = job.progress_percent == null ? null : Math.min(100, Math.max(0, job.progress_percent));
          const rawDownloadPercent = percent == null ? (['verifying', 'completed'].includes(job.state) ? 100 : 0) : percent;
          const downloadPercent = job.state === 'downloading' ? Math.min(99, rawDownloadPercent) : rawDownloadPercent;
          const downloadLabel = job.state === 'resolving_source' ? '定位源录像' : job.state === 'queued' ? '等待下载' : `${downloadPercent}%`;
          const jobVerified = verified && normalizedState === 'completed';
          const verifyPercent = jobVerified || job.state === 'verifying' ? 100 : 0;
          const verifyLabel = jobVerified ? '已校验' : job.state === 'verifying' ? '校验中' : job.state === 'failed' ? '校验失败' : '待校验';
          const expected = job.source_size_bytes ? humanBytes(job.source_size_bytes) : '大小未知';
          const error = job.error_code ? ` · ${jobErrorLabel(job.error_code)}` : '';
          const tone = job.state === 'completed' ? 'ok' : job.state === 'failed' ? 'fail' : job.state === 'verifying' ? 'exp' : 'wait';
          const failedLabel = job.error_code === 'NVR_SOURCE_SEGMENT_NOT_FOUND' ? '源已过期' : '可重试';
          const playback = jobVerified ? `<button class="btn" data-task-asset="${esc(asset.asset_id)}">定位播放</button>` : normalizedState ? `<span>${esc(normalizationLabel(normalizedState))}</span>` : `<span>${job.state === 'failed' ? failedLabel : job.state === 'completed' ? '已完成' : '实时更新'}</span>`;
          const retry = retryableJob(job) && archiveControlActions.has('retry_failed') ? '<button class="btn" data-task-retry>单独重试</button>' : retryableJob(job) ? '<button class="btn" disabled title="Worker 未就绪，暂不能重试">单独重试</button>' : '';
          return `<div class="taskrow" data-source-recording-id="${esc(job.source_recording_id)}"><div><b>${esc(subject)}</b><div class="meta">任务 ID ${esc(job.archive_job_id)} · ${esc(jobStateLabel(job.state))}${esc(error)}</div></div><span class="pill ${tone}">${esc(jobStateLabel(job.state))}</span><div class="task-stages"><div class="task-stage"><span>下载</span><div class="progress"><span style="width:${downloadPercent}%"></span></div><b>${esc(downloadLabel)}</b></div><div class="task-stage"><span>校验</span><div class="progress"><span style="width:${verifyPercent}%"></span></div><b>${esc(verifyLabel)}</b></div><div class="task-stage unavailable"><span>转录</span><div class="progress"><span style="width:0%"></span></div><b>${jobVerified ? '已完成' : normalizedState ? esc(normalizationLabel(normalizedState)) : '等待'}</b></div></div><span>${esc(humanRate(speed))}</span><span data-job-bytes>${esc(humanBytes(job.bytes_received || 0))} / ${esc(expected)}</span><span class="task-action"><span class="task-actions">${playback}${retry}</span></span></div>`;
        };
        const renderGroup = (title, items, extra = '') => `<section class="task-group"><div class="task-group-head">${title} <small class="muted">${items.length} 条${extra}</small></div>${items.length ? items.map(renderTaskRow).join('') : '<div class="pad muted">当前没有任务。</div>'}</section>`;
        const activeTaskRows = jobs.filter((job) => activeJobs.includes(job));
        const completedJobs = jobs.filter((job) => job.state === 'completed');
        const cancelledJobs = jobs.filter((job) => job.state === 'cancelled');
        panel.innerHTML = `${renderGroup('当前任务', activeTaskRows)}<details class="task-history"><summary>失败记录 · ${failedJobs.length} 条（可单独重试）</summary>${failedJobs.length ? failedJobs.map(renderTaskRow).join('') : '<div class="pad muted">没有失败记录。</div>'}</details><details class="task-history"><summary>已完成记录 · ${completedJobs.length} 条</summary>${completedJobs.length ? completedJobs.map(renderTaskRow).join('') : '<div class="pad muted">没有已完成记录。</div>'}</details>${cancelledJobs.length ? `<details class="task-history"><summary>已取消记录 · ${cancelledJobs.length} 条</summary>${cancelledJobs.map(renderTaskRow).join('')}</details>` : ''}`;
        panel.querySelectorAll('[data-task-asset]').forEach((button) => button.addEventListener('click', () => openRuntimeMedia(button.dataset.taskAsset, button)));
        panel.querySelectorAll('[data-task-retry]').forEach((button) => button.addEventListener('click', async (event) => {
          const row = event.currentTarget.closest('.taskrow');
          const job = jobs.find((item) => row?.textContent.includes(item.archive_job_id));
          if (!job || !retryableJob(job)) return;
          button.disabled = true;
          button.textContent = '初始化中…';
          try {
            const result = await postJson('/internal/archive/batch', {source_recording_ids: [job.source_recording_id]});
            toast(`${result.queued_count || 0} 段已初始化并进入归档队列`);
            await hydrate({refreshJobs: true, deferCatalogRefresh: true});
          } catch (error) {
            button.disabled = false;
            button.textContent = '单独重试';
            toast(`重试失败：${error.message}`);
          }
        }));
      }
    }
    const taskToolbar = document.querySelector('#tasks .surface.pad.toolbar');
    if (taskToolbar) {
      taskToolbar.innerHTML = '<button class="btn" data-task-action="pause-all">暂停等待任务</button><button class="btn" data-task-action="resume-all">继续已暂停任务</button><button class="btn" data-task-action="retry-failed">重试可重试失败</button><button class="btn danger" data-task-action="cancel-unfinished">取消等待中的任务</button><span class="muted">任务记录会保留；取消只作用于等待/已暂停任务，不会覆盖录像，也不会打断正在下载或校验的任务。</span>';
      const queuedCount = state.jobs.filter((job) => job.state === 'queued').length;
      const pausedCount = state.jobs.filter((job) => job.state === 'paused').length;
      const failedCount = state.jobs.filter((job) => job.state === 'failed').length;
      const retryableFailedCount = state.jobs.filter(retryableJob).length;
      const unfinishedCount = queuedCount + pausedCount;
      const wireTaskAction = (action, path, enabled, confirmation) => {
        const button = taskToolbar.querySelector(`[data-task-action="${action}"]`);
        if (!button) return;
        button.disabled = !enabled;
        button.title = enabled ? '' : '当前没有可执行任务，或 Worker 状态不允许此操作';
        button.onclick = async () => {
          if (button.disabled) return;
          if (confirmation && !window.confirm(confirmation)) return;
          button.disabled = true;
          try {
            const result = await postJson(path, {});
            const suffix = result.active_unaffected_count ? `，${result.active_unaffected_count} 个进行中任务未改变` : '';
            toast(`${result.changed_count ?? result.queued_count ?? 0} 个任务已处理${suffix}`);
            await hydrate({refreshJobs: true, deferCatalogRefresh: true});
          } catch (error) {
            button.disabled = false;
            toast(`任务操作失败：${error.message}`);
          }
        };
      };
      wireTaskAction('pause-all', '/internal/archive/jobs/pause-all', queuedCount > 0 && archiveControlActions.has('pause_queued'));
      wireTaskAction('resume-all', '/internal/archive/jobs/resume-all', pausedCount > 0 && archiveControlActions.has('resume_paused'));
      wireTaskAction('retry-failed', '/internal/archive/jobs/retry-failed', retryableFailedCount > 0 && archiveControlActions.has('retry_failed'));
      wireTaskAction('cancel-unfinished', '/internal/archive/jobs/cancel-unfinished', unfinishedCount > 0 && archiveControlActions.has('cancel_waiting'), unfinishedCount ? `将取消 ${unfinishedCount} 个等待中的归档任务；进行中的任务不会被中断。确认继续吗？` : null);
    }
    if (!state.runs.length) document.querySelectorAll('#analysis select').forEach((select) => { while (select.options.length > 1) select.remove(1); });

    const reviewGrid = document.querySelector('#review .reviewgrid');
    if (reviewGrid) {
      reviewGrid.innerHTML = !secondaryLoaded.has('review') ? '<div class="surface pad runtime-empty">正在读取真实待复核项目。</div>' : state.reviews.length ? state.reviews.map((flag, index) => `<article class="surface reviewcard"><h3>${esc(flag.asset_id)}</h3><div class="muted">${esc(flag.flag_type || '待复核')} · ${esc(stateLabel(flag.review_state))} · ${flag.confidence == null ? '置信度未知' : `置信度 ${esc(flag.confidence)}`}</div><div class="actions"><button class="btn" disabled>保留（待接入）</button><button class="btn danger" data-delete-preview="${index}">考虑删除</button></div><div class="meta">Cloud runtime 只提供删除影响预览；没有真实删除 mutation API 时绝不伪造物理删除成功。</div></article>`).join('') : `<div class="surface pad muted">当前没有真实待复核项目。</div>`;
      reviewGrid.querySelectorAll('[data-delete-preview]').forEach((button) => button.addEventListener('click', () => {
        const flag = state.reviews[Number(button.dataset.deletePreview)];
        const modal = document.getElementById('pullModal');
        if (!flag || !modal) return;
        window.homeVisionModalTrigger = button;
        modal.innerHTML = `<div class="modal" role="dialog" aria-modal="true" tabindex="-1"><h2>删除本地归档录像？</h2><div class="impact"><span>资产</span><b>${esc(flag.asset_id)}</b><span>候选原因</span><b>${esc(flag.flag_type || '待复核')}</b><span>复核状态</span><b>${esc(stateLabel(flag.review_state))}</b></div><div class="dangerbox">物理删除必须由 Human 明确确认；正式执行后保留 Tombstone / Audit，并抑制自动重新归档。</div><p class="muted">当前 Cloud runtime 未暴露物理删除 mutation API，因此这里只允许查看影响，不会伪造删除成功。</p><div class="actions"><button class="btn" data-delete-cancel>取消</button><button class="btn danger" disabled title="真实物理删除在具备受控 mutation API 后才可执行">删除本地录像</button></div></div>`;
        modal.classList.add('show');
        modal.querySelector('.modal')?.focus();
        modal.querySelector('[data-delete-cancel]')?.addEventListener('click', () => { modal.classList.remove('show'); button.focus({preventScroll: true}); });
      }));
    }
    const history = document.querySelector('#historyList');
    if (history) { const auditLabel = (value) => ({archive_verified:'归档已验证',archive_requested:'已请求归档',archive_failed:'归档失败',archive_paused:'任务已暂停',archive_resumed:'任务已继续',source_expired_observed:'源录像已过期'}[value] || '审计事件'); const entityLabel = (value) => ({local_asset:'本地资产',archive_job:'归档任务',source_recording:'源录像'}[value] || '实体'); const audits = [...state.audits].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))).slice(0, 200); history.innerHTML = !secondaryLoaded.has('history') ? '<div class="pad runtime-empty">正在读取真实审计记录。</div>' : audits.length ? `${audits.map((audit) => `<div class="taskrow"><span>${esc(formatDate(audit.created_at))}</span><b>${esc(auditLabel(audit.event_type))}</b><span class="pill del">${esc(entityLabel(audit.entity_type))}</span><span>${esc(audit.actor === 'system' ? '系统' : (audit.actor || '未知主体'))}</span></div>`).join('')}<div class="muted history-note">显示最近 ${audits.length} 条真实审计记录${state.audits.length > audits.length ? `（共 ${state.audits.length} 条）` : ''}。</div>` : empty('当前没有真实审计记录。'); }
  };

  const patchArchiveProgress = () => {
    const jobsBySource = new Map(state.jobs.map((job) => [job.source_recording_id, job]));
    const jobLabel = {resolving_source: '定位源录像', queued: '等待下载', downloading: '等待数据', verifying: '校验中'};
    const humanRate = (value) => value > 0 ? `${humanBytes(value)}/秒` : '—';
    document.querySelectorAll('[data-source-recording-id]').forEach((row) => {
      const job = jobsBySource.get(row.dataset.sourceRecordingId);
      if (!job) return;
      const raw = job.progress_percent == null ? (job.state === 'verifying' ? 100 : 0) : Math.min(100, Math.max(0, job.progress_percent));
      const percent = job.state === 'downloading' ? Math.min(99, raw) : raw;
      const label = job.state === 'downloading' && (job.bytes_received || 0) > 0 ? `${percent}%` : (jobLabel[job.state] || `${percent}%`);
      const stateText = {queued: '初始化中', resolving_source: '定位源录像', downloading: '拉取中', verifying: '校验中'}[job.state];
      if (row.classList.contains('seg') && stateText) {
        const pill = row.querySelector('.pill.state');
        const action = row.querySelector('.actions button');
        if (pill) { pill.className = 'pill state wait'; pill.textContent = stateText; }
        if (action) { action.disabled = true; action.textContent = stateText; }
        const body = row.querySelector('.segbody');
        let progress = body?.querySelector('.card-progress');
        if (!progress && body) {
          body.insertAdjacentHTML('afterbegin', '<div class="card-progress"><div class="progress"><span></span></div><small></small></div>');
          progress = body.querySelector('.card-progress');
        }
        if (progress) {
          progress.querySelector('.progress span').style.width = `${percent}%`;
          progress.querySelector('small').textContent = `${stateText} · ${label}${job.throughput_bytes_per_second ? ` · ${humanRate(job.throughput_bytes_per_second)}` : ''}`;
        }
      }
      if (row.classList.contains('taskrow')) {
        const pill = row.querySelector('.pill');
        const stage = row.querySelector('.task-stage:first-of-type');
        if (pill) { pill.className = 'pill wait'; pill.textContent = stateText || job.state; }
        if (stage) {
          stage.querySelector('.progress span').style.width = `${percent}%`;
          stage.querySelector('b').textContent = label;
        }
      }
      const bar = row.querySelector('.card-progress .progress span, .task-stage:first-of-type .progress span');
      if (bar) bar.style.width = `${percent}%`;
      const cardText = row.querySelector('.card-progress small');
      if (cardText) cardText.textContent = `${jobLabel[job.state] || job.state} · ${label}${job.throughput_bytes_per_second ? ` · ${humanRate(job.throughput_bytes_per_second)}` : ''}`;
      const taskText = row.querySelector('.task-stage:first-of-type b');
      if (taskText) taskText.textContent = label;
      const bytesText = row.querySelector('[data-job-bytes]');
      if (bytesText) bytesText.textContent = `${humanBytes(job.bytes_received || 0)} / ${humanBytes(job.source_size_bytes || 0)}`;
    });
  };

  const playableItems = () => {
    const assetsBySource = new Map(state.assets.map((asset) => [asset.source_recording_id, asset]));
    return [...state.recordings]
      .sort((a, b) => String(b.source_start || '').localeCompare(String(a.source_start || '')))
      .map((recording) => ({recording, asset: assetsBySource.get(recording.source_recording_id)}))
      .filter((item) => item.asset?.verification_state === 'verified' && item.asset?.lifecycle_state === 'present' && item.asset?.normalization_state === 'completed');
  };

  const updatePlayerNavigation = () => {
    const previous = document.querySelector('[data-player-prev]');
    const next = document.querySelector('[data-player-next]');
    if (previous) previous.disabled = playerIndex <= 0;
    if (next) next.disabled = playerIndex < 0 || playerIndex >= playerItems.length - 1;
  };

  const loadRuntimeMedia = async (assetId, trigger, focusPlayer = false) => {
    const requestId = ++mediaRequestId;
    openPlayer(`资产 ${assetId}`, focusPlayer);
    const screen = document.getElementById('screen');
    if (!screen) return;
    screen.innerHTML = '';
    const status = document.createElement('div');
    status.className = 'screen-status';
    status.textContent = '正在查找录像…';
    screen.append(status);
    try {
      const access = await safeJson(`/api/v1/media/${encodeURIComponent(assetId)}/access`);
      if (requestId !== mediaRequestId) return;
      if (access.status !== 'available' || !access.playback_path) {
        const stateLabel = access.status || 'unknown';
        status.textContent = `录像当前不可播放（状态：${stateLabel}），正在刷新录像清单…`;
        toast(`录像当前不可播放（状态：${stateLabel}），已刷新录像清单`);
        await hydrate({refreshCatalog: true});
        if (requestId !== mediaRequestId) return;
        closePlayer();
        if (trigger) trigger.focus({preventScroll: true});
        return;
      }
      const playbackLabel = access.playback_mode === 'normalized' ? '正在加载兼容副本…' : '正在加载录像…';
      status.textContent = playbackLabel;
      const video = document.createElement('video');
      video.controls = true; video.preload = 'auto'; video.playsInline = true; video.autoplay = true;
      video.setAttribute('aria-label', `真实资产 ${assetId}`);
      video.addEventListener('loadstart', () => { status.classList.remove('ready'); status.textContent = playbackLabel; });
      video.addEventListener('loadedmetadata', () => {
        const seconds = Math.max(0, Math.round(Number.isFinite(video.duration) ? video.duration : 0));
        const duration = seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
        status.classList.remove('ready'); status.textContent = `已识别录像 · ${duration}`; window.setTimeout(() => status.classList.add('ready'), 900);
      });
      video.addEventListener('canplay', () => status.classList.add('ready'));
      video.addEventListener('waiting', () => { status.classList.remove('ready'); status.textContent = '正在缓冲…'; });
      video.addEventListener('error', () => { status.classList.remove('ready'); status.textContent = '该录像暂时无法解码，请稍后重试'; toast('该录像无法解码，请稍后重试'); }, {once: true});
      video.src = access.playback_path;
      screen.replaceChildren(video, status);
      video.focus({preventScroll: true});
      video.play().catch(() => { status.classList.remove('ready'); status.textContent = '已准备好，点击播放'; });
    } catch (_error) { toast('受控媒体暂不可用'); if (trigger) trigger.focus({preventScroll: true}); }
  };

  const openRuntimeMedia = async (assetId, trigger) => {
    if (document.getElementById('archive')?.classList.contains('active')) archiveScrollTop = window.scrollY;
    playerItems = playableItems();
    playerIndex = playerItems.findIndex((item) => item.asset.asset_id === assetId);
    updatePlayerNavigation();
    await loadRuntimeMedia(assetId, trigger, Boolean(trigger));
  };

  const movePlayer = (delta) => {
    const targetIndex = playerIndex + delta;
    const item = playerItems[targetIndex];
    if (!item) return;
    playerIndex = targetIndex;
    updatePlayerNavigation();
    loadRuntimeMedia(item.asset.asset_id);
  };

  document.querySelector('[data-player-prev]')?.addEventListener('click', () => {
    movePlayer(-1);
  });
  document.querySelector('[data-player-next]')?.addEventListener('click', () => {
    movePlayer(1);
  });

  const pageData = {
    overview: [['scans', '/api/v1/scan-runs', 500]],
    analysis: [['profiles', '/api/v1/analysis-profiles'], ['runs', '/api/v1/analysis-runs', 500], ['events', '/api/v1/events', 500]],
    review: [['reviews', '/api/v1/review-flags', 500]],
    history: [['audits', '/api/v1/audit-events', 500]],
  };
  const ensurePageData = async (page) => {
    const requests = pageData[page];
    const fresh = secondaryLoaded.has(page) && Date.now() - (secondaryLoadedAt.get(page) || 0) < SECONDARY_REFRESH_MS;
    if (!requests || fresh || secondaryInFlight.has(page)) return;
    secondaryInFlight.add(page);
    try {
      const results = await Promise.all(requests.map(([key, path, limit]) => limit ? fetchAll(path, limit) : safeJson(path)));
      results.forEach((result, index) => { state[requests[index][0]] = result.items || []; });
      secondaryLoaded.add(page);
      secondaryLoadedAt.set(page, Date.now());
      renderTruthfulSurfaces();
    } catch (_error) {
      if (document.getElementById(page)?.classList.contains('active')) toast('该页面的附加记录暂时无法读取');
    } finally {
      secondaryInFlight.delete(page);
    }
  };

  const mergeHydrateIntent = (left, right) => ({
    refreshCatalog: Boolean(left?.refreshCatalog || right?.refreshCatalog),
    refreshJobs: Boolean(left?.refreshJobs || right?.refreshJobs),
    deferCatalogRefresh: Boolean(left?.deferCatalogRefresh && right?.deferCatalogRefresh),
  });

  const hydrate = async (intent = {}) => {
    if (hydrateInFlight) {
      pendingHydrateIntent = mergeHydrateIntent(pendingHydrateIntent || {}, intent);
      return;
    }
    hydrateInFlight = true;
    const {refreshCatalog = false, refreshJobs = false, deferCatalogRefresh = false} = intent;
    try {
      const shouldRefreshJobs = refreshCatalog || refreshJobs || !jobsLoaded || Date.now() - lastJobRefreshAt >= JOB_REFRESH_MS;
      const [health, runtime, archiveStatus] = await Promise.all([
        safeJson('/health'),
        safeJson('/api/v1/runtime-status'),
        safeJson('/internal/archive/status'),
      ]);
      state.health = health;
      state.runtime = runtime;
      state.archiveStatus = archiveStatus;
      let refreshCoreCatalog = refreshCatalog || !state.capabilities || (!deferCatalogRefresh && Date.now() - lastCatalogRefreshAt >= CATALOG_REFRESH_MS);
      if (refreshCoreCatalog) {
        const [capabilities, cameras, recordings, assets, volumes] = await Promise.all([
          safeJson('/api/v1/capabilities'),
          fetchAll('/api/v1/cameras', 500),
          fetchAll('/api/v1/recordings', 500),
          fetchAll('/api/v1/assets', 500),
          fetchAll('/api/v1/storage-volumes', 500),
        ]);
        state.capabilities = capabilities;
        state.cameras = cameras.items || [];
        state.recordings = recordings.items || [];
        state.assets = assets.items || [];
        state.volumes = volumes.items || [];
        lastCatalogRefreshAt = Date.now();
      }
      let jobs = {items: state.jobs};
      if (shouldRefreshJobs) {
        try { jobs = await fetchAll('/api/v1/archive-jobs', 500); } catch (_error) { /* keep current job state */ }
      }
      if (shouldRefreshJobs) { jobsLoaded = true; lastJobRefreshAt = Date.now(); }
      const nextJobs = jobs.items || [];
      const previousJobs = new Map(state.jobs.map((job) => [job.archive_job_id, job]));
      const catalogChanged = nextJobs.length !== state.jobs.length || nextJobs.some((job) => {
        const previous = previousJobs.get(job.archive_job_id);
        return !previous || previous.state !== job.state;
      });
      const terminalJobChanged = nextJobs.some((job) => {
        const previous = previousJobs.get(job.archive_job_id);
        return previous && previous.state !== job.state && ['completed', 'failed', 'cancelled'].includes(job.state);
      });
      const progressChanged = nextJobs.some((job) => {
        const previous = previousJobs.get(job.archive_job_id);
        return previous && (previous.bytes_received !== job.bytes_received || previous.progress_percent !== job.progress_percent);
      });
      const nonTerminalJobChanged = nextJobs.some((job) => {
        const previous = previousJobs.get(job.archive_job_id);
        return previous && previous.state !== job.state && !['completed', 'failed', 'cancelled'].includes(job.state);
      });
      if (terminalJobChanged && !refreshCoreCatalog) {
        const [assets, volumes] = await Promise.all([
          fetchAll('/api/v1/assets', 500),
          fetchAll('/api/v1/storage-volumes', 500),
        ]);
        state.assets = assets.items || [];
        state.volumes = volumes.items || [];
        refreshCoreCatalog = true;
      }
      state.jobs = nextJobs;
      if (refreshCoreCatalog || catalogChanged) {
        if (document.getElementById('player')?.classList.contains('show')) archiveRenderDeferred = true;
        else renderTruthfulSurfaces();
      }
      if (progressChanged || nonTerminalJobChanged) {
        patchArchiveProgress();
      }
      hydrateOverview();
      hydrateAnalysis();
      ensurePageData(document.querySelector('.page.active')?.id);
      document.documentElement.dataset.homeVisionHydrated = 'true';
    } catch (_error) {
      const meta = document.querySelector('.top .meta');
      if (meta) meta.textContent = '● Home Vision API 暂不可用 · 归档事实保持不变';
      document.documentElement.dataset.homeVisionHydrated = 'error';
    } finally {
      hydrateInFlight = false;
      const nextIntent = pendingHydrateIntent;
      pendingHydrateIntent = null;
      if (nextIntent) await hydrate(nextIntent);
    }
  };

  document.querySelectorAll('[data-page]').forEach((button) => {
    button.addEventListener('click', () => {
      window.requestAnimationFrame(() => window.scrollTo({top: 0, behavior: 'auto'}));
    });
  });

  window.homeVisionRuntime = {
    state,
    hydrate,
    showRuntimeAnalysis,
    ensurePageData,
    renderTruthfulSurfaces,
    invalidateMediaRequest: () => { mediaRequestId += 1; },
    flushArchiveRender: () => {
      if (!archiveRenderDeferred || document.getElementById('player')?.classList.contains('show')) return;
      archiveRenderDeferred = false;
      renderTruthfulSurfaces();
    },
  };
  window.addEventListener('DOMContentLoaded', () => hydrate({refreshCatalog: true}), {once: true});
  window.setInterval(() => {
    if (document.visibilityState !== 'hidden' && !hydrateInFlight) hydrate();
  }, 5000);
})();
</script>
'''


def _remove_prototype_state_lab(html: str) -> str:
    start = html.find('<aside class="statelab" id="lab">')
    end = html.find('<nav class="mobile">', start)
    if start >= 0 and end > start:
        return html[:start] + html[end:]
    return html


def _remove_prototype_modals(html: str) -> str:
    start = html.find('<div class="modalback" id="deleteModal">')
    end = html.find('<aside class="statelab"', start)
    if start >= 0 and end > start:
        return html[:start] + html[end:]
    return html


def _remove_prototype_bulk_pull_preview(html: str) -> str:
    button = '<button class="btn primary push" onclick="openPullAll(this)">拉取全部当前可拉取</button>'
    replacement = '<button class="btn primary push" disabled title="正在读取真实归档能力">批量拉取</button>'
    html = html.replace(button, replacement)
    start = html.find('<div class="modalback" id="pullModal">')
    end = html.find('<div class="modalback" id="deleteModal">', start)
    if start >= 0 and end > start:
        unavailable = ('<div class="modalback" id="pullModal"><div class="modal" role="dialog" '
                       'aria-modal="true" tabindex="-1"><h2>批量归档</h2>'
                       '<p class="muted">正在读取当前真实录像清单。</p></div></div>')
        html = html[:start] + unavailable + html[end:]
    return html


def _remove_prototype_script(html: str) -> str:
    start = html.find('<script>')
    end = html.find('</script>', start)
    if start < 0 or end < 0:
        raise RuntimeError("UI_BASELINE_PROTOTYPE_SCRIPT_MISSING")
    return html[:start] + html[end + len('</script>'):]


def _neutralize_prototype_surfaces(html: str) -> str:
    """Leave stable layout hooks, but never ship fixture values in the first HTML."""
    html = re.sub(
        r'<div class="surface calendar">.*?</div><div class="summary">',
        '<div class="surface calendar"><div class="calhead"></div><div class="calgrid"></div></div><div class="summary">',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<div class="summary">.*?(?=<div class="storage">)',
        '<div class="summary"><div class="surface metric"><div class="k">未完成日期</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">已验证</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">最旧未完成</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">可用空间</div><div class="v">读取中</div></div></div>',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<div class="storage">.*?(?=<div class="telemetry">)',
        '<div class="storage"><div class="surface volume"><div class="row"><b>归档盘 A</b><span class="pill gray">读取中</span></div></div><div class="surface volume"><div class="row"><b>归档盘 B</b><span class="pill gray">读取中</span></div></div></div>',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<div class="telemetry">.*?(?=<div class="surface pad" style="margin-top:15px">)',
        '<div class="telemetry"><div class="surface telecard"><b>Home Vision 容器</b><div class="muted">正在读取真实运行指标。</div></div><div class="surface telecard"><b>OMV Edge 主机</b><div class="muted">正在读取主机遥测边界。</div></div></div>',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<div class="surface activity">.*?(?=</section><section class="page" id="archive">)',
        '<div class="surface activity"><div class="pad runtime-empty">正在读取真实活动记录。</div></div>',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<div class="surface date">.*?</div></section><section class="page" id="analysis">',
        '<div class="pad runtime-empty">正在读取真实录像清单。</div></section><section class="page" id="analysis">',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<div class="stats"><div class="surface metric"><div class="k">今天已分析</div>.*?</div></div><div class="surface pad" style="margin-top:12px"><div class="analysis-tools">.*?</div></div>',
        '<div class="stats"><div class="surface metric"><div class="k">今天已分析</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">待分析</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">分析中</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">失败</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">发现事件</div><div class="v">读取中</div></div></div><div class="surface pad" style="margin-top:12px"><div class="analysis-tools"><span class="muted">正在读取真实分析运行记录。</span></div></div>',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<div class="surface analysisitem".*?</div></section><section class="page" id="tasks">',
        '<div class="surface analysisitem"><div class="pad runtime-empty">正在读取真实分析运行记录。</div></div></section><section class="page" id="tasks">',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<section class="page" id="tasks">.*?</section><section class="page" id="review">',
        '<section class="page" id="tasks"><div class="head"><div><h1>任务</h1><div class="muted">正在读取真实归档任务。</div></div></div><div class="stats"><div class="surface metric"><div class="k">拉取中</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">等待</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">校验中</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">失败</div><div class="v">读取中</div></div><div class="surface metric"><div class="k">已完成</div><div class="v">读取中</div></div></div><div class="surface pad toolbar"></div><div class="surface"><div class="pad runtime-empty">正在读取真实归档任务。</div></div></section><section class="page" id="review">',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<section class="page" id="review">.*?</section><section class="page" id="history">',
        '<section class="page" id="review"><div class="head"><div><h1>待复核</h1><div class="muted">正在读取真实待复核项目。</div></div></div><div class="reviewgrid"><div class="surface pad runtime-empty">正在读取真实待复核项目。</div></div></section><section class="page" id="history">',
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'<section class="page" id="history">.*?</section></div></main>',
        '<section class="page" id="history"><div class="head"><div><h1>记录</h1></div></div><div class="surface" id="historyList"><div class="pad runtime-empty">正在读取真实审计记录。</div></div></section></div></main>',
        html, count=1, flags=re.DOTALL,
    )
    return html


def _repair_prototype_page_nesting(html: str) -> str:
    """Repair one malformed fixture closing tag without changing the frozen blob."""
    malformed = '</div></div></div></section><section class="page" id="analysis">'
    corrected = '</div></div></section><section class="page" id="analysis">'
    if malformed not in html:
        raise RuntimeError("UI_BASELINE_PAGE_NESTING_MISSING")
    return html.replace(malformed, corrected, 1)


def render_runtime_html() -> str:
    html = BASELINE_PATH.read_text(encoding="utf-8")
    for old, new in SEMANTIC_REPLACEMENTS:
        html = html.replace(old, new)
    html = _remove_prototype_script(html)
    html = _repair_prototype_page_nesting(html)
    html = _neutralize_prototype_surfaces(html)
    html = _remove_prototype_state_lab(html)
    html = _remove_prototype_bulk_pull_preview(html)
    html = _remove_prototype_modals(html)
    html = html.replace(
        '<div class="pfoot"><button>上一段</button><span>保持页面上下文</span><button>下一段</button></div>',
        '<div class="pfoot"><button data-player-prev>上一段</button><span>保持页面上下文</span><button data-player-next>下一段</button></div>',
        1,
    )
    html = html.replace("</head>", f"{RUNTIME_STYLE}</head>", 1)
    if "</body>" not in html:
        raise RuntimeError("UI_BASELINE_BODY_MISSING")
    html = html.replace("</body>", f"{RUNTIME_SUPPORT_SCRIPT}{RUNTIME_SCRIPT_MARKER}{RUNTIME_SCRIPT}</body>", 1)
    return html


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> HTMLResponse:
    return HTMLResponse(render_runtime_html())
