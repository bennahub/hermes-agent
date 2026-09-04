"""Owner-facing Human Takeover page.

Served by the existing authenticated dashboard. No CDP, no raw ports,
no second application. Human language only.
"""

from __future__ import annotations

COMPUTER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Agent needs you — Hermes</title>
<style>
:root{--bg:#10110f;--panel:#1a1c18;--ink:#f4f1e8;--muted:#9a9484;--gold:#c9a227;--ok:#6fbf73;--line:#2c2e28}
*{box-sizing:border-box}body{margin:0;font-family:ui-sans-serif,system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:16px;align-items:center}
h1{font-size:1.1rem;margin:0}
.status{color:var(--muted)}
main{display:grid;grid-template-columns:minmax(0,1fr) 280px;min-height:calc(100vh - 64px)}
@media(max-width:800px){main{grid-template-columns:1fr}}
.screen{padding:16px;display:flex;flex-direction:column;gap:12px}
.frame{background:#000;border:1px solid var(--line);border-radius:8px;min-height:360px;display:flex;align-items:center;justify-content:center;overflow:hidden}
.frame img{max-width:100%;cursor:crosshair;display:block}
.hint{color:var(--muted);font-size:.85rem}
aside{border-left:1px solid var(--line);padding:16px;background:var(--panel)}
button,select,textarea{width:100%;margin:6px 0;padding:10px 12px;border-radius:8px;border:1px solid var(--line);background:#111;color:var(--ink);font:inherit}
button.primary{background:var(--gold);color:#111;border:0;font-weight:600}
button.ok{background:var(--ok);color:#111;border:0;font-weight:600}
button:disabled{opacity:.45}
label{display:block;color:var(--muted);font-size:.8rem;margin-top:10px}
.banner{padding:10px 12px;border-radius:8px;background:#2a2110;color:#f3d27a}
.err{color:#f0a0a0;font-size:.85rem;min-height:1.2em}
</style>
</head>
<body>
<header>
  <h1 id="headline">Waiting for an agent</h1>
  <div class="status" id="statusLine">No computer selected</div>
</header>
<main>
  <section class="screen">
    <div class="banner" id="banner">Open a computer, then take control only when the agent needs you.</div>
    <div class="frame">
      <img id="shot" alt="Current agent screen" hidden/>
      <div class="hint" id="emptyShot">No live screen yet</div>
    </div>
    <div class="hint" id="viewMeta"></div>
  </section>
  <aside>
    <label for="computer">Computer</label>
    <select id="computer"></select>
    <button type="button" id="refresh">Refresh list</button>
    <button type="button" class="primary" id="take">Take Control</button>
    <button type="button" class="ok" id="give" disabled>Give Control Back</button>
    <label for="typeBox">Type into the page</label>
    <textarea id="typeBox" rows="3" placeholder="Text the page should receive"></textarea>
    <button type="button" id="sendText" disabled>Type</button>
    <button type="button" id="scrollUp" disabled>Scroll up</button>
    <button type="button" id="scrollDown" disabled>Scroll down</button>
    <div class="err" id="err"></div>
  </aside>
</main>
<script>
const $ = (id) => document.getElementById(id);
const state = {computerId:"",leaseId:"",fencingEpoch:0,authority:"",agentName:"Agent",viewport:{width:800,height:600}};
function headline(){
  const name = state.agentName || "Agent";
  if(state.authority === "OWNER_CONTROLLED") return "You have control";
  if(state.authority === "TAKEOVER_PENDING" || state.authority === "YIELDING") return name + " needs you";
  if(state.authority === "RETURNING") return name + " resumed";
  return name + " is working";
}
function banner(){
  if(state.authority === "OWNER_CONTROLLED") return "You have control. Click the screen, type, or scroll. Give Control Back when finished.";
  if(state.authority === "TAKEOVER_PENDING") return state.agentName + " needs you — take control of the same screen.";
  if(state.authority === "RETURNING") return state.agentName + " resumed from the same place.";
  return "The agent keeps ordinary work. Take Control only at a human checkpoint.";
}
function showErr(msg){ $("err").textContent = msg || ""; }
async function api(path, opts){
  const r = await fetch(path, Object.assign({credentials:"same-origin", headers:{"Content-Type":"application/json"}}, opts||{}));
  const data = await r.json().catch(()=>({}));
  if(r.status === 401){ location.href = "/login?next=/computer"; throw new Error("Sign in required"); }
  if(!r.ok) throw new Error(data.detail && data.detail.message || data.detail || data.error || ("HTTP "+r.status));
  return data;
}
function ownerHasControl(){ return state.authority === "OWNER_CONTROLLED" && state.leaseId; }
function syncButtons(){
  const on = ownerHasControl();
  $("give").disabled = !on;
  $("sendText").disabled = !on;
  $("scrollUp").disabled = !on;
  $("scrollDown").disabled = !on;
  $("headline").textContent = headline();
  $("banner").textContent = banner();
}
function applyComputer(c){
  state.computerId = c.computer_id || c.id || state.computerId;
  state.authority = c.control || c.control_authority || "";
  state.fencingEpoch = c.fencing_epoch || state.fencingEpoch;
  if(c.lease_id) state.leaseId = c.lease_id;
  if(c.lease && c.lease.lease_id) state.leaseId = c.lease.lease_id;
  const profile = (c.agent_profile_id || "").replace(/^agent:/,"");
  state.agentName = profile ? profile.replace(/(^|[-_])(\\w)/g, (_,a,b)=> (a?" ":"")+b.toUpperCase()) : "Agent";
  $("statusLine").textContent = (state.agentName+" · "+(state.authority||"idle")).trim();
  syncButtons();
}
async function loadList(){
  const data = await api("/api/agent-computers");
  const items = data.computers || [];
  const sel = $("computer");
  const prev = sel.value || new URLSearchParams(location.search).get("computer") || "";
  sel.innerHTML = items.map(c => {
    const id = c.computer_id || c.id;
    const label = (c.agent_profile_id||id) + " — " + (c.control||c.control_authority||"");
    return `<option value="${id}">${label}</option>`;
  }).join("");
  if(!items.length){ sel.innerHTML = "<option value=''>No computers yet</option>"; return; }
  if(prev && [...sel.options].some(o=>o.value===prev)) sel.value = prev;
  state.computerId = sel.value;
  applyComputer(items.find(c => (c.computer_id||c.id)===sel.value) || items[0]);
  ensureAwake().then(() => observe()).catch(e=>showErr(e.message));
}
async function ensureAwake(){
  if(!state.computerId) return;
  const data = await api("/api/agent-computers/"+state.computerId+"/wake", {method:"POST", body: "{}"});
  applyComputer(data);
}
async function observe(){
  if(!state.computerId) return;
  const data = await api("/api/agent-computers/"+state.computerId+"/observe", {
    method:"POST",
    body: JSON.stringify({lease_id:state.leaseId || "", fencing_epoch:state.fencingEpoch || 0})
  });
  state.fencingEpoch = data.fencing_epoch || state.fencingEpoch;
  if(data.viewport) state.viewport = data.viewport;
  if(data.screenshot && data.screenshot.data){
    $("shot").src = "data:"+(data.screenshot.mime||"image/jpeg")+";base64,"+data.screenshot.data;
    $("shot").hidden = false;
    $("emptyShot").hidden = true;
  }
  $("viewMeta").textContent = [data.title, data.url].filter(Boolean).join(" · ");
  if(data.controller) $("statusLine").textContent = headline();
}
async function act(body){
  return api("/api/agent-computers/"+state.computerId+"/act", {
    method:"POST",
    body: JSON.stringify(Object.assign({lease_id:state.leaseId, fencing_epoch:state.fencingEpoch}, body))
  });
}
$("refresh").onclick = () => loadList().catch(e=>showErr(e.message));
$("computer").onchange = () => { state.computerId=$("computer").value; state.leaseId=""; loadList().catch(e=>showErr(e.message)); };
$("take").onclick = async () => {
  try{
    showErr("");
    await ensureAwake();
    const req = await api("/api/agent-computers/"+state.computerId+"/takeover", {method:"POST", body: JSON.stringify({reason:"owner takeover"})});
    const token = req.takeover_token || "";
    const connected = await api("/api/agent-computers/"+state.computerId+"/takeover/connect", {method:"POST", body: JSON.stringify({takeover_token: token})});
    applyComputer(Object.assign({}, req, connected));
    state.leaseId = connected.lease_id || req.lease_id;
    state.fencingEpoch = connected.fencing_epoch || req.fencing_epoch || state.fencingEpoch;
    state.authority = "OWNER_CONTROLLED";
    syncButtons();
    await observe();
  }catch(e){ showErr(e.message); }
};
$("give").onclick = async () => {
  try{
    showErr("");
    const data = await api("/api/agent-computers/"+state.computerId+"/give-back", {
      method:"POST",
      body: JSON.stringify({lease_id:state.leaseId, fencing_epoch:state.fencingEpoch})
    });
    applyComputer(data);
    state.leaseId = "";
    state.authority = data.control || data.control_authority || "RETURNING";
    syncButtons();
    $("headline").textContent = (state.agentName||"Agent")+" resumed";
  }catch(e){ showErr(e.message); }
};
$("shot").onclick = async (ev) => {
  if(!ownerHasControl()) return;
  const img = ev.currentTarget;
  const rect = img.getBoundingClientRect();
  const x = (ev.clientX-rect.left) * ((img.naturalWidth||state.viewport.width)/rect.width);
  const y = (ev.clientY-rect.top) * ((img.naturalHeight||state.viewport.height)/rect.height);
  try{ await act({kind:"pointer_click", x, y}); await observe(); }catch(e){ showErr(e.message); }
};
$("sendText").onclick = async () => {
  const text = $("typeBox").value;
  if(!text) return;
  try{ await act({kind:"text", text}); $("typeBox").value=""; await observe(); }catch(e){ showErr(e.message); }
};
$("scrollUp").onclick = async () => { try{ await act({kind:"scroll", delta_y:-240}); await observe(); }catch(e){ showErr(e.message); } };
$("scrollDown").onclick = async () => { try{ await act({kind:"scroll", delta_y:240}); await observe(); }catch(e){ showErr(e.message); } };
loadList().catch(e=>showErr(e.message));
setInterval(() => { if(ownerHasControl()) observe().catch(()=>{}); }, 2000);
</script>
</body>
</html>
"""
