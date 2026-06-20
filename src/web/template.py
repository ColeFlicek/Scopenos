HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Phronosis</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64' fill='none'><rect width='64' height='64' rx='14' fill='%230f172a'/><line x1='11' y1='53' x2='32' y2='11' stroke='%2338bdf8' stroke-width='4' stroke-linecap='round'/><line x1='32' y1='11' x2='53' y2='53' stroke='%2338bdf8' stroke-width='4' stroke-linecap='round'/><line x1='19' y1='38' x2='45' y2='38' stroke='%2338bdf8' stroke-width='3' stroke-linecap='round'/><circle cx='32' cy='11' r='5' fill='%237dd3fc'/><circle cx='19' cy='38' r='3.5' fill='%230ea5e9'/><circle cx='45' cy='38' r='3.5' fill='%230ea5e9'/><circle cx='11' cy='53' r='3.5' fill='%230ea5e9'/><circle cx='53' cy='53' r='3.5' fill='%230ea5e9'/></svg>">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:      #06060f;
      --bg2:     #09090f;
      --bg3:     #0e0e1a;
      --bg4:     #131325;
      --border:  #1a1a2e;
      --border2: #252540;
      --accent:  #7c3aed;
      --accent2: #9f67ff;
      --glow:    rgba(124,58,237,0.15);
      --glow2:   rgba(124,58,237,0.30);
      --text:    #dbd8f0;
      --text2:   #9996b0;
      --text3:   #5a5870;
      --green:   #4ade80;
      --red:     #f87171;
      --amber:   #fb923c;
    }

    html, body {
      height: 100%; font-family: 'Space Mono', monospace;
      background: var(--bg); color: var(--text);
      font-size: 13px; line-height: 1.6;
    }

    body::before {
      content: '';
      position: fixed; inset: 0;
      background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.025) 2px, rgba(0,0,0,0.025) 4px);
      pointer-events: none; z-index: 9999;
    }

    .layout { display: flex; height: 100vh; overflow: hidden; }

    /* ── Sidebar ── */
    .sidebar {
      width: 200px; flex-shrink: 0;
      background: var(--bg2); border-right: 1px solid var(--border);
      display: flex; flex-direction: column; overflow: hidden;
    }
    .logo { padding: 20px 18px 16px; border-bottom: 1px solid var(--border); }
    .logo-mark {
      font-size: 17px; font-weight: 700; color: var(--accent2);
      display: flex; align-items: center; gap: 9px; letter-spacing: -0.3px;
    }
    .logo-sub { font-size: 10px; color: var(--text3); margin-top: 3px; letter-spacing: 0.1em; text-transform: uppercase; }

    nav { padding: 10px; flex: 1; }
    .nav-item {
      display: flex; align-items: center; gap: 10px;
      padding: 8px 10px; border-radius: 6px;
      color: var(--text3); cursor: pointer;
      transition: all 0.12s; font-size: 12px;
      border: 1px solid transparent; margin-bottom: 2px;
      letter-spacing: 0.01em;
    }
    .nav-item:hover { color: var(--text2); background: var(--bg3); }
    .nav-item.active {
      color: var(--accent2); background: var(--glow);
      border-color: rgba(124,58,237,0.2);
    }
    .nav-item .icon { width: 14px; height: 14px; flex-shrink: 0; opacity: 0.6; }
    .nav-item.active .icon { opacity: 1; }

    .sidebar-footer { padding: 14px 18px; border-top: 1px solid var(--border); }
    .conn-row { display: flex; align-items: center; gap: 8px; }
    .conn-label { font-size: 11px; color: var(--text3); }

    /* ── Main ── */
    .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    .topbar {
      padding: 14px 28px; border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between;
      flex-shrink: 0; background: var(--bg2);
    }
    .page-title { font-size: 14px; font-weight: 700; color: var(--text); }
    .page-sub { font-size: 11px; color: var(--text3); margin-top: 1px; }
    .content { flex: 1; overflow-y: auto; padding: 24px 28px; }

    /* ── Dot ── */
    .dot {
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
      position: relative; display: inline-block;
    }
    .dot::after {
      content: ''; position: absolute; inset: -3px;
      border-radius: 50%; animation: ring 2s ease-out infinite;
    }
    .dot.ok    { background: var(--green); }
    .dot.ok::after    { border: 1px solid var(--green); }
    .dot.error { background: var(--red); }
    .dot.error::after { border: 1px solid var(--red); }
    .dot.wait  { background: var(--amber); animation: blink 1.4s ease infinite; }
    .dot.wait::after  { display: none; }
    @keyframes ring  { 0% { opacity:.8; transform:scale(1); } 100% { opacity:0; transform:scale(2.2); } }
    @keyframes blink { 0%,100% { opacity:1; } 50% { opacity:.3; } }

    /* ── Card ── */
    .card {
      background: var(--bg3); border: 1px solid var(--border);
      border-radius: 10px; position: relative; overflow: hidden;
    }
    .card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
      background: linear-gradient(90deg, transparent, var(--border2), transparent);
    }
    .layer-card { padding: 22px 24px; transition: border-color 0.2s; }
    .layer-card:hover { border-color: var(--border2); }
    .layer-card.ok    { border-top-color: rgba(74,222,128,0.3); }
    .layer-card.error { border-top-color: rgba(248,113,113,0.3); }

    .layer-header { display:flex; align-items:flex-start; justify-content:space-between; margin-bottom:18px; }
    .layer-name { font-size:10px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:var(--text3); }
    .layer-tech { font-size:10px; color:var(--text3); margin-top:2px; }
    .layer-count { font-size:40px; font-weight:700; color:var(--text); letter-spacing:-2px; line-height:1; margin-bottom:6px; }
    .layer-label { font-size:11px; color:var(--text3); }
    .layer-meta  { font-size:11px; color:var(--text3); margin-top:14px; padding-top:12px; border-top:1px solid var(--border); }
    .layer-meta span { color:var(--text2); }

    /* ── Table ── */
    .data-table { width:100%; border-collapse:collapse; }
    .data-table th {
      font-size:10px; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
      color:var(--text3); padding:10px 16px; text-align:left; border-bottom:1px solid var(--border);
    }
    .data-table th:not(:first-child) { text-align:right; }
    .data-table td { padding:11px 16px; border-bottom:1px solid var(--border); font-size:12px; }
    .data-table td:not(:first-child) { text-align:right; color:var(--text2); }
    .data-table tr:last-child td { border-bottom:none; }
    .data-table tr:hover td { background:rgba(255,255,255,0.012); }
    .path-cell { color:var(--text); font-size:11px; max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .proj-id-cell { font-size:11px; color:var(--accent2); cursor:pointer; }
    .proj-id-cell:hover { text-decoration:underline; }

    /* ── Form ── */
    .field-label { font-size:10px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--text3); margin-bottom:6px; display:block; }
    .field-hint  { font-size:10px; color:var(--accent2); margin-top:4px; min-height:14px; }

    input[type=text], select, textarea {
      width:100%; background:var(--bg4); border:1px solid var(--border2);
      border-radius:6px; padding:9px 12px;
      font-family:'Space Mono',monospace; font-size:12px; color:var(--text);
      outline:none; transition:border-color .15s, box-shadow .15s; appearance:none;
    }
    textarea { resize:vertical; min-height:80px; }
    input:focus, select:focus, textarea:focus { border-color:var(--accent); box-shadow:0 0 0 3px var(--glow); }
    input::placeholder, textarea::placeholder { color:var(--text3); }
    select option { background:var(--bg4); }

    /* ── Buttons ── */
    .btn {
      display:inline-flex; align-items:center; gap:7px;
      padding:8px 16px; border-radius:6px;
      font-family:'Space Mono',monospace; font-size:12px; font-weight:700;
      cursor:pointer; border:none; transition:all .12s; letter-spacing:.02em;
    }
    .btn:disabled { opacity:.5; cursor:default; }
    .btn-accent { background:var(--accent); color:#fff; }
    .btn-accent:hover:not(:disabled) { background:var(--accent2); box-shadow:0 0 12px var(--glow2); }
    .btn-ghost  { background:transparent; color:var(--text2); border:1px solid var(--border2); }
    .btn-ghost:hover:not(:disabled)  { color:var(--text); background:var(--bg4); }
    .btn-sm { padding:6px 12px; font-size:11px; }

    /* ── Badge ── */
    .badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
    .badge-set   { background:rgba(74,222,128,.12); color:var(--green); border:1px solid rgba(74,222,128,.2); }
    .badge-unset { background:rgba(255,255,255,.04); color:var(--text3); border:1px solid var(--border); }

    /* ── Key row ── */
    .key-row {
      display:grid; grid-template-columns:1fr auto auto;
      align-items:center; gap:16px;
      padding:14px 0; border-bottom:1px solid var(--border);
    }
    .key-row:last-child { border-bottom:none; }
    .key-name { font-size:12px; font-weight:700; color:var(--text); }
    .key-desc { font-size:10px; color:var(--text3); margin-top:2px; }
    .key-preview {
      font-size:11px; color:var(--text2); background:var(--bg4);
      padding:4px 10px; border-radius:4px; border:1px solid var(--border);
      letter-spacing:.05em;
    }

    /* ── Instruction box ── */
    .instr-box {
      background:var(--bg2); border:1px solid var(--border);
      border-left:2px solid var(--accent); border-radius:8px; padding:16px 18px;
    }
    .instr-section { margin-bottom:14px; }
    .instr-section:last-child { margin-bottom:0; }
    .instr-step { font-size:10px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:var(--accent2); margin-bottom:6px; }
    .instr-text { font-size:12px; color:var(--text2); margin-bottom:6px; }
    .cmd {
      display:block; background:var(--bg4); border:1px solid var(--border);
      border-radius:4px; padding:6px 10px; font-size:11px; color:var(--accent2); margin-bottom:4px;
    }

    /* ── Search results ── */
    .result-item {
      padding:14px 16px; border-bottom:1px solid var(--border);
      transition:background .1s;
    }
    .result-item:last-child { border-bottom:none; }
    .result-item:hover { background:rgba(255,255,255,0.012); }
    .result-header { display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:4px; }
    .result-fn { font-size:12px; font-weight:700; color:var(--accent2); }
    .result-score { font-size:11px; color:var(--green); flex-shrink:0; }
    .result-sig { font-size:10px; color:var(--text3); margin-bottom:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .result-summary { font-size:11px; color:var(--text2); line-height:1.5; }
    .result-meta { font-size:10px; color:var(--text3); margin-top:6px; }
    .result-badge { display:inline-block; padding:1px 6px; border-radius:3px; font-size:9px; font-weight:700; background:var(--glow); color:var(--accent2); border:1px solid rgba(124,58,237,.2); margin-right:6px; }

    /* ── Code block ── */
    .code-result {
      background:var(--bg2); border:1px solid var(--border); border-radius:8px;
      padding:18px; font-size:11px; color:var(--green);
      white-space:pre-wrap; overflow-x:auto; line-height:1.7;
      max-height:420px; overflow-y:auto;
    }

    /* ── Section header ── */
    .section-header { padding:16px 22px; border-bottom:1px solid var(--border); }
    .section-title  { font-size:12px; font-weight:700; color:var(--text); }
    .section-sub    { font-size:11px; color:var(--text3); margin-top:2px; }

    /* ── Running config ── */
    .cfg-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; padding:20px; }
    .cfg-key { font-size:10px; font-weight:700; color:var(--text3); letter-spacing:.08em; text-transform:uppercase; margin-bottom:4px; }
    .cfg-val { font-size:12px; color:var(--accent2); }

    /* ── Restart banner ── */
    .restart-banner {
      display:flex; align-items:center; gap:12px;
      background:rgba(251,146,60,.06); border:1px solid rgba(251,146,60,.2);
      border-radius:8px; padding:12px 16px;
      font-size:11px; color:var(--amber); margin-bottom:20px;
    }
    .restart-banner code {
      background:rgba(251,146,60,.1); padding:2px 6px; border-radius:4px;
      font-family:'Space Mono',monospace;
    }

    /* ── Scope bar ── */
    .scope-bar {
      display:flex; align-items:center; gap:10px;
      padding:10px 22px; border-bottom:1px solid var(--border);
      background:var(--bg2);
    }
    .scope-label { font-size:10px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--text3); flex-shrink:0; }
    .scope-bar select { max-width:240px; padding:6px 10px; font-size:11px; }

    /* ── Empty ── */
    .empty { text-align:center; padding:40px 20px; color:var(--text3); font-size:12px; }
    .empty code {
      display:inline-block; background:var(--bg4); border:1px solid var(--border);
      padding:4px 10px; border-radius:4px; color:var(--accent2);
      font-size:11px; margin-top:8px;
    }

    /* ── Panels ── */
    .panel { display:none; }
    .panel.active { display:block; animation:fadein .15s ease; }
    @keyframes fadein { from { opacity:0; transform:translateY(3px); } to { opacity:1; transform:none; } }

    ::-webkit-scrollbar { width:4px; }
    ::-webkit-scrollbar-track { background:transparent; }
    ::-webkit-scrollbar-thumb { background:var(--border2); border-radius:10px; }
  </style>
</head>
<body>
<div class="layout">

  <!-- ═══ SIDEBAR ═══ -->
  <aside class="sidebar">
    <div class="logo">
      <div class="logo-mark">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 2L2 7l10 5 10-5-10-5z"/>
          <path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
        </svg>
        Phronosis
      </div>
      <div class="logo-sub">ai code intelligence platform</div>
    </div>

    <nav>
      <div class="nav-item active" id="nav-home" onclick="showPanel('home')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
          <polyline points="9 22 9 12 15 12 15 22"/>
        </svg>
        home
      </div>
      <div class="nav-item" id="nav-overview" onclick="showPanel('overview')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
          <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
        </svg>
        overview
      </div>
      <div class="nav-item" id="nav-search" onclick="showPanel('search')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        search
      </div>
      <div class="nav-item" id="nav-contracts" onclick="showPanel('contracts')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
        </svg>
        contracts
      </div>
      <div class="nav-item" id="nav-settings" onclick="showPanel('settings')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="3"/>
          <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
        </svg>
        settings
      </div>
      <div class="nav-item" id="nav-admin" onclick="showPanel('admin')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
        </svg>
        admin
      </div>
    </nav>

    <div class="sidebar-footer">
      <div class="conn-row">
        <div class="dot wait" id="conn-dot"></div>
        <span class="conn-label" id="conn-label">connecting...</span>
      </div>
    </div>
  </aside>

  <!-- ═══ MAIN ═══ -->
  <div class="main">
    <div class="topbar">
      <div>
        <div class="page-title" id="page-title">overview</div>
        <div class="page-sub"  id="page-sub">system status · layer health · indexed projects</div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="loadStatus()">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
        </svg>
        refresh
      </button>
    </div>

    <div class="content">

      <!-- ─── HOME ─── -->
      <div class="panel active" id="panel-home">

        <!-- Project selector -->
        <div class="card" style="margin-bottom:16px;">
          <div class="scope-bar">
            <span class="scope-label">project</span>
            <select id="home-project" onchange="loadHome()" style="max-width:280px;">
              <option value="">— select a project —</option>
            </select>
            <button class="btn btn-ghost btn-sm" onclick="loadHome()" style="margin-left:8px;">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
              </svg>
              refresh
            </button>
            <span id="home-msg" style="font-size:11px;color:var(--text3);margin-left:8px;"></span>
          </div>
        </div>

        <div id="home-body">
          <div class="empty" style="margin-top:40px;">select a project above to load its architectural snapshot</div>
        </div>
      </div>

      <!-- ─── OVERVIEW ─── -->
      <div class="panel" id="panel-overview">

        <!-- Project scope selector -->
        <div class="card" style="margin-bottom:16px;">
          <div class="scope-bar">
            <span class="scope-label">project scope</span>
            <select id="proj-scope" onchange="onScopeChange()">
              <option value="">all projects</option>
            </select>
            <span style="font-size:10px;color:var(--text3);margin-left:4px;" id="scope-hint">showing global totals</span>
          </div>
        </div>

        <!-- Layer stat cards -->
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px;">

          <div class="card layer-card" id="card-cg">
            <div class="layer-header">
              <div>
                <div class="layer-name">call graph</div>
                <div class="layer-tech">sqlite · tree-sitter</div>
              </div>
              <div class="dot wait" id="dot-cg"></div>
            </div>
            <div class="layer-count" id="cnt-cg">—</div>
            <div class="layer-label">functions &amp; classes</div>
            <div class="layer-meta">edges: <span id="cnt-edges">—</span></div>
          </div>

          <div class="card layer-card" id="card-emb">
            <div class="layer-header">
              <div>
                <div class="layer-name">embeddings</div>
                <div class="layer-tech" id="emb-tech">sqlite-vec · vector index</div>
              </div>
              <div class="dot wait" id="dot-emb"></div>
            </div>
            <div class="layer-count" id="cnt-emb">—</div>
            <div class="layer-label">embedded functions</div>
            <div class="layer-meta">model: <span id="emb-model-val">—</span></div>
          </div>

          <div class="card layer-card" id="card-dec">
            <div class="layer-header">
              <div>
                <div class="layer-name">decision memory</div>
                <div class="layer-tech">sqlite-vec · sqlite</div>
              </div>
              <div class="dot wait" id="dot-dec"></div>
            </div>
            <div class="layer-count" id="cnt-dec">—</div>
            <div class="layer-label">decisions logged</div>
            <div class="layer-meta">linked functions: <span id="cnt-linked">—</span></div>
          </div>
        </div>

        <!-- Projects table -->
        <div class="card">
          <div class="section-header">
            <div class="section-title">indexed projects</div>
            <div class="section-sub" id="proj-table-sub">all projects · click a project ID to scope the view</div>
          </div>
          <div id="projects-wrap"><div class="empty">loading...</div></div>
        </div>
      </div>

      <!-- ─── SEARCH ─── -->
      <div class="panel" id="panel-search">
        <div class="card" style="margin-bottom:16px;">
          <div class="section-header">
            <div class="section-title">semantic similarity search</div>
            <div class="section-sub">find functions semantically similar to a code snippet or description</div>
          </div>
          <div style="padding:20px;display:flex;flex-direction:column;gap:14px;">
            <div style="display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end;">
              <div>
                <label class="field-label">project scope</label>
                <select id="search-project">
                  <option value="">all projects</option>
                </select>
              </div>
              <div>
                <label class="field-label">results</label>
                <select id="search-k" style="width:80px;">
                  <option value="5">5</option>
                  <option value="10" selected>10</option>
                  <option value="20">20</option>
                </select>
              </div>
            </div>
            <div>
              <label class="field-label">snippet or description</label>
              <textarea id="search-snippet" placeholder="paste a code snippet, function signature, or plain-language description..."></textarea>
            </div>
            <div style="display:flex;align-items:center;gap:12px;">
              <button class="btn btn-accent" id="search-btn" onclick="runSearch()">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                search
              </button>
              <span id="search-msg" style="font-size:11px;color:var(--text3);"></span>
            </div>
          </div>
        </div>

        <div class="card" id="search-results-card" style="display:none;">
          <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
            <div>
              <div class="section-title" id="search-results-title">results</div>
              <div class="section-sub" id="search-results-sub"></div>
            </div>
          </div>
          <div id="search-results-body"></div>
        </div>
      </div>

      <!-- ─── SETTINGS ─── -->
      <div class="panel" id="panel-settings">

        <div class="restart-banner" id="restart-banner" style="display:none;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
            <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
          </svg>
          pending config differs from running — restart to apply:
          <code>docker compose restart phronosis</code>
        </div>

        <div class="card" style="margin-bottom:16px;">
          <div class="section-header">
            <div class="section-title">embedding configuration</div>
            <div class="section-sub">saved to /data/config.json · restart required to apply</div>
          </div>
          <div style="padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:16px;">
            <div>
              <label class="field-label">provider</label>
              <select id="cfg-provider" onchange="onProviderChange()">
                <option value="openai">openai</option>
                <option value="ollama">ollama (local)</option>
              </select>
            </div>
            <div>
              <label class="field-label">model name</label>
              <input type="text" id="cfg-model" placeholder="text-embedding-3-small" oninput="updateDimHint()">
              <div class="field-hint" id="dim-hint"></div>
            </div>
            <div>
              <label class="field-label">vector dimensions</label>
              <input type="text" id="cfg-dim" placeholder="auto-detected for known models">
              <div class="field-hint">leave blank to auto-detect</div>
            </div>
            <div id="ollama-field" style="display:none;">
              <label class="field-label">ollama base url</label>
              <input type="text" id="cfg-ollama" placeholder="http://localhost:11434">
            </div>
          </div>
          <div style="padding:0 20px 20px;display:flex;align-items:center;gap:14px;">
            <button class="btn btn-accent" onclick="saveConfig()">save to config.json</button>
            <span id="save-msg" style="font-size:11px;color:var(--text3);"></span>
          </div>
        </div>

        <div class="card" style="margin-bottom:16px;">
          <div class="section-header">
            <div class="section-title">currently running</div>
            <div class="section-sub">loaded at server startup · restart to pick up config.json changes</div>
          </div>
          <div class="cfg-grid" id="running-cfg">
            <div style="color:var(--text3);font-size:12px;">loading...</div>
          </div>
        </div>

        <div class="card">
          <div class="section-header">
            <div class="section-title">api keys</div>
            <div class="section-sub">read from .env on the server · never transmitted to this interface</div>
          </div>
          <div style="padding:4px 22px;" id="keys-wrap">
            <div style="color:var(--text3);font-size:12px;padding:20px 0;">loading...</div>
          </div>
          <div style="padding:0 22px 22px;">
            <div style="font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--text3);margin-bottom:10px;">managing api keys</div>
            <div class="instr-box">
              <div class="instr-section">
                <div class="instr-step">1 · ssh into your server</div>
                <code class="cmd">ssh user@your-server</code>
                <code class="cmd">nano ~/Phronosis/.env</code>
              </div>
              <div class="instr-section">
                <div class="instr-step">2 · add or change a key</div>
                <div class="instr-text">add or update the line for the key:</div>
                <code class="cmd">ANTHROPIC_API_KEY=sk-ant-...</code>
                <code class="cmd">OPENAI_API_KEY=sk-...</code>
              </div>
              <div class="instr-section">
                <div class="instr-step">3 · apply changes</div>
                <code class="cmd">docker compose restart phronosis</code>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ─── CONTRACTS ─── -->
      <div class="panel" id="panel-contracts">

        <!-- Create form -->
        <div class="card" style="margin-bottom:16px;" id="contract-create-card">
          <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
            <div>
              <div class="section-title">new contract</div>
              <div class="section-sub">define an architectural rule in plain english — Phronosis generates enforceable examples</div>
            </div>
          </div>

          <!-- Step 1: Define -->
          <div id="contract-form-step" style="padding:20px;display:flex;flex-direction:column;gap:14px;">
            <div>
              <label class="field-label">title</label>
              <input type="text" id="ct-title" placeholder="e.g. Password DB access must use read_secrets">
            </div>
            <div>
              <label class="field-label">rule (plain english)</label>
              <textarea id="ct-rule" style="min-height:72px;" placeholder="e.g. any code that reads the password database with raw SQL must go through read_secrets instead"></textarea>
            </div>
            <div>
              <label class="field-label">apply to projects</label>
              <select id="ct-projects" multiple style="min-height:80px;padding:6px;"></select>
              <div style="font-size:10px;color:var(--text3);margin-top:4px;">hold Ctrl/Cmd to select multiple</div>
            </div>
            <div style="display:flex;align-items:center;gap:12px;">
              <button class="btn btn-accent" id="ct-generate-btn" onclick="generateContract()">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                </svg>
                generate examples
              </button>
              <span id="ct-msg" style="font-size:11px;color:var(--text3);"></span>
            </div>
          </div>

          <!-- Step 2: Review draft -->
          <div id="contract-draft-step" style="display:none;padding:20px;display:none;flex-direction:column;gap:16px;">
            <div style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--bg4);border:1px solid rgba(124,58,237,.2);border-radius:6px;">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--accent2)" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
              <span style="font-size:11px;color:var(--accent2);">draft generated — review and edit examples before approving</span>
            </div>

            <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
              <div>
                <label class="field-label" style="color:var(--red);">violation examples</label>
                <div style="font-size:10px;color:var(--text3);margin-bottom:8px;">code that breaks the rule — one per textarea</div>
                <div id="ct-viol-list" style="display:flex;flex-direction:column;gap:8px;"></div>
                <button class="btn btn-ghost btn-sm" style="margin-top:8px;" onclick="addExample('violation')">+ add violation</button>
              </div>
              <div>
                <label class="field-label" style="color:var(--green);">compliance examples</label>
                <div style="font-size:10px;color:var(--text3);margin-bottom:8px;">code that correctly follows the rule — one per textarea</div>
                <div id="ct-comp-list" style="display:flex;flex-direction:column;gap:8px;"></div>
                <button class="btn btn-ghost btn-sm" style="margin-top:8px;" onclick="addExample('compliance')">+ add compliance</button>
              </div>
            </div>

            <div>
              <label class="field-label">structural expression</label>
              <textarea id="ct-structural" style="min-height:60px;font-size:10px;color:var(--accent2);" readonly></textarea>
              <div style="font-size:10px;color:var(--text3);margin-top:4px;">auto-extracted from rule — used for deterministic call-graph checking</div>
            </div>

            <div style="display:flex;align-items:center;gap:12px;">
              <button class="btn btn-accent" id="ct-approve-btn" onclick="approveContract()">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="20 6 9 17 4 12"/>
                </svg>
                approve &amp; activate
              </button>
              <button class="btn btn-ghost btn-sm" onclick="discardDraft()">discard</button>
              <span id="ct-approve-msg" style="font-size:11px;color:var(--text3);"></span>
            </div>
          </div>
        </div>

        <!-- Active contracts list -->
        <div class="card" style="margin-bottom:16px;">
          <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
            <div>
              <div class="section-title">active contracts</div>
              <div class="section-sub" id="contracts-sub">loading...</div>
            </div>
            <div style="display:flex;align-items:center;gap:10px;">
              <select id="ct-filter-project" onchange="loadContracts()" style="width:160px;padding:6px 10px;font-size:11px;">
                <option value="">all projects</option>
              </select>
              <button class="btn btn-ghost btn-sm" onclick="loadContracts()">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
                </svg>
                refresh
              </button>
            </div>
          </div>
          <div id="contracts-list-body"><div class="empty">loading...</div></div>
        </div>

        <!-- Violations log -->
        <div class="card">
          <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;">
            <div>
              <div class="section-title">violations log</div>
              <div class="section-sub">structural and semantic violations detected at commit time</div>
            </div>
            <button class="btn btn-ghost btn-sm" onclick="loadViolations()">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
              </svg>
              refresh
            </button>
          </div>
          <div id="violations-body"><div class="empty">no violations logged yet</div></div>
        </div>

      </div>

      <!-- ─── ADMIN ─── -->
      <div class="panel" id="panel-admin">
        <div class="card">
          <div class="section-header">
            <div class="section-title">health check</div>
            <div class="section-sub">test live connectivity to all three layers</div>
          </div>
          <div style="padding:20px;">
            <button class="btn btn-accent" id="health-btn" onclick="runHealth()">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
              </svg>
              run health check
            </button>
            <div id="health-wrap" style="display:none;margin-top:16px;">
              <pre class="code-result" id="health-out"></pre>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>
</div>

<script>
  const KNOWN_DIMS = {
    'text-embedding-3-small':1536,'text-embedding-3-large':3072,
    'text-embedding-ada-002':1536,'nomic-embed-code':768,
    'nomic-embed-text':768,'mxbai-embed-large':1024,'all-minilm':384
  };
  const PAGE_META = {
    home:      ['home',      'architectural intelligence · subsystems · risk surface · wiring'],
    overview:  ['overview',  'system status · layer health · indexed projects'],
    search:    ['search',    'semantic similarity search across indexed projects'],
    contracts: ['contracts', 'invariant contracts · architectural rule enforcement'],
    settings:  ['settings',  'embedding config · api key management'],
    admin:     ['admin',     'diagnostics · maintenance tools'],
  };
  const KEY_DESC = {
    ANTHROPIC_API_KEY: 'required · generates one-line function summaries via claude haiku',
    OPENAI_API_KEY:    'required for openai embeddings · not needed when using ollama',
  };

  // All projects loaded from the last status call.
  let _allProjects = [];
  // Currently scoped project_id (empty = all).
  let _scopeId = '';

  // API key for authenticated write operations — persisted in localStorage.
  let _apiKey = localStorage.getItem('phronosis_api_key') || '';

  function writeHeaders(extra) {
    const h = {'Content-Type': 'application/json'};
    if (_apiKey) h['X-API-Key'] = _apiKey;
    return Object.assign(h, extra || {});
  }

  function promptApiKey(action) {
    const key = prompt('Enter your Phronosis API key to ' + action + ':\n(It will be saved in your browser for this session)');
    if (key) {
      _apiKey = key.trim();
      localStorage.setItem('phronosis_api_key', _apiKey);
    }
    return !!key.trim();
  }

  function setDot(id, state) { document.getElementById(id).className = 'dot ' + state; }
  function fmt(n) {
    if (n === undefined || n === null) return '—';
    return typeof n === 'number' ? n.toLocaleString() : n;
  }

  // ── Project scope selector ──────────────────────────────────────────────

  function populateProjectSelectors(projects) {
    ['proj-scope', 'search-project', 'ct-filter-project', 'home-project'].forEach(selId => {
      const sel = document.getElementById(selId);
      if (!sel) return;
      const cur = sel.value;
      while (sel.options.length > 1) sel.remove(1);
      projects.forEach(p => {
        const opt = new Option(`${p.id}  (${fmt(p.node_count)} fns)`, p.id);
        sel.add(opt);
      });
      if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
    });
    // Multi-select for contract creation.
    const ms = document.getElementById('ct-projects');
    if (ms) {
      const selected = [...ms.selectedOptions].map(o => o.value);
      ms.innerHTML = '';
      projects.forEach(p => {
        const opt = new Option(p.id, p.id);
        opt.selected = selected.includes(p.id);
        ms.add(opt);
      });
    }
  }

  function onScopeChange() {
    _scopeId = document.getElementById('proj-scope').value;
    const hint = document.getElementById('scope-hint');
    if (_scopeId) {
      hint.textContent = `scoped to project "${_scopeId}"`;
    } else {
      hint.textContent = 'showing global totals';
    }
    renderScopedStats();
  }

  function renderScopedStats() {
    if (!_allProjects.length) return;
    if (!_scopeId) {
      // Global: restore the last full-status totals.
      loadStatus();
      return;
    }
    const p = _allProjects.find(x => x.id === _scopeId);
    if (!p) return;

    document.getElementById('cnt-cg').textContent    = fmt(p.node_count);
    document.getElementById('cnt-edges').textContent = fmt(p.edge_count);
    document.getElementById('cnt-emb').textContent   = fmt(p.embedded);
    // Decisions are global (cross-project linking is intentional).
    document.getElementById('card-cg').className  = 'card layer-card ok';
    document.getElementById('card-emb').className = 'card layer-card ok';
    setDot('dot-cg', 'ok');
    setDot('dot-emb', 'ok');
  }

  // ── Status load ─────────────────────────────────────────────────────────

  async function loadStatus() {
    try {
      const d = await fetch('/api/status').then(r => r.json());

      setDot('conn-dot', 'ok');
      document.getElementById('conn-label').textContent = 'connected';

      const cg  = d.layers?.call_graph  || {};
      const emb = d.layers?.embeddings  || {};
      const dec = d.layers?.decisions   || {};

      if (!_scopeId) {
        setDot('dot-cg',  cg.status  === 'ok' ? 'ok' : 'error');
        setDot('dot-emb', emb.status === 'ok' ? 'ok' : 'error');
        document.getElementById('cnt-cg').textContent    = fmt(cg.nodes);
        document.getElementById('cnt-edges').textContent = fmt(cg.edges);
        document.getElementById('cnt-emb').textContent   = fmt(emb.functions);
        document.getElementById('card-cg').className  = 'card layer-card ' + (cg.status  === 'ok' ? 'ok' : 'error');
        document.getElementById('card-emb').className = 'card layer-card ' + (emb.status === 'ok' ? 'ok' : 'error');
      }

      document.getElementById('emb-model-val').textContent = emb.model || '—';
      document.getElementById('emb-tech').textContent      = `sqlite-vec · ${emb.dim || '—'}d · ${emb.provider || '—'}`;

      setDot('dot-dec', dec.status === 'ok' ? 'ok' : 'error');
      document.getElementById('cnt-dec').textContent    = fmt(dec.count);
      document.getElementById('cnt-linked').textContent = fmt(dec.linked_functions);
      document.getElementById('card-dec').className = 'card layer-card ' + (dec.status === 'ok' ? 'ok' : 'error');

      _allProjects = d.projects || [];
      renderProjects(_allProjects);
      populateProjectSelectors(_allProjects);
      populateSettings(d);

      // If a scope is active, re-apply it over the fresh data.
      if (_scopeId) renderScopedStats();

    } catch(e) {
      setDot('conn-dot', 'error');
      document.getElementById('conn-label').textContent = 'error';
    }
  }

  // ── Projects table ──────────────────────────────────────────────────────

  function scopeTo(pid) {
    document.getElementById('proj-scope').value = pid;
    _scopeId = pid;
    document.getElementById('scope-hint').textContent = `scoped to project "${pid}"`;
    renderScopedStats();
  }

  function renderProjects(projects) {
    const el = document.getElementById('projects-wrap');
    if (!projects.length) {
      el.innerHTML = `<div class="empty">no projects indexed yet<br><code>index_project("/path/to/project")</code></div>`;
      return;
    }
    el.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>project id</th><th>root path</th><th>functions</th><th>edges</th><th>embedded</th><th>last indexed</th>
        </tr></thead>
        <tbody>${projects.map(p => `
          <tr>
            <td><span class="proj-id-cell" onclick="scopeTo('${p.id}')" title="click to scope">${p.id}</span></td>
            <td class="path-cell" title="${p.root || ''}">${p.root || '—'}</td>
            <td>${fmt(p.node_count)}</td>
            <td>${fmt(p.edge_count)}</td>
            <td>${fmt(p.embedded)}</td>
            <td style="font-size:10px;color:var(--text3);">${p.last_indexed ? p.last_indexed.slice(0,16).replace('T',' ') : '—'}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  }

  // ── Semantic search ─────────────────────────────────────────────────────

  async function runSearch() {
    const snippet = document.getElementById('search-snippet').value.trim();
    if (!snippet) return;
    const projectId = document.getElementById('search-project').value;
    const k         = parseInt(document.getElementById('search-k').value, 10);
    const btn       = document.getElementById('search-btn');
    const msg       = document.getElementById('search-msg');
    const card      = document.getElementById('search-results-card');

    btn.disabled = true; btn.textContent = 'searching...';
    msg.textContent = ''; card.style.display = 'none';

    try {
      // Call query_similar_functions via the MCP HTTP transport isn't directly
      // accessible from the browser, so we expose it through /api/search.
      // For now we POST to /api/search which the server routes to query_similar.
      const body = { snippet, top_k: k };
      if (projectId) body.project_id = projectId;

      const res = await fetch('/api/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      }).then(r => r.json());

      const results = res.results || [];
      const scope   = projectId ? `project "${projectId}"` : 'all projects';
      document.getElementById('search-results-title').textContent = `${results.length} result${results.length !== 1 ? 's' : ''}`;
      document.getElementById('search-results-sub').textContent   = `${scope} · top ${k} by similarity`;

      if (!results.length) {
        document.getElementById('search-results-body').innerHTML =
          `<div class="empty">no results — try a different snippet or index more functions</div>`;
      } else {
        document.getElementById('search-results-body').innerHTML = results.map(r => `
          <div class="result-item">
            <div class="result-header">
              <span class="result-fn">${r.name || r.id}</span>
              <span class="result-score">${(r.similarity * 100).toFixed(1)}% match</span>
            </div>
            <div class="result-sig">${r.signature || ''}</div>
            ${r.summary ? `<div class="result-summary">${r.summary}</div>` : ''}
            <div class="result-meta">
              <span class="result-badge">${r.project_id || '—'}</span>
              ${r.file || ''}
            </div>
          </div>`).join('');
      }

      card.style.display = 'block';
    } catch(e) {
      msg.textContent = 'error: ' + e.message;
      msg.style.color = 'var(--red)';
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> search`;
    }
  }

  // ── Settings ────────────────────────────────────────────────────────────

  function populateSettings(d) {
    const cfg     = d.config || {};
    const pending = d.pending_config || {};
    const keys    = d.keys || {};

    const src = Object.keys(pending).length ? pending : cfg;
    if (src.EMBEDDING_PROVIDER) { document.getElementById('cfg-provider').value = src.EMBEDDING_PROVIDER; onProviderChange(); }
    if (src.EMBEDDING_MODEL)    document.getElementById('cfg-model').value  = src.EMBEDDING_MODEL;
    if (src.EMBEDDING_DIM)      document.getElementById('cfg-dim').value    = src.EMBEDDING_DIM;
    if (src.OLLAMA_BASE_URL)    document.getElementById('cfg-ollama').value = src.OLLAMA_BASE_URL;
    updateDimHint();

    const rows = Object.entries(cfg).filter(([,v]) => v);
    document.getElementById('running-cfg').innerHTML = rows.length
      ? rows.map(([k,v]) => `<div><div class="cfg-key">${k}</div><div class="cfg-val">${v}</div></div>`).join('')
      : `<div style="color:var(--text3);font-size:12px;">no running config loaded</div>`;

    document.getElementById('restart-banner').style.display = d.config_differs ? 'flex' : 'none';

    document.getElementById('keys-wrap').innerHTML = Object.entries(keys).map(([name, info]) => `
      <div class="key-row">
        <div>
          <div class="key-name">${name}</div>
          <div class="key-desc">${KEY_DESC[name] || ''}</div>
        </div>
        ${info.set
          ? `<span class="key-preview">${info.preview}</span>`
          : `<span style="font-size:11px;color:var(--text3);">—</span>`}
        <span class="badge ${info.set ? 'badge-set' : 'badge-unset'}">${info.set ? 'set' : 'not set'}</span>
      </div>`).join('');
  }

  function onProviderChange() {
    const p = document.getElementById('cfg-provider').value;
    document.getElementById('ollama-field').style.display = p === 'ollama' ? 'block' : 'none';
    updateDimHint();
  }

  function updateDimHint() {
    const model = (document.getElementById('cfg-model')?.value || '').trim();
    const dim   = KNOWN_DIMS[model];
    document.getElementById('dim-hint').textContent = dim ? `auto: ${dim} dimensions` : '';
  }

  async function saveConfig() {
    const payload = {
      EMBEDDING_PROVIDER: document.getElementById('cfg-provider').value,
      EMBEDDING_MODEL:    document.getElementById('cfg-model').value    || null,
      EMBEDDING_DIM:      document.getElementById('cfg-dim').value      || null,
      OLLAMA_BASE_URL:    document.getElementById('cfg-ollama').value   || null,
    };
    const msg = document.getElementById('save-msg');
    msg.textContent = 'saving...'; msg.style.color = 'var(--text3)';
    try {
      const d = await fetch('/api/config', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      }).then(r => r.json());
      if (d.status === 'ok') {
        msg.textContent = '✓ saved · restart to apply'; msg.style.color = 'var(--green)';
        document.getElementById('restart-banner').style.display = 'flex';
      } else {
        msg.textContent = 'error: ' + (d.detail || 'unknown'); msg.style.color = 'var(--red)';
      }
    } catch(e) {
      msg.textContent = 'request failed'; msg.style.color = 'var(--red)';
    }
  }

  // ── Admin / Health ──────────────────────────────────────────────────────

  async function runHealth() {
    const btn  = document.getElementById('health-btn');
    const wrap = document.getElementById('health-wrap');
    const out  = document.getElementById('health-out');
    btn.textContent = 'running...'; btn.disabled = true;
    wrap.style.display = 'none';
    try {
      const d = await fetch('/api/health').then(r => r.json());
      out.textContent = JSON.stringify(d, null, 2);
      wrap.style.display = 'block';
    } catch(e) {
      out.textContent = 'error: ' + e.message;
      wrap.style.display = 'block';
    } finally {
      btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg> run health check`;
      btn.disabled = false;
    }
  }

  // ── Contracts ───────────────────────────────────────────────────────────

  let _draftContractId = null;

  function showPanel(name) {
    // Override the earlier definition to also load contracts data.
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('panel-' + name).classList.add('active');
    document.getElementById('nav-' + name).classList.add('active');
    const [title, sub] = PAGE_META[name];
    document.getElementById('page-title').textContent = title;
    document.getElementById('page-sub').textContent   = sub;
    if (name === 'home' && document.getElementById('home-project').value) loadHome();
    if (name === 'contracts') {
      loadContracts();
      loadViolations();
    }
  }

  async function generateContract() {
    const title = document.getElementById('ct-title').value.trim();
    const rule  = document.getElementById('ct-rule').value.trim();
    const sel   = document.getElementById('ct-projects');
    const pids  = [...sel.selectedOptions].map(o => o.value);
    if (!title || !rule) {
      document.getElementById('ct-msg').textContent = 'title and rule are required';
      document.getElementById('ct-msg').style.color = 'var(--red)';
      return;
    }
    const btn = document.getElementById('ct-generate-btn');
    const msg = document.getElementById('ct-msg');
    btn.disabled = true; btn.textContent = 'generating...';
    msg.textContent = 'calling claude haiku to parse rule...'; msg.style.color = 'var(--text3)';
    try {
      if (!_apiKey && !promptApiKey('create a contract')) return;
      const d = await fetch('/api/contracts', {
        method: 'POST',
        headers: writeHeaders(),
        body: JSON.stringify({title, natural_language: rule, project_ids: pids}),
      }).then(r => r.json());
      if (d.status === 'error') throw new Error(d.detail);
      _draftContractId = d.id;
      showDraftReview(d);
    } catch(e) {
      msg.textContent = 'error: ' + e.message; msg.style.color = 'var(--red)';
    } finally {
      btn.disabled = false; btn.textContent = 'generate examples';
    }
  }

  function showDraftReview(contract) {
    document.getElementById('contract-form-step').style.display = 'none';
    const draft = document.getElementById('contract-draft-step');
    draft.style.display = 'flex';

    // Violation examples.
    const violList = document.getElementById('ct-viol-list');
    violList.innerHTML = '';
    (contract.violation_examples || []).forEach(code => {
      violList.appendChild(makeExampleTextarea(code, 'violation'));
    });

    // Compliance examples.
    const compList = document.getElementById('ct-comp-list');
    compList.innerHTML = '';
    (contract.compliance_examples || []).forEach(code => {
      compList.appendChild(makeExampleTextarea(code, 'compliance'));
    });

    // Structural expression (read-only preview).
    try {
      const s = JSON.parse(contract.structural_expression || '{}');
      document.getElementById('ct-structural').value = JSON.stringify(s, null, 2);
    } catch(_) {
      document.getElementById('ct-structural').value = contract.structural_expression || '{}';
    }
  }

  function makeExampleTextarea(code, kind) {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;';
    const ta = document.createElement('textarea');
    ta.value = code;
    ta.dataset.kind = kind;
    ta.style.cssText = 'min-height:80px;font-size:10px;line-height:1.5;border-color:' +
      (kind === 'violation' ? 'rgba(248,113,113,.3)' : 'rgba(74,222,128,.3)') + ';';
    const del = document.createElement('button');
    del.textContent = '×';
    del.className = 'btn btn-ghost btn-sm';
    del.style.cssText = 'position:absolute;top:4px;right:4px;padding:2px 7px;font-size:12px;';
    del.onclick = () => wrap.remove();
    wrap.appendChild(ta);
    wrap.appendChild(del);
    return wrap;
  }

  function addExample(kind) {
    const list = document.getElementById(kind === 'violation' ? 'ct-viol-list' : 'ct-comp-list');
    list.appendChild(makeExampleTextarea('', kind));
  }

  function discardDraft() {
    if (_draftContractId) {
      fetch(`/api/contracts/${_draftContractId}/deactivate`, {method:'POST', headers: writeHeaders()});
      _draftContractId = null;
    }
    resetCreateForm();
  }

  function resetCreateForm() {
    document.getElementById('contract-form-step').style.display = 'flex';
    document.getElementById('contract-draft-step').style.display = 'none';
    document.getElementById('ct-title').value = '';
    document.getElementById('ct-rule').value  = '';
    document.getElementById('ct-msg').textContent = '';
    document.getElementById('ct-approve-msg').textContent = '';
  }

  async function approveContract() {
    if (!_draftContractId) return;

    // Collect updated examples from textareas.
    const viols = [...document.querySelectorAll('#ct-viol-list textarea')].map(t => t.value).filter(Boolean);
    const comps = [...document.querySelectorAll('#ct-comp-list textarea')].map(t => t.value).filter(Boolean);

    const btn = document.getElementById('ct-approve-btn');
    const msg = document.getElementById('ct-approve-msg');
    btn.disabled = true; btn.textContent = 'activating...';
    msg.textContent = 'updating examples...'; msg.style.color = 'var(--text3)';

    try {
      // Save any edits first.
      await fetch(`/api/contracts/${_draftContractId}`, {
        method: 'PUT',
        headers: writeHeaders(),
        body: JSON.stringify({violation_examples: viols, compliance_examples: comps}),
      });

      msg.textContent = 'embedding examples...';
      const d = await fetch(`/api/contracts/${_draftContractId}/approve`, {
        method:'POST', headers: writeHeaders()
      }).then(r => r.json());
      if (d.status === 'error') throw new Error(d.detail);

      msg.textContent = '✓ contract activated'; msg.style.color = 'var(--green)';
      _draftContractId = null;
      setTimeout(() => { resetCreateForm(); loadContracts(); }, 1200);
    } catch(e) {
      msg.textContent = 'error: ' + e.message; msg.style.color = 'var(--red)';
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> approve &amp; activate';
    }
  }

  async function loadContracts() {
    const pid = document.getElementById('ct-filter-project')?.value || '';
    const url = '/api/contracts' + (pid ? `?project_id=${encodeURIComponent(pid)}` : '');
    const body = document.getElementById('contracts-list-body');
    const sub  = document.getElementById('contracts-sub');
    try {
      const d = await fetch(url).then(r => r.json());
      const contracts = d.contracts || [];
      sub.textContent = `${contracts.length} contract${contracts.length !== 1 ? 's' : ''} · click to expand`;
      if (!contracts.length) {
        body.innerHTML = `<div class="empty">no contracts yet — create one above</div>`;
        return;
      }
      body.innerHTML = contracts.map(c => renderContractRow(c)).join('');
    } catch(e) {
      body.innerHTML = `<div class="empty">error loading contracts: ${e.message}</div>`;
    }
  }

  function renderContractRow(c) {
    const statusColor = c.status === 'active' ? 'var(--green)' : 'var(--amber)';
    const pids = (c.project_ids || []).map(p =>
      `<span class="result-badge">${p}</span>`).join('');
    const violCount = (c.violation_examples || []).length;
    const compCount = (c.compliance_examples || []).length;

    const violHtml = (c.violation_examples || []).map((code, i) => `
      <div style="margin-bottom:6px;">
        <div style="font-size:9px;color:var(--red);margin-bottom:3px;letter-spacing:.08em;">VIOLATION ${i+1}</div>
        <textarea data-contract="${c.id}" data-kind="violation" data-idx="${i}"
          style="min-height:72px;font-size:10px;line-height:1.5;border-color:rgba(248,113,113,.3);"
          onchange="markEdited('${c.id}')">${escHtml(code)}</textarea>
      </div>`).join('');

    const compHtml = (c.compliance_examples || []).map((code, i) => `
      <div style="margin-bottom:6px;">
        <div style="font-size:9px;color:var(--green);margin-bottom:3px;letter-spacing:.08em;">COMPLIANCE ${i+1}</div>
        <textarea data-contract="${c.id}" data-kind="compliance" data-idx="${i}"
          style="min-height:72px;font-size:10px;line-height:1.5;border-color:rgba(74,222,128,.3);"
          onchange="markEdited('${c.id}')">${escHtml(code)}</textarea>
      </div>`).join('');

    return `
      <div style="border-bottom:1px solid var(--border);">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;cursor:pointer;"
             onclick="toggleContract('${c.id}')">
          <div style="flex:1;min-width:0;">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
              <span style="font-size:12px;font-weight:700;color:var(--text);">${escHtml(c.title)}</span>
              <span style="font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:${statusColor};">${c.status}</span>
            </div>
            <div style="font-size:10px;color:var(--text3);">${pids} &nbsp;${violCount} violations · ${compCount} compliance</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;margin-left:16px;">
            ${c.status === 'active'
              ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();deactivateContract('${c.id}')">deactivate</button>`
              : `<button class="btn btn-accent btn-sm" onclick="event.stopPropagation();reactivateContract('${c.id}')">activate</button>`}
            <button class="btn btn-ghost btn-sm" style="color:var(--red);" onclick="event.stopPropagation();deleteContract('${c.id}')">delete</button>
          </div>
        </div>
        <div id="ct-expand-${c.id}" style="display:none;padding:0 18px 18px;border-top:1px solid var(--border);">
          <div style="font-size:11px;color:var(--text2);margin:12px 0 14px;font-style:italic;">"${escHtml(c.natural_language)}"</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:14px;">
            <div>
              <div style="font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--red);margin-bottom:8px;">violations</div>
              ${violHtml || '<div style="color:var(--text3);font-size:11px;">none</div>'}
            </div>
            <div>
              <div style="font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--green);margin-bottom:8px;">compliance</div>
              ${compHtml || '<div style="color:var(--text3);font-size:11px;">none</div>'}
            </div>
          </div>
          <div id="ct-save-row-${c.id}" style="display:none;align-items:center;gap:10px;">
            <button class="btn btn-accent btn-sm" id="ct-save-btn-${c.id}"
              onclick="saveContractExamples('${c.id}')">save changes</button>
            <span id="ct-save-msg-${c.id}" style="font-size:11px;color:var(--text3);"></span>
          </div>
        </div>
      </div>`;
  }

  function escHtml(s) {
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function toggleContract(id) {
    const el = document.getElementById(`ct-expand-${id}`);
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
  }

  function markEdited(contractId) {
    const row = document.getElementById(`ct-save-row-${contractId}`);
    if (row) row.style.display = 'flex';
  }

  async function saveContractExamples(contractId) {
    const viols = [...document.querySelectorAll(`[data-contract="${contractId}"][data-kind="violation"]`)].map(t => t.value).filter(Boolean);
    const comps = [...document.querySelectorAll(`[data-contract="${contractId}"][data-kind="compliance"]`)].map(t => t.value).filter(Boolean);
    const btn = document.getElementById(`ct-save-btn-${contractId}`);
    const msg = document.getElementById(`ct-save-msg-${contractId}`);
    btn.disabled = true; btn.textContent = 'saving...';
    msg.textContent = ''; msg.style.color = 'var(--text3)';
    try {
      const d = await fetch(`/api/contracts/${contractId}`, {
        method: 'PUT',
        headers: writeHeaders(),
        body: JSON.stringify({violation_examples: viols, compliance_examples: comps}),
      }).then(r => r.json());
      if (d.status === 'error') throw new Error(d.detail);
      msg.textContent = '✓ saved · embeddings updated'; msg.style.color = 'var(--green)';
    } catch(e) {
      msg.textContent = 'error: ' + e.message; msg.style.color = 'var(--red)';
    } finally {
      btn.disabled = false; btn.textContent = 'save changes';
    }
  }

  async function deactivateContract(id) {
    await fetch(`/api/contracts/${id}/deactivate`, {method:'POST', headers: writeHeaders()});
    loadContracts();
  }

  async function reactivateContract(id) {
    const msg = document.createElement('span');
    try {
      await fetch(`/api/contracts/${id}/approve`, {method:'POST', headers: writeHeaders()});
      loadContracts();
    } catch(e) { console.error(e); }
  }

  async function deleteContract(id) {
    if (!confirm('Deactivate this contract? It will no longer be enforced.')) return;
    await fetch(`/api/contracts/${id}/deactivate`, {method:'POST', headers: writeHeaders()});
    loadContracts();
  }

  async function loadViolations() {
    const pid = document.getElementById('ct-filter-project')?.value || '';
    const url = '/api/violations' + (pid ? `?project_id=${encodeURIComponent(pid)}` : '');
    const body = document.getElementById('violations-body');
    try {
      const d = await fetch(url).then(r => r.json());
      const viols = d.violations || [];
      if (!viols.length) {
        body.innerHTML = `<div class="empty">no violations logged yet</div>`;
        return;
      }
      body.innerHTML = `<table class="data-table">
        <thead><tr>
          <th style="text-align:left;">function</th>
          <th style="text-align:left;">contract</th>
          <th>type</th><th>score</th><th>detected</th>
        </tr></thead>
        <tbody>${viols.map(v => `
          <tr>
            <td style="font-size:11px;color:var(--accent2);text-align:left;">${v.function_id}</td>
            <td style="text-align:left;font-size:11px;">${v.contract_title || v.contract_id}</td>
            <td><span style="font-size:9px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:${v.violation_type==='structural'?'var(--red)':'var(--amber)'};">${v.violation_type}</span></td>
            <td>${v.violation_type==='structural'?'—':(v.score*100).toFixed(0)+'%'}</td>
            <td style="font-size:10px;color:var(--text3);">${(v.detected_at||'').slice(0,16).replace('T',' ')}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
    } catch(e) {
      body.innerHTML = `<div class="empty">error: ${e.message}</div>`;
    }
  }

  // ── Project Home ────────────────────────────────────────────────────────

  async function loadHome() {
    const pid = document.getElementById('home-project').value;
    const body = document.getElementById('home-body');
    const msg  = document.getElementById('home-msg');
    if (!pid) {
      body.innerHTML = '<div class="empty" style="margin-top:40px;">select a project above to load its architectural snapshot</div>';
      return;
    }
    msg.textContent = 'loading...';
    try {
      const d = await fetch(`/api/project-home/${encodeURIComponent(pid)}`).then(r => r.json());
      if (d.status === 'error') throw new Error(d.detail);
      msg.textContent = `${d.function_count} functions`;
      body.innerHTML = renderHome(d);
    } catch(e) {
      msg.textContent = '';
      body.innerHTML = `<div class="empty">error: ${e.message}</div>`;
    }
  }

  function renderHome(d) {
    const sections = [];

    // ── Subsystems ──────────────────────────────────────────────────────
    sections.push(`
      <div class="card" style="margin-bottom:16px;">
        <div class="section-header">
          <div class="section-title">subsystems</div>
          <div class="section-sub">module groups · anchor class · what each layer does</div>
        </div>
        <div style="padding:4px 0;">
          ${(d.subsystems||[]).map(s => `
            <div style="display:grid;grid-template-columns:180px 60px 1fr;align-items:start;gap:16px;padding:12px 22px;border-bottom:1px solid var(--border);">
              <div>
                <div style="font-size:12px;font-weight:700;color:var(--accent2);">${s.name}</div>
                <div style="font-size:10px;color:var(--text3);margin-top:2px;">${s.anchor.split('.').slice(-1)[0]}</div>
              </div>
              <div style="font-size:11px;color:var(--text3);padding-top:2px;">${s.function_count} fns</div>
              <div style="font-size:11px;color:var(--text2);line-height:1.5;">${escHtml(s.anchor_summary||'—')}</div>
            </div>`).join('')}
        </div>
      </div>`);

    // ── Connections ──────────────────────────────────────────────────────
    if ((d.connections||[]).length) {
      sections.push(`
        <div class="card" style="margin-bottom:16px;">
          <div class="section-header">
            <div class="section-title">wiring</div>
            <div class="section-sub">cross-subsystem call graph — how the layers connect</div>
          </div>
          <div style="padding:4px 0;">
            ${(d.connections||[]).map(c => `
              <div style="display:flex;align-items:center;gap:12px;padding:10px 22px;border-bottom:1px solid var(--border);">
                <span style="font-size:11px;font-weight:700;color:var(--accent2);min-width:160px;">${c.from}</span>
                <svg width="16" height="10" viewBox="0 0 16 10" fill="none">
                  <path d="M0 5h13M9 1l4 4-4 4" stroke="var(--text3)" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
                <span style="font-size:11px;color:var(--text);">${c.to}</span>
                <span style="font-size:10px;color:var(--text3);margin-left:auto;">${c.edge_count} calls</span>
              </div>`).join('')}
          </div>
        </div>`);
    }

    // ── Chokepoints + Entry points ───────────────────────────────────────
    const riskRows = (d.risk_surface||[]).map(r =>
      `<tr>
        <td style="color:var(--red);text-align:left;font-size:11px;">${r.id.split('.').slice(-2).join('.')}</td>
        <td>${r.churn}</td><td>${r.caller_count}</td>
        <td><span style="font-size:9px;font-weight:700;color:var(--red);letter-spacing:.08em;">HIGH RISK</span></td>
      </tr>`).join('');

    const chkRows = (d.chokepoints||[]).map(c =>
      `<tr>
        <td style="color:var(--amber);text-align:left;font-size:11px;">${c.id.split('.').slice(-2).join('.')}</td>
        <td colspan="2">${c.caller_count} callers</td>
        <td><span style="font-size:9px;font-weight:700;color:var(--amber);letter-spacing:.08em;">CHOKEPOINT</span></td>
      </tr>`).join('');

    if (riskRows || chkRows) {
      sections.push(`
        <div class="card" style="margin-bottom:16px;">
          <div class="section-header">
            <div class="section-title">risk surface</div>
            <div class="section-sub">high-churn + high-impact functions · chokepoints · touch carefully</div>
          </div>
          <table class="data-table">
            <thead><tr><th style="text-align:left;">function</th><th>patches</th><th>callers</th><th>flag</th></tr></thead>
            <tbody>${riskRows}${chkRows}</tbody>
          </table>
        </div>`);
    }

    // ── Entry points ─────────────────────────────────────────────────────
    if ((d.entry_points||[]).length) {
      sections.push(`
        <div class="card" style="margin-bottom:16px;">
          <div class="section-header">
            <div class="section-title">entry points</div>
            <div class="section-sub">top of the call graph — nothing calls these</div>
          </div>
          <div style="padding:12px 22px;display:flex;flex-wrap:wrap;gap:8px;">
            ${(d.entry_points||[]).map(e =>
              `<span style="font-size:11px;background:var(--bg4);border:1px solid var(--border2);border-radius:4px;padding:4px 10px;color:var(--text2);">${e.name}</span>`
            ).join('')}
          </div>
        </div>`);
    }

    // ── Health + recent decisions ────────────────────────────────────────
    const h = d.health || {};
    const contractStatus = h.active_contract_count > 0
      ? (h.recent_violation_count === 0
          ? `<span style="color:var(--green);">${h.active_contract_count} active · 0 violations</span>`
          : `<span style="color:var(--red);">${h.active_contract_count} active · ${h.recent_violation_count} recent violations</span>`)
      : `<span style="color:var(--text3);">none</span>`;

    sections.push(`
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
        <div class="card">
          <div class="section-header">
            <div class="section-title">health</div>
          </div>
          <div style="padding:16px 22px;display:flex;flex-direction:column;gap:12px;">
            <div style="display:flex;justify-content:space-between;font-size:12px;">
              <span style="color:var(--text3);">contracts</span>${contractStatus}
            </div>
            <div style="display:flex;justify-content:space-between;font-size:12px;">
              <span style="color:var(--text3);">knowledge gaps</span>
              <span style="color:${h.knowledge_gap_count > 20 ? 'var(--amber)' : 'var(--text2)'};">${h.knowledge_gap_count} functions undocumented</span>
            </div>
            ${(h.churn_hotspots||[]).length ? `
            <div style="font-size:10px;color:var(--text3);border-top:1px solid var(--border);padding-top:10px;">
              <div style="font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px;">churn hotspots</div>
              ${h.churn_hotspots.map(c =>
                `<div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                  <span style="color:var(--text2);">${c.id.split('.').slice(-2).join('.')}</span>
                  <span style="color:var(--amber);">${c.decision_count} patches</span>
                </div>`
              ).join('')}
            </div>` : ''}
          </div>
        </div>

        <div class="card">
          <div class="section-header"><div class="section-title">recent decisions</div></div>
          <div style="padding:4px 0;">
            ${(d.recent_decisions||[]).length
              ? d.recent_decisions.map(dec => `
                <div style="padding:10px 18px;border-bottom:1px solid var(--border);">
                  <div style="display:flex;gap:8px;align-items:center;margin-bottom:3px;">
                    <span style="font-size:9px;font-weight:700;letter-spacing:.08em;color:var(--accent2);text-transform:uppercase;">${dec.type}</span>
                    <span style="font-size:10px;color:var(--text3);">${(dec.created_at||'').slice(0,10)}</span>
                  </div>
                  <div style="font-size:11px;color:var(--text2);line-height:1.5;">${escHtml(dec.description)}</div>
                </div>`).join('')
              : '<div class="empty" style="padding:20px;">no decisions logged yet</div>'}
          </div>
        </div>
      </div>`);

    return sections.join('');
  }

  window.addEventListener('DOMContentLoaded', loadStatus);
</script>
</body>
</html>"""
