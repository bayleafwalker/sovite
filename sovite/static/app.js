/* Sovite operator console. The API is the source of operational state.
   Private artifacts are fetched only after an explicit operator action. */
"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const items = value => Array.isArray(value) ? value : Array.isArray(value?.items) ? value.items : [];
const pretty = value => JSON.stringify(value, null, 2);
const textValue = value => value == null ? "Not declared" : typeof value === "object" ? JSON.stringify(value) : String(value);
const shortId = value => value ? String(value).replace(/^sha256:/, "").slice(0, 12) : "—";
const titleCase = value => String(value ?? "Unknown").replaceAll("_", " ").replaceAll("-", " ").replace(/\b\w/g, c => c.toUpperCase());
const formatNumber = value => typeof value === "number" ? value.toLocaleString() : "—";
const numbered = (value, singular, plural = singular + "s") => `${formatNumber(value)} ${value === 1 ? singular : plural}`;
const when = value => value ? new Date(Number(value) * 1000).toLocaleString(undefined, {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const fullTime = value => value ? new Date(Number(value) * 1000).toLocaleString(undefined, {dateStyle:"medium",timeStyle:"medium"}) : "Not recorded";
const relativeTime = value => {
  if (!value) return "Not recorded";
  const seconds = Math.round(Date.now() / 1000 - Number(value));
  if (seconds < 0) return `in ${Math.ceil(-seconds / 60)} min`;
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
};

const ICONS = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="4" rx="1.5"/><rect x="14" y="11" width="7" height="10" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/>',
  deployments:'<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  catalog:'<path d="M3 5a2 2 0 0 1 2-2h5v8H3V5ZM14 3h5a2 2 0 0 1 2 2v6h-7V3ZM3 15h7v6H5a2 2 0 0 1-2-2v-4ZM14 15h7v4a2 2 0 0 1-2 2h-5v-6Z"/>',
  grants:'<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z"/><path d="m8 12 3 3 5-6"/>',
  jobs:'<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m10 8 6 4-6 4V8Z"/>',
  receipts:'<path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3Z"/><path d="M9 7h6M9 11h6M9 15h3"/>',
  qualification:'<path d="m12 3 3 2 4 1 1 4-1 4-4 2-3 2-3-2-4-2-1-4 1-4 4-1 3-2Z"/><path d="m9 10 2 2 4-4M8 17l-1 5 5-2 5 2-1-5"/>',
  audit:'<path d="M7 3h10l3 3v15H4V3h3ZM15 3v5h5M8 12h8M8 16h8"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.6-1L20 9M4 15l2.3 3A7 7 0 0 0 17.9 17"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  worker:'<rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01M8 17.5h.01M12 7h5M12 18h5"/>',
  models:'<path d="m12 3 8 4v10l-8 4-8-4V7l8-4ZM4 7l8 5 8-5M12 12v9"/>',
  harnesses:'<path d="M8 4H5v16h3M16 4h3v16h-3M10 8l-3 4 3 4M14 8l3 4-3 4"/>',
  agents:'<rect x="4" y="7" width="16" height="13" rx="3"/><path d="M12 3v4M9 12h.01M15 12h.01M9 16h6M2 12v4M22 12v4"/>',
  tools:'<path d="M14 6a5 5 0 0 0-6 6L3 17a2.8 2.8 0 0 0 4 4l5-5a5 5 0 0 0 6-6l-4 4-4-4 4-4Z"/>',
  compute:'<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  destinations:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z"/>',
  data_classes:'<path d="M20 12 12 20l-9-9V3h8l9 9Z"/><circle cx="7.5" cy="7.5" r="1"/>',
  profiles:'<path d="M4 7h16M4 17h16M8 4v6M16 14v6"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  warning:'<path d="m12 3 10 18H2L12 3ZM12 9v5M12 17h.01"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v6l4 2"/>',
  pause:'<path d="M8 5v14M16 5v14"/>',
  play:'<path d="m7 4 14 8-14 8V4Z"/>',
  stop:'<rect x="5" y="5" width="14" height="14" rx="2"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  logout:'<path d="M9 4H4v16h5M9 12h12m-4-4 4 4-4 4"/>',
  menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  key:'<circle cx="8" cy="8" r="5"/><path d="m11.5 11.5 9 9M17 17l3-3M14 14l3-3"/>',
  network:'<rect x="8" y="3" width="8" height="6" rx="1"/><rect x="2" y="16" width="7" height="5" rx="1"/><rect x="15" y="16" width="7" height="5" rx="1"/><path d="M12 9v4M5.5 16v-3h13v3"/>',
  package:'<path d="m3 7 9-4 9 4-9 5-9-5ZM3 7v10l9 4 9-4V7M12 12v9M8 5l9 5"/>',
};
function icon(name, size = 18) { return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.package}</svg>`; }
function mark() { return '<svg class="brand-mark" viewBox="0 0 40 40" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 28V14L20 6l14 8v14M12 31V19h16v12M6 34h28M20 6v7M17 25h6"/></svg>'; }

const ROUTES = {
  overview: {label:"Overview", group:"Operate", description:"Control plane"},
  deployments: {label:"Deployments", group:"Operate", description:"Platform configuration"},
  catalog: {label:"Component catalog", group:"Operate", description:"Composable infrastructure"},
  grants: {label:"Flow grants", group:"Operate", description:"Authority & placement"},
  jobs: {label:"Tasks", group:"Work", description:"Durable execution"},
  receipts: {label:"Receipts", group:"Work", description:"Controlled release"},
  qualification: {label:"Qualification", group:"Assure", description:"Evidence & release"},
  audit: {label:"Audit trail", group:"Assure", description:"Operational history"},
};
const CATALOG_LABELS = {models:"Models",harnesses:"Harnesses",agents:"Agents",tools:"Tools",compute:"Compute",destinations:"Destinations",data_classes:"Data classes",profiles:"Profiles"};
const ENDPOINTS = {catalog:"/api/catalog",grants:"/api/grants",jobs:"/api/jobs?limit=100",receipts:"/api/receipts",audit:"/api/audit?limit=200",qualifications:"/api/qualifications",deployments:"/api/deployments"};
const TERMINAL = new Set(["completed","failed","cancelled","blocked"]);
const state = {authenticated:false,loading:false,hasLoaded:false,overview:null,data:{},errors:{},route:"overview",search:{},filters:{jobs:"all",grants:"all",receipts:"all"},catalogTab:"models",dialog:null,dialogTrigger:null,menuOpen:false,lastSync:null,online:true,autoRefresh:true};
let refreshPromise = null;
let pollTimer = null;

class ApiError extends Error {
  constructor(message, status = 0, code = "request_failed") {super(message);this.status=status;this.code=code;}
}
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {credentials:"same-origin",cache:"no-store",...options,headers:{Accept:"application/json",...(options.body ? {"Content-Type":"application/json"}:{}),...(options.headers||{})}});
  } catch (_) { throw new ApiError("The control plane could not be reached. Check connectivity and try again.", 0, "unreachable"); }
  let body;
  try { body = await response.json(); } catch (_) { body = null; }
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : typeof detail?.message === "string" ? detail.message : `The request failed (${response.status}).`;
    if (response.status === 401 && path !== "/api/auth/session") expireSession();
    throw new ApiError(message,response.status,detail?.code || "request_failed");
  }
  return body;
}
function post(path, body) {return api(path,{method:"POST",body:JSON.stringify(body)});}
function expireSession() {
  if (!state.authenticated) return;
  state.authenticated = false;
  state.data = {};
  state.overview = null;
  state.hasLoaded = false;
  closeDialog(false);
  renderLogin("Your operator session has ended. Sign in to continue.");
}
function notify(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error":""}`;
  node.innerHTML = `${icon(error?"warning":"check",17)}<span>${esc(message)}</span><button aria-label="Dismiss notification">${icon("close",14)}</button>`;
  node.querySelector("button").addEventListener("click",()=>node.remove());
  $("#notifications").appendChild(node);
  setTimeout(()=>node.remove(),error?10000:6000);
}
function rowLabel(item) {return item?.name || item?.id || "Unnamed component";}
function catalogItems(key) {return items(state.data.catalog?.[key]);}
function findCatalog(key, id) {return catalogItems(key).find(item=>item.id===id);}
function refLabel(key,id) {return rowLabel(findCatalog(key,id)||{id:id||"Not bound"});}
function grantItems() {return items(state.data.grants);}
function jobs() {return items(state.data.jobs);}
function isSynthetic(agentId) {
  const agent=findCatalog("agents",agentId), harness=findCatalog("harnesses",agent?.harness), model=findCatalog("models",agent?.model);
  return harness?.kind === "demo" || model?.adapter === "demo";
}
function grantState(grant) {
  if (grant.inhibited) return "inhibited";
  if (grant.enabled === false) return "disabled";
  if (state.overview?.qualification_required && !grant.qualified) return "unqualified";
  return "enabled";
}
function pill(status, label) {
  const classes={completed:"good",running:"info",queued:"warn",retry_wait:"warn",failed:"bad",cancelled:"neutral",blocked:"bad",enabled:"good",inhibited:"bad",disabled:"neutral",unqualified:"warn",valid:"good",invalid:"bad",expired:"warn",current:"good",historical:"neutral",healthy:"good",stale:"warn",development:"warn",enforced:"info",synthetic:"warn",configured:"neutral",released:"good",withheld:"warn",pending:"warn",working:"info",available:"good",unavailable:"neutral"};
  return `<span class="pill ${classes[status]||"neutral"}"><span class="dot"></span>${esc(label||titleCase(status))}</span>`;
}
function kv(rows, classes = "") {return `<dl class="kv-list ${classes}">${rows.filter(row=>row.length>1).map(([key,value])=>`<dt>${esc(key)}</dt><dd>${value}</dd>`).join("")}</dl>`;}
function dataClass(value) {return value ? `<span class="class-pill" title="${esc(findCatalog("data_classes",value)?.description||refLabel("data_classes",value))}">${esc(value)}</span>` : '<span class="muted">Not declared</span>';}
function safeCode(value) {return `<code>${esc(value || "Not declared")}</code>`;}
function generationCode(value, full = false) {return `<code title="${esc(value||"")}">${esc(full ? value : shortId(value))}</code>`;}
function jsonDetails(value, label = "View declared configuration", open = false) {return `<details class="code-details"${open?" open":""}><summary>${esc(label)}</summary><pre>${esc(pretty(value))}</pre></details>`;}
function empty(title, description, name = "package", action = "", compact = false) {return `<div class="empty-state${compact?" compact":""}"><div class="empty-icon">${icon(name,20)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;}
function errorPanel(key) {
  const error=state.errors[key];
  if (!error) return "";
  return `<div class="panel"><div class="empty-state error"><div class="empty-icon">${icon("warning",21)}</div><h3>This view could not be loaded</h3><p>${esc(error.message)}</p><button class="button" data-action="refresh">${icon("refresh",15)}Retry</button></div></div>`;
}
function newTaskButton(small = false) {return `<button class="button primary${small?" small":""}" data-action="new-job">${icon("plus",16)}New task</button>`;}
function heading(eyebrow,title,description,action="") {return `<div class="page-heading"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${action?`<div class="page-actions">${action}</div>`:""}</div>`;}
function searchToolbar(route,placeholder,count,select = "") {return `<div class="toolbar"><div class="toolbar-left"><div class="search-box">${icon("search",16)}<label class="sr-only" for="${route}-search">${esc(placeholder)}</label><input id="${route}-search" type="search" data-search="${route}" autocomplete="off" placeholder="${esc(placeholder)}" value="${esc(state.search[route]||"")}"></div>${select}</div><span class="result-count">${formatNumber(count)} ${count===1?"item":"items"}</span></div>`;}
function statusSelect(route, statuses) {return `<label class="sr-only" for="${route}-status">Filter by status</label><select class="filter-select" id="${route}-status" data-filter="${route}"><option value="all">All statuses</option>${statuses.map(status=>`<option value="${esc(status)}"${state.filters[route]===status?" selected":""}>${esc(titleCase(status))}</option>`).join("")}</select>`;}
function matchSearch(item,route) {return !state.search[route] || JSON.stringify(item).toLowerCase().includes(state.search[route].toLowerCase());}
function linkButton(action, id, text, extra = "") {return `<button class="row-title${extra ? " "+esc(extra):""}" data-action="${action}" data-id="${esc(id)}">${esc(text)}</button>`;}

function renderLogin(message = "") {
  document.title = "Sovite · Sign in";
  $("#app").innerHTML = `<div class="login-page"><section class="login-story" aria-label="Sovite platform"><div class="brand">${mark()}<div><div class="brand-name">Sovite</div><div class="brand-sub">Platform operations</div></div></div><div class="login-story-content"><div class="eyebrow">An estate, assembled.</div><h1>Models. Agents.<br>A place to operate.</h1><p>Compose the model, harness, and compute you need. Keep execution, authority, and results within an explicit operating boundary.</p><div class="login-primitives"><div class="login-primitive">${icon("models")}Model serving</div><div class="login-primitive">${icon("harnesses")}Native harnesses</div><div class="login-primitive">${icon("grants")}Flow authority</div><div class="login-primitive">${icon("receipts")}Durable execution</div></div></div><div class="login-story-footer">Configurable packages. Versioned deployments.<br>One operator view across the platform.</div></section><main class="login-form-area"><div class="login-form-wrap"><div class="login-form-icon">${icon("key",21)}</div><h2>Enter your workspace</h2><p>Sign in with an operator token to inspect the platform, manage authority, and work with private results.</p><form id="login-form"><div class="form-error" id="login-error" role="alert">${esc(message)}</div><div class="form-field"><label for="operator-token">Operator token</label><div class="password-wrap"><input id="operator-token" name="token" type="password" required minlength="24" autocomplete="current-password" spellcheck="false" placeholder="Enter your operator token" autofocus><button class="icon-button" type="button" data-action="toggle-token" aria-label="Show operator token" aria-pressed="false">${icon("eye",18)}</button></div><p class="help">Use the token created for this installation.</p></div><button class="button primary" type="submit">Sign in to platform${icon("arrow",16)}</button></form><div class="login-help"><strong>Private operator access</strong><br>Your token establishes a server-managed session. This console does not store it in browser local storage. Caller credentials do not grant access to private tasks or artifacts.</div></div></main></div><div class="login-corner">Sovite · Operator console</div>`;
}
function renderShell() {
  if (!state.authenticated && state.hasLoaded) return;
  const name=state.overview?.name||"Sovite", route=ROUTES[state.route]||ROUTES.overview;
  document.title=`${route.label} · ${name}`;
  let nav="", group="";
  for (const [key,value] of Object.entries(ROUTES)) {
    if (value.group!==group) {nav+=`${group?"</div>":""}<div class="nav-section-label">${value.group}</div><div class="nav-list">`;group=value.group;}
    const count=key==="jobs" ? (state.overview?.counts?.running||0)+(state.overview?.counts?.queued||0) : 0;
    nav+=`<a class="nav-item${state.route===key?" active":""}" href="#${key}"${state.route===key?' aria-current="page"':""}>${icon(key)}<span>${value.label}</span>${count?`<span class="nav-count">${count}</span>`:""}</a>`;
  }
  nav+="</div>";
  $("#app").innerHTML=`<div class="shell${state.menuOpen?" menu-open":""}"><button class="mobile-backdrop" data-action="close-menu" aria-label="Close navigation"></button><aside class="sidebar"><a href="#overview" class="brand" aria-label="${esc(name)} overview">${mark()}<div><div class="brand-name">${esc(name)}</div><div class="brand-sub">Platform operations</div></div></a><div class="workspace-indicator"><span class="dot ${state.online?"good":"warn"}"></span><span class="truncate">${state.online?"Operator workspace":"Connection interrupted"}</span></div><nav aria-label="Main navigation">${nav}</nav><div class="sidebar-bottom"><div class="rail-rule"></div><div class="operator-identity"><div class="avatar">${icon("user",16)}</div><div><div>Platform operator</div><div class="identity-sub">Private boundary access</div></div><button class="rail-signout" data-action="logout" aria-label="Sign out" title="Sign out">${icon("logout",16)}</button></div><div class="rail-footer">State is read from the control plane.<br>Configuration remains versioned.</div></div></aside><div class="main-wrap"><header class="topbar"><div class="breadcrumb"><button class="icon-button mobile-toggle" data-action="toggle-menu" aria-label="Open navigation" aria-expanded="${state.menuOpen}">${icon("menu",20)}</button><span class="workspace-label">Workspace</span><span class="divider workspace-separator">/</span><strong>${esc(route.label)}</strong></div><div class="topbar-actions"><span class="sync-state" title="${state.lastSync?`Last synchronized ${new Date(state.lastSync).toLocaleTimeString()}`:"Connecting to the control plane"}"><span class="dot ${state.online?"good":"warn"}"></span><span class="sync-text">${state.loading?"Synchronizing":state.lastSync?`Updated ${new Date(state.lastSync).toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"})}`:"Connecting"}</span></span><button class="icon-button${state.loading?" spinning":""}" data-action="refresh" title="Refresh platform state" aria-label="Refresh platform state"${state.loading?" disabled":""}>${icon("refresh",17)}</button><span class="topbar-divider"></span><span class="environment-label">${esc(state.overview?.mode||"Connecting")}</span></div></header><main class="main" id="main" tabindex="-1">${renderContent()}</main></div></div>`;
}
function renderContent() {
  if (!state.overview) return `${heading("Connecting","Opening the workspace","Loading the current configuration and operational state.")}<div class="stats-grid">${Array.from({length:4},()=>'<div class="skeleton-stat"><div class="loading-line"></div><div class="loading-line"></div><div class="loading-line"></div></div>').join("")}</div><div class="skeleton-panel">${Array.from({length:5},()=>'<div class="loading-line"></div>').join("")}</div>`;
  const views={overview:viewOverview,deployments:viewDeployments,catalog:viewCatalog,grants:viewGrants,jobs:viewJobs,receipts:viewReceipts,qualification:viewQualification,audit:viewAudit};
  const warning=!state.online?`<div class="global-warning">${icon("warning",17)}<span>Connection interrupted. Values below were last synchronized ${state.lastSync?new Date(state.lastSync).toLocaleTimeString():"earlier"}; they may no longer reflect current authority or task state.</span><button class="button small" data-action="refresh">Retry</button></div>`:"";
  return warning+(views[state.route]||viewOverview)()+`<footer class="content-footer"><span>${icon("lock",12)}Operator view · private data boundary</span><span>Bundle <code title="${esc(state.overview.generation)}">${esc(shortId(state.overview.generation))}</code> · ${esc(state.overview.name||"Sovite")}</span></footer>`;
}
function renderPage(preserveFocus = false) {
  const focused=document.activeElement, id=focused?.id, start=focused?.selectionStart, end=focused?.selectionEnd;
  const main=$("#main");
  if (!main) {renderShell();return;}
  main.innerHTML=renderContent();
  if (preserveFocus && id) {
    const replacement=document.getElementById(id);
    replacement?.focus({preventScroll:true});
    if (typeof start==="number" && replacement?.setSelectionRange) {try{replacement.setSelectionRange(start,end);}catch(_){}}
  }
}

function viewOverview() {
  const overview=state.overview, counts=overview.counts||{}, grants=grantItems(), active=grants.filter(g=>grantState(g)==="enabled").length;
  const required=overview.qualification_required, qualified=required && overview.qualified;
  const title=qualified?"Current generation is qualified":required?"Qualification is required for this generation":"Development mode · qualification optional";
  const text=qualified?"Execution still depends on the current grant, its runtime inhibit, and the selected worker. Qualification alone does not authorize a release.":required?"Dispatch and release require valid evidence for the loaded bundle. Inspect the qualification record before submitting work.":"Assess the installation as a whole. Synthetic adapters are labeled, and model or cluster readiness is reported only where observed.";
  const workerList=items(overview.workers), recent=jobs().slice(0,5), synthetic=catalogItems("models").filter(m=>m.adapter==="demo").length;
  return heading("Control plane","Operating overview","Serving, agent execution, and flow authority — assembled into one operational view.",newTaskButton())+
    `<div class="release-banner ${qualified?"":required?"warn":"warn"}"><div class="release-symbol">${icon(qualified?"qualification":"info",22)}</div><div><h2>${title}</h2><p>${text}</p></div><div class="release-meta"><span>CONFIGURATION GENERATION</span>${generationCode(overview.generation)}</div></div>`+
    `<div class="stats-grid">${stat("Active execution",counts.running??0,"jobs",`${formatNumber(counts.queued??0)} queued · ${numbered(counts.jobs??0,"task")} in total`,"jobs")}${stat("Agent estate",counts.agents??catalogItems("agents").length,"agents",`${numbered(counts.models??catalogItems("models").length,"model")} · ${numbered(counts.harnesses??catalogItems("harnesses").length,"harness","harnesses")}`,"catalog")}${stat("Enabled flow grants",state.errors.grants?null:active,"grants",`${grants.filter(g=>g.inhibited).length} inhibited · ${grants.length} declared`,"grants")}${stat("Completed tasks",counts.completed??0,"receipts",`${formatNumber(counts.blocked??0)} blocked · private result retention`,"jobs")}</div>`+
    `<div class="two-columns"><div class="panel"><div class="panel-header"><div><h2>Recent tasks</h2><p class="subtitle">Durable work, from admission to result</p></div><a class="text-link" href="#jobs">All tasks${icon("arrow",14)}</a></div>${state.errors.jobs?empty("Tasks unavailable",state.errors.jobs.message,"warning","",true):recent.length?jobsTable(recent,true):empty("No tasks yet","Start a task with a configured flow grant to exercise execution, private retention, and receipt release.","jobs",newTaskButton(true))}</div><div class="panel"><div class="panel-header"><div><h2>Worker activity</h2><p class="subtitle">Observed process heartbeats</p></div>${pill(workerList.some(w=>w.healthy)?"healthy": "stale",workerList.some(w=>w.healthy)?"Receiving heartbeats":"No healthy heartbeat")}</div><div class="panel-body">${workerList.length?workerList.map(worker=>`<div class="worker-row"><div class="worker-icon">${icon("worker",17)}</div><div class="worker-text"><div class="worker-title mono break">${esc(worker.id)}</div><div class="worker-sub">${worker.job_id?`Executing ${esc(shortId(worker.job_id))}`:"No task assigned"} · heartbeat ${esc(relativeTime(worker.updated_at))}</div></div>${pill(worker.healthy?"healthy":"stale",worker.healthy?"Active":"Stale")}</div>`).join(""):empty("No worker observed","Start the worker service to process the durable queue. A configured component does not imply a running worker.","worker","",true)}</div></div></div>`+
    `<div class="two-columns equal"><div class="panel"><div class="panel-header"><div><h2>Component placement</h2><p class="subtitle">Independent packages, explicit destinations</p></div><a href="#catalog" class="text-link">Explore catalog${icon("arrow",14)}</a></div><div class="panel-body"><div class="inventory-grid">${[["compute","Compute profiles"],["destinations","Destinations"],["tools","Tools"]].map(([key,label])=>`<div class="inventory-block"><div class="number">${state.errors.catalog?"—":catalogItems(key).length}</div><div class="label">${label}</div></div>`).join("")}</div><div class="inventory-foot"><p>Declared information classes</p><div class="tags">${catalogItems("data_classes").map(c=>`<span class="class-pill" title="${esc(c.description||"")}">${esc(c.id)} · ${esc(c.name||`rank ${c.rank}`)}</span>`).join("")||'<span class="muted">No classes loaded</span>'}</div></div>${synthetic?`<div class="boundary-note warning small-gap">${icon("info",16)}<span>${synthetic} synthetic ${synthetic===1?"model is":"models are"} configured for installation assessment. Synthetic completion does not measure model quality or prove upstream readiness.</span></div>`:""}</div></div><div class="panel"><div class="panel-header"><div><h2>Current operating boundary</h2><p class="subtitle">State and release posture</p></div>${icon("grants",19)}</div><div class="panel-body">${kv([["Private artifact store",esc(titleCase(overview.storage?.backend||"unknown"))],["Qualification",pill(qualified?"valid":required?"unqualified":"development",qualified?"Valid":required?"Required":"Optional in development")],["Audit chain",overview.audit?.valid===true?pill("valid",`${overview.audit.events??0} events verified`):overview.audit?.valid===false?pill("invalid","Verification failed"):'<span class="muted">Not reported</span>'],["Session transport",esc(overview.security?.secure_cookies?"Secure cookie":"Development HTTP cookie")]])}<div class="kv-divider"></div><div class="boundary-note private">${icon("lock",16)}<span>Task output stays in private artifact storage. The caller interface exposes a trusted receipt and fixed status; it has no result-fetching method.</span></div></div></div></div>`;
}
function stat(label,value,iconName,description,route) {return `<div class="stat-card"><div class="stat-heading"><span>${esc(label)}</span>${icon(iconName,18)}</div><div class="stat-value">${formatNumber(value)}</div><p class="stat-description">${esc(description)} <a class="metric-link" href="#${route}" aria-label="Open ${esc(ROUTES[route].label)}">${icon("arrow",12)}</a></p></div>`;}
function jobsTable(list,compact = false) {return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Task / flow grant</th><th>Status</th>${compact?"":"<th>Caller</th><th>Attempts</th>"}<th>Created</th><th class="narrow"><span class="sr-only">Details</span></th></tr></thead><tbody>${list.map(job=>`<tr><td>${linkButton("open-job",job.id,shortId(job.id))}<div class="row-subtitle">${esc(refLabelFromGrant(job.grant_id))}</div></td><td>${pill(job.status)}${job.cancel_requested&&!TERMINAL.has(job.status)?'<div class="row-subtitle">Cancellation requested</div>':""}</td>${compact?"":`<td>${esc(job.caller)}</td><td>${esc(job.attempts??0)}</td>`}<td class="nowrap"><span title="${esc(fullTime(job.created_at))}">${esc(relativeTime(job.created_at))}</span></td><td class="narrow"><button class="icon-button" data-action="open-job" data-id="${esc(job.id)}" aria-label="Open task ${esc(shortId(job.id))}">${icon("chevron",15)}</button></td></tr>`).join("")}</tbody></table></div>`;}
function refLabelFromGrant(id) {return rowLabel(grantItems().find(g=>g.id===id)||{id});}

function viewDeployments() {
  const source=items(state.data.deployments),list=source.filter(item=>matchSearch(item,"deployments"));
  return heading("Platform configuration","Deployments","Loaded bundle revisions bind the configured model, harness, agent, compute, and flow policy.")+
    `<div class="boundary-note small-gap no-margin">${icon("info",16)}<span>These records describe loaded configuration. They do not assert that Kubernetes has reconciled, GPUs are available, or upstream services are healthy. Apply and qualify changes with the deployment tooling.</span></div><div class="section-gap">${searchToolbar("deployments","Find a generation or release…",list.length)}</div>`+
    (state.errors.deployments?errorPanel("deployments"):list.length?list.map(deployment=>`<article class="deployment-card"><div class="deployment-heading"><div><h2>${esc(deployment.name||"Platform bundle")}</h2><p>Loaded ${esc(fullTime(deployment.created_at))}</p></div>${pill(deployment.current?"current":"historical",deployment.current?"Current configuration":"Previous configuration")}</div><div class="deployment-fields"><div><div class="field-caption">Generation</div><div class="field-value mono" title="${esc(deployment.generation)}">${esc(shortId(deployment.generation))}</div></div><div><div class="field-caption">Operating mode</div><div class="field-value">${pill(deployment.mode)}</div></div><div><div class="field-caption">Record</div><div class="field-value mono">${esc(shortId(deployment.id))}</div></div></div><div class="section-label">${icon("package",13)}Declared components</div><div class="component-list">${Object.entries(deployment.components||{}).map(([key,value])=>`<div class="component-item">${icon(key,14)}<strong>${esc(titleCase(key))}</strong><span>${esc(Array.isArray(value)?value.length:typeof value==="object"?Object.keys(value||{}).length:value)}</span></div>`).join("")||'<span class="muted">No component summary was recorded.</span>'}</div>${jsonDetails(deployment,"Inspect complete deployment record")}</article>`).join(""):empty(source.length?"No matching deployments":"No deployment record","Loaded configurations appear here after the control plane records a bundle revision.","deployments"));
}
function viewCatalog() {
  const key=state.catalogTab, source=catalogItems(key), list=source.filter(item=>matchSearch(item,"catalog"));
  return heading("Composable infrastructure","Component catalog","Inspect the packages and placement bindings that make up this installation. Definitions are managed through the versioned bundle.")+
    `<div class="tabs" role="tablist" aria-label="Component category">${Object.entries(CATALOG_LABELS).map(([name,label])=>`<button class="tab${name===key?" active":""}" id="tab-${name}" role="tab" aria-selected="${name===key}" aria-controls="catalog-panel" data-action="catalog-tab" data-tab="${name}">${label}<span class="tab-count">${catalogItems(name).length}</span></button>`).join("")}</div>`+
    searchToolbar("catalog",`Search ${CATALOG_LABELS[key].toLowerCase()}…`,list.length)+
    `<div id="catalog-panel" role="tabpanel" aria-labelledby="tab-${key}">${state.errors.catalog?errorPanel("catalog"):list.length?`<div class="catalog-grid">${list.map(item=>catalogCard(key,item)).join("")}</div>`:empty(source.length?"No matching components":`No ${CATALOG_LABELS[key].toLowerCase()} declared`,source.length?"Try a different search term.":"This component category is empty in the loaded configuration. Add definitions to the bundle to make them available.",key)}</div>`;
}
function catalogFields(key,item) {
  const ref=(category,id)=>esc(refLabel(category,id));
  const fields={
    models:[["Adapter",esc(item.adapter)],["Model",esc(item.model)],["Destination",ref("destinations",item.destination)],["Digest",item.digest?generationCode(item.digest):'<span class="muted">Not pinned</span>']],
    harnesses:[["Kind",esc(item.kind)],["Version",esc(item.version||"Not declared")],["Capabilities",esc((item.capabilities||[]).join(", ")||"None declared")],["Digest",item.digest?generationCode(item.digest):'<span class="muted">Not pinned</span>']],
    agents:[["Harness",ref("harnesses",item.harness)],["Model",ref("models",item.model)],["Tools",esc((item.tools||[]).length)],["Step limit",esc(item.max_steps??"Runtime default")]],
    tools:[["Kind",esc(item.kind)],["Input → output",`${dataClass(item.input_class)} <span class="quiet">→</span> ${dataClass(item.output_class)}`],["Destination",ref("destinations",item.destination)],["Operations",esc((item.allowed_operations||[]).length)]],
    compute:[["Kind",esc(item.kind)],["Destination",ref("destinations",item.destination)],["Namespace",esc(item.namespace||"Not applicable")],["Accelerator",esc(item.profile?.accelerator||"Not declared")]],
    destinations:[["Boundary",esc(item.kind)],["Clearance",dataClass(item.clearance)],["Allowed hosts",esc((item.allowed_hosts||[]).length)]],
    data_classes:[["Identifier",safeCode(item.id)],["Rank",esc(item.rank)]],
    profiles:[["Components",esc((item.components||[]).length)],["Bindings",esc((item.components||[]).join(", ")||"None")]],
  };
  return fields[key]||[];
}
function catalogCard(key,item) {
  const synthetic=item.adapter==="demo"||item.kind==="demo"||(key==="agents"&&isSynthetic(item.id));
  return `<article class="catalog-card"><div class="catalog-card-top"><div class="catalog-icon">${icon(key,20)}</div><div class="catalog-title"><h3>${esc(rowLabel(item))}</h3><div class="row-subtitle mono">${esc(item.id)}</div></div></div><p class="catalog-card-description">${esc(item.description||catalogDescription(key,item))}</p><div class="catalog-card-fields">${kv(catalogFields(key,item))}</div><div class="catalog-card-footer">${synthetic?pill("synthetic","Synthetic adapter"):pill("configured","Declared configuration")}<button class="button quiet-button small" data-action="open-component" data-id="${esc(item.id)}" data-category="${key}">Inspect${icon("arrow",13)}</button></div></article>`;
}
function catalogDescription(key,item) {
  const descriptions={models:"A model binding to a serving endpoint and information boundary.",harnesses:"The execution harness preserves its own tool loop and capabilities.",agents:"A versioned composition of instructions, tools, model, and harness.",tools:"Declared argument and result classes govern this tool's data flow.",compute:"A compute placement profile, independent of the agent package.",destinations:"A declared execution or data destination with a clearance.",data_classes:"A member of the installation's ordered information-class policy.",profiles:"A named group of components for installation composition."};
  if (item.adapter==="demo"||item.kind==="demo") return "Deterministic synthetic execution for installation assessment; no live model or production data.";
  return descriptions[key]||"A component in the platform bundle.";
}

function viewGrants() {
  const source=grantItems(),list=source.filter(g=>matchSearch(g,"grants")&&(state.filters.grants==="all"||grantState(g)===state.filters.grants));
  return heading("Authority & placement","Flow grants","A grant binds who can request work to an agent, its placement, information classes, and return contract.")+
    searchToolbar("grants","Find a grant, agent, or caller…",list.length,statusSelect("grants",["enabled","inhibited","disabled","unqualified"]))+
    (state.errors.grants?errorPanel("grants"):list.length?`<div class="panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>Flow grant</th><th>Authority</th><th>Agent / compute</th><th>Input → output</th><th>Return</th><th class="narrow"><span class="sr-only">Inspect</span></th></tr></thead><tbody>${list.map(grant=>`<tr><td>${linkButton("open-grant",grant.id,rowLabel(grant))}<div class="row-subtitle mono">${esc(grant.id)}</div></td><td>${pill(grantState(grant))}${grant.inhibited?`<div class="row-subtitle">${esc(grant.reason||"Runtime inhibit")}</div>`:""}</td><td>${esc(refLabel("agents",grant.agent))}<div class="row-subtitle">${esc(refLabel("compute",grant.compute))}</div></td><td><div class="tags">${dataClass(grant.input_class)}<span class="quiet">→</span>${dataClass(grant.output_class)}</div></td><td><span class="pill outline">${icon("receipts",11)}${esc(grant.return_contract)}</span></td><td class="narrow"><button class="icon-button" data-action="open-grant" data-id="${esc(grant.id)}" aria-label="Inspect flow grant ${esc(grant.id)}">${icon("chevron",15)}</button></td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>Runtime inhibits deny dispatch and release. They do not edit the desired bundle.</span><span>${source.length} declared</span></div></div>`:empty(source.length?"No matching grants":"No flow grants declared",source.length?"Try a different filter or search term.":"Declare and qualify a flow grant before admitting work.","grants"))+
    `<div class="boundary-note section-gap">${icon("grants",16)}<span>Authority is evaluated at dispatch and release. Clearing an inhibit remains subject to the current enabled configuration and the installation’s qualification rule.</span></div>`;
}
function viewJobs() {
  const source=jobs(),list=source.filter(job=>matchSearch(job,"jobs")&&(state.filters.jobs==="all"||job.status===state.filters.jobs));
  return heading("Durable execution","Tasks","Follow admitted work through the queue, execution attempts, private result retention, and controlled release.",newTaskButton())+
    searchToolbar("jobs","Find a task, grant, or caller…",list.length,statusSelect("jobs",["queued","running","retry_wait","completed","blocked","failed","cancelled"]))+
    (state.errors.jobs?errorPanel("jobs"):list.length?`<div class="panel">${jobsTable(list)}<div class="table-footer"><span>Most recent ${source.length} tasks · full task text and results are opened individually.</span><button class="button quiet-button small" data-action="refresh">${icon("refresh",13)}Refresh</button></div></div>`:empty(source.length?"No matching tasks":"Your task queue is clear",source.length?"Try a different search term or status filter.":"Submit work using a flow grant. The platform retains the task and its lifecycle across worker restarts.","jobs",source.length?"":newTaskButton(true)))+
    `<div class="boundary-note private section-gap">${icon("lock",16)}<span>This is the authenticated operator surface. Caller receipts expose fixed status only; they do not authorize reading task text or private artifacts.</span></div>`;
}
function viewReceipts() {
  const source=items(state.data.receipts),list=source.filter(item=>matchSearch(item,"receipts")&&(state.filters.receipts==="all"||item.status===state.filters.receipts));
  return heading("Controlled release","Receipts","A narrow caller-facing handle for admitted work. Private job identity and results remain available only to the operator.")+
    searchToolbar("receipts","Find a receipt or caller…",list.length,statusSelect("receipts",["pending","working","available","unavailable"]))+
    (state.errors.receipts?errorPanel("receipts"):list.length?`<div class="panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>Receipt</th><th>Caller</th><th>Caller status</th><th>Release recorded</th><th class="narrow"><span class="sr-only">Details</span></th></tr></thead><tbody>${list.map(receipt=>`<tr><td>${linkButton("open-receipt",receipt.receipt,shortId(receipt.receipt))}<div class="row-subtitle">${esc(refLabelFromGrant(receipt.grant_id))}</div></td><td>${esc(receipt.caller)}</td><td>${pill(receipt.status)}</td><td>${receipt.released_at?`<span title="${esc(fullTime(receipt.released_at))}">${esc(when(receipt.released_at))}</span>`:'<span class="muted">No release recorded</span>'}</td><td class="narrow"><button class="icon-button" data-action="open-receipt" data-id="${esc(receipt.receipt)}" aria-label="Inspect receipt ${esc(shortId(receipt.receipt))}">${icon("chevron",15)}</button></td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>Public projections contain a receipt and fixed status. They contain no artifact URL, tool output, or private job ID.</span></div></div>`:empty(source.length?"No matching receipts":"No receipts yet",source.length?"Try a different search term or status.":"Receipts are created as work is admitted. Completion releases only the bounded status projection.","receipts"));
}
function viewQualification() {
  const source=items(state.data.qualifications),list=source.filter(item=>matchSearch(item,"qualification")),overview=state.overview;
  return heading("Evidence & release","Qualification","Signed evidence binds to a complete configuration generation. Current authority remains a separate decision.")+
    `<div class="assurance-grid"><div class="assurance-card"><div class="section-label">Current generation</div><div class="assurance-value">${pill(!overview.qualification_required?"development":overview.qualified?"valid":"unqualified",!overview.qualification_required?"Evidence optional":overview.qualified?"Qualified":"Unqualified")}</div><p>Evidence applies to ${generationCode(overview.generation)}. Changing the bundle produces a different generation.</p></div><div class="assurance-card"><div class="section-label">Admission rule</div><div class="assurance-value">${overview.qualification_required?"Evidence required":"Development exception"}</div><p>${overview.qualification_required?"The current generation must have valid qualification before work can dispatch or release.":"This installation allows development operation without required qualification. This is visible in the operating mode."}</p></div><div class="assurance-card"><div class="section-label">Recorded attestations</div><div class="assurance-value">${state.errors.qualifications?"—":source.length}</div><p>Review signature validity, profile, expiry, and evidence payload. Records are issued through the qualification tooling.</p></div></div>`+
    searchToolbar("qualification","Find a profile, issuer, or generation…",list.length)+
    (state.errors.qualifications?errorPanel("qualifications"):list.length?`<div class="panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>Profile / generation</th><th>Validity</th><th>Outcome</th><th>Expires</th><th>Issuer</th><th class="narrow"><span class="sr-only">Details</span></th></tr></thead><tbody>${list.map(record=>`<tr><td>${linkButton("open-qualification",record.id,record.profile||"Unnamed profile")}<div class="row-subtitle mono">${esc(shortId(record.generation))}${record.generation===overview.generation?" · current":""}</div></td><td>${pill(record.valid?"valid":"invalid",record.valid?"Valid":"Invalid / expired")}</td><td>${pill(record.passed?"valid":"invalid",record.passed?"Passed":"Failed")}</td><td class="nowrap">${esc(when(record.expires_at))}</td><td>${esc(record.issuer||"Unknown")}</td><td class="narrow"><button class="icon-button" data-action="open-qualification" data-id="${esc(record.id)}" aria-label="Inspect qualification ${esc(record.profile)}">${icon("chevron",15)}</button></td></tr>`).join("")}</tbody></table></div></div>`:empty(source.length?"No matching qualification":"No qualification records",source.length?"Try a different search term.":"Use the qualification command to run the installation checks and issue generation-bound evidence, then reload this view.","qualification"))+
    `<div class="boundary-note section-gap">${icon("info",16)}<span>Evidence has the scope described in its payload. A successful signature check does not, by itself, establish network enforcement, upstream model quality, or current authorization.</span></div>`;
}
function viewAudit() {
  const source=items(state.data.audit),list=source.filter(item=>matchSearch(item,"audit")),verification=state.data.audit?.verification||state.overview.audit||{};
  return heading("Operational history","Audit trail","A hash-linked record of authority changes and execution transitions, with bounded operational metadata.")+
    `<div class="release-banner ${verification.valid===true?"":"bad"}"><div class="release-symbol">${icon(verification.valid===true?"check":"warning",22)}</div><div><h2>${verification.valid===true?"Audit chain verified":verification.valid===false?"Audit verification failed":"Audit verification unavailable"}</h2><p>${esc(verification.limitation||"Hash linkage detects changes relative to a trusted head. Preserve independent checkpoints to detect complete history replacement.")}</p></div><div class="release-meta"><span>VERIFIED EVENTS</span><strong>${formatNumber(verification.events??source.length)}</strong></div></div>`+
    searchToolbar("audit","Search action, actor, or target…",list.length)+
    (state.errors.audit?errorPanel("audit"):list.length?`<div class="panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>Sequence</th><th>Action</th><th>Actor</th><th>Target</th><th>Recorded</th><th class="narrow"><span class="sr-only">Details</span></th></tr></thead><tbody>${list.map(event=>`<tr><td><span class="sequence">#${esc(event.sequence)}</span></td><td>${linkButton("open-audit",event.sequence,event.action,"action-label")}</td><td>${esc(event.actor)}</td><td><span class="mono" title="${esc(event.target)}">${esc(String(event.target||"—").length>26?`${String(event.target).slice(0,24)}…`:event.target||"—")}</span></td><td class="nowrap" title="${esc(fullTime(event.created_at))}">${esc(when(event.created_at))}</td><td class="narrow"><button class="icon-button" data-action="open-audit" data-id="${esc(event.sequence)}" aria-label="Inspect audit event ${esc(event.sequence)}">${icon("chevron",15)}</button></td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>Most recent ${source.length} events. Task, model, and tool payloads do not belong in audit metadata.</span>${verification.head?`<span>Head ${generationCode(verification.head)}</span>`:""}</div></div>`:empty(source.length?"No matching events":"No audit events",source.length?"Try a different actor, action, or target.":"Operational transitions will appear here as the platform is used.","audit"));
}

function openDialog(kind,id = null,category = null) {
  state.dialogTrigger=document.activeElement;
  state.dialog={kind,id,category,data:null,artifact:null,artifactOpen:false,busy:false};
  renderDialog();
  if (kind==="job") loadJob(id);
  setTimeout(()=>$("#dialog-root [autofocus]")?.focus()||$("#dialog-root .dialog")?.focus(),0);
}
function closeDialog(restore = true) {
  state.dialog=null;
  $("#dialog-root").innerHTML="";
  document.body.classList.remove("dialog-open");
  if (restore && state.dialogTrigger?.isConnected) state.dialogTrigger.focus();
  state.dialogTrigger=null;
}
function dialogFrame({eyebrow,title,subtitle="",body,footer="",modal=false,wide=false}) {
  return `<div class="dialog-backdrop" data-dialog-backdrop><section class="dialog${modal?" modal":""}${wide?" wide":""}" role="dialog" aria-modal="true" aria-labelledby="dialog-title" tabindex="-1"><header class="dialog-header"><div><div class="dialog-eyebrow">${esc(eyebrow)}</div><h2 id="dialog-title">${esc(title)}</h2>${subtitle?`<p class="dialog-subtitle">${esc(subtitle)}</p>`:""}</div><button class="icon-button" data-action="close-dialog" aria-label="Close dialog">${icon("close",19)}</button></header><div class="dialog-body">${body}</div>${footer?`<footer class="dialog-footer">${footer}</footer>`:""}</section></div>`;
}
function renderDialog() {
  const dialog=state.dialog;
  if (!dialog) return;
  document.body.classList.add("dialog-open");
  const renderers={"new-job":newJobDialog,job:jobDialog,grant:grantDialog,component:componentDialog,receipt:receiptDialog,qualification:qualificationDialog,audit:auditDialog,inhibit:reasonDialog,resume:reasonDialog};
  $("#dialog-root").innerHTML=(renderers[dialog.kind]||componentDialog)(dialog);
}
function newJobDialog(dialog) {
  const grants=grantItems(),selected=dialog.grantId||grants.find(g=>grantState(g)==="enabled")?.id||grants[0]?.id;
  dialog.grantId=selected;
  const enabled=grants.some(g=>grantState(g)==="enabled");
  return dialogFrame({eyebrow:"Durable execution",title:"Submit a task",subtitle:"Choose the authority and placement before admitting work.",body:
    `${!enabled?'<div class="boundary-note warning">'+icon("warning",16)+'<span>No grant is currently enabled and qualified for execution. Review flow authority before submitting a task.</span></div>':""}<form id="new-job-form"><div class="form-error" id="job-form-error" role="alert"></div><div class="form-field"><label for="job-grant">Flow grant</label><select id="job-grant" name="grant_id" required data-grant-select>${grants.map(grant=>`<option value="${esc(grant.id)}"${grant.id===selected?" selected":""}>${esc(rowLabel(grant))} · ${esc(titleCase(grantState(grant)))}</option>`).join("")||'<option value="">No grants configured</option>'}</select><p class="help">The grant fixes the agent, model placement, tool authority, and return contract.</p></div><div id="grant-preview">${grantPreview(selected)}</div><div class="form-field"><label for="job-task">Task instructions</label><textarea id="job-task" name="task" rows="6" maxlength="48000" required placeholder="Describe the work this agent should carry out…"></textarea><p class="help">Task text is private. It is retained with the job and is not included in public receipts or audit metadata.</p></div><div class="form-field"><label for="job-input-class">Input data class</label><select id="job-input-class" name="input_class"><option value="">Use grant input class</option>${catalogItems("data_classes").map(c=>`<option value="${esc(c.id)}">${esc(c.id)} · ${esc(c.name||`rank ${c.rank}`)}</option>`).join("")}</select><p class="help">Declare the class of the supplied task data. The platform checks it against the grant.</p></div><details class="code-details"><summary>Request identity</summary><div class="panel-body padded"><div class="form-field"><label for="job-idempotency">Idempotency key <span class="optional">optional</span></label><input id="job-idempotency" name="idempotency_key" maxlength="128" autocomplete="off" placeholder="Generated automatically if left blank"><p class="help">Reuse the same key when retrying the same submission. A key identifies one logical request.</p></div></div></details></form>`,footer:`<button class="button" data-action="close-dialog">Cancel</button><button class="button primary" type="submit" form="new-job-form"${!grants.length?" disabled":""}>${icon("plus",16)}Submit task</button>`});
}
function grantPreview(id) {
  const grant=grantItems().find(g=>g.id===id);
  if (!grant) return "";
  const agent=findCatalog("agents",grant.agent),model=findCatalog("models",agent?.model);
  return `<div class="grant-preview"><div class="preview-heading"><strong>Execution binding</strong>${pill(grantState(grant))}</div>${kv([["Agent",esc(refLabel("agents",grant.agent))],["Model / adapter",`${esc(model?.model||agent?.model||"Unknown")} · ${esc(model?.adapter||"Unknown")}`],["Compute",esc(refLabel("compute",grant.compute))],["Input → output",`${dataClass(grant.input_class)} <span class="quiet">→</span> ${dataClass(grant.output_class)}`],["Caller return",esc(grant.return_contract)]])}${isSynthetic(grant.agent)?'<div class="boundary-note warning small-gap">'+icon("info",14)+'<span>This grant uses a synthetic adapter. It exercises the installation lifecycle without contacting a real model or production tool.</span></div>':""}</div>`;
}
async function loadJob(id) {
  try {
    const job=await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (state.dialog?.kind==="job" && state.dialog.id===id) {state.dialog.data=job;state.dialog.error=null;renderDialog();}
  } catch(error) {if (state.dialog?.kind==="job"&&state.dialog.id===id){state.dialog.error=error.message;renderDialog();}}
}
function jobDialog(dialog) {
  const job=dialog.data;
  if (!job) return dialogFrame({eyebrow:"Private task",title:shortId(dialog.id),body:dialog.error?`<div class="form-error">${esc(dialog.error)}</div><button class="button" data-action="refresh-job">Retry</button>`:'<div class="loading-line"></div><div class="skeleton-panel"><div class="loading-line"></div><div class="loading-line"></div></div>'});
  const timeline=[["Admitted",job.created_at,"Persisted in the durable queue"],["Execution started",job.started_at,`${job.attempts??0} execution ${job.attempts===1?"attempt":"attempts"}`],["Execution finished",job.finished_at,titleCase(job.status)],["Release recorded",job.released_at,"Receipt projection recorded after current authorization"]].filter(row=>row[1]);
  let artifact;
  if (dialog.artifactOpen) artifact=`<div class="artifact-view"><div class="artifact-view-header"><span>${icon("lock",13)}Private artifact · operator access</span><div><button class="icon-button" data-action="copy-artifact" aria-label="Copy private artifact">${icon("copy",14)}</button><button class="icon-button" data-action="download-artifact" aria-label="Download private artifact">${icon("download",14)}</button><button class="icon-button" data-action="hide-artifact" aria-label="Hide private artifact">${icon("close",14)}</button></div></div><pre>${esc(pretty(dialog.artifact))}</pre></div>`;
  else if (job.artifact_available) artifact=`<div class="artifact-prompt">${icon("lock",24)}<h3>Private result available</h3><p>The result may contain sensitive task and tool data. Opening it fetches the artifact into this authenticated operator view.</p><button class="button" data-action="open-artifact"${dialog.busy?" disabled":""}>${icon("eye",15)}${dialog.busy?"Loading private result…":"Open private result"}</button></div>`;
  else artifact='<div class="artifact-hidden">No private artifact is available for this task.</div>';
  return dialogFrame({eyebrow:"Private task",title:`Task ${shortId(job.id)}`,subtitle:`${refLabelFromGrant(job.grant_id)} · ${fullTime(job.created_at)}`,body:
    `<div class="detail-status-row">${pill(job.status)}<button class="button quiet-button small" data-action="refresh-job">${icon("refresh",14)}Refresh task</button></div>${job.cancel_requested&&!TERMINAL.has(job.status)?'<div class="boundary-note warning">'+icon("stop",16)+'<span>Cancellation has been requested. The worker must observe it; a running operation may take time to stop.</span></div>':""}${job.error_code?`<div class="boundary-note warning">${icon("info",16)}<span>Operational code: ${safeCode(job.error_code)}. Private exception text is not included in this status.</span></div>`:""}${job.artifact_available&&!job.released_at?'<div class="boundary-note warning">'+icon("grants",16)+'<span>A private result is retained without a recorded public release. Completion does not override current authority.</span></div>':""}<section class="dialog-section"><h3>Execution record</h3>${kv([["Task ID",safeCode(job.id)],["Caller",esc(job.caller)],["Flow grant",esc(refLabelFromGrant(job.grant_id))],["Generation",generationCode(job.generation,true)],["Attempts",esc(job.attempts??0)],["Next retry",esc(job.next_attempt_at?fullTime(job.next_attempt_at):"Not scheduled")],["Receipt",safeCode(job.receipt)]])}</section><section class="dialog-section"><h3>Task instructions</h3><div class="task-text">${esc(job.task||"No task text returned.")}</div></section><section class="dialog-section"><h3>Private result</h3>${artifact}</section><section class="dialog-section"><h3>Lifecycle</h3><div class="timeline">${timeline.map(([label,time,detail])=>`<div class="timeline-event"><div class="label">${esc(label)}</div><div class="time">${esc(fullTime(time))}</div><div class="detail">${esc(detail)}</div></div>`).join("")}</div></section>${job.resolved?`<section class="dialog-section"><h3>Resolved execution binding</h3>${jsonDetails(job.resolved,"Inspect resolved configuration")}</section>`:""}`,footer:`${!TERMINAL.has(job.status)?`<button class="button danger" data-action="cancel-job" data-id="${esc(job.id)}"${job.cancel_requested||dialog.busy?" disabled":""}>${icon("stop",15)}${job.cancel_requested?"Cancellation requested":"Cancel task"}</button>`:""}<button class="button" data-action="close-dialog">Close</button>`});
}
function grantDialog(dialog) {
  const grant=grantItems().find(g=>g.id===dialog.id);
  if (!grant) return dialogFrame({eyebrow:"Flow authority",title:"Grant unavailable",body:empty("Grant is no longer loaded","Refresh the workspace to inspect the current configuration.","grants")});
  const agent=findCatalog("agents",grant.agent),model=findCatalog("models",agent?.model),harness=findCatalog("harnesses",agent?.harness);
  return dialogFrame({eyebrow:"Flow authority",title:rowLabel(grant),subtitle:grant.description||grant.id,body:
    `<div class="detail-status-row">${pill(grantState(grant))}${!state.overview.qualification_required?pill("development","Evidence optional"):grant.qualified?pill("valid","Generation qualified"):pill("unqualified","No valid qualification")}</div>${grant.inhibited?`<div class="boundary-note error">${icon("pause",16)}<span>Runtime inhibit: ${esc(grant.reason||"No reason recorded")}. New dispatch and result release are denied.</span></div>`:""}${isSynthetic(grant.agent)?'<div class="boundary-note warning">'+icon("info",16)+'<span>This flow uses a synthetic adapter for installation assessment.</span></div>':""}<section class="dialog-section"><h3>Execution composition</h3>${kv([["Agent",esc(refLabel("agents",grant.agent))],["Harness",esc(rowLabel(harness))],["Model",esc(model?.model||"Not resolved")],["Serving adapter",esc(model?.adapter||"Not resolved")],["Inference destination",esc(refLabel("destinations",model?.destination))],["Compute",esc(refLabel("compute",grant.compute))],["Tools",esc((agent?.tools||[]).map(id=>refLabel("tools",id)).join(", ")||"No tools")]])}</section><section class="dialog-section"><h3>Information boundary</h3>${kv([["Input class",dataClass(grant.input_class)],["Output class",dataClass(grant.output_class)],["State destination",esc(refLabel("destinations",grant.state_destination))],["Telemetry destination",esc(refLabel("destinations",grant.telemetry_destination))],["Telemetry class",dataClass(grant.telemetry_class)],["Caller clearance",dataClass(grant.caller_clearance)],["Receipt class",dataClass(grant.receipt_class)],["Return contract",safeCode(grant.return_contract)]])}</section><section class="dialog-section"><h3>Admission & recovery</h3>${kv([["Declared callers",esc((grant.callers||[]).join(", ")||"No external callers")],["Desired enabled",esc(grant.enabled===false?"No":"Yes")],["Outage behavior",esc(grant.on_outage||"queue")],["Maximum attempts",esc(grant.max_attempts||"Installation default")],["Timeout",esc(grant.timeout_seconds?`${grant.timeout_seconds} seconds`:"Installation default")],["Runtime update",esc(fullTime(grant.updated_at))]])}</section>${jsonDetails(grant)}`,footer:`<button class="button" data-action="close-dialog">Close</button>${grant.inhibited?`<button class="button primary" data-action="confirm-resume" data-id="${esc(grant.id)}">${icon("play",14)}Clear inhibit</button>`:`<button class="button danger" data-action="confirm-inhibit" data-id="${esc(grant.id)}">${icon("pause",14)}Inhibit grant</button>`}<button class="button primary" data-action="new-job-for-grant" data-id="${esc(grant.id)}"${grantState(grant)!=="enabled"?" disabled":""}>${icon("plus",15)}New task</button>`});
}
function componentDialog(dialog) {
  const item=findCatalog(dialog.category,dialog.id);
  if (!item) return dialogFrame({eyebrow:"Component catalog",title:"Component unavailable",body:empty("Definition is no longer loaded","Refresh the workspace to see the current bundle.","catalog")});
  const usage=catalogItems("agents").filter(agent=>agent.model===item.id||agent.harness===item.id||(agent.tools||[]).includes(item.id));
  const grants=grantItems().filter(grant=>grant.agent===item.id||grant.compute===item.id||grant.state_destination===item.id||grant.telemetry_destination===item.id);
  return dialogFrame({eyebrow:CATALOG_LABELS[dialog.category]||"Component",title:rowLabel(item),subtitle:item.id,body:
    `<div class="boundary-note">${icon("info",16)}<span>This view describes the loaded package definition. Runtime readiness must be observed separately.</span></div><section class="dialog-section"><h3>Declared binding</h3>${kv(catalogFields(dialog.category,item))}${item.description?`<p class="inline-help small-gap no-margin">${esc(item.description)}</p>`:""}</section>${usage.length?`<section class="dialog-section"><h3>Used by agents</h3><div class="tags">${usage.map(agent=>`<button class="button small" data-action="open-component" data-category="agents" data-id="${esc(agent.id)}">${icon("agents",14)}${esc(rowLabel(agent))}</button>`).join("")}</div></section>`:""}${grants.length?`<section class="dialog-section"><h3>Referenced by flow grants</h3><div class="tags">${grants.map(grant=>`<button class="button small" data-action="open-grant" data-id="${esc(grant.id)}">${icon("grants",14)}${esc(rowLabel(grant))}</button>`).join("")}</div></section>`:""}${jsonDetails(item,"Complete component definition",true)}`,footer:'<button class="button" data-action="close-dialog">Close</button>'});
}
function receiptDialog(dialog) {
  const receipt=items(state.data.receipts).find(item=>item.receipt===dialog.id);
  if (!receipt) return dialogFrame({eyebrow:"Controlled release",title:"Receipt unavailable",body:empty("Receipt is not in the loaded page","Refresh the receipts view.","receipts")});
  const publicProjection={receipt:receipt.receipt,status:receipt.status};
  return dialogFrame({eyebrow:"Controlled release",title:"Receipt projection",subtitle:"Caller-visible status and private operator linkage",body:
    `<div class="receipt-projection"><div class="projection-header">${icon("receipts",15)}Bounded caller projection</div><pre>${esc(pretty(publicProjection))}</pre></div><p class="receipt-explanation">${receipt.caller==="operator"?"This task was submitted from the operator console. Operator credentials are not accepted by the caller gateway, so this receipt is unavailable through MCP even when a private result is complete.":"This is the shape of a status projection. Caller requests are authenticated against the receipt’s owner; possession of the receipt alone does not grant access."}</p><section class="dialog-section"><h3>Private operator linkage</h3>${kv([["Flow grant",esc(refLabelFromGrant(receipt.grant_id))],["Caller",esc(receipt.caller)],["Admitted",esc(fullTime(receipt.created_at))],["Release recorded",esc(fullTime(receipt.released_at))],["Private task ID",safeCode(receipt.job_id)]])}</section><div class="boundary-note private">${icon("lock",16)}<span>The task ID and metadata above belong to the operator API. They are not fields of the public caller projection.</span></div>`,footer:`<button class="button" data-action="close-dialog">Close</button>${receipt.job_id?`<button class="button primary" data-action="open-job" data-id="${esc(receipt.job_id)}">${icon("lock",14)}Inspect private task</button>`:""}`});
}
function qualificationDialog(dialog) {
  const record=items(state.data.qualifications).find(item=>item.id===dialog.id);
  if (!record) return dialogFrame({eyebrow:"Qualification",title:"Record unavailable",body:empty("Record is not loaded","Refresh the qualification view.","qualification")});
  return dialogFrame({eyebrow:"Generation-bound evidence",title:record.profile||"Qualification record",subtitle:record.id,body:
    `<div class="detail-status-row">${pill(record.valid?"valid":"invalid",record.valid?"Valid attestation":"Invalid / expired")}${pill(record.passed?"valid":"invalid",record.passed?"Checks passed":"Checks failed")}</div><section class="dialog-section"><h3>Attestation scope</h3>${kv([["Generation",generationCode(record.generation,true)],["Current generation",esc(record.generation===state.overview.generation?"Yes":"No")],["Profile",esc(record.profile)],["Issuer",esc(record.issuer)],["Created",esc(fullTime(record.created_at))],["Expires",esc(fullTime(record.expires_at))]])}</section><div class="boundary-note">${icon("info",16)}<span>Read the evidence payload for the exact checks and their limits. Qualification does not override a disabled grant, runtime inhibit, or changed configuration.</span></div>${jsonDetails(record.payload,"Evidence payload",true)}${jsonDetails({signature:record.signature,public_key:record.public_key},"Signature and public key")}`,footer:'<button class="button" data-action="close-dialog">Close</button>'});
}
function auditDialog(dialog) {
  const event=items(state.data.audit).find(item=>String(item.sequence)===String(dialog.id));
  if (!event) return dialogFrame({eyebrow:"Audit trail",title:"Event unavailable",body:empty("Event is not in the loaded page","Refresh the audit trail.","audit")});
  return dialogFrame({eyebrow:`Audit event #${event.sequence}`,title:event.action,subtitle:fullTime(event.created_at),body:
    `<section class="dialog-section"><h3>Operational transition</h3>${kv([["Actor",esc(event.actor)],["Action",safeCode(event.action)],["Target",safeCode(event.target)],["Recorded",esc(fullTime(event.created_at))]])}</section>${jsonDetails(event.detail,"Bounded event metadata",true)}${jsonDetails({previous_hash:event.previous_hash,hash:event.hash},"Hash-chain linkage")}`,footer:'<button class="button" data-action="close-dialog">Close</button>'});
}
function reasonDialog(dialog) {
  const inhibit=dialog.kind==="inhibit",grant=grantItems().find(g=>g.id===dialog.id);
  return dialogFrame({eyebrow:"Runtime authority",title:inhibit?"Inhibit this flow grant?":"Clear the runtime inhibit?",subtitle:rowLabel(grant||{id:dialog.id}),modal:true,body:
    `<div class="boundary-note ${inhibit?"warning":""}">${icon(inhibit?"pause":"grants",16)}<span>${inhibit?"This denies new dispatch and result release for this grant. Active jobs receive cancellation intent. It does not change the desired configuration.":"This removes the runtime deny. The grant must still be enabled in the current bundle and satisfy the installation’s qualification rule."}</span></div><form id="reason-form"><div class="form-error" id="reason-error" role="alert"></div><div class="form-field"><label for="authority-reason">Operational reason</label><textarea id="authority-reason" name="reason" rows="3" minlength="3" maxlength="500" required autofocus placeholder="Record why authority is changing…"></textarea><p class="help">This reason is retained as operational metadata. Do not include task content, credentials, or sensitive production data.</p></div></form>`,footer:`<button class="button" data-action="close-dialog">Cancel</button><button class="button ${inhibit?"danger":"primary"}" type="submit" form="reason-form">${icon(inhibit?"pause":"play",14)}${inhibit?"Inhibit grant":"Clear inhibit"}</button>`});
}

async function refresh(initial = false) {
  if (refreshPromise) return refreshPromise;
  refreshPromise=(async()=>{
    state.loading=true;
    if (initial&&!state.hasLoaded) renderShell();
    const refreshButton=$('[data-action="refresh"]');
    refreshButton?.classList.add("spinning");
    if(refreshButton)refreshButton.disabled=true;
    try {
      const overview=await api("/api/overview");
      state.overview=overview;state.authenticated=true;
      const entries=Object.entries(ENDPOINTS);
      const results=await Promise.allSettled(entries.map(([,path])=>api(path)));
      if (!state.authenticated) return;
      results.forEach((result,index)=>{
        const key=entries[index][0];
        if(result.status==="fulfilled"){state.data[key]=result.value;delete state.errors[key];}
        else state.errors[key]=result.reason;
      });
      state.online=true;state.lastSync=Date.now();state.hasLoaded=true;
      const active=document.activeElement;
      if (!state.dialog && !["INPUT","SELECT","TEXTAREA"].includes(active?.tagName)) renderShell();
      else updateSyncLabel();
    } catch(error) {
      if(error.status===401){state.authenticated=false;state.hasLoaded=true;renderLogin();}
      else {state.online=false;if(state.overview)renderShell();else renderConnectionError(error.message);}
    } finally {
      state.loading=false;refreshPromise=null;updateSyncLabel();schedulePoll();
    }
  })();
  return refreshPromise;
}
function updateSyncLabel() {
  const button=$('.topbar [data-action="refresh"]');
  if(button){button.disabled=state.loading;button.classList.toggle("spinning",state.loading);}
  const sync=$(".sync-text");
  if(sync)sync.textContent=state.loading?"Synchronizing":!state.online?"Connection interrupted":state.lastSync?`Updated ${new Date(state.lastSync).toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"})}`:"Connecting";
}
function schedulePoll() {
  clearTimeout(pollTimer);
  const active=(state.overview?.counts?.running||0)+(state.overview?.counts?.queued||0)>0;
  pollTimer=setTimeout(()=>{if(state.authenticated&&!document.hidden)refresh();else schedulePoll();},active?6500:25000);
}
function renderConnectionError(message) {
  $("#app").innerHTML=`<div class="main"><div class="connection-error"><div class="brand connection-brand">${mark()}<div class="brand-name">Sovite</div></div><div class="panel"><div class="empty-state error"><div class="empty-icon">${icon("network",23)}</div><h3>Control plane unavailable</h3><p>${esc(message)}</p><button class="button primary" data-action="refresh">${icon("refresh",15)}Retry connection</button></div></div></div></div>`;
}
async function submitLogin(form) {
  const button=form.querySelector('button[type="submit"]'), token=form.elements.token.value;
  button.disabled=true;button.textContent="Signing in…";$("#login-error").textContent="";
  try {await post("/api/auth/session",{token});form.elements.token.value="";state.authenticated=true;state.hasLoaded=false;await refresh(true);}
  catch(error){if($("#login-error"))$("#login-error").textContent=error.message;}
  finally{if(button.isConnected){button.disabled=false;button.innerHTML=`Sign in to platform${icon("arrow",16)}`;}}
}
async function submitJob(form) {
  const submit=$('button[form="new-job-form"]'), values=new FormData(form), selected=grantItems().find(g=>g.id===values.get("grant_id"));
  const errorBox=$("#job-form-error");
  if(!selected){errorBox.textContent="Choose a configured flow grant.";return;}
  if(grantState(selected)!=="enabled"){errorBox.textContent="This grant is not currently enabled for execution. Review its authority and qualification.";return;}
  const task=String(values.get("task")||"").trim();
  if(!task){errorBox.textContent="Add task instructions before submitting.";return;}
  if(submit){submit.disabled=true;submit.textContent="Admitting task…";}
  errorBox.textContent="";
  const body={grant_id:String(values.get("grant_id")),task,idempotency_key:String(values.get("idempotency_key")||"").trim()||crypto.randomUUID()};
  if(values.get("input_class"))body.input_class=String(values.get("input_class"));
  // Keep the generated identity in the form if admission must be retried.
  form.elements.idempotency_key.value=body.idempotency_key;
  try {
    const job=await post("/api/jobs",body);
    closeDialog(false);state.route="jobs";window.location.hash="jobs";
    notify(`Task ${shortId(job.id)} admitted to the durable queue.`);
    await refresh();openDialog("job",job.id);
  } catch(error) {if(errorBox.isConnected)errorBox.textContent=error.message;else notify(error.message,true);}
  finally{if(submit?.isConnected){submit.disabled=false;submit.innerHTML=`${icon("plus",16)}Submit task`;}}
}
async function submitReason(form) {
  const dialog=state.dialog;if(!dialog)return;
  const reason=form.elements.reason.value.trim(),inhibit=dialog.kind==="inhibit",submit=$('button[form="reason-form"]'),errorBox=$("#reason-error");
  if(reason.length<3){errorBox.textContent="Record a short reason for this authority change.";return;}
  submit.disabled=true;errorBox.textContent="";
  try {
    await post(`/api/grants/${encodeURIComponent(dialog.id)}/${inhibit?"inhibit":"resume"}`,{reason});
    closeDialog(false);notify(inhibit?"Flow grant inhibited. Dispatch and release are denied.":"Runtime inhibit cleared under the current configuration.");await refresh();openDialog("grant",dialog.id);
  } catch(error){if(errorBox.isConnected)errorBox.textContent=error.message;else notify(error.message,true);}
  finally{if(submit.isConnected)submit.disabled=false;}
}
async function cancelJob(id,button) {
  button.disabled=true;
  try {await post(`/api/jobs/${encodeURIComponent(id)}/cancel`,{});notify("Cancellation requested. The worker will observe the cancellation intent.");await loadJob(id);await refresh();}
  catch(error){notify(error.message,true);if(button.isConnected)button.disabled=false;}
}
async function openArtifact() {
  const dialog=state.dialog;if(dialog?.kind!=="job"||!dialog.data?.artifact_available)return;
  dialog.busy=true;renderDialog();
  try {
    const artifact=await api(`/api/jobs/${encodeURIComponent(dialog.id)}/artifact`);
    if(state.dialog===dialog){dialog.artifact=artifact;dialog.artifactOpen=true;}
  } catch(error){notify(error.message,true);}
  finally{if(state.dialog===dialog){dialog.busy=false;renderDialog();}}
}
async function copyArtifact() {
  if(!state.dialog?.artifactOpen)return;
  try{await navigator.clipboard.writeText(pretty(state.dialog.artifact));notify("Private artifact copied to your clipboard.");}
  catch(_){notify("Clipboard access is unavailable in this browser. Use the download action or select the result text.",true);}
}
function downloadArtifact() {
  if(!state.dialog?.artifactOpen)return;
  const data=new Blob([pretty(state.dialog.artifact)+"\n"],{type:"application/json"});
  const url=URL.createObjectURL(data),anchor=document.createElement("a");
  anchor.href=url;anchor.download=`sovite-private-result-${shortId(state.dialog.id)}.json`;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),5000);
}
async function logout() {
  try{await api("/api/auth/session",{method:"DELETE"});state.authenticated=false;state.data={};state.overview=null;state.errors={};state.hasLoaded=true;closeDialog(false);renderLogin();}
  catch(error){notify(`Sign out failed: ${error.message}`,true);}
}

document.addEventListener("click",event=>{
  if(event.target.matches("[data-dialog-backdrop]")){closeDialog();return;}
  const button=event.target.closest("[data-action]");if(!button||button.disabled)return;
  const action=button.dataset.action,id=button.dataset.id;
  const actions={
    refresh:()=>refresh(true),"close-dialog":()=>closeDialog(),logout,
    "toggle-menu":()=>{state.menuOpen=!state.menuOpen;$(".shell")?.classList.toggle("menu-open",state.menuOpen);button.setAttribute("aria-expanded",String(state.menuOpen));},
    "close-menu":()=>{state.menuOpen=false;$(".shell")?.classList.remove("menu-open");},
    "new-job":()=>openDialog("new-job"),"new-job-for-grant":()=>{openDialog("new-job");state.dialog.grantId=id;renderDialog();},
    "open-job":()=>openDialog("job",id),"refresh-job":()=>{if(state.dialog?.id)loadJob(state.dialog.id);},
    "cancel-job":()=>cancelJob(id,button),"open-artifact":openArtifact,"copy-artifact":copyArtifact,"download-artifact":downloadArtifact,
    "hide-artifact":()=>{if(state.dialog){state.dialog.artifactOpen=false;state.dialog.artifact=null;renderDialog();}},
    "open-grant":()=>openDialog("grant",id),"confirm-inhibit":()=>openDialog("inhibit",id),"confirm-resume":()=>openDialog("resume",id),
    "open-component":()=>openDialog("component",id,button.dataset.category),"open-receipt":()=>openDialog("receipt",id),
    "open-qualification":()=>openDialog("qualification",id),"open-audit":()=>openDialog("audit",id),
    "catalog-tab":()=>{state.catalogTab=button.dataset.tab;renderPage();$(`#tab-${state.catalogTab}`)?.focus({preventScroll:true});},
    "toggle-token":()=>{const input=$("#operator-token"),show=input.type==="password";input.type=show?"text":"password";button.setAttribute("aria-pressed",String(show));button.setAttribute("aria-label",show?"Hide operator token":"Show operator token");},
  };
  if(actions[action]){event.preventDefault();Promise.resolve(actions[action]()).catch(error=>notify(error.message||"The action could not be completed.",true));}
});
document.addEventListener("submit",event=>{
  const handlers={"login-form":submitLogin,"new-job-form":submitJob,"reason-form":submitReason};
  const handler=handlers[event.target.id];if(handler){event.preventDefault();handler(event.target).catch(error=>notify(error.message,true));}
});
document.addEventListener("input",event=>{
  const route=event.target.dataset.search;
  if(route){state.search[route]=event.target.value;renderPage(true);}
});
document.addEventListener("change",event=>{
  if(event.target.dataset.filter){state.filters[event.target.dataset.filter]=event.target.value;renderPage(true);}
  if(event.target.matches("[data-grant-select]")){if(state.dialog)state.dialog.grantId=event.target.value;$("#grant-preview").innerHTML=grantPreview(event.target.value);}
});
document.addEventListener("keydown",event=>{
  if(event.key==="Escape"){if(state.dialog)closeDialog();else if(state.menuOpen){state.menuOpen=false;$(".shell")?.classList.remove("menu-open");}}
  if(event.key==="Tab"&&state.dialog){
    const focusable=$$('#dialog-root button:not([disabled]), #dialog-root [href], #dialog-root input:not([disabled]), #dialog-root select:not([disabled]), #dialog-root textarea:not([disabled]), #dialog-root summary, #dialog-root [tabindex="0"]').filter(node=>node.getClientRects().length);
    const first=focusable[0],last=focusable[focusable.length-1];
    if(event.shiftKey&&(document.activeElement===first||!$("#dialog-root").contains(document.activeElement))){event.preventDefault();last?.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||!$("#dialog-root").contains(document.activeElement))){event.preventDefault();first?.focus();}
  }
  if(event.target.matches('[role="tab"]')&&["ArrowLeft","ArrowRight","Home","End"].includes(event.key)){
    event.preventDefault();const keys=Object.keys(CATALOG_LABELS),current=keys.indexOf(state.catalogTab),next=event.key==="Home"?0:event.key==="End"?keys.length-1:(current+(event.key==="ArrowRight"?1:-1)+keys.length)%keys.length;
    state.catalogTab=keys[next];renderPage();$(`#tab-${state.catalogTab}`)?.focus();
  }
});
window.addEventListener("hashchange",()=>{
  const route=location.hash.slice(1).split("/")[0];
  if(route==="main"){$("#main")?.focus();return;}
  state.route=ROUTES[route]?route:"overview";state.menuOpen=false;
  if(state.authenticated){closeDialog(false);renderShell();$("#main")?.focus({preventScroll:true});window.scrollTo(0,0);}
});
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&state.authenticated)refresh();});
window.addEventListener("online",()=>{if(state.authenticated)refresh();});
window.addEventListener("offline",()=>{state.online=false;if(state.authenticated&&!state.dialog)renderShell();});
state.route=ROUTES[location.hash.slice(1)]?location.hash.slice(1):"overview";
refresh(true);
