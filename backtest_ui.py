"""
backtest_ui.py  --  Flask Blueprint for the Polymarket backtest dashboard.

Adds routes:
  GET  /backtest                   -- full backtest UI (6-tab SPA)
  GET  /api/backtest/health        -- system health check
  GET  /api/backtest/needs-review  -- NEEDS_REVIEW queue
  POST /api/backtest/run           -- run a point-in-time backtest

Registration (already done in dashboard.py):
  from backtest_ui import backtest_bp
  app.register_blueprint(backtest_bp)
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from flask import Blueprint, jsonify, request as _req

backtest_bp = Blueprint("backtest_ui", __name__)


# ── DB helper ─────────────────────────────────────────────────────────────────

def _bt_conn():
    """Return a read-only sqlite3.Connection to backtest.db, or None."""
    from backtest_db import get_conn, BACKTEST_DB
    if not os.path.exists(BACKTEST_DB):
        return None
    try:
        return get_conn(BACKTEST_DB)
    except Exception:
        return None


# ── Health endpoint ───────────────────────────────────────────────────────────

@backtest_bp.route("/api/backtest/health")
def api_health():
    """
    System health:
    - DB exists, wallet count
    - Latest snapshot / resolver timestamps
    - Stale data warnings (>24h)
    - Past-due unresolved count
    - NEEDS_REVIEW count
    """
    from backtest_db import get_conn, BACKTEST_DB

    now = datetime.now(timezone.utc)
    out = {
        "db_exists":          os.path.exists(BACKTEST_DB),
        "tracked_wallets":    0,
        "latest_snapshot_ts": None,
        "latest_resolved_ts": None,
        "unresolved_past_due": 0,
        "needs_review_count": 0,
        "snapshot_stale":     True,
        "resolver_stale":     True,
        "ok":                 False,
    }

    if not out["db_exists"]:
        return jsonify(out)

    try:
        conn = get_conn(BACKTEST_DB)

        out["tracked_wallets"] = conn.execute(
            "SELECT COUNT(*) FROM tracked_wallets WHERE is_active=1"
        ).fetchone()[0]

        snap_ts = conn.execute(
            "SELECT MAX(snapshot_ts) FROM position_snapshots"
        ).fetchone()[0]
        out["latest_snapshot_ts"] = snap_ts
        if snap_ts:
            t = datetime.fromisoformat(snap_ts.replace("Z", "+00:00"))
            out["snapshot_stale"] = (now - t) > timedelta(hours=24)

        res_ts = conn.execute(
            "SELECT MAX(resolved_at) FROM resolved_positions"
        ).fetchone()[0]
        out["latest_resolved_ts"] = res_ts
        if res_ts:
            t = datetime.fromisoformat(res_ts.replace("Z", "+00:00"))
            out["resolver_stale"] = (now - t) > timedelta(hours=24)

        today = now.date().isoformat()
        out["unresolved_past_due"] = conn.execute("""
            SELECT COUNT(DISTINCT ps.wallet_address || '|' || COALESCE(ps.token_id,''))
            FROM position_snapshots ps
            LEFT JOIN resolved_positions rp
                ON  rp.wallet_address = ps.wallet_address
                AND rp.token_id       = ps.token_id
                AND rp.condition_id   = ps.condition_id
            WHERE rp.resolved_id IS NULL
              AND ps.resolution_time IS NOT NULL
              AND ps.resolution_time <= ?
        """, (today,)).fetchone()[0]

        out["needs_review_count"] = conn.execute(
            "SELECT COUNT(*) FROM resolved_positions WHERE win_loss='NEEDS_REVIEW'"
        ).fetchone()[0]

        conn.close()
        out["ok"] = (
            not out["snapshot_stale"]
            and out["tracked_wallets"] > 0
        )
    except Exception as exc:
        out["error"] = str(exc)

    return jsonify(out)


# ── Needs-review endpoint ─────────────────────────────────────────────────────

@backtest_bp.route("/api/backtest/needs-review")
def api_needs_review():
    """Return all NEEDS_REVIEW entries from resolved_positions."""
    conn = _bt_conn()
    if conn is None:
        return jsonify({"data": [], "note": "backtest.db not found"})
    rows = conn.execute("""
        SELECT * FROM resolved_positions
        WHERE win_loss = 'NEEDS_REVIEW'
        ORDER BY resolved_at DESC
    """).fetchall()
    conn.close()
    return jsonify({"data": [dict(r) for r in rows]})


# ── Backtest runner endpoint ──────────────────────────────────────────────────

@backtest_bp.route("/api/backtest/run", methods=["POST"])
def api_run():
    """
    Run a point-in-time backtest with the provided filters.

    POST body (JSON):
      as_of          -- ISO-8601 UTC cutoff (optional)
      category       -- category filter (optional)
      wallet         -- wallet label/address substring (optional)
      min_size       -- minimum cost basis (default 0)
      resolve_window -- only positions resolving within N hours of first snap
      entry          -- "avg" | "snapshot"
      only_resolved  -- boolean
    """
    from backtest import load_positions, compute_metrics
    from backtest_db import init_db

    body           = _req.get_json(silent=True) or {}
    as_of          = body.get("as_of")    or None
    category       = body.get("category") or None
    wallet         = body.get("wallet")   or None
    min_size       = float(body.get("min_size") or 0)
    resolve_window: Optional[float] = None
    if body.get("resolve_window"):
        resolve_window = float(body["resolve_window"])
    entry          = body.get("entry", "avg")
    only_resolved  = bool(body.get("only_resolved", False))

    try:
        conn = init_db()
        positions = load_positions(
            conn,
            as_of=as_of,
            category=category,
            wallet=wallet,
            min_size=min_size,
            resolve_window_hours=resolve_window,
            only_resolved=only_resolved,
        )
        conn.close()
        metrics = compute_metrics(
            positions,
            entry_method=entry,
            as_of_label=as_of or "all-time",
        )
        return jsonify({"ok": True, "metrics": metrics})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Backtest dashboard page ───────────────────────────────────────────────────

@backtest_bp.route("/backtest")
def backtest_page():
    return _BACKTEST_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── HTML template ─────────────────────────────────────────────────────────────
# Single-page app with 6 tabs.  All JS uses fetch() against the API endpoints
# defined above + the existing /api/backtest/summary, /resolved, /snapshots.
# No Jinja2 template variables -- returned directly to avoid {{ }} escaping.

_BACKTEST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest — Polymarket Tracker</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#0e1412;color:#96b8a2;font:13px 'Segoe UI',system-ui,sans-serif;min-height:100vh}
a{color:inherit;text-decoration:none}

/* Header */
header{padding:12px 20px;border-bottom:1px solid #222e27;display:flex;align-items:center;gap:12px;background:#0e1412;flex-wrap:wrap}
h1{font-size:15px;font-weight:700;color:#34c759;letter-spacing:.2px}
.hbadge{background:#151c18;border:1px solid #222e27;border-radius:10px;padding:2px 9px;font-size:11px;color:#4d6659}
.nav-link{margin-left:auto;font-size:11px;color:#4d6659}
.nav-link:hover{color:#8aa898}
.hdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.hdot-ok{background:#34c759}.hdot-warn{background:#e6a817}.hdot-err{background:#f0606e}.hdot-dim{background:#2a3a30}

/* Tab nav */
.tab-nav{display:flex;border-bottom:1px solid #222e27;background:#0e1412;padding:0 20px;gap:0}
.tab-btn{background:none;border:none;border-bottom:2px solid transparent;padding:10px 16px;font:13px 'Segoe UI',system-ui;color:#4d6659;cursor:pointer;transition:color .1s,border-color .15s;white-space:nowrap}
.tab-btn:hover{color:#8aa898}
.tab-btn.active{color:#34c759;border-bottom-color:#34c759}

/* Panes */
.tab-pane{display:none;padding:20px}
.tab-pane.active{display:block}

/* KPI grid */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;margin-bottom:20px}
.kpi-card{background:#111814;border:1px solid #1e2921;border-radius:8px;padding:14px 16px}
.kpi-label{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#4d6659;margin-bottom:5px}
.kpi-val{font-size:22px;font-weight:700;color:#e0ece4;line-height:1}

/* Two-col layout */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:680px){.two-col{grid-template-columns:1fr}}

/* Panel */
.panel{background:#111814;border:1px solid #1e2921;border-radius:8px;padding:16px}
.panel h3{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:#4d6659;margin-bottom:12px}
.panel-row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #151c18;font-size:12px}
.panel-row:last-child{border-bottom:none}
.panel-row .lbl{color:#4d6659}
.warn-box{background:#1f1a10;border:1px solid #6b4c10;border-radius:5px;padding:8px 12px;margin-bottom:8px;font-size:12px;color:#e6a817}
.err-box{background:#1f1014;border:1px solid #6b1020;border-radius:5px;padding:8px 12px;margin-bottom:8px;font-size:12px;color:#f0606e}

/* Toolbar */
.tbar{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.tbar input,.tbar select{background:#151c18;color:#96b8a2;border:1px solid #222e27;border-radius:4px;padding:4px 9px;font:12px 'Segoe UI',system-ui}
.tbar input{min-width:160px}
.tbar-count{color:#4d6659;font-size:12px;margin-left:auto}

/* Tables */
.tscroll{overflow-x:auto;max-height:72vh}
.data-table{width:100%;border-collapse:collapse;font-size:12px}
.data-table th{text-align:left;padding:7px 10px;background:#111814;border-bottom:2px solid #222e27;color:#4d6659;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;position:sticky;top:0}
.data-table td{padding:6px 10px;border-bottom:1px solid #151c18;white-space:nowrap}
.data-table tr:hover td{background:#141f18}
.mw{max-width:220px;overflow:hidden;text-overflow:ellipsis}

/* Result colors */
.pos{color:#34c759}.neg{color:#f0606e}.gold{color:#e6a817}.dim{color:#4d6659}
.win-b{display:inline-block;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700;background:#0d2a18;color:#34c759;border:1px solid #1a5230}
.loss-b{display:inline-block;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700;background:#2a0d12;color:#f0606e;border:1px solid #521a20}
.push-b{display:inline-block;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700;background:#2a2208;color:#e6a817;border:1px solid #524010}
.rev-b{display:inline-block;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700;background:#1a1a1a;color:#5a7a68;border:1px solid #2a2a2a}

/* Runner */
.runner-wrap{display:grid;grid-template-columns:300px 1fr;gap:20px;align-items:start}
@media(max-width:780px){.runner-wrap{grid-template-columns:1fr}}
.runner-form{background:#111814;border:1px solid #1e2921;border-radius:8px;padding:18px}
.runner-form h3{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:#4d6659;margin-bottom:14px}
.frow{display:flex;align-items:center;margin-bottom:9px;gap:10px}
.frow label{font-size:12px;color:#4d6659;min-width:130px;flex-shrink:0}
.frow input,.frow select{background:#151c18;color:#96b8a2;border:1px solid #222e27;border-radius:4px;padding:5px 9px;font:12px 'Segoe UI',system-ui;flex:1}
.frow input[type=checkbox]{width:14px;height:14px;flex:none;cursor:pointer}
.btn-pri{background:#34c759;color:#0e1412;border:none;border-radius:5px;padding:8px 20px;font:600 13px 'Segoe UI',system-ui;cursor:pointer;margin-top:6px;transition:opacity .1s}
.btn-pri:hover{opacity:.85}.btn-pri:disabled{opacity:.4;cursor:default}
.btn-sec{background:#151c18;color:#8aa898;border:1px solid #222e27;border-radius:4px;padding:4px 10px;font:12px 'Segoe UI',system-ui;cursor:pointer}
.btn-sec:hover{border-color:#34c759;color:#34c759}
.runner-results{background:#111814;border:1px solid #1e2921;border-radius:8px;padding:18px}
.runner-results h2{font-size:13px;color:#34c759;margin-bottom:14px}
.r-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
.r-kpi{text-align:center;padding:10px;background:#0e1412;border-radius:6px;border:1px solid #1a2420}
.r-kpi .rl{font-size:10px;color:#4d6659;text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px}
.r-kpi .rv{font-size:17px;font-weight:700;color:#e0ece4}
.sec-title{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:#4d6659;margin:14px 0 8px}
.empty{padding:48px 20px;text-align:center;color:#4d6659}
</style>
</head>
<body>

<header>
  <h1>Polymarket Backtest</h1>
  <span class="hdot hdot-dim" id="hdot"></span>
  <span id="htext" style="font-size:11px;color:#4d6659">loading...</span>
  <a href="/" class="nav-link hbadge">← Live</a>
</header>

<nav class="tab-nav">
  <button class="tab-btn active" data-tab="overview"  onclick="switchTab(this)">Overview</button>
  <button class="tab-btn"        data-tab="perf"      onclick="switchTab(this)">Performance</button>
  <button class="tab-btn"        data-tab="resolved"  onclick="switchTab(this)">Resolved</button>
  <button class="tab-btn"        data-tab="snapshots" onclick="switchTab(this)">Snapshots</button>
  <button class="tab-btn"        data-tab="review"    onclick="switchTab(this)">Needs Review <span id="revBadge"></span></button>
  <button class="tab-btn"        data-tab="runner"    onclick="switchTab(this)">Runner</button>
</nav>

<!-- 1. Overview ─────────────────────────────────────────────────── -->
<div class="tab-pane active" id="pane-overview">
  <div class="kpi-grid" id="kpiGrid">
    <div class="kpi-card" style="grid-column:1/-1"><div class="kpi-label">Loading...</div></div>
  </div>
  <div class="two-col">
    <div class="panel" id="healthPanel"><h3>System Health</h3><div class="dim">Loading...</div></div>
    <div class="panel" id="bestPanel"><h3>Best / Worst</h3><div class="dim">Loading...</div></div>
  </div>
</div>

<!-- 2. Performance ──────────────────────────────────────────────── -->
<div class="tab-pane" id="pane-perf">
  <div class="tbar">
    <input id="perfSearch" placeholder="filter wallet..." oninput="renderPerf()">
    <select id="perfCat"  onchange="renderPerf()"><option value="">All categories</option></select>
    <select id="perfSort" onchange="renderPerf()">
      <option value="roi_pct">Sort: ROI %</option>
      <option value="total_pnl">Sort: P/L</option>
      <option value="total_cost_basis">Sort: Volume</option>
      <option value="win_rate">Sort: Win Rate</option>
      <option value="resolved_positions">Sort: Resolved</option>
    </select>
    <select id="perfDir" onchange="renderPerf()">
      <option value="-1">Desc</option><option value="1">Asc</option>
    </select>
    <label style="display:flex;align-items:center;gap:5px;font-size:12px;color:#4d6659;cursor:pointer">
      <input type="checkbox" id="perfAllOnly" onchange="renderPerf()"> All-wallets aggregate
    </label>
    <span id="perfCount" class="tbar-count"></span>
  </div>
  <div class="tscroll" id="perfTable"></div>
</div>

<!-- 3. Resolved ─────────────────────────────────────────────────── -->
<div class="tab-pane" id="pane-resolved">
  <div class="tbar">
    <select id="resolvedWL" onchange="renderResolved()">
      <option value="">All outcomes</option>
      <option value="WIN">WIN</option>
      <option value="LOSS">LOSS</option>
      <option value="PUSH">PUSH</option>
      <option value="NEEDS_REVIEW">NEEDS_REVIEW</option>
    </select>
    <select id="resolvedCat" onchange="renderResolved()"><option value="">All categories</option></select>
    <input id="resolvedSearch" placeholder="wallet / market..." oninput="renderResolved()">
    <span id="resolvedCount" class="tbar-count"></span>
  </div>
  <div class="tscroll" id="resolvedTable"></div>
</div>

<!-- 4. Snapshots ────────────────────────────────────────────────── -->
<div class="tab-pane" id="pane-snapshots">
  <div class="tbar">
    <select id="snapCat" onchange="renderSnaps()"><option value="">All categories</option></select>
    <input id="snapSearch" placeholder="wallet / market..." oninput="renderSnaps()">
    <span id="snapCount" class="tbar-count"></span>
  </div>
  <div class="tscroll" id="snapTable"></div>
</div>

<!-- 5. Needs Review ─────────────────────────────────────────────── -->
<div class="tab-pane" id="pane-review">
  <div class="tbar">
    <span id="reviewCount" class="tbar-count"></span>
    <button class="btn-sec" onclick="exportReviewCSV()">Export CSV</button>
  </div>
  <div class="tscroll" id="reviewTable"></div>
</div>

<!-- 6. Runner ───────────────────────────────────────────────────── -->
<div class="tab-pane" id="pane-runner">
  <div class="runner-wrap">
    <div class="runner-form">
      <h3>Parameters</h3>
      <div class="frow"><label>As-of (UTC)</label><input id="r_asof" placeholder="2026-01-01T00:00:00Z"></div>
      <div class="frow"><label>Category</label><input id="r_cat" placeholder="FIFA WC"></div>
      <div class="frow"><label>Wallet</label><input id="r_wallet" placeholder="@swisstony"></div>
      <div class="frow"><label>Min size ($)</label><input id="r_minsize" type="number" value="0" min="0" step="10"></div>
      <div class="frow"><label>Resolve window (h)</label><input id="r_window" type="number" placeholder="leave blank = all"></div>
      <div class="frow"><label>Entry method</label>
        <select id="r_entry">
          <option value="avg">avg (wallet average)</option>
          <option value="snapshot">snapshot price</option>
        </select>
      </div>
      <div class="frow"><label>Only resolved</label>
        <input id="r_onlyresolved" type="checkbox">
      </div>
      <button class="btn-pri" id="runBtn" onclick="runBacktest()">Run Backtest</button>
    </div>
    <div id="runnerResult"></div>
  </div>
</div>

<script>
"use strict";

// ── State ──────────────────────────────────────────────────────────
var perfData=null, resolvedData=null, snapData=null, reviewData=null;

// ── Tabs ───────────────────────────────────────────────────────────
function switchTab(btn) {
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active')});
  document.querySelectorAll('.tab-pane').forEach(function(p){p.classList.remove('active')});
  btn.classList.add('active');
  document.getElementById('pane-'+btn.dataset.tab).classList.add('active');
  var t = btn.dataset.tab;
  if      (t==='overview')  loadOverview();
  else if (t==='perf'    && !perfData)     loadPerf();
  else if (t==='resolved'&& !resolvedData) loadResolved();
  else if (t==='snapshots'&& !snapData)    loadSnaps();
  else if (t==='review'  && !reviewData)   loadReview();
}

// ── Utils ──────────────────────────────────────────────────────────
function $$(id){ return document.getElementById(id); }
function fmt1(v){ return v==null?'--':Number(v).toFixed(1); }
function fmt2(v){ return v==null?'--':Number(v).toFixed(2); }
function fmtPct(v){
  if(v==null) return '--';
  return (v>=0?'+':'')+Number(v).toFixed(1)+'%';
}
function fmtPnl(v){
  if(v==null) return '--';
  return (v>=0?'+$':'-$')+Math.abs(Number(v)).toFixed(0);
}
function fmtTs(v){ return v?v.slice(0,16).replace('T',' '):'--'; }
function fmtDate(v){ return v?v.slice(0,10):'--'; }
function clsPnl(v){ return Number(v)>=0?'pos':'neg'; }
function wlBadge(wl){
  if(wl==='WIN')  return '<span class="win-b">WIN</span>';
  if(wl==='LOSS') return '<span class="loss-b">LOSS</span>';
  if(wl==='PUSH') return '<span class="push-b">PUSH</span>';
  return '<span class="rev-b">REVIEW</span>';
}
function unique(arr, key){
  var seen={}, out=[];
  arr.forEach(function(r){ var v=r[key]; if(v&&!seen[v]){seen[v]=1;out.push(v);} });
  return out.sort();
}
function populateSel(id, vals, ph){
  var el=$$( id); if(!el) return;
  var cur=el.value;
  el.innerHTML='<option value="">'+ph+'</option>'+
    vals.map(function(v){ return '<option value="'+v+'"'+(v===cur?' selected':'')+'>'+v+'</option>'; }).join('');
}
function makeRows(rows, cols){
  if(!rows.length) return '<div class="empty">No data yet.</div>';
  var head='<tr>'+cols.map(function(c){
    return '<th style="text-align:'+(c.r?'right':'left')+'">'+(c.label||c.k)+'</th>';
  }).join('')+'</tr>';
  var body=rows.map(function(r){
    return '<tr>'+cols.map(function(c){
      var v=r[c.k];
      var disp = c.html ? c.html(v,r) : (c.fmt ? c.fmt(v,r) : (v!=null?v:'--'));
      var cls  = c.cls  ? c.cls(v,r)  : '';
      return '<td class="'+(c.mw?'mw ':'')+cls+'" style="text-align:'+(c.r?'right':'left')+'">'+disp+'</td>';
    }).join('')+'</tr>';
  }).join('');
  return '<table class="data-table"><thead>'+head+'</thead><tbody>'+body+'</tbody></table>';
}

// ── 1. Overview ────────────────────────────────────────────────────
function loadOverview(){
  Promise.all([
    fetch('/api/backtest/health').then(function(r){return r.json();}).catch(function(){return {};}),
    fetch('/api/backtest/summary').then(function(r){return r.json();}).catch(function(){return {data:[]};})
  ]).then(function(results){
    var h=results[0], sumRows=results[1].data||[];
    renderHeaderHealth(h);
    renderKPIs(h, sumRows);
    renderHealthPanel(h);
    renderBestWorst(sumRows);
    // surface needs-review badge
    if(h.needs_review_count>0){
      $$('revBadge').innerHTML='<span style="background:#2a1408;color:#e6a817;border-radius:8px;padding:1px 5px;font-size:10px;margin-left:4px">'+h.needs_review_count+'</span>';
    }
  });
}

function renderHeaderHealth(h){
  var dot=$$('hdot'), txt=$$('htext');
  if(!h.db_exists){
    dot.className='hdot hdot-err';
    txt.textContent='backtest.db missing -- run snapshot.py';
    txt.style.color='#f0606e';
  } else if(h.snapshot_stale){
    dot.className='hdot hdot-warn';
    txt.textContent='snapshot stale (>24 h)';
    txt.style.color='#e6a817';
  } else if(h.ok){
    dot.className='hdot hdot-ok';
    txt.textContent=(h.tracked_wallets||0)+' wallets tracked';
    txt.style.color='#34c759';
  } else {
    dot.className='hdot hdot-warn';
    txt.textContent='warning -- check health panel';
    txt.style.color='#e6a817';
  }
}

function renderKPIs(h, sumRows){
  var allRows=sumRows.filter(function(r){return r.category==='__all__';});
  var totalRes = allRows.reduce(function(s,r){return s+(r.resolved_positions||0);},0);
  var totalW   = allRows.reduce(function(s,r){return s+(r.wins||0);},0);
  var totalL   = allRows.reduce(function(s,r){return s+(r.losses||0);},0);
  var totalCost= allRows.reduce(function(s,r){return s+(r.total_cost_basis||0);},0);
  var totalPnl = allRows.reduce(function(s,r){return s+(r.total_pnl||0);},0);
  var roi      = totalCost>0?totalPnl/totalCost*100:0;
  var dec      = totalW+totalL;
  var wr       = dec>0?totalW/dec*100:0;

  var kpis=[
    {label:'Resolved',       val:totalRes,                    fmt:function(v){return v;}},
    {label:'Past-Due Open',  val:h.unresolved_past_due||0,    fmt:function(v){return v;},   cls:function(v){return v>0?'gold':'';}},
    {label:'Needs Review',   val:h.needs_review_count||0,     fmt:function(v){return v;},   cls:function(v){return v>0?'gold':'';}},
    {label:'Total P/L',      val:totalPnl,                    fmt:fmtPnl,                   cls:clsPnl},
    {label:'Overall ROI',    val:roi,                         fmt:fmtPct,                   cls:clsPnl},
    {label:'Win Rate',       val:wr,                          fmt:function(v){return fmt1(v)+'%';}},
    {label:'Tracked Wallets',val:h.tracked_wallets||0,        fmt:function(v){return v;}},
    {label:'Last Snapshot',  val:h.latest_snapshot_ts,        fmt:fmtTs},
    {label:'Last Resolved',  val:h.latest_resolved_ts,        fmt:fmtTs},
  ];
  $$('kpiGrid').innerHTML=kpis.map(function(k){
    var cls=k.cls?k.cls(k.val):'';
    return '<div class="kpi-card"><div class="kpi-label">'+k.label+'</div>'+
           '<div class="kpi-val '+cls+'">'+k.fmt(k.val)+'</div></div>';
  }).join('');
}

function renderHealthPanel(h){
  var warnings=[];
  if(!h.db_exists)         warnings.push({cls:'err-box',  msg:'backtest.db not found -- run: python snapshot.py'});
  if(h.snapshot_stale)     warnings.push({cls:'warn-box', msg:'Snapshot stale (>24 h) -- run: python snapshot.py'});
  if(h.resolver_stale)     warnings.push({cls:'warn-box', msg:'Resolver not run in >24 h -- run: python resolver.py'});
  if((h.unresolved_past_due||0)>0)
    warnings.push({cls:'warn-box', msg:h.unresolved_past_due+' past-due positions need resolving'});

  var rows=[
    ['DB exists',       h.db_exists?'<span class="pos">Yes</span>':'<span class="neg">No</span>'],
    ['Tracked wallets', h.tracked_wallets??'--'],
    ['Last snapshot',   fmtTs(h.latest_snapshot_ts)],
    ['Last resolve',    fmtTs(h.latest_resolved_ts)],
    ['Past-due open',   h.unresolved_past_due??'--'],
    ['Needs review',    h.needs_review_count??'--'],
    ['Snapshot stale',  h.snapshot_stale?'<span class="neg">Yes</span>':'<span class="pos">No</span>'],
    ['Resolver stale',  h.resolver_stale?'<span class="neg">Yes</span>':'<span class="pos">No</span>'],
  ];
  $$('healthPanel').innerHTML='<h3>System Health</h3>'+
    warnings.map(function(w){return '<div class="'+w.cls+'">'+w.msg+'</div>';}).join('')+
    rows.map(function(r){
      return '<div class="panel-row"><span class="lbl">'+r[0]+'</span><span>'+r[1]+'</span></div>';
    }).join('');
}

function renderBestWorst(sumRows){
  var wRows=sumRows.filter(function(r){return r.category==='__all__'&&(r.resolved_positions||0)>0;});
  var cRows=sumRows.filter(function(r){return r.category!=='__all__'&&(r.resolved_positions||0)>0;});
  wRows.sort(function(a,b){return (b.roi_pct||0)-(a.roi_pct||0);});
  cRows.sort(function(a,b){return (b.roi_pct||0)-(a.roi_pct||0);});

  function bwRow(label, r){
    if(!r) return '<div class="panel-row"><span class="lbl">'+label+'</span><span class="dim">--</span></div>';
    var name=r.label||r.wallet_address||r.category||'?';
    return '<div class="panel-row"><span class="lbl">'+label+'</span>'+
      '<span>'+name.slice(0,20)+' <span class="'+clsPnl(r.roi_pct)+'">'+fmtPct(r.roi_pct)+'</span></span></div>';
  }

  $$('bestPanel').innerHTML='<h3>Best / Worst</h3>'+
    bwRow('Best wallet',    wRows[0])+
    bwRow('Worst wallet',   wRows[wRows.length-1])+
    bwRow('Best category',  cRows[0])+
    bwRow('Worst category', cRows[cRows.length-1]);
}

// ── 2. Performance ─────────────────────────────────────────────────
function loadPerf(){
  fetch('/api/backtest/summary').then(function(r){return r.json();})
  .catch(function(){return {data:[]};})
  .then(function(res){
    perfData=(res.data||[]).map(function(r){
      return Object.assign({}, r, {label:r.label||r.wallet_address||'?'});
    });
    populateSel('perfCat', unique(perfData.filter(function(r){return r.category!=='__all__';}), 'category'), 'All categories');
    renderPerf();
  });
}

function renderPerf(){
  if(!perfData) return;
  var search   = ($$('perfSearch')?.value||'').toLowerCase();
  var catF     = $$('perfCat')?.value||'';
  var sortKey  = $$('perfSort')?.value||'roi_pct';
  var sortDir  = parseInt($$('perfDir')?.value||'-1');
  var allOnly  = $$('perfAllOnly')?.checked;

  var rows=perfData.filter(function(r){
    if(allOnly) return r.category==='__all__';
    return r.category!=='__all__';
  });
  if(catF)   rows=rows.filter(function(r){return r.category===catF;});
  if(search) rows=rows.filter(function(r){
    return (r.label||'').toLowerCase().includes(search)||
           (r.wallet_address||'').toLowerCase().includes(search);
  });
  rows.sort(function(a,b){return ((b[sortKey]||0)-(a[sortKey]||0))*sortDir;});

  $$('perfCount').textContent=rows.length+' rows';

  var cols=[
    {k:'label',           label:'Wallet',   mw:true},
    {k:'category',        label:'Category'},
    {k:'resolved_positions',label:'Res',    r:true},
    {k:'_wlp',            label:'W-L-P',    fmt:function(v,r){return (r.wins||0)+'-'+(r.losses||0)+'-'+(r.pushes||0);}},
    {k:'win_rate',        label:'WR%',      r:true, fmt:function(v){return v!=null?fmt1(v)+'%':'--';}},
    {k:'total_cost_basis',label:'Cost',     r:true, fmt:function(v){return '$'+Math.round(v||0);}},
    {k:'total_payout',    label:'Payout',   r:true, fmt:function(v){return '$'+Math.round(v||0);}},
    {k:'total_pnl',       label:'P/L',      r:true, fmt:fmtPnl, cls:clsPnl},
    {k:'roi_pct',         label:'ROI%',     r:true, fmt:fmtPct,  cls:clsPnl},
    {k:'rolling_7d_roi',  label:'7d',       r:true,
      fmt:function(v){return v!=null?fmtPct(v):'--';},
      cls:function(v){return v!=null?clsPnl(v):'dim';}},
    {k:'rolling_30d_roi', label:'30d',      r:true,
      fmt:function(v){return v!=null?fmtPct(v):'--';},
      cls:function(v){return v!=null?clsPnl(v):'dim';}},
    {k:'rolling_90d_roi', label:'90d',      r:true,
      fmt:function(v){return v!=null?fmtPct(v):'--';},
      cls:function(v){return v!=null?clsPnl(v):'dim';}},
    {k:'last_updated_at', label:'Updated',  fmt:fmtDate},
  ];
  $$('perfTable').innerHTML=makeRows(rows, cols);
}

// ── 3. Resolved ────────────────────────────────────────────────────
function loadResolved(){
  fetch('/api/backtest/resolved?limit=500').then(function(r){return r.json();})
  .catch(function(){return {data:[]};})
  .then(function(res){
    resolvedData=res.data||[];
    populateSel('resolvedCat', unique(resolvedData,'category'), 'All categories');
    renderResolved();
  });
}

function renderResolved(){
  if(!resolvedData) return;
  var wl    = $$('resolvedWL')?.value||'';
  var cat   = $$('resolvedCat')?.value||'';
  var search= ($$('resolvedSearch')?.value||'').toLowerCase();

  var rows=resolvedData.filter(function(r){
    if(wl&&r.win_loss!==wl) return false;
    if(cat&&r.category!==cat) return false;
    if(search&&!(r.wallet_label||'').toLowerCase().includes(search)&&
               !(r.market_title||'').toLowerCase().includes(search)) return false;
    return true;
  });
  $$('resolvedCount').textContent=rows.length+' positions';

  var cols=[
    {k:'resolved_at',    label:'Resolved',   fmt:fmtTs},
    {k:'wallet_label',   label:'Wallet',     mw:true},
    {k:'category',       label:'Cat'},
    {k:'market_title',   label:'Market',     mw:true},
    {k:'side',           label:'Side'},
    {k:'shares',         label:'Shares',     r:true, fmt:function(v){return v!=null?Math.round(v):'--';}},
    {k:'avg_entry_price',label:'Entry¢',r:true, fmt:function(v){return v!=null?fmt1(v)+'¢':'--';}},
    {k:'cost_basis',     label:'Cost',       r:true, fmt:function(v){return '$'+Math.round(v||0);}},
    {k:'payout_value',   label:'Payout',     r:true, fmt:function(v){return '$'+Math.round(v||0);}},
    {k:'realized_pnl',   label:'P/L',        r:true, fmt:fmtPnl, cls:clsPnl},
    {k:'roi_pct',        label:'ROI%',       r:true, fmt:fmtPct,  cls:clsPnl},
    {k:'holding_hours',  label:'Hold (h)',   r:true, fmt:function(v){return v!=null?Math.round(v):'--';}},
    {k:'win_loss',       label:'Result',     html:function(v){return wlBadge(v);}},
  ];
  $$('resolvedTable').innerHTML=makeRows(rows, cols);
}

// ── 4. Snapshots ───────────────────────────────────────────────────
function loadSnaps(){
  fetch('/api/backtest/snapshots?limit=500').then(function(r){return r.json();})
  .catch(function(){return {data:[]};})
  .then(function(res){
    snapData=res.data||[];
    populateSel('snapCat', unique(snapData,'category'), 'All categories');
    renderSnaps();
  });
}

function renderSnaps(){
  if(!snapData) return;
  var cat   = $$('snapCat')?.value||'';
  var search= ($$('snapSearch')?.value||'').toLowerCase();

  var rows=snapData.filter(function(r){
    if(cat&&r.category!==cat) return false;
    if(search&&!(r.wallet_label||'').toLowerCase().includes(search)&&
               !(r.market_title||'').toLowerCase().includes(search)) return false;
    return true;
  });
  $$('snapCount').textContent=rows.length+' positions';

  var cols=[
    {k:'snapshot_ts',    label:'Snapshot',   fmt:fmtTs},
    {k:'wallet_label',   label:'Wallet',     mw:true},
    {k:'category',       label:'Cat'},
    {k:'market_title',   label:'Market',     mw:true},
    {k:'side',           label:'Side'},
    {k:'shares',         label:'Shares',     r:true, fmt:function(v){return v!=null?Math.round(v):'--';}},
    {k:'avg_entry_price',label:'Avg¢',  r:true, fmt:function(v){return v!=null?fmt1(v)+'¢':'--';}},
    {k:'current_price',  label:'Cur¢',  r:true, fmt:function(v){return v!=null?fmt1(v)+'¢':'--';}},
    {k:'cost_basis',     label:'Cost',       r:true, fmt:function(v){return '$'+Math.round(v||0);}},
    {k:'current_value',  label:'Value',      r:true, fmt:function(v){return '$'+Math.round(v||0);}},
    {k:'unrealized_pnl', label:'Unrealized', r:true, fmt:fmtPnl, cls:clsPnl},
    {k:'resolution_time',label:'Resolves',   fmt:fmtDate},
    {k:'market_status',  label:'Status',     cls:function(v){return v==='resolved'?'dim':'';}},
  ];
  $$('snapTable').innerHTML=makeRows(rows, cols);
}

// ── 5. Needs Review ────────────────────────────────────────────────
function loadReview(){
  fetch('/api/backtest/needs-review').then(function(r){return r.json();})
  .catch(function(){return {data:[]};})
  .then(function(res){
    reviewData=res.data||[];
    $$('reviewCount').textContent=reviewData.length+' positions need manual review';
    renderReview();
  });
}

function renderReview(){
  if(!reviewData) return;
  var cols=[
    {k:'wallet_label',  label:'Wallet',      mw:true},
    {k:'category',      label:'Cat'},
    {k:'market_title',  label:'Market',      mw:true},
    {k:'condition_id',  label:'Condition ID',
      html:function(v){
        if(!v) return '--';
        var short=v.slice(0,16)+'...';
        return '<span style="font-family:monospace;font-size:11px;cursor:pointer" title="'+v+'" onclick="navigator.clipboard.writeText(\''+v+'\')">'+short+'</span>';
      }
    },
    {k:'token_id',      label:'Token ID',    fmt:function(v){return v?v.slice(0,14)+'...':'--';}},
    {k:'side',          label:'Side'},
    {k:'final_price',   label:'Last¢',  r:true, fmt:function(v){return v!=null&&v>=0?fmt1(v)+'¢':'--';}},
    {k:'resolved_at',   label:'Checked',     fmt:fmtTs},
    {k:'first_seen_at', label:'First Seen',  fmt:fmtTs},
  ];
  $$('reviewTable').innerHTML=makeRows(reviewData, cols);
}

function exportReviewCSV(){
  if(!reviewData||!reviewData.length) return;
  var keys=Object.keys(reviewData[0]);
  var csv=[keys.join(',')].concat(reviewData.map(function(r){
    return keys.map(function(k){return JSON.stringify(r[k]!=null?r[k]:'');}).join(',');
  })).join('\\n');
  var a=document.createElement('a');
  a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);
  a.download='needs_review_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
}

// ── 6. Runner ──────────────────────────────────────────────────────
function runBacktest(){
  var btn=$$('runBtn');
  btn.disabled=true; btn.textContent='Running...';
  $$('runnerResult').innerHTML='<div class="runner-results"><div class="dim">Running backtest...</div></div>';

  var rw=$$('r_window').value;
  var body={
    as_of:         $$('r_asof').value.trim()||null,
    category:      $$('r_cat').value.trim()||null,
    wallet:        $$('r_wallet').value.trim()||null,
    min_size:      parseFloat($$('r_minsize').value)||0,
    resolve_window:rw?parseFloat(rw):null,
    entry:         $$('r_entry').value,
    only_resolved: $$('r_onlyresolved').checked,
  };

  fetch('/api/backtest/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(function(r){return r.json();})
  .catch(function(e){return {ok:false,error:e.message};})
  .then(function(res){
    btn.disabled=false; btn.textContent='Run Backtest';
    if(!res.ok||!res.metrics){
      $$('runnerResult').innerHTML='<div class="runner-results"><div class="err-box">'+(res.error||'Unknown error')+'</div></div>';
      return;
    }
    var m=res.metrics;
    var dec=(m.wins||0)+(m.losses||0);
    var kpis=[
      ['Positions', m.total_positions,''],
      ['Resolved',  m.resolved,''],
      ['Wins',      m.wins,  'pos'],
      ['Losses',    m.losses,'neg'],
      ['Win Rate',  fmt1(m.win_rate_pct)+'%', ''],
      ['Cost',      '$'+Math.round(m.total_cost_basis||0),''],
      ['Payout',    '$'+Math.round(m.total_payout||0),''],
      ['P/L',       fmtPnl(m.total_pnl), clsPnl(m.total_pnl||0)],
      ['ROI',       fmtPct(m.roi_pct),   clsPnl(m.roi_pct||0)],
      ['7d ROI',    m.rolling_7d_roi!=null?fmtPct(m.rolling_7d_roi):'--', m.rolling_7d_roi!=null?clsPnl(m.rolling_7d_roi):'dim'],
      ['30d ROI',   m.rolling_30d_roi!=null?fmtPct(m.rolling_30d_roi):'--', m.rolling_30d_roi!=null?clsPnl(m.rolling_30d_roi):'dim'],
      ['Avg Size',  '$'+Math.round(m.avg_position_size||0),''],
    ];
    var html='<div class="runner-results"><h2>Backtest: '+m.as_of+'</h2>'+
      '<div class="r-kpis">'+kpis.map(function(k){
        return '<div class="r-kpi"><div class="rl">'+k[0]+'</div><div class="rv '+k[2]+'">'+k[1]+'</div></div>';
      }).join('')+'</div>';

    function miniTable(entries){
      return '<div class="tscroll"><table class="data-table"><thead><tr>'+
        ['Wallet/Category','W-L','WR%','Cost','P/L','ROI%'].map(function(c){return '<th>'+c+'</th>';}).join('')+
        '</tr></thead><tbody>'+
        entries.map(function(e){
          var name=e[0], b=e[1];
          var d=(b.wins||0)+(b.losses||0);
          return '<tr><td>'+name+'</td><td>'+(b.wins||0)+'-'+(b.losses||0)+'</td>'+
            '<td>'+(d>0?fmt1((b.wins||0)/d*100):'--')+'%</td>'+
            '<td>$'+Math.round(b.cost||0)+'</td>'+
            '<td class="'+clsPnl(b.pnl||0)+'">'+fmtPnl(b.pnl||0)+'</td>'+
            '<td class="'+clsPnl(b.roi||0)+'">'+fmtPct(b.roi||0)+'</td></tr>';
        }).join('')+
        '</tbody></table></div>';
    }

    var wE=Object.entries(m.by_wallet||{}).sort(function(a,b){return (b[1].pnl||0)-(a[1].pnl||0);});
    if(wE.length){
      html+='<div class="sec-title">By wallet</div>'+miniTable(wE);
    }
    var cE=Object.entries(m.by_category||{}).sort(function(a,b){return (b[1].pnl||0)-(a[1].pnl||0);});
    if(cE.length){
      html+='<div class="sec-title">By category</div>'+miniTable(cE);
    }
    html+='</div>';
    $$('runnerResult').innerHTML=html;
  });
}

// ── Boot ───────────────────────────────────────────────────────────
loadOverview();
</script>
</body>
</html>"""
