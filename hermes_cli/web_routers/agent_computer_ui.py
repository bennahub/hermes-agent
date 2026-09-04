"""Owner-facing Human Takeover page.

Authenticated dashboard only. No CDP, no raw ports, no second app.
Control labels come from server ``control_label``.
"""

from __future__ import annotations

COMPUTER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Agent needs you — Hermes</title>
<style>
:root{--bg:#0e0f0d;--panel:#171915;--ink:#f4f1e8;--muted:#9a9484;--gold:#c9a227;--ok:#6fbf73;--line:#2c2e28}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:ui-sans-serif,system-ui,sans-serif}
body{display:flex;flex-direction:column}
header{display:flex;flex:0 0 auto;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:8px 12px;padding:8px 14px;border-bottom:1px solid var(--line)}
.brand{display:flex;flex-direction:column;gap:2px;min-width:8rem;flex:0 1 auto}
h1{font-size:1rem;margin:0}
.status{color:var(--muted);font-size:.85rem}
.controlBar{display:none;flex-wrap:wrap;align-items:center;gap:8px;flex:1 1 280px;min-width:0}
.controlBar.open{display:flex}
.trust{display:flex;flex-wrap:wrap;align-items:center;gap:6px;flex:1 1 220px;min-width:0}
.origin{font-weight:600;font-size:.9rem;word-break:break-all}
.https{font-size:.75rem;color:var(--ok);white-space:nowrap}
.https.off{color:#f0a0a0}
.fullUrl{display:none;flex:1 1 100%;font-size:.75rem;color:var(--muted);word-break:break-all;background:#111;border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.fullUrl.open{display:block}
.navRow{display:flex;flex-wrap:wrap;align-items:center;gap:6px;flex:1 1 100%}
.navRow input{flex:1 1 160px;min-width:0;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:#111;color:var(--ink);font:inherit}
main.chooser{display:grid;grid-template-columns:minmax(0,1fr) 280px;flex:1;min-height:0}
main.control{display:flex;flex-direction:column;flex:1;min-height:0}
@media(max-width:800px){main.chooser{grid-template-columns:1fr}}
@media(max-width:720px){
  header{flex-direction:column;align-items:stretch;flex-wrap:nowrap}
  .controlBar{flex:0 0 auto;width:100%}
  .navRow,.trust{flex:1 1 100%}
  header button{flex:1 1 auto}
}
.screen{padding:16px}
.stage{flex:1;min-height:0;background:#000;display:flex;flex-direction:column;overflow:hidden;position:relative}
.surfaceWrap{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden}
main,.stage,.surfaceWrap{min-width:0;max-width:100%}
#surface{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain;cursor:default;background:#111;touch-action:none;outline:none}
#surface.debugPointer{cursor:crosshair}
.hint{color:var(--muted);font-size:.85rem;pointer-events:none}
aside{border-left:1px solid var(--line);padding:16px;background:var(--panel)}
aside button,aside select,aside textarea{width:100%;margin:6px 0;padding:10px 12px;border-radius:8px;border:1px solid var(--line);background:#111;color:var(--ink);font:inherit}
header button,.fsChrome button{margin:0;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:#111;color:var(--ink);font:inherit;width:auto;flex:0 1 auto}
button.primary{background:var(--gold);color:#111;border:0;font-weight:600}
button.ok{background:var(--ok);color:#111;border:0;font-weight:600}
button:disabled{opacity:.45}
label{display:block;color:var(--muted);font-size:.8rem;margin-top:10px}
.banner{padding:10px 12px;border-radius:8px;background:#2a2110;color:#f3d27a}
.err{color:#f0a0a0;font-size:.85rem;min-height:1.2em}
.err:empty{display:none}
#liveError{position:absolute;bottom:8px;left:8px;right:8px;background:#2a1111;padding:8px;z-index:3}
#liveError:empty{display:none}
.chip{background:rgba(16,17,15,.82);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:.8rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.fallback{display:none;padding:12px;border-top:1px solid var(--line);background:var(--panel)}
.fallback.open{display:block}
.hidden{display:none !important}
.fsChrome{display:none;flex-wrap:wrap;align-items:center;gap:8px;padding:6px 10px;background:#111;border-bottom:1px solid var(--line);flex:0 0 auto}
.stage:fullscreen .fsChrome,.stage:-webkit-full-screen .fsChrome{display:flex}
.stage:fullscreen .surfaceWrap,.stage:-webkit-full-screen .surfaceWrap{flex:1}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1 id="headline">Waiting for an agent</h1>
    <div class="status" id="statusLine">No computer selected</div>
  </div>
  <div class="controlBar" id="controlBar">
    <div class="chip" id="chip">OWNER_CONTROL</div>
    <div class="trust">
      <span class="https off" id="httpsMark">HTTP</span>
      <span class="origin" id="origin">—</span>
      <button type="button" id="urlToggle">Show URL</button>
    </div>
    <div class="fullUrl" id="fullUrl"></div>
    <div class="navRow">
      <button type="button" id="backBtn">Back</button>
      <button type="button" id="fwdBtn">Forward</button>
      <button type="button" id="reloadBtn">Reload</button>
      <input id="urlBox" type="url" placeholder="https://…" autocomplete="off" spellcheck="false"/>
      <button type="button" id="goUrl">Open</button>
      <button type="button" id="fullBtn">Full screen</button>
      <button type="button" class="ok" id="giveOverlay">Give Control Back</button>
    </div>
  </div>
</header>
<main id="main" class="chooser">
  <section class="screen" id="chooserPane">
    <div class="banner" id="banner">Open a computer, then take control only when the agent needs you.</div>
    <p class="hint">After takeover the remote browser fills this window. Mouse, trackpad, and keyboard go to that computer.</p>
  </section>
  <div class="stage hidden" id="stage">
    <div class="fsChrome" id="fsChrome">
      <div class="chip" id="fsAgent">Agent</div>
      <div class="chip" id="fsChip">OWNER_CONTROL</div>
      <span class="origin" id="fsOrigin">—</span>
      <button type="button" id="exitFull">Exit Full Screen</button>
      <button type="button" id="fsUrlToggle">Show URL</button>
      <button type="button" class="ok" id="giveFs">Give Control Back</button>
      <div class="fullUrl" id="fsFullUrl"></div>
    </div>
    <div class="err" id="liveError" role="alert"></div>
    <div class="surfaceWrap">
      <canvas id="surface" tabindex="0" width="1440" height="900" hidden></canvas>
      <div class="hint" id="emptyShot">Starting live view…</div>
    </div>
  </div>
  <aside id="aside">
    <label for="computer">Computer</label>
    <select id="computer"></select>
    <button type="button" id="refresh">Refresh list</button>
    <button type="button" class="primary" id="take">Take Control</button>
    <button type="button" id="suspend">Suspend computer</button>
    <button type="button" class="ok" id="give" disabled>Give Control Back</button>
    <button type="button" id="toggleFallback">Fallback controls</button>
    <div class="fallback" id="fallback">
      <label for="typeBox">Type into the page</label>
      <textarea id="typeBox" rows="3" placeholder="Fallback only"></textarea>
      <button type="button" id="sendText" disabled>Type</button>
      <button type="button" id="scrollUp" disabled>Scroll up</button>
      <button type="button" id="scrollDown" disabled>Scroll down</button>
    </div>
    <div class="err" id="err"></div>
  </aside>
</main>
<script>
const $ = (id) => document.getElementById(id);
const state = {
  computerId:"", leaseId:"", fencingEpoch:0, generation:0,
  authority:"", label:"", agentName:"Agent", identityId:"",
  viewport:{width:1440,height:900}, ws:null, drawing:false,
  opening:false, retries:0, lastMoveAt:0, lastCursorAt:0, cursor:"default",
  surfaceOpen:false, canResume:false, frameReady:false, lastDrawnSeq:0,
  lastClickAt:0, lastClickX:0, lastClickY:0, clickCount:1,
  debugPointer: new URLSearchParams(location.search).get("pointer") === "debug"
};
function labelOf(c){
  return c.control_label || ({
    AGENT_CONTROLLED:"AGENT_CONTROL",
    OWNER_CONTROLLED:"OWNER_CONTROL",
    TAKEOVER_PENDING:"TAKEOVER_PENDING",
    YIELDING:"YIELDING",
    RETURNING:"RETURNING"
  }[c.control || c.control_authority] || c.control || c.control_authority || "");
}
function headline(){
  const name = state.agentName || "Agent";
  if(state.surfaceOpen && state.label === "OWNER_CONTROL") return "You have control";
  if(state.canResume && state.label === "OWNER_CONTROL") return "Resume control of " + name;
  if(state.label === "TAKEOVER_PENDING" || state.label === "YIELDING") return name + " needs you";
  if(state.label === "RETURNING") return name + " resumed";
  return name + " is working";
}
function banner(){
  if(state.canResume && !state.surfaceOpen) return "You still have control of this computer. Resume Control to return to the same screen, or Give Control Back.";
  if(state.label === "OWNER_CONTROL" && state.surfaceOpen) return "You have control of the remote browser. Verify the origin before typing secrets. Give Control Back when finished.";
  if(state.label === "TAKEOVER_PENDING") return state.agentName + " needs you — take control of the same screen.";
  if(state.label === "RETURNING") return state.agentName + " resumed from the same place.";
  return "The agent keeps ordinary work. Take Control only at a human checkpoint.";
}
function showErr(msg){ $("err").textContent = msg || ""; $("liveError").textContent = msg || ""; }
function ownerHasLease(){ return state.label === "OWNER_CONTROL" && state.leaseId; }
function ownerHasControl(){ return ownerHasLease() && state.surfaceOpen; }
function applyLocation(loc){
  if(!loc) return;
  const origin = loc.origin || "";
  const url = loc.url || "";
  const https = !!loc.https;
  $("origin").textContent = origin || "—";
  $("fsOrigin").textContent = origin || "—";
  $("fullUrl").textContent = url || "";
  $("fsFullUrl").textContent = url || "";
  $("httpsMark").textContent = https ? "HTTPS" : (loc.scheme === "fixture" ? "FIXTURE" : "HTTP");
  $("httpsMark").classList.toggle("off", !https);
  if(document.activeElement !== $("urlBox")) $("urlBox").value = url.startsWith("fixture://") ? "" : url;
}
function setControlMode(on){
  state.surfaceOpen = !!on;
  $("main").className = on ? "control" : "chooser";
  $("chooserPane").classList.toggle("hidden", on);
  $("stage").classList.toggle("hidden", !on);
  $("aside").classList.toggle("hidden", on);
}
function syncChrome(){
  const live = ownerHasControl();
  $("give").disabled = !ownerHasLease();
  $("suspend").disabled = !!ownerHasLease();
  $("giveOverlay").disabled = !live;
  $("giveFs").disabled = !live;
  $("sendText").disabled = !live;
  $("scrollUp").disabled = !live;
  $("scrollDown").disabled = !live;
  $("backBtn").disabled = !live;
  $("fwdBtn").disabled = !live;
  $("reloadBtn").disabled = !live;
  $("goUrl").disabled = !live;
  $("headline").textContent = headline();
  $("banner").textContent = banner();
  $("chip").textContent = state.label || "AGENT_CONTROL";
  $("fsChip").textContent = state.label || "AGENT_CONTROL";
  $("fsAgent").textContent = state.agentName || "Agent";
  const sel = $("computer");
  if(sel.value === state.computerId){
    const opt = sel.selectedOptions[0];
    if(opt) opt.textContent = (state.agentName || "agent") + " — " + (state.label || "");
  }
  $("statusLine").textContent = (state.agentName+" · "+(state.label||"idle")).trim();
  $("controlBar").classList.toggle("open", live);
  $("take").textContent = (ownerHasLease() && !state.surfaceOpen) ? "Resume Control" : "Take Control";
  if(!live) setControlMode(false);
}
function applyComputer(c){
  const id = c.computer_id || c.id || state.computerId;
  if(id !== state.computerId){ state.leaseId = ""; state.fencingEpoch = 0; }
  state.computerId = c.computer_id || c.id || state.computerId;
  state.authority = c.control || c.control_authority || "";
  state.label = labelOf(c);
  state.fencingEpoch = c.fencing_epoch || state.fencingEpoch;
  state.canResume = !!c.can_resume;
  state.leaseId = c.lease_id || (c.lease && c.lease.lease_id) || "";
  if(c.browser_identity && c.browser_identity.id) state.identityId = c.browser_identity.id;
  const profile = (c.agent_profile_id || "").replace(/^agent:/,"");
  if(profile) state.agentName = profile.replace(/(^|[-_])(\\w)/g, (_,a,b)=> (a?" ":"")+b.toUpperCase());
  applyLocation(c.location || {});
  syncChrome();
}
async function api(path, opts){
  const r = await fetch(path, Object.assign({credentials:"same-origin", headers:{"Content-Type":"application/json"}}, opts||{}));
  const data = await r.json().catch(()=>({}));
  if(r.status === 401){ location.href = "/login?next=/computer"; throw new Error("Sign in required"); }
  if(!r.ok) throw new Error((data.detail && data.detail.message) || data.detail || data.error || ("HTTP "+r.status));
  return data;
}
async function mintTicket(){
  const data = await api("/api/auth/ws-ticket", {method:"POST", body:"{}"});
  return data.ticket || "";
}
function surfaceSize(){
  return {width: state.viewport.width || 1440, height: state.viewport.height || 900};
}
function drawFrame(msg, ws){
  const img = new Image();
  img.onload = () => {
    if(state.ws !== ws || !ownerHasControl() || msg.generation !== state.generation) return;
    if(msg.seq <= state.lastDrawnSeq){ sendEvent({type:"ack", session_id:msg.session_id}); return; }
    state.lastDrawnSeq = msg.seq;
    const canvas = $("surface");
    const wasHidden = canvas.hidden;
    const vw = state.viewport.width || 1440;
    const vh = state.viewport.height || 900;
    if(canvas.width !== vw) canvas.width = vw;
    if(canvas.height !== vh) canvas.height = vh;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, vw, vh);
    applyLocation(msg.location || {});
    state.frameReady = true;
    state.retries = 0;
    showErr("");
    canvas.hidden = false;
    $("emptyShot").hidden = true;
    if(wasHidden && ownerHasControl()) canvas.focus();
    applyCursor(state.cursor);
    sendEvent({type:"ack", session_id:msg.session_id});
  };
  img.onerror = () => { if(state.ws === ws) ws.close(); };
  img.src = "data:"+(msg.mime||"image/jpeg")+";base64,"+msg.data;
}
function mapPoint(ev){
  const canvas = $("surface");
  const rect = canvas.getBoundingClientRect();
  if(rect.width < 2 || rect.height < 2) return null;
  return {
    x: ev.clientX - rect.left,
    y: ev.clientY - rect.top,
    client_width: rect.width,
    client_height: rect.height,
    frame_width: canvas.width,
    frame_height: canvas.height
  };
}
const CURSORS = {
  default:1, pointer:1, text:1, "not-allowed":1, grab:1, grabbing:1,
  "col-resize":1, "row-resize":1, "ew-resize":1, "ns-resize":1,
  "nesw-resize":1, "nwse-resize":1, move:1, wait:1, progress:1,
  help:1, cell:1, copy:1, alias:1, "context-menu":1
};
function applyCursor(name){
  const canvas = $("surface");
  if(state.debugPointer){
    canvas.classList.add("debugPointer");
    canvas.style.cursor = "crosshair";
    return;
  }
  canvas.classList.remove("debugPointer");
  let next = String(name || "default").split(",")[0].trim().toLowerCase();
  if(next === "auto") next = "default";
  if(next === "vertical-text") next = "text";
  if(next === "no-drop") next = "not-allowed";
  if(next === "crosshair" || next.indexOf("url(") === 0 || !CURSORS[next]) next = "default";
  if(state.cursor === next && canvas.style.cursor === next) return;
  state.cursor = next;
  canvas.style.cursor = next;
}
function sendEvent(payload){
  if(!state.frameReady && ["pointer","key","wheel","text","cursor"].includes(payload.type)) return false;
  if(!state.ws || state.ws.readyState !== 1){
    if(payload && (payload.type === "pointer" || payload.type === "key" || payload.type === "wheel")){
      $("fullUrl").textContent = "Live view not ready — wait, then click again";
      $("fullUrl").classList.add("open");
    }
    return false;
  }
  state.ws.send(JSON.stringify(payload));
  return true;
}
async function openStream(){
  if(state.opening) return;
  state.opening = true;
  closeStream();
  try{
    const size = surfaceSize();
    const ticket = await mintTicket();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const qs = new URLSearchParams({
      ticket,
      lease_id: state.leaseId || "",
      fencing_epoch: String(state.fencingEpoch || 0),
      width: String(size.width),
      height: String(size.height)
    });
    const ws = new WebSocket(proto+"://"+location.host+"/api/agent-computers/"+state.computerId+"/stream?"+qs.toString());
    state.ws = ws;
    ws.onmessage = (ev) => {
      if(state.ws !== ws) return;
      const msg = JSON.parse(ev.data);
      if(msg.type === "status"){
        if(!$("origin").textContent || $("origin").textContent === "—") $("origin").textContent = msg.phase === "starting" ? "Starting…" : (msg.phase || "Live");
        return;
      }
      if(msg.type === "error"){
        showErr("Live view could not start (" + (msg.reason || msg.code || "error") + ").");
        if(msg.retry === false || msg.code === 4401 || msg.code === 4403){
          state.retries = 99;
        }
        return;
      }
      if(msg.type === "cursor"){
        applyCursor(msg.cursor || "default");
        return;
      }
      if(msg.type === "location"){
        applyLocation(msg);
        return;
      }
      if(msg.type === "hello"){
        state.generation = msg.generation || 0;
        state.label = msg.control || labelOf(msg);
        state.lastDrawnSeq = 0;
        if(msg.viewport) state.viewport = msg.viewport;
        applyLocation(msg);
        syncChrome();
        if(msg.session_id) sendEvent({type:"ack", session_id: msg.session_id});
      }
      if(msg.type === "frame"){
        if(msg.generation && state.generation && msg.generation !== state.generation) return;
        drawFrame(msg, ws);
      }
    };
    ws.onclose = (ev) => {
      if(state.ws !== ws) return;
      state.ws = null;
      state.frameReady = false;
      showErr("Connection lost. Remote input is paused while reconnecting.");
      if(ev.code === 4401){ location.href = "/login?next=/computer"; return; }
      if(ev.code === 4403 || ev.code === 4400){
        showErr("Live view was refused (" + ev.code + "). Refresh the page.");
        return;
      }
      if(!ownerHasControl()) return;
      if(ev.code === 4409){
        loadList().catch(e=>showErr(e.message));
        return;
      }
      if(state.retries >= 8){
        showErr("Live view could not stay connected. Give Control Back and take control again.");
        return;
      }
      state.retries += 1;
      const wait = Math.min(5000, 600 * Math.pow(2, state.retries - 1));
      setTimeout(() => { if(ownerHasControl()) openStream().catch(e=>showErr(e.message)); }, wait);
    };
  }finally{
    state.opening = false;
  }
}
function closeStream(){
  const ws = state.ws;
  state.ws = null;
  state.frameReady = false;
  state.drawing = false;
  if(ws){
    ws.onclose = null;
    try{ ws.close(); }catch(_e){}
  }
}
async function loadList(){
  const data = await api("/api/agent-computers");
  const items = data.computers || [];
  const sel = $("computer");
  const prev = sel.value || new URLSearchParams(location.search).get("computer") || "";
  sel.replaceChildren(...items.map(c => {
    const id = c.computer_id || c.id;
    return new Option((c.agent_profile_id||id) + " — " + labelOf(c), id);
  }));
  if(!items.length){ sel.innerHTML = "<option value=''>No computers yet</option>"; return; }
  if(prev && [...sel.options].some(o=>o.value===prev)) sel.value = prev;
  applyComputer(items.find(c => (c.computer_id||c.id)===sel.value) || items[0]);
}
async function ensureAwake(){
  if(!state.computerId) return;
  const data = await api("/api/agent-computers/"+state.computerId+"/wake", {method:"POST", body: "{}"});
  applyComputer(data);
}
async function enterSurface(){
  showErr("");
  await ensureAwake();
  if(!ownerHasLease()){
    const req = await api("/api/agent-computers/"+state.computerId+"/takeover", {method:"POST", body: JSON.stringify({reason:"owner takeover"})});
    const token = req.takeover_token || "";
    const connected = await api("/api/agent-computers/"+state.computerId+"/takeover/connect", {method:"POST", body: JSON.stringify({takeover_token: token})});
    applyComputer(Object.assign({}, req, connected, {control:"OWNER_CONTROLLED", control_label:"OWNER_CONTROL", can_resume:true}));
    state.leaseId = connected.lease_id || req.lease_id;
    state.fencingEpoch = connected.fencing_epoch || req.fencing_epoch || state.fencingEpoch;
    state.label = "OWNER_CONTROL";
  }
  state.retries = 0;
  setControlMode(true);
  syncChrome();
  $("surface").focus();
  await openStream();
}
async function giveBack(){
  showErr("");
  closeStream();
  if(document.fullscreenElement){
    try{ await document.exitFullscreen(); }catch(_e){}
  }
  const data = await api("/api/agent-computers/"+state.computerId+"/give-back", {
    method:"POST",
    body: JSON.stringify({lease_id:state.leaseId, fencing_epoch:state.fencingEpoch})
  });
  state.leaseId = "";
  state.canResume = false;
  state.surfaceOpen = false;
  applyComputer(data);
  state.label = data.control_label || "AGENT_CONTROL";
  syncChrome();
}
async function exitFullscreenSafe(){
  if(document.fullscreenElement){
    try{ await document.exitFullscreen(); }catch(_e){}
  }
}
$("refresh").onclick = () => loadList().catch(e=>showErr(e.message));
$("computer").onchange = () => { state.computerId=$("computer").value; state.leaseId=""; state.surfaceOpen=false; closeStream(); loadList().catch(e=>showErr(e.message)); };
$("take").onclick = () => enterSurface().catch(e=>showErr(e.message));
$("suspend").onclick = async () => {
  try{
    const data = await api("/api/agent-computers/"+state.computerId+"/sleep", {method:"POST",body:"{}"});
    applyComputer(data);
    $("statusLine").textContent = state.agentName + " · suspended";
  }catch(e){showErr(e.message);}
};
$("give").onclick = () => giveBack().catch(e=>showErr(e.message));
$("giveOverlay").onclick = () => giveBack().catch(e=>showErr(e.message));
$("giveFs").onclick = () => giveBack().catch(e=>showErr(e.message));
$("fullBtn").onclick = () => { if(!document.fullscreenElement) $("stage").requestFullscreen().catch(()=>{}); };
$("exitFull").onclick = () => exitFullscreenSafe();
$("urlToggle").onclick = () => {
  $("fullUrl").classList.toggle("open");
  $("urlToggle").textContent = $("fullUrl").classList.contains("open") ? "Hide URL" : "Show URL";
};
$("fsUrlToggle").onclick = () => {
  $("fsFullUrl").classList.toggle("open");
  $("fsUrlToggle").textContent = $("fsFullUrl").classList.contains("open") ? "Hide URL" : "Show URL";
};
$("backBtn").onclick = () => sendEvent({type:"nav", action:"back"});
$("fwdBtn").onclick = () => sendEvent({type:"nav", action:"forward"});
$("reloadBtn").onclick = () => sendEvent({type:"nav", action:"reload"});
$("goUrl").onclick = () => {
  const url = $("urlBox").value.trim();
  if(!url) return;
  sendEvent({type:"nav", action:"open", url});
};
$("urlBox").addEventListener("keydown", (ev) => {
  if(ev.key === "Enter"){ ev.preventDefault(); $("goUrl").click(); }
});
$("toggleFallback").onclick = () => $("fallback").classList.toggle("open");
document.addEventListener("keydown", (ev) => {
  if(ev.key !== "Escape") return;
  if(!document.fullscreenElement) return;
  ev.preventDefault();
  ev.stopPropagation();
  exitFullscreenSafe();
}, true);
const surface = $("surface");
function wheelDeltas(ev){
  let dx = ev.deltaX, dy = ev.deltaY;
  if(ev.deltaMode === 1){ dx *= 16; dy *= 16; }
  if(ev.deltaMode === 2){ dx *= 800; dy *= 800; }
  return {delta_x: dx, delta_y: dy};
}
surface.addEventListener("pointerdown", (ev) => {
  if(!ownerHasControl()) return;
  if(ev.button !== 0) return;
  ev.preventDefault();
  surface.setPointerCapture(ev.pointerId);
  state.drawing = true;
  const p = mapPoint(ev);
  if(!p) return;
  const now = Date.now();
  state.clickCount = now - state.lastClickAt < 500 && Math.hypot(ev.clientX-state.lastClickX, ev.clientY-state.lastClickY) < 5 ? Math.min(state.clickCount+1, 3) : 1;
  state.lastClickAt = now; state.lastClickX = ev.clientX; state.lastClickY = ev.clientY;
  state.lastCursorAt = Date.now();
  sendEvent(Object.assign({type:"cursor"}, p));
  sendEvent(Object.assign({type:"pointer", phase:"down", buttons:1, click_count: state.clickCount}, p));
  surface.focus();
});
surface.addEventListener("pointermove", (ev) => {
  if(!ownerHasControl()) return;
  const now = Date.now();
  const p = mapPoint(ev);
  if(!p) return;
  if(now - state.lastCursorAt >= 80){
    state.lastCursorAt = now;
    sendEvent(Object.assign({type:"cursor"}, p));
  }
  if(!state.drawing && now - state.lastMoveAt < 40) return;
  if(state.drawing && now - state.lastMoveAt < 16) return;
  state.lastMoveAt = now;
  sendEvent(Object.assign({type:"pointer", phase:"move", buttons: state.drawing ? 1 : 0}, p));
});
surface.addEventListener("pointerleave", () => {
  if(state.debugPointer) return;
  applyCursor("default");
});
surface.addEventListener("pointerup", (ev) => {
  if(!ownerHasControl()) return;
  state.drawing = false;
  const p = mapPoint(ev);
  if(!p) return;
  sendEvent(Object.assign({type:"pointer", phase:"up", buttons:0, click_count: state.clickCount}, p));
});
surface.addEventListener("wheel", (ev) => {
  if(!ownerHasControl()) return;
  ev.preventDefault();
  const p = mapPoint(ev);
  if(!p) return;
  sendEvent(Object.assign({type:"wheel"}, wheelDeltas(ev), p));
}, {passive:false});
surface.addEventListener("keydown", (ev) => {
  if(!ownerHasControl()) return;
  if(ev.target !== surface) return;
  if((ev.metaKey || ev.ctrlKey) && "lrtwnv".includes(ev.key.toLowerCase())) return;
  ev.preventDefault();
  const mods = (ev.altKey?1:0) | ((ev.ctrlKey || ev.metaKey)?2:0) | (ev.shiftKey?8:0);
  sendEvent({type:"key", phase:"down", key: ev.key, code: ev.code, modifiers: mods});
});
surface.addEventListener("keyup", (ev) => {
  if(!ownerHasControl() || ev.target !== surface) return;
  if((ev.metaKey || ev.ctrlKey) && "lrtwnv".includes(ev.key.toLowerCase())) return;
  ev.preventDefault();
  const mods = (ev.altKey?1:0) | ((ev.ctrlKey || ev.metaKey)?2:0) | (ev.shiftKey?8:0);
  sendEvent({type:"key", phase:"up", key: ev.key, code: ev.code, modifiers: mods});
});
surface.addEventListener("paste", (ev) => {
  if(!ownerHasControl()) return;
  ev.preventDefault();
  const text = ev.clipboardData && ev.clipboardData.getData("text/plain");
  if(text) sendEvent({type:"text", text});
});
$("sendText").onclick = async () => {
  const text = $("typeBox").value;
  if(!text) return;
  sendEvent({type:"text", text});
  $("typeBox").value = "";
};
$("scrollUp").onclick = () => sendEvent({type:"wheel", x: 200, y: 200, delta_x:0, delta_y:-240, client_width:state.viewport.width, client_height:state.viewport.height});
$("scrollDown").onclick = () => sendEvent({type:"wheel", x: 200, y: 200, delta_x:0, delta_y:240, client_width:state.viewport.width, client_height:state.viewport.height});
applyCursor("default");
loadList().catch(e=>showErr(e.message));
</script>
</body>
</html>
"""
