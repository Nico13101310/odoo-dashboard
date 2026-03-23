from flask import Flask, render_template_string, jsonify, request
import xmlrpc.client
import ssl
import json
import os
from datetime import datetime, date
from calendar import monthrange

app = Flask(__name__)

# ── Connexion Odoo ─────────────────────────────────────────
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

# ── Données : CA du mois ───────────────────────────────────
def get_ca_mois():
    models, uid = get_connection()
    today = date.today()
    date_debut = today.replace(day=1).strftime("%Y-%m-%d")
    date_fin = today.strftime("%Y-%m-%d")

    invoices = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "account.move", "search_read",
        [[
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["invoice_date", ">=", date_debut],
            ["invoice_date", "<=", date_fin],
        ]],
        {"fields": ["partner_id", "amount_untaxed"], "limit": 500}
    )

    ca_par_client = {}
    for inv in invoices:
        nom = inv["partner_id"][1] if inv["partner_id"] else "Inconnu"
        ca_par_client[nom] = ca_par_client.get(nom, 0) + inv["amount_untaxed"]

    ca_trie = sorted(ca_par_client.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "labels": [x[0] for x in ca_trie],
        "values": [round(x[1], 2) for x in ca_trie],
        "total": round(sum(ca_par_client.values()), 2)
    }

# ── Données : Factures en retard ───────────────────────────
def get_factures_retard():
    models, uid = get_connection()
    today = date.today().strftime("%Y-%m-%d")

    invoices = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "account.move", "search_read",
        [[
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["payment_state", "in", ["not_paid", "partial"]],
            ["invoice_date_due", "<", today],
        ]],
        {"fields": ["name", "partner_id", "amount_residual", "invoice_date_due"], "limit": 100}
    )

    result = []
    for inv in invoices:
        due = datetime.strptime(inv["invoice_date_due"], "%Y-%m-%d").date()
        retard = (date.today() - due).days
        result.append({
            "numero": inv["name"],
            "client": inv["partner_id"][1] if inv["partner_id"] else "Inconnu",
            "montant": round(inv["amount_residual"], 2),
            "echeance": inv["invoice_date_due"],
            "retard_jours": retard
        })

    result.sort(key=lambda x: x["retard_jours"], reverse=True)
    total = round(sum(x["montant"] for x in result), 2)
    return {"factures": result, "total": total, "count": len(result)}

# ── Données : Dépenses par compte ──────────────────────────
def get_depenses_mois():
    models, uid = get_connection()
    today = date.today()
    date_debut = today.replace(day=1).strftime("%Y-%m-%d")
    date_fin = today.strftime("%Y-%m-%d")

    lines = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "account.move.line", "search_read",
        [[
            ["move_id.move_type", "in", ["in_invoice", "in_refund"]],
            ["move_id.state", "=", "posted"],
            ["date", ">=", date_debut],
            ["date", "<=", date_fin],
            ["account_id.account_type", "in", ["expense", "expense_depreciation", "expense_direct_cost"]],
        ]],
        {"fields": ["account_id", "debit"], "limit": 500}
    )

    depenses_par_compte = {}
    for line in lines:
        compte = line["account_id"][1] if line["account_id"] else "Inconnu"
        depenses_par_compte[compte] = depenses_par_compte.get(compte, 0) + line["debit"]

    dep_trie = sorted(depenses_par_compte.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "labels": [x[0] for x in dep_trie],
        "values": [round(x[1], 2) for x in dep_trie],
        "total": round(sum(depenses_par_compte.values()), 2)
    }

# ── Template HTML ──────────────────────────────────────────
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
    --bg: #0f1117;
    --surface: #181c27;
    --surface2: #1e2333;
    --border: #2a2f45;
    --accent: #4f8ef7;
    --accent2: #f7634f;
    --accent3: #4ff7a0;
    --text: #e8eaf0;
    --text2: #8890a8;
    --danger: #f7634f;
    --warning: #f7c44f;
    --success: #4ff7a0;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    min-height: 100vh;
  }

  /* Header */
  header {
    padding: 24px 40px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--surface);
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .logo-icon {
    width: 36px; height: 36px;
    background: var(--accent);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
  }

  .logo-text {
    font-size: 18px;
    font-weight: 600;
    letter-spacing: -0.3px;
  }

  .logo-sub {
    font-size: 12px;
    color: var(--text2);
    font-family: 'DM Mono', monospace;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .date-badge {
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    color: var(--text2);
    background: var(--bg);
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
  }

  .refresh-btn {
    background: var(--accent);
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    font-family: 'DM Sans', sans-serif;
    transition: opacity 0.2s;
  }
  .refresh-btn:hover { opacity: 0.85; }

  /* Layout */
  main { padding: 32px 40px; max-width: 1400px; margin: 0 auto; }

  /* KPI Cards */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 32px;
  }

  .kpi-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    position: relative;
    overflow: hidden;
  }

  .kpi-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
  }
  .kpi-card.blue::before { background: var(--accent); }
  .kpi-card.red::before { background: var(--accent2); }
  .kpi-card.green::before { background: var(--accent3); }

  .kpi-label {
    font-size: 12px;
    color: var(--text2);
    text-transform: uppercase;
    letter-spacing: 1px;
    font-weight: 500;
    margin-bottom: 12px;
  }

  .kpi-value {
    font-size: 32px;
    font-weight: 600;
    letter-spacing: -1px;
    margin-bottom: 4px;
  }

  .kpi-value.blue { color: var(--accent); }
  .kpi-value.red { color: var(--accent2); }
  .kpi-value.green { color: var(--accent3); }

  .kpi-sub {
    font-size: 12px;
    color: var(--text2);
    font-family: 'DM Mono', monospace;
  }

  /* Charts Grid */
  .charts-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
  }

  .card-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--text);
  }

  .card-sub {
    font-size: 12px;
    color: var(--text2);
    font-family: 'DM Mono', monospace;
    margin-bottom: 20px;
  }

  canvas { max-height: 260px; }

  /* Table */
  .table-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 20px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 4px;
  }

  thead th {
    text-align: left;
    font-size: 11px;
    color: var(--text2);
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    font-weight: 500;
  }

  tbody tr {
    border-bottom: 1px solid var(--border);
    transition: background 0.15s;
  }
  tbody tr:hover { background: var(--surface2); }
  tbody tr:last-child { border-bottom: none; }

  tbody td {
    padding: 12px;
    font-size: 13px;
  }

  .retard-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    font-weight: 500;
  }

  .retard-low { background: rgba(247, 196, 79, 0.15); color: var(--warning); }
  .retard-mid { background: rgba(247, 99, 79, 0.15); color: var(--danger); }
  .retard-high { background: rgba(247, 99, 79, 0.3); color: var(--danger); }

  .montant {
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    font-weight: 500;
  }

  /* Loading */
  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 200px;
    color: var(--text2);
    font-size: 13px;
    gap: 10px;
  }

  .spinner {
    width: 16px; height: 16px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* Mois selector */
  .mois-label {
    font-size: 13px;
    color: var(--text2);
  }

  @media (max-width: 900px) {
    .kpi-grid { grid-template-columns: 1fr; }
    .charts-grid { grid-template-columns: 1fr; }
    main { padding: 20px; }
    header { padding: 16px 20px; }
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
  <div class="header-right">
    <div class="date-badge" id="dateBadge">—</div>
    <button class="refresh-btn" onclick="loadAll()">↻ Actualiser</button>
  </div>
</header>

<main>

  <!-- KPI Cards -->
  <div class="kpi-grid">
    <div class="kpi-card blue">
      <div class="kpi-label">CA du mois</div>
      <div class="kpi-value blue" id="kpiCA">—</div>
      <div class="kpi-sub" id="kpiCASub">Chargement...</div>
    </div>
    <div class="kpi-card red">
      <div class="kpi-label">Factures en retard</div>
      <div class="kpi-value red" id="kpiRetard">—</div>
      <div class="kpi-sub" id="kpiRetardSub">Chargement...</div>
    </div>
    <div class="kpi-card green">
      <div class="kpi-label">Dépenses du mois</div>
      <div class="kpi-value green" id="kpiDepenses">—</div>
      <div class="kpi-sub" id="kpiDepensesSub">Chargement...</div>
    </div>
  </div>

  <!-- Charts -->
  <div class="charts-grid">
    <div class="chart-card">
      <div class="card-title">Chiffre d'affaires du mois</div>
      <div class="card-sub">Top 10 clients — factures validées</div>
      <canvas id="chartCA"></canvas>
    </div>
    <div class="chart-card">
      <div class="card-title">Dépenses par compte comptable</div>
      <div class="card-sub">Top 10 comptes — mois en cours</div>
      <canvas id="chartDepenses"></canvas>
    </div>
  </div>

  <!-- Table factures en retard -->
  <div class="table-card">
    <div class="card-title">Factures en retard</div>
    <div class="card-sub">Factures impayées dont l'échéance est dépassée</div>
    <div id="tableRetard">
      <div class="loading"><div class="spinner"></div> Chargement...</div>
    </div>
  </div>

</main>

<script>
let chartCA = null;
let chartDep = null;

function fmt(n) {
  return new Intl.NumberFormat('fr-BE', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 }).format(n);
}

function updateDate() {
  const now = new Date();
  document.getElementById('dateBadge').textContent = now.toLocaleDateString('fr-BE', {
    weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
  });
}

async function loadCA() {
  const res = await fetch('/api/ca');
  const data = await res.json();

  document.getElementById('kpiCA').textContent = fmt(data.total);
  document.getElementById('kpiCASub').textContent = `${data.labels.length} client(s) actif(s) ce mois`;

  if (chartCA) chartCA.destroy();
  chartCA = new Chart(document.getElementById('chartCA'), {
    type: 'bar',
    data: {
      labels: data.labels,
      datasets: [{
        data: data.values,
        backgroundColor: 'rgba(79, 142, 247, 0.7)',
        borderColor: 'rgba(79, 142, 247, 1)',
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8890a8', font: { size: 11 } }, grid: { color: '#2a2f45' } },
        y: { ticks: { color: '#8890a8', font: { size: 11 }, callback: v => fmt(v) }, grid: { color: '#2a2f45' } }
      }
    }
  });
}

async function loadDepenses() {
  const res = await fetch('/api/depenses');
  const data = await res.json();

  document.getElementById('kpiDepenses').textContent = fmt(data.total);
  document.getElementById('kpiDepensesSub').textContent = `${data.labels.length} compte(s) de dépenses`;

  if (chartDep) chartDep.destroy();
  chartDep = new Chart(document.getElementById('chartDepenses'), {
    type: 'doughnut',
    data: {
      labels: data.labels,
      datasets: [{
        data: data.values,
        backgroundColor: [
          'rgba(79,142,247,0.8)', 'rgba(79,247,160,0.8)', 'rgba(247,99,79,0.8)',
          'rgba(247,196,79,0.8)', 'rgba(160,79,247,0.8)', 'rgba(79,220,247,0.8)',
          'rgba(247,79,196,0.8)', 'rgba(130,247,79,0.8)', 'rgba(247,150,79,0.8)', 'rgba(79,100,247,0.8)'
        ],
        borderWidth: 0,
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: 'right', labels: { color: '#8890a8', font: { size: 11 }, boxWidth: 12 } }
      }
    }
  });
}

async function loadRetard() {
  const res = await fetch('/api/retard');
  const data = await res.json();

  document.getElementById('kpiRetard').textContent = fmt(data.total);
  document.getElementById('kpiRetardSub').textContent = `${data.count} facture(s) en retard`;

  if (data.factures.length === 0) {
    document.getElementById('tableRetard').innerHTML = '<div class="loading">✅ Aucune facture en retard</div>';
    return;
  }

  let html = `<table>
    <thead><tr>
      <th>Numéro</th><th>Client</th><th>Montant dû</th><th>Échéance</th><th>Retard</th>
    </tr></thead><tbody>`;

  for (const f of data.factures) {
    const cls = f.retard_jours > 60 ? 'retard-high' : f.retard_jours > 30 ? 'retard-mid' : 'retard-low';
    html += `<tr>
      <td><span style="font-family:monospace;font-size:12px;color:#8890a8">${f.numero}</span></td>
      <td>${f.client}</td>
      <td><span class="montant">${fmt(f.montant)}</span></td>
      <td><span style="font-family:monospace;font-size:12px">${f.echeance}</span></td>
      <td><span class="retard-badge ${cls}">+${f.retard_jours}j</span></td>
    </tr>`;
  }

  html += '</tbody></table>';
  document.getElementById('tableRetard').innerHTML = html;
}

async function loadAll() {
  updateDate();
  await Promise.all([loadCA(), loadDepenses(), loadRetard()]);
}

loadAll();
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML, db=ODOO_DB)

@app.route("/api/ca")
def api_ca():
    return jsonify(get_ca_mois())

@app.route("/api/depenses")
def api_depenses():
    return jsonify(get_depenses_mois())

@app.route("/api/retard")
def api_retard():
    return jsonify(get_factures_retard())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
