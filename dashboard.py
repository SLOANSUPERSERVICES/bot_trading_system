"""
Crypto Strategy Tournament Dashboard
Serves a live KPI board reading state_*.json and log_*.txt from the trading folder.
Run: python dashboard.py
Open: http://localhost:5050  (or the ngrok URL printed at startup)
"""

import os, json, time, threading
from flask import Flask, jsonify, render_template_string, send_from_directory
from datetime import datetime

app = Flask(__name__)

def start_ngrok():
    """Try to start ngrok tunnel and print the public URL."""
    try:
        from pyngrok import ngrok as pyngrok
        public_url = pyngrok.connect(5050).public_url
        print("\n" + "=" * 55)
        print(f"  📱 PHONE URL  →  {public_url}")
        print("=" * 55 + "\n")
    except ImportError:
        print("\n  [ngrok] pyngrok not installed.")
        print("  Run: pip install pyngrok")
        print("  Then restart dashboard.py\n")
    except Exception as e:
        print(f"\n  [ngrok] Could not start tunnel: {e}")
        print("  Make sure ngrok is installed and authenticated.\n")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INSTANCES = [
    {"id": "momentum", "label": "MOMENTUM",  "coins": ["BTC","SOL","ETH"],    "color": "#3b82f6"},
    {"id": "defi",     "label": "DEFI",       "coins": ["MATIC","LINK","AVAX"],"color": "#8b5cf6"},
    {"id": "macro",    "label": "MACRO",      "coins": ["BTC","ETH"],          "color": "#10b981"},
]

def load_state(instance_id):
    path = os.path.join(BASE_DIR, f"state_{instance_id}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def load_log(instance_id, max_lines=50):
    path = os.path.join(BASE_DIR, f"log_{instance_id}.txt")
    try:
        with open(path) as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-max_lines:]][::-1]
    except Exception:
        return []

@app.route("/api/data")
def api_data():
    result = []
    for inst in INSTANCES:
        state = load_state(inst["id"])
        logs  = load_log(inst["id"], 30)
        result.append({
            "id":    inst["id"],
            "label": inst["label"],
            "color": inst["color"],
            "state": state,
            "logs":  logs,
        })
    return jsonify({"ts": datetime.now().isoformat(), "instances": result})

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Crypto Strategy Tournament</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0a0f;
  --surface: #13131a;
  --surface2: #1c1c28;
  --border: #2a2a3a;
  --text: #e8e8f0;
  --muted: #6b6b85;
  --green: #22c55e;
  --red: #ef4444;
  --yellow: #f59e0b;
  --blue: #3b82f6;
  --purple: #8b5cf6;
  --teal: #10b981;
  color-scheme: dark;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; min-height: 100%; }
body { padding: 16px; padding-top: max(16px, env(safe-area-inset-top, 0px)); }

h1 { font-size: 1.25rem; font-weight: 700; letter-spacing: 0.05em; color: #fff; }
.subtitle { font-size: 0.75rem; color: var(--muted); font-family: 'JetBrains Mono', monospace; }

header {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 20px;
}
.pulse-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--green); box-shadow: 0 0 0 0 rgba(34,197,94,0.4);
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%   { box-shadow: 0 0 0 0 rgba(34,197,94,0.4); }
  70%  { box-shadow: 0 0 0 8px rgba(34,197,94,0); }
  100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
}
.status-row { display: flex; align-items: center; gap: 8px; }

/* Summary bar */
.summary-bar {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px;
  margin-bottom: 24px;
}
.summary-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px;
}
.summary-card .label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 6px; }
.summary-card .val   { font-size: 1.4rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.summary-card .val.pos { color: var(--green); }
.summary-card .val.neg { color: var(--red); }
.summary-card .val.neu { color: var(--text); }

/* Instance columns */
.instances { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
.instance-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
}
.inst-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 16px 12px; border-bottom: 1px solid var(--border);
}
.inst-name { font-size: 0.85rem; font-weight: 700; letter-spacing: 0.08em; }
.inst-badge {
  font-size: 0.7rem; font-family: 'JetBrains Mono', monospace;
  background: var(--surface2); border-radius: 6px; padding: 3px 8px; color: var(--muted);
}
.inst-pnl { font-size: 1.1rem; font-weight: 700; font-variant-numeric: tabular-nums; }

/* Coin rows */
.coin-list { padding: 10px 16px; display: flex; flex-direction: column; gap: 8px; }
.coin-row {
  background: var(--surface2); border-radius: 8px; padding: 10px 12px;
  display: grid; grid-template-columns: 52px 1fr 1fr; gap: 8px; align-items: center;
}
.coin-sym { font-size: 0.78rem; font-weight: 600; }
.coin-price { font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; color: var(--muted); }
.coin-stats { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.coin-pnl { font-family: 'JetBrains Mono', monospace; font-size: 0.82rem; font-weight: 600; }
.coin-meta { font-size: 0.68rem; color: var(--muted); }

.bar-wrap { width: 100%; height: 4px; background: var(--border); border-radius: 2px; margin-top: 4px; }
.bar-fill  { height: 4px; border-radius: 2px; transition: width 0.4s; }

.tag { display: inline-block; font-size: 0.65rem; border-radius: 4px; padding: 1px 5px; font-weight: 600; }
.tag.buy  { background: rgba(34,197,94,0.15);  color: var(--green); }
.tag.sell { background: rgba(239,68,68,0.15);  color: var(--red); }
.tag.stop { background: rgba(245,158,11,0.15); color: var(--yellow); }
.tag.hold { background: rgba(107,107,133,0.12); color: var(--muted); }

/* Log section */
.log-section { padding: 0 16px 14px; }
.log-title { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 8px; }
.log-box {
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px; max-height: 160px; overflow-y: auto;
  font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; line-height: 1.6;
  display: flex; flex-direction: column; gap: 1px;
}
.log-line { color: #a0a0c0; white-space: pre-wrap; word-break: break-all; }
.log-line.buy  { color: #86efac; }
.log-line.sell { color: #fca5a5; }
.log-line.stop { color: #fde68a; }
.log-line.err  { color: #f97316; }

/* Divider */
.inst-divider { height: 1px; background: var(--border); margin: 0 16px 10px; }

.no-data { color: var(--muted); font-size: 0.78rem; padding: 16px; text-align: center; }
.spinner { color: var(--muted); font-size: 0.8rem; padding: 40px; text-align: center; }

footer {
  margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--border);
  font-size: 0.68rem; color: var(--muted); font-family: 'JetBrains Mono', monospace;
  display: flex; justify-content: space-between;
}
@media (max-width: 600px) {
  .instances { grid-template-columns: 1fr; }
  .summary-bar { grid-template-columns: repeat(2, 1fr); }
}
</style>
</head>
<body>
<header>
  <div>
    <h1>⚡ Strategy Tournament</h1>
    <div class="subtitle" id="last-update">Loading…</div>
  </div>
  <div class="status-row">
    <div class="pulse-dot"></div>
    <span style="font-size:0.75rem;color:var(--muted)">LIVE</span>
  </div>
</header>

<div class="summary-bar" id="summary-bar">
  <div class="summary-card"><div class="label">Session P&L</div><div class="val neu" id="s-pnl">—</div></div>
  <div class="summary-card"><div class="label">Total Trades</div><div class="val neu" id="s-trades">—</div></div>
  <div class="summary-card"><div class="label">Win Rate</div><div class="val neu" id="s-winrate">—</div></div>
  <div class="summary-card"><div class="label">Daily Spent</div><div class="val neu" id="s-spent">—</div></div>
  <div class="summary-card"><div class="label">Daily Cap</div><div class="val neu" id="s-cap">$40.00</div></div>
  <div class="summary-card"><div class="label">Leader 🏆</div><div class="val neu" id="s-leader">—</div></div>
</div>

<div class="instances" id="instances-grid">
  <div class="spinner">Loading data…</div>
</div>

<footer>
  <span>Crypto Strategy Tournament — RSI + BB + Momentum</span>
  <span id="footer-ts">—</span>
</footer>

<script>
const COLORS = { MOMENTUM: '#3b82f6', DEFI: '#8b5cf6', MACRO: '#10b981' };

function fmt(n, digits=2) {
  if (n == null || isNaN(n)) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(digits);
}
function fmtUSD(n) {
  if (n == null || isNaN(n)) return '—';
  return '$' + Math.abs(n).toFixed(2);
}
function colorClass(n) {
  if (n == null) return 'neu';
  return n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu';
}
function logClass(line) {
  const l = line.toLowerCase();
  if (l.includes('buy'))  return 'buy';
  if (l.includes('sell')) return 'sell';
  if (l.includes('stop')) return 'stop';
  if (l.includes('error') || l.includes('except')) return 'err';
  return '';
}
function tagForAction(action) {
  if (!action) return '<span class="tag hold">HOLD</span>';
  const a = action.toLowerCase();
  if (a.includes('buy'))  return '<span class="tag buy">BUY</span>';
  if (a.includes('sell')) return '<span class="tag sell">SELL</span>';
  if (a.includes('stop')) return '<span class="tag stop">STOP</span>';
  return '<span class="tag hold">' + action.toUpperCase() + '</span>';
}

function renderInstance(inst) {
  const { id, label, color, state, logs } = inst;
  if (!state) {
    return `<div class="instance-card">
      <div class="inst-header">
        <span class="inst-name" style="color:${color}">${label}</span>
        <span class="inst-badge">OFFLINE</span>
      </div>
      <div class="no-data">No state file found — is the bot running?</div>
    </div>`;
  }

  const pnl = state.session_pnl ?? 0;
  const spent = state.daily_total ?? 0;
  const cap   = state.daily_cap ?? 15;
  const ts    = state.ts ? state.ts.replace('T',' ').slice(0,19) : '—';
  const pnlClass = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu';
  const pnlColor = pnl > 0 ? '#22c55e' : pnl < 0 ? '#ef4444' : '#a0a0c0';

  // coin rows
  const coins = state.coins || {};
  let coinHtml = '';
  for (const [sym, c] of Object.entries(coins)) {
    const pnlPct = c.pnl_pct ?? 0;
    const pnlCol = pnlPct > 0 ? '#22c55e' : pnlPct < 0 ? '#ef4444' : '#a0a0c0';
    const wr = c.trades > 0 ? Math.round((c.wins / c.trades) * 100) : 0;
    const budgetUsedPct = Math.min(100, ((c.day_spent ?? 0) / (c.budget ?? 5)) * 100);
    const barColor = budgetUsedPct > 80 ? '#ef4444' : budgetUsedPct > 50 ? '#f59e0b' : color;
    const priceStr = c.price ? '$' + c.price.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2}) : '—';

    coinHtml += `
    <div class="coin-row">
      <div>
        <div class="coin-sym">${sym}</div>
        <div class="coin-price">${priceStr}</div>
        ${tagForAction(c.last_action)}
      </div>
      <div>
        <div style="font-size:0.7rem;color:var(--muted)">Spent</div>
        <div style="font-size:0.78rem;font-variant-numeric:tabular-nums">$${(c.day_spent??0).toFixed(2)} / $${(c.budget??5).toFixed(2)}</div>
        <div class="bar-wrap"><div class="bar-fill" style="width:${budgetUsedPct}%;background:${barColor}"></div></div>
      </div>
      <div class="coin-stats">
        <div class="coin-pnl" style="color:${pnlCol}">${fmt(pnlPct)}%</div>
        <div class="coin-meta">${c.trades??0}T ${c.wins??0}W ${c.losses??0}L</div>
        <div class="coin-meta">WR ${wr}%</div>
      </div>
    </div>`;
  }

  // logs
  const logLines = (logs || []).slice(0, 20).map(l =>
    `<div class="log-line ${logClass(l)}">${l}</div>`
  ).join('');

  const budgetPct = Math.min(100, (spent / cap) * 100);
  const budgetColor = budgetPct > 80 ? '#ef4444' : budgetPct > 50 ? '#f59e0b' : color;

  return `<div class="instance-card">
    <div class="inst-header">
      <span class="inst-name" style="color:${color}">${label}</span>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="inst-pnl" style="color:${pnlColor}">${fmt(pnl, 4)} USD</span>
        <span class="inst-badge">${ts}</span>
      </div>
    </div>

    <div style="padding:10px 16px 4px">
      <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--muted);margin-bottom:4px">
        <span>Daily Budget</span>
        <span style="font-variant-numeric:tabular-nums">$${spent.toFixed(2)} / $${cap.toFixed(2)}</span>
      </div>
      <div class="bar-wrap" style="height:6px">
        <div class="bar-fill" style="width:${budgetPct}%;background:${budgetColor};height:6px"></div>
      </div>
    </div>

    <div class="coin-list">${coinHtml || '<div class="no-data">No coin data yet</div>'}</div>

    <div class="inst-divider"></div>
    <div class="log-section">
      <div class="log-title">Recent Log</div>
      <div class="log-box">${logLines || '<div class="log-line">No log entries yet…</div>'}</div>
    </div>
  </div>`;
}

async function refresh() {
  try {
    const res = await fetch('/api/data');
    const data = await res.json();

    document.getElementById('last-update').textContent =
      'Last update: ' + data.ts.replace('T',' ').slice(0,19);
    document.getElementById('footer-ts').textContent = data.ts.replace('T',' ').slice(0,19);

    // Aggregate summary
    let totalPnl=0, totalTrades=0, totalWins=0, totalSpent=0, totalCap=0;
    let leader='—', leaderPnl=-Infinity;
    for (const inst of data.instances) {
      if (!inst.state) continue;
      const s = inst.state;
      totalPnl += s.session_pnl ?? 0;
      totalSpent += s.daily_total ?? 0;
      totalCap += s.daily_cap ?? 15;
      if ((s.session_pnl ?? 0) > leaderPnl) { leaderPnl = s.session_pnl; leader = inst.label; }
      for (const c of Object.values(s.coins || {})) {
        totalTrades += c.trades ?? 0;
        totalWins   += c.wins   ?? 0;
      }
    }
    const wr = totalTrades > 0 ? ((totalWins/totalTrades)*100).toFixed(1)+'%' : '—';

    const pnlEl = document.getElementById('s-pnl');
    pnlEl.textContent = (totalPnl>=0?'+':'') + totalPnl.toFixed(4);
    pnlEl.className = 'val ' + (totalPnl>0?'pos':totalPnl<0?'neg':'neu');
    document.getElementById('s-trades').textContent = totalTrades;
    document.getElementById('s-winrate').textContent = wr;
    document.getElementById('s-winrate').className = 'val ' + (parseFloat(wr)>50?'pos':parseFloat(wr)<40?'neg':'neu');
    document.getElementById('s-spent').textContent = '$' + totalSpent.toFixed(2);
    document.getElementById('s-cap').textContent = '$' + totalCap.toFixed(2);
    document.getElementById('s-leader').textContent = leader;

    // Render instances
    document.getElementById('instances-grid').innerHTML =
      data.instances.map(renderInstance).join('');

  } catch(e) {
    document.getElementById('last-update').textContent = 'Error: ' + e.message;
  }
}

refresh();
setInterval(refresh, 5000); // refresh every 5s
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    print("=" * 55)
    print("  ⚡ Crypto Strategy Tournament Dashboard")
    print("  Local  → http://localhost:5050")
    print("  Reads: state_momentum.json, state_defi.json, state_macro.json")
    print("  Ctrl+C to stop")
    print("=" * 55)
    # Start ngrok tunnel in background thread
    threading.Thread(target=start_ngrok, daemon=True).start()
    app.run(host="0.0.0.0", port=5050, debug=False)
