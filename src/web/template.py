HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>code-intel</title>
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

    /* Subtle scanline */
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
    .path-cell { color:var(--text); font-size:11px; max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

    /* ── Form ── */
    .field-label { font-size:10px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--text3); margin-bottom:6px; display:block; }
    .field-hint  { font-size:10px; color:var(--accent2); margin-top:4px; min-height:14px; }

    input[type=text], select {
      width:100%; background:var(--bg4); border:1px solid var(--border2);
      border-radius:6px; padding:9px 12px;
      font-family:'Space Mono',monospace; font-size:12px; color:var(--text);
      outline:none; transition:border-color .15s, box-shadow .15s; appearance:none;
    }
    input:focus, select:focus { border-color:var(--accent); box-shadow:0 0 0 3px var(--glow); }
    input::placeholder { color:var(--text3); }
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
        code-intel
      </div>
      <div class="logo-sub">mcp server</div>
    </div>

    <nav>
      <div class="nav-item active" id="nav-overview" onclick="showPanel('overview')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
          <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
        </svg>
        overview
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

      <!-- ─── OVERVIEW ─── -->
      <div class="panel active" id="panel-overview">
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
                <div class="layer-tech" id="emb-tech">neo4j · vector index</div>
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
                <div class="layer-tech">graphiti · neo4j</div>
              </div>
              <div class="dot wait" id="dot-dec"></div>
            </div>
            <div class="layer-count" id="cnt-dec">—</div>
            <div class="layer-label">decisions logged</div>
            <div class="layer-meta">linked functions: <span id="cnt-linked">—</span></div>
          </div>
        </div>

        <div class="card">
          <div class="section-header">
            <div class="section-title">indexed projects</div>
            <div class="section-sub">projects with functions in the call graph</div>
          </div>
          <div id="projects-wrap"><div class="empty">loading...</div></div>
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
          <code>docker compose restart code-intel</code>
        </div>

        <!-- Embedding config form -->
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

        <!-- Running config -->
        <div class="card" style="margin-bottom:16px;">
          <div class="section-header">
            <div class="section-title">currently running</div>
            <div class="section-sub">loaded at server startup · restart to pick up config.json changes</div>
          </div>
          <div class="cfg-grid" id="running-cfg">
            <div style="color:var(--text3);font-size:12px;">loading...</div>
          </div>
        </div>

        <!-- API Keys -->
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
                <code class="cmd">nano ~/code-intel/.env</code>
              </div>
              <div class="instr-section">
                <div class="instr-step">2 · add or change a key</div>
                <div class="instr-text">add or update the line for the key:</div>
                <code class="cmd">ANTHROPIC_API_KEY=sk-ant-...</code>
                <code class="cmd">OPENAI_API_KEY=sk-...</code>
              </div>
              <div class="instr-section">
                <div class="instr-step">3 · delete an unused key</div>
                <div class="instr-text">remove the line or comment it out:</div>
                <code class="cmd"># OPENAI_API_KEY=sk-...   ← commented out = deleted</code>
              </div>
              <div class="instr-section">
                <div class="instr-step">4 · apply changes</div>
                <code class="cmd">docker compose restart code-intel</code>
              </div>
            </div>
          </div>
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
    overview: ['overview', 'system status · layer health · indexed projects'],
    settings: ['settings', 'embedding config · api key management'],
    admin:    ['admin',    'diagnostics · maintenance tools'],
  };
  const KEY_DESC = {
    ANTHROPIC_API_KEY: 'required · generates one-line function summaries via claude haiku',
    OPENAI_API_KEY:    'required for openai embeddings · not needed when using ollama',
  };

  function showPanel(name) {
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('panel-' + name).classList.add('active');
    document.getElementById('nav-' + name).classList.add('active');
    const [title, sub] = PAGE_META[name];
    document.getElementById('page-title').textContent = title;
    document.getElementById('page-sub').textContent   = sub;
  }

  function setDot(id, state) {
    document.getElementById(id).className = 'dot ' + state;
  }
  function fmt(n) {
    if (n === undefined || n === null) return '—';
    return typeof n === 'number' ? n.toLocaleString() : n;
  }

  async function loadStatus() {
    try {
      const d = await fetch('/api/status').then(r => r.json());

      setDot('conn-dot', 'ok');
      document.getElementById('conn-label').textContent = 'connected';

      // Call graph
      const cg = d.layers?.call_graph || {};
      setDot('dot-cg', cg.status === 'ok' ? 'ok' : 'error');
      document.getElementById('cnt-cg').textContent    = fmt(cg.nodes);
      document.getElementById('cnt-edges').textContent = fmt(cg.edges);
      document.getElementById('card-cg').className = 'card layer-card ' + (cg.status === 'ok' ? 'ok' : 'error');

      // Embeddings
      const emb = d.layers?.embeddings || {};
      setDot('dot-emb', emb.status === 'ok' ? 'ok' : 'error');
      document.getElementById('cnt-emb').textContent      = fmt(emb.functions);
      document.getElementById('emb-model-val').textContent = emb.model || '—';
      document.getElementById('emb-tech').textContent      = `neo4j · ${emb.dim || '—'}d · ${emb.provider || '—'}`;
      document.getElementById('card-emb').className = 'card layer-card ' + (emb.status === 'ok' ? 'ok' : 'error');

      // Decisions
      const dec = d.layers?.decisions || {};
      setDot('dot-dec', dec.status === 'ok' ? 'ok' : 'error');
      document.getElementById('cnt-dec').textContent    = fmt(dec.count);
      document.getElementById('cnt-linked').textContent = fmt(dec.linked_functions);
      document.getElementById('card-dec').className = 'card layer-card ' + (dec.status === 'ok' ? 'ok' : 'error');

      renderProjects(d.projects || []);
      populateSettings(d);
    } catch(e) {
      setDot('conn-dot', 'error');
      document.getElementById('conn-label').textContent = 'error';
    }
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
          <th>path</th><th>functions</th><th>edges</th><th>embedded</th>
        </tr></thead>
        <tbody>${projects.map(p => `
          <tr>
            <td class="path-cell" title="${p.path}">${p.path}</td>
            <td>${fmt(p.nodes)}</td>
            <td>${fmt(p.edges)}</td>
            <td>${fmt(p.embedded)}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  }

  function populateSettings(d) {
    const cfg     = d.config || {};
    const pending = d.pending_config || {};
    const keys    = d.keys || {};

    // Form: prefer pending (config.json) over running
    const src = Object.keys(pending).length ? pending : cfg;
    if (src.EMBEDDING_PROVIDER) { document.getElementById('cfg-provider').value = src.EMBEDDING_PROVIDER; onProviderChange(); }
    if (src.EMBEDDING_MODEL)    document.getElementById('cfg-model').value  = src.EMBEDDING_MODEL;
    if (src.EMBEDDING_DIM)      document.getElementById('cfg-dim').value    = src.EMBEDDING_DIM;
    if (src.OLLAMA_BASE_URL)    document.getElementById('cfg-ollama').value = src.OLLAMA_BASE_URL;
    updateDimHint();

    // Running config grid
    const rows = Object.entries(cfg).filter(([,v]) => v);
    document.getElementById('running-cfg').innerHTML = rows.length
      ? rows.map(([k,v]) => `<div><div class="cfg-key">${k}</div><div class="cfg-val">${v}</div></div>`).join('')
      : `<div style="color:var(--text3);font-size:12px;">no running config loaded</div>`;

    // Restart banner
    document.getElementById('restart-banner').style.display = d.config_differs ? 'flex' : 'none';

    // Keys
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

  window.addEventListener('DOMContentLoaded', loadStatus);
</script>
</body>
</html>"""
