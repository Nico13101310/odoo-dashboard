from flask import Flask, render_template_string, jsonify, request
import xmlrpc.client
import ssl
import json
import os
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta

app = Flask(__name__)

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")

def get_connection():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", context=ssl_context)
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", context=ssl_context)
    return models, uid

def get_period_dates(period):
    today = date.today()
    if period == "today":
        return today, today
    elif period == "week":
        start = today - timedelta(days=today.weekday())
        return start, today
    elif period == "month":
        return today.replace(day=1), today
    elif period == "quarter":
        q = (today.month - 1) // 3
        start = date(today.year, q * 3 + 1, 1)
        return start, today
    elif period == "year":
        return today.replace(month=1, day=1), today
    else:
        return today.replace(day=1), today

def get_prev_period_dates(period):
    start, end = get_period_dates(period)
    return start - relativedelta(years=1), end - relativedelta(years=1)

def pct_change(current, previous):
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)

# ── CA ─────────────────────────────────────────────────────
def fetch_ca(date_debut, date_fin):
    models, uid = get_connection()
    invoices = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "account.move", "search_read",
        [[
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["invoice_date", ">=", str(date_debut)],
            ["invoice_date", "<=", str(date_fin)],
        ]],
        {"fields": ["partner_id", "amount_untaxed"], "limit": 500}
    )
    ca = {}
    for inv in invoices:
        nom = inv["partner_id"][1] if inv["partner_id"] else "Inconnu"
        ca[nom] = ca.get(nom, 0) + inv["amount_untaxed"]
    return ca

def get_ca(period):
    start, end = get_period_dates(period)
    prev_start, prev_end = get_prev_period_dates(period)
    current = fetch_ca(start, end)
    previous = fetch_ca(prev_start, prev_end)
    total_current = round(sum(current.values()), 2)
    total_previous = round(sum(previous.values()), 2)
    trie = sorted(current.items(), key=lambda x: x[1], reverse=True)[:10]
    prev_trie = sorted(previous.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "labels": [x[0] for x in trie],
        "values": [round(x[1], 2) for x in trie],
        "prev_labels": [x[0] for x in prev_trie],
        "prev_values": [round(x[1], 2) for x in prev_trie],
        "total": total_current,
        "total_prev": total_previous,
        "pct": pct_change(total_current, total_previous),
        "period_label": f"{start} → {end}",
        "prev_period_label": f"{prev_start} → {prev_end}",
    }

# ── Factures en retard ─────────────────────────────────────
def get_factures_retard(period):
    models, uid = get_connection()
    start, end = get_period_dates(period)
    prev_start, prev_end = get_prev_period_dates(period)
    today = date.today()

    def fetch(d_start, d_end):
        return models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "account.move", "search_read",
            [[
                ["move_type", "=", "out_invoice"],
                ["state", "=", "posted"],
                ["payment_state", "in", ["not_paid", "partial"]],
                ["invoice_date_due", "<", str(today)],
                ["invoice_date", ">=", str(d_start)],
                ["invoice_date", "<=", str(d_end)],
            ]],
            {"fields": ["name", "partner_id", "amount_residual", "invoice_date_due"], "limit": 200}
        )

    def process(invoices):
        result = []
        for inv in invoices:
            due = datetime.strptime(inv["invoice_date_due"], "%Y-%m-%d").date()
            retard = (today - due).days
            result.append({
                "numero": inv["name"],
                "client": inv["partner_id"][1] if inv["partner_id"] else "Inconnu",
                "montant": round(inv["amount_residual"], 2),
                "echeance": inv["invoice_date_due"],
                "retard_jours": retard
            })
        result.sort(key=lambda x: x["retard_jours"], reverse=True)
        return result

    current_list = process(fetch(start, end))
    previous_list = process(fetch(prev_start, prev_end))
    total_current = round(sum(x["montant"] for x in current_list), 2)
    total_previous = round(sum(x["montant"] for x in previous_list), 2)
    return {
        "factures": current_list,
        "total": total_current,
        "count": len(current_list),
        "total_prev": total_previous,
        "count_prev": len(previous_list),
        "pct": pct_change(total_current, total_previous),
        "period_label": f"{start} → {end}",
        "prev_period_label": f"{prev_start} → {prev_end}",
    }

# ── Dépenses ───────────────────────────────────────────────
def fetch_depenses(date_debut, date_fin):
    models, uid = get_connection()
    lines = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "account.move.line", "search_read",
        [[
            ["move_id.move_type", "in", ["in_invoice", "in_refund"]],
            ["move_id.state", "=", "posted"],
            ["date", ">=", str(date_debut)],
            ["date", "<=", str(date_fin)],
            ["account_id.account_type", "in", ["expense", "expense_depreciation", "expense_direct_cost"]],
        ]],
        {"fields": ["account_id", "debit"], "limit": 500}
    )
    dep = {}
    for line in lines:
        compte = line["account_id"][1] if line["account_id"] else "Inconnu"
        dep[compte] = dep.get(compte, 0) + line["debit"]
    return dep

def get_depenses(period):
    start, end = get_period_dates(period)
    prev_start, prev_end = get_prev_period_dates(period)
    current = fetch_depenses(start, end)
    previous = fetch_depenses(prev_start, prev_end)
    total_current = round(sum(current.values()), 2)
    total_previous = round(sum(previous.values()), 2)
    trie = sorted(current.items(), key=lambda x: x[1], reverse=True)[:10]
    prev_trie = sorted(previous.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "labels": [x[0] for x in trie],
        "values": [round(x[1], 2) for x in trie],
        "prev_labels": [x[0] for x in prev_trie],
        "prev_values": [round(x[1], 2) for x in prev_trie],
        "total": total_current,
        "total_prev": total_previous,
        "pct": pct_change(total_current, total_previous),
        "period_label": f"{start} → {end}",
        "prev_period_label": f"{prev_start} → {prev_end}",
    }

# ── HTML ───────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Odoo Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --surface: #181c27; --surface2: #1e2333; --border: #2a2f45;
    --accent: #4f8ef7; --accent2: #f7634f; --accent3: #4ff7a0;
    --text: #e8eaf0; --text2: #8890a8;
    --danger: #f7634f; --warning: #f7c44f; --success: #4ff7a0;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: 'DM Sans', sans-serif; min-height: 100vh; }

  header { padding: 20px 40px; border-bottom: 1px solid var(--border); background: var(--surface); display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .logo { display: flex; align-items: center; gap: 12px; }
  .logo-icon { width: 36px; height: 36px; background: var(--accent); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 18px; }
  .logo-text { font-size: 18px; font-weight: 600; }
  .logo-sub { font-size: 12px; color: var(--text2); font-family: 'DM Mono', monospace; }

  .global-filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .filter-label { font-size: 12px; color: var(--text2); margin-right: 4px; }
  .filter-btn { background: var(--bg); border: 1px solid var(--border); color: var(--text2); padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.15s; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active { background: var(--accent); border-color: var(--accent); color: white; }

  .header-right { display: flex; align-items: center; gap: 10px; }
  .date-badge { font-family: 'DM Mono', monospace; font-size: 11px; color: var(--text2); background: var(--bg); padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border); }
  .refresh-btn { background: var(--surface2); color: var(--text); border: 1px solid var(--border); padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 500; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.15s; }
  .refresh-btn:hover { border-color: var(--accent); color: var(--accent); }

  main { padding: 28px 40px; max-width: 1400px; margin: 0 auto; }

  .kpi-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 28px; }
  .kpi-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 22px; position: relative; overflow: hidden; }
  .kpi-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; }
  .kpi-card.blue::before { background: var(--accent); }
  .kpi-card.red::before { background: var(--accent2); }
  .kpi-card.green::before { background: var(--accent3); }
  .kpi-top { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 10px; }
  .kpi-label { font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: 1px; font-weight: 500; }
  .kpi-local-filter { display: flex; gap: 4px; }
  .kpi-filter-btn { background: none; border: 1px solid var(--border); color: var(--text2); padding: 2px 7px; border-radius: 4px; font-size: 10px; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.15s; }
  .kpi-filter-btn:hover { border-color: var(--accent); color: var(--accent); }
  .kpi-filter-btn.active { background: var(--accent); border-color: var(--accent); color: white; }
  .kpi-value { font-size: 28px; font-weight: 600; letter-spacing: -0.5px; margin-bottom: 6px; }
  .kpi-value.blue { color: var(--accent); }
  .kpi-value.red { color: var(--accent2); }
  .kpi-value.green { color: var(--accent3); }
  .kpi-bottom { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .kpi-sub { font-size: 11px; color: var(--text2); font-family: 'DM Mono', monospace; }
  .pct-badge { font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 4px; font-family: 'DM Mono', monospace; }
  .pct-up { background: rgba(79,247,160,0.15); color: var(--success); }
  .pct-down { background: rgba(247,99,79,0.15); color: var(--danger); }
  .pct-neutral { background: rgba(136,144,168,0.15); color: var(--text2); }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 22px; }
  .card-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 16px; gap: 12px; }
  .card-title { font-size: 14px; font-weight: 600; margin-bottom: 3px; }
  .card-sub { font-size: 11px; color: var(--text2); font-family: 'DM Mono', monospace; }
  .local-filters { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
  .local-filter-btn { background: none; border: 1px solid var(--border); color: var(--text2); padding: 3px 8px; border-radius: 4px; font-size: 11px; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.15s; }
  .local-filter-btn:hover { border-color: var(--accent); color: var(--accent); }
  .local-filter-btn.active { background: var(--accent); border-color: var(--accent); color: white; }
  .compare-toggle { display: flex; align-items: center; gap: 6px; margin-top: 6px; justify-content: flex-end; }
  .compare-toggle label { font-size: 11px; color: var(--text2); cursor: pointer; }
  .compare-toggle input { cursor: pointer; accent-color: var(--accent); }
  canvas { max-height: 240px; }

  .table-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 22px; margin-bottom: 20px; }
  table { width: 100%; border-collapse: collapse; margin-top: 4px; }
  thead th { text-align: left; font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: 1px; padding: 8px 12px; border-bottom: 1px solid var(--border); font-weight: 500; }
  tbody tr { border-bottom: 1px solid var(--border); transition: background 0.15s; }
  tbody tr:hover { background: var(--surface2); }
  tbody tr:last-child { border-bottom: none; }
  tbody td { padding: 11px 12px; font-size: 13px; }
  .retard-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-family: 'DM Mono', monospace; font-size: 11px; font-weight: 500; }
  .retard-low { background: rgba(247,196,79,0.15); color: var(--warning); }
  .retard-mid { background: rgba(247,99,79,0.15); color: var(--danger); }
  .retard-high { background: rgba(247,99,79,0.3); color: var(--danger); }
  .montant { font-family: 'DM Mono', monospace; font-size: 13px; font-weight: 500; }

  .loading { display: flex; align-items: center; justify-content: center; height: 160px; color: var(--text2); font-size: 13px; gap: 10px; }
  .spinner { width: 16px; height: 16px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 900px) {
    .kpi-grid { grid-template-columns: 1fr; }
    .charts-grid { grid-template-columns: 1fr; }
    main { padding: 16px; }
    header { padding: 16px; }
  }
</style>
</head>
<body>
<header>
  <div class="logo">
    <div class="logo-icon">📊</div>
    <div>
      <div class="logo-text">Odoo Dashboard</div>
      <div class="logo-sub">{{ db }}</div>
    </div>
  </div>
  <div class="global-filters">
    <span class="filter-label">Période globale :</span>
    <button class="filter-btn" data-period="today" onclick="setGlobal('today')">Aujourd'hui</button>
    <button class="filter-btn" data-period="week" onclick="setGlobal('week')">Semaine</button>
    <button class="filter-btn active" data-period="month" onclick="setGlobal('month')">Mois</button>
    <button class="filter-btn" data-period="quarter" onclick="setGlobal('quarter')">Trimestre</button>
    <button class="filter-btn" data-period="year" onclick="setGlobal('year')">Année</button>
  </div>
  <div class="header-right">
    <div class="date-badge" id="dateBadge">—</div>
    <button class="refresh-btn" onclick="loadAll()">↻ Actualiser</button>
  </div>
</header>

<main>
  <div class="kpi-grid">
    <div class="kpi-card blue">
      <div class="kpi-top">
        <div class="kpi-label">CA</div>
        <div class="kpi-local-filter">
          <button class="kpi-filter-btn active" onclick="setLocalKpi('ca','month',this)">M</button>
          <button class="kpi-filter-btn" onclick="setLocalKpi('ca','quarter',this)">T</button>
          <button class="kpi-filter-btn" onclick="setLocalKpi('ca','year',this)">A</button>
        </div>
      </div>
      <div class="kpi-value blue" id="kpiCA">—</div>
      <div class="kpi-bottom">
        <div class="kpi-sub" id="kpiCASub">Chargement...</div>
        <div id="kpiCAPct"></div>
      </div>
    </div>
    <div class="kpi-card red">
      <div class="kpi-top">
        <div class="kpi-label">Factures en retard</div>
        <div class="kpi-local-filter">
          <button class="kpi-filter-btn active" onclick="setLocalKpi('retard','month',this)">M</button>
          <button class="kpi-filter-btn" onclick="setLocalKpi('retard','quarter',this)">T</button>
          <button class="kpi-filter-btn" onclick="setLocalKpi('retard','year',this)">A</button>
        </div>
      </div>
      <div class="kpi-value red" id="kpiRetard">—</div>
      <div class="kpi-bottom">
        <div class="kpi-sub" id="kpiRetardSub">Chargement...</div>
        <div id="kpiRetardPct"></div>
      </div>
    </div>
    <div class="kpi-card green">
      <div class="kpi-top">
        <div class="kpi-label">Dépenses</div>
        <div class="kpi-local-filter">
          <button class="kpi-filter-btn active" onclick="setLocalKpi('depenses','month',this)">M</button>
          <button class="kpi-filter-btn" onclick="setLocalKpi('depenses','quarter',this)">T</button>
          <button class="kpi-filter-btn" onclick="setLocalKpi('depenses','year',this)">A</button>
        </div>
      </div>
      <div class="kpi-value green" id="kpiDepenses">—</div>
      <div class="kpi-bottom">
        <div class="kpi-sub" id="kpiDepensesSub">Chargement...</div>
        <div id="kpiDepensesPct"></div>
      </div>
    </div>
  </div>

  <div class="charts-grid">
    <div class="chart-card">
      <div class="card-header">
        <div>
          <div class="card-title">Chiffre d'affaires</div>
          <div class="card-sub" id="caChartSub">Top 10 clients</div>
        </div>
        <div>
          <div class="local-filters">
            <button class="local-filter-btn" onclick="setChartPeriod('ca','today',this)">Jour</button>
            <button class="local-filter-btn" onclick="setChartPeriod('ca','week',this)">Semaine</button>
            <button class="local-filter-btn active" onclick="setChartPeriod('ca','month',this)">Mois</button>
            <button class="local-filter-btn" onclick="setChartPeriod('ca','quarter',this)">Trimestre</button>
            <button class="local-filter-btn" onclick="setChartPeriod('ca','year',this)">Année</button>
          </div>
          <div class="compare-toggle">
            <input type="checkbox" id="compareCA" onchange="loadCA()">
            <label for="compareCA">vs année précédente</label>
          </div>
        </div>
      </div>
      <canvas id="chartCA"></canvas>
    </div>
    <div class="chart-card">
      <div class="card-header">
        <div>
          <div class="card-title">Dépenses par compte</div>
          <div class="card-sub" id="depChartSub">Top 10 comptes</div>
        </div>
        <div>
          <div class="local-filters">
            <button class="local-filter-btn" onclick="setChartPeriod('dep','today',this)">Jour</button>
            <button class="local-filter-btn" onclick="setChartPeriod('dep','week',this)">Semaine</button>
            <button class="local-filter-btn active" onclick="setChartPeriod('dep','month',this)">Mois</button>
            <button class="local-filter-btn" onclick="setChartPeriod('dep','quarter',this)">Trimestre</button>
            <button class="local-filter-btn" onclick="setChartPeriod('dep','year',this)">Année</button>
          </div>
          <div class="compare-toggle">
            <input type="checkbox" id="compareDep" onchange="loadDepenses()">
            <label for="compareDep">vs année précédente</label>
          </div>
        </div>
      </div>
      <canvas id="chartDep"></canvas>
    </div>
  </div>

  <div class="table-card">
    <div class="card-header">
      <div>
        <div class="card-title">Factures en retard</div>
        <div class="card-sub" id="retardTableSub">Factures impayées dont l'échéance est dépassée</div>
      </div>
      <div class="local-filters">
        <button class="local-filter-btn" onclick="setChartPeriod('retardTable','today',this)">Jour</button>
        <button class="local-filter-btn" onclick="setChartPeriod('retardTable','week',this)">Semaine</button>
        <button class="local-filter-btn active" onclick="setChartPeriod('retardTable','month',this)">Mois</button>
        <button class="local-filter-btn" onclick="setChartPeriod('retardTable','quarter',this)">Trimestre</button>
        <button class="local-filter-btn" onclick="setChartPeriod('retardTable','year',this)">Année</button>
      </div>
    </div>
    <div id="tableRetard"><div class="loading"><div class="spinner"></div> Chargement...</div></div>
  </div>
</main>

<script>
let chartCA = null, chartDep = null;
let periods = { ca: 'month', dep: 'month', retardTable: 'month' };
let localKpi = { ca: 'month', retard: 'month', depenses: 'month' };

function fmt(n) {
  return new Intl.NumberFormat('fr-BE', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 }).format(n);
}

function pctBadge(pct) {
  if (pct === null || pct === undefined) return '';
  const cls = pct > 0 ? 'pct-up' : pct < 0 ? 'pct-down' : 'pct-neutral';
  const sign = pct > 0 ? '+' : '';
  return `<span class="pct-badge ${cls}">${sign}${pct}% vs N-1</span>`;
}

function setGlobal(period) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.period === period));
  periods = { ca: period, dep: period, retardTable: period };
  localKpi = { ca: period, retard: period, depenses: period };
  document.querySelectorAll('.local-filter-btn, .kpi-filter-btn').forEach(b => {
    const labels = { today: 'Jour', week: 'Semaine', month: 'Mois', quarter: 'Trimestre', year: 'Année', M: 'month', T: 'quarter', A: 'year' };
    // reset all to inactive then re-activate matching
    b.classList.remove('active');
  });
  loadAll();
}

function setLocalKpi(kpi, period, btn) {
  localKpi[kpi] = period;
  btn.closest('.kpi-local-filter').querySelectorAll('.kpi-filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (kpi === 'ca') loadCA();
  if (kpi === 'retard') loadRetard();
  if (kpi === 'depenses') loadDepenses();
}

function setChartPeriod(chart, period, btn) {
  periods[chart] = period;
  btn.closest('.local-filters').querySelectorAll('.local-filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (chart === 'ca') loadCA();
  if (chart === 'dep') loadDepenses();
  if (chart === 'retardTable') loadRetard();
}

async function loadCA() {
  const compare = document.getElementById('compareCA').checked;
  const [chartData, kpiData] = await Promise.all([
    fetch(`/api/ca?period=${periods.ca}`).then(r => r.json()),
    fetch(`/api/ca?period=${localKpi.ca}`).then(r => r.json()),
  ]);
  document.getElementById('kpiCA').textContent = fmt(kpiData.total);
  document.getElementById('kpiCASub').textContent = `${kpiData.labels.length} client(s) — ${kpiData.period_label}`;
  document.getElementById('kpiCAPct').innerHTML = pctBadge(kpiData.pct);
  document.getElementById('caChartSub').textContent = `Top 10 clients — ${chartData.period_label}`;
  if (chartCA) chartCA.destroy();
  const datasets = [{ label: 'Période actuelle', data: chartData.values, backgroundColor: 'rgba(79,142,247,0.7)', borderColor: 'rgba(79,142,247,1)', borderWidth: 1, borderRadius: 4 }];
  if (compare) datasets.push({ label: 'Année précédente', data: chartData.prev_values, backgroundColor: 'rgba(79,142,247,0.2)', borderColor: 'rgba(79,142,247,0.5)', borderWidth: 1, borderRadius: 4 });
  chartCA = new Chart(document.getElementById('chartCA'), {
    type: 'bar', data: { labels: chartData.labels, datasets },
    options: { responsive: true, plugins: { legend: { display: compare, labels: { color: '#8890a8', font: { size: 11 } } } }, scales: { x: { ticks: { color: '#8890a8', font: { size: 10 } }, grid: { color: '#2a2f45' } }, y: { ticks: { color: '#8890a8', font: { size: 10 }, callback: v => fmt(v) }, grid: { color: '#2a2f45' } } } }
  });
}

async function loadDepenses() {
  const compare = document.getElementById('compareDep').checked;
  const [chartData, kpiData] = await Promise.all([
    fetch(`/api/depenses?period=${periods.dep}`).then(r => r.json()),
    fetch(`/api/depenses?period=${localKpi.depenses}`).then(r => r.json()),
  ]);
  document.getElementById('kpiDepenses').textContent = fmt(kpiData.total);
  document.getElementById('kpiDepensesSub').textContent = `${kpiData.labels.length} compte(s) — ${kpiData.period_label}`;
  document.getElementById('kpiDepensesPct').innerHTML = pctBadge(kpiData.pct);
  document.getElementById('depChartSub').textContent = `Top 10 comptes — ${chartData.period_label}`;
  if (chartDep) chartDep.destroy();
  const colors = ['rgba(79,142,247,0.8)','rgba(79,247,160,0.8)','rgba(247,99,79,0.8)','rgba(247,196,79,0.8)','rgba(160,79,247,0.8)','rgba(79,220,247,0.8)','rgba(247,79,196,0.8)','rgba(130,247,79,0.8)','rgba(247,150,79,0.8)','rgba(79,100,247,0.8)'];
  const datasets = [{ label: 'Période actuelle', data: chartData.values, backgroundColor: compare ? 'rgba(79,142,247,0.7)' : colors, borderWidth: 0 }];
  if (compare) datasets.push({ label: 'Année précédente', data: chartData.prev_values, backgroundColor: 'rgba(136,144,168,0.3)', borderWidth: 0 });
  chartDep = new Chart(document.getElementById('chartDep'), {
    type: compare ? 'bar' : 'doughnut', data: { labels: chartData.labels, datasets },
    options: { responsive: true, plugins: { legend: { position: compare ? 'top' : 'right', labels: { color: '#8890a8', font: { size: 11 }, boxWidth: 12 } } }, ...(compare ? { scales: { x: { ticks: { color: '#8890a8', font: { size: 10 } }, grid: { color: '#2a2f45' } }, y: { ticks: { color: '#8890a8', font: { size: 10 }, callback: v => fmt(v) }, grid: { color: '#2a2f45' } } } } : {}) }
  });
}

async function loadRetard() {
  const [tableData, kpiData] = await Promise.all([
    fetch(`/api/retard?period=${periods.retardTable}`).then(r => r.json()),
    fetch(`/api/retard?period=${localKpi.retard}`).then(r => r.json()),
  ]);
  document.getElementById('kpiRetard').textContent = fmt(kpiData.total);
  document.getElementById('kpiRetardSub').textContent = `${kpiData.count} facture(s) — ${kpiData.period_label}`;
  document.getElementById('kpiRetardPct').innerHTML = pctBadge(kpiData.pct);
  document.getElementById('retardTableSub').textContent = `Factures en retard — ${tableData.period_label}`;
  if (tableData.factures.length === 0) { document.getElementById('tableRetard').innerHTML = '<div class="loading">✅ Aucune facture en retard</div>'; return; }
  let html = `<table><thead><tr><th>Numéro</th><th>Client</th><th>Montant dû</th><th>Échéance</th><th>Retard</th></tr></thead><tbody>`;
  for (const f of tableData.factures) {
    const cls = f.retard_jours > 60 ? 'retard-high' : f.retard_jours > 30 ? 'retard-mid' : 'retard-low';
    html += `<tr><td><span style="font-family:monospace;font-size:12px;color:#8890a8">${f.numero}</span></td><td>${f.client}</td><td><span class="montant">${fmt(f.montant)}</span></td><td><span style="font-family:monospace;font-size:12px">${f.echeance}</span></td><td><span class="retard-badge ${cls}">+${f.retard_jours}j</span></td></tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('tableRetard').innerHTML = html;
}

async function loadAll() {
  document.getElementById('dateBadge').textContent = new Date().toLocaleDateString('fr-BE', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  await Promise.all([loadCA(), loadDepenses(), loadRetard()]);
}

loadAll();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML, db=ODOO_DB)

@app.route("/api/ca")
def api_ca():
    return jsonify(get_ca(request.args.get("period", "month")))

@app.route("/api/depenses")
def api_depenses():
    return jsonify(get_depenses(request.args.get("period", "month")))

@app.route("/api/retard")
def api_retard():
    return jsonify(get_factures_retard(request.args.get("period", "month")))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
