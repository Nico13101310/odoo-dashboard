
from flask import Flask, render_template_string, jsonify, request
import os
import ssl
import time
import xmlrpc.client
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

app = Flask(__name__)

ODOO_URL = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "120"))
ODOO_PAGE_SIZE = int(os.environ.get("ODOO_PAGE_SIZE", "200"))
ALLOW_INSECURE_SSL = os.environ.get("ODOO_INSECURE_SSL", "0") == "1"

_cache = {}


def _validate_env():
    missing = [
        key for key, value in {
            "ODOO_URL": ODOO_URL,
            "ODOO_DB": ODOO_DB,
            "ODOO_USERNAME": ODOO_USERNAME,
            "ODOO_API_KEY": ODOO_API_KEY,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError(f"Variables d'environnement manquantes : {', '.join(missing)}")


_validate_env()


def cached(ttl_seconds=CACHE_TTL):
    def decorator(func):
        def wrapper(*args):
            key = (func.__name__, args)
            now = time.time()
            item = _cache.get(key)
            if item and now - item["ts"] < ttl_seconds:
                return item["value"]
            value = func(*args)
            _cache[key] = {"ts": now, "value": value}
            return value
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


class OdooDashboardError(Exception):
    pass


def get_connection():
    try:
        ssl_context = None
        if ALLOW_INSECURE_SSL:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/common",
            context=ssl_context,
            allow_none=True,
        )
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
        if not uid:
            raise OdooDashboardError(
                "Authentification Odoo refusée. Vérifie ODOO_DB, ODOO_USERNAME et ODOO_API_KEY."
            )
        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/object",
            context=ssl_context,
            allow_none=True,
        )
        return models, uid
    except OdooDashboardError:
        raise
    except Exception as exc:
        raise OdooDashboardError(f"Connexion Odoo impossible : {exc}") from exc


def execute_kw(model, method, args=None, kwargs=None):
    args = args or []
    kwargs = kwargs or {}
    models, uid = get_connection()
    try:
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs)
    except xmlrpc.client.Fault as exc:
        raise OdooDashboardError(f"Erreur Odoo ({model}.{method}) : {exc}") from exc
    except Exception as exc:
        raise OdooDashboardError(f"Erreur réseau Odoo ({model}.{method}) : {exc}") from exc


VALID_PERIODS = {"today", "week", "month", "quarter", "year", "custom"}


def parse_custom_dates(date_from=None, date_to=None):
    if not date_from or not date_to:
        raise OdooDashboardError("Pour la période personnalisée, une date de début et une date de fin sont requises.")
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        raise OdooDashboardError("Format de date invalide. Utilise YYYY-MM-DD.") from exc
    if end < start:
        raise OdooDashboardError("La date de fin doit être postérieure ou égale à la date de début.")
    return start, end


def get_period_dates(period, date_from=None, date_to=None):
    period = period if period in VALID_PERIODS else "month"
    today = date.today()
    if period == "today":
        return today, today
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if period == "month":
        return today.replace(day=1), today
    if period == "quarter":
        q = (today.month - 1) // 3
        start = date(today.year, q * 3 + 1, 1)
        return start, today
    if period == "year":
        return today.replace(month=1, day=1), today
    return parse_custom_dates(date_from, date_to)


def get_prev_period_dates(period, date_from=None, date_to=None):
    start, end = get_period_dates(period, date_from, date_to)
    return start - relativedelta(years=1), end - relativedelta(years=1)


def pct_change(current, previous):
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


STATUS_LABELS = {
    "draft": "Devis",
    "sent": "Devis envoyé",
    "sale": "Commande",
    "done": "Terminé",
    "cancel": "Annulé",
    "upselling": "Upsell",
    "no": "Rien à facturer",
    "to invoice": "À facturer",
    "invoiced": "Entièrement facturé",
}


def label_for_status(value):
    return STATUS_LABELS.get(value, value or "—")


def summarize_top_items(mapping, limit=5):
    items = sorted(mapping.items(), key=lambda x: x[1], reverse=True)
    top = items[:limit]
    others_total = round(sum(v for _, v in items[limit:]), 2)
    if others_total > 0:
        top.append(("Autres", others_total))
    return top


def search_read_all(model, domain, fields, order=None, limit=ODOO_PAGE_SIZE):
    records = []
    offset = 0
    while True:
        batch = execute_kw(
            model,
            "search_read",
            [domain],
            {
                "fields": fields,
                "limit": limit,
                "offset": offset,
                **({"order": order} if order else {}),
            },
        )
        records.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return records


def make_period_label(start, end, period):
    if period == "custom":
        return f"Personnalisé — {start} → {end}"
    return f"{start} → {end}"


def get_odoo_record_url(model, record_id):
    return f"{ODOO_URL}/web#id={record_id}&model={model}&view_type=form"


@cached()
def fetch_ca(date_debut, date_fin):
    invoices = search_read_all(
        "account.move",
        [
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["invoice_date", ">=", str(date_debut)],
            ["invoice_date", "<=", str(date_fin)],
        ],
        ["partner_id", "amount_untaxed"],
        order="amount_untaxed desc",
    )
    ca = {}
    for inv in invoices:
        nom = inv["partner_id"][1] if inv.get("partner_id") else "Inconnu"
        ca[nom] = ca.get(nom, 0) + (inv.get("amount_untaxed") or 0)
    return ca


@cached()
def get_ca(period, date_from=None, date_to=None):
    start, end = get_period_dates(period, date_from, date_to)
    prev_start, prev_end = get_prev_period_dates(period, date_from, date_to)
    current = fetch_ca(start, end)
    previous = fetch_ca(prev_start, prev_end)
    total_current = round(sum(current.values()), 2)
    total_previous = round(sum(previous.values()), 2)
    trie = summarize_top_items(current, limit=5)
    prev_trie = summarize_top_items(previous, limit=5)
    return {
        "labels": [x[0] for x in trie],
        "values": [round(x[1], 2) for x in trie],
        "prev_labels": [x[0] for x in prev_trie],
        "prev_values": [round(x[1], 2) for x in prev_trie],
        "total": total_current,
        "total_prev": total_previous,
        "pct": pct_change(total_current, total_previous),
        "period_label": make_period_label(start, end, period),
        "prev_period_label": make_period_label(prev_start, prev_end, period),
    }


@cached()
def get_factures_retard(period, date_from=None, date_to=None):
    start, end = get_period_dates(period, date_from, date_to)
    prev_start, prev_end = get_prev_period_dates(period, date_from, date_to)
    today = date.today()

    def fetch(d_start, d_end):
        return search_read_all(
            "account.move",
            [
                ["move_type", "=", "out_invoice"],
                ["state", "=", "posted"],
                ["payment_state", "in", ["not_paid", "partial"]],
                ["invoice_date_due", "!=", False],
                ["invoice_date_due", "<", str(today)],
                ["invoice_date", ">=", str(d_start)],
                ["invoice_date", "<=", str(d_end)],
            ],
            ["id", "name", "partner_id", "amount_residual", "invoice_date_due"],
            order="invoice_date_due asc",
        )

    def process(invoices):
        result = []
        for inv in invoices:
            due_raw = inv.get("invoice_date_due")
            if not due_raw:
                continue
            due = datetime.strptime(due_raw, "%Y-%m-%d").date()
            retard = (today - due).days
            result.append({
                "id": inv.get("id"),
                "numero": inv.get("name") or "—",
                "client": inv["partner_id"][1] if inv.get("partner_id") else "Inconnu",
                "montant": round(inv.get("amount_residual") or 0, 2),
                "echeance": due_raw,
                "retard_jours": retard,
                "priority": "Critique" if retard > 60 else "Haute" if retard > 30 else "Normale",
                "url": get_odoo_record_url("account.move", inv.get("id")) if inv.get("id") else None,
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
        "period_label": make_period_label(start, end, period),
        "prev_period_label": make_period_label(prev_start, prev_end, period),
    }


@cached()
def fetch_sales_pipeline(date_debut, date_fin):
    fields = ["id", "name", "partner_id", "amount_untaxed", "date_order", "state", "invoice_status"]

    quotes = search_read_all(
        "sale.order",
        [
            ["state", "in", ["draft", "sent"]],
            ["invoice_status", "=", "no"],
            ["date_order", ">=", f"{date_debut} 00:00:00"],
            ["date_order", "<=", f"{date_fin} 23:59:59"],
        ],
        fields,
        order="date_order desc",
    )

    orders_to_invoice = search_read_all(
        "sale.order",
        [
            ["state", "in", ["sale", "done"]],
            ["invoice_status", "=", "to invoice"],
            ["date_order", ">=", f"{date_debut} 00:00:00"],
            ["date_order", "<=", f"{date_fin} 23:59:59"],
        ],
        fields,
        order="date_order desc",
    )

    def map_rows(records, kind):
        rows = []
        for rec in records:
            rows.append({
                "id": rec.get("id"),
                "type": kind,
                "numero": rec.get("name") or "—",
                "client": rec["partner_id"][1] if rec.get("partner_id") else "Inconnu",
                "montant": round(rec.get("amount_untaxed") or 0, 2),
                "date": (rec.get("date_order") or "")[:10],
                "state": rec.get("state") or "",
                "state_label": label_for_status(rec.get("state")),
                "invoice_status": rec.get("invoice_status") or "",
                "invoice_status_label": label_for_status(rec.get("invoice_status")),
                "url": get_odoo_record_url("sale.order", rec.get("id")) if rec.get("id") else None,
            })
        return rows

    quotes_rows = map_rows(quotes, "devis")
    orders_rows = map_rows(orders_to_invoice, "commande")

    return {
        "quotes": quotes_rows,
        "orders": orders_rows,
        "quote_total": round(sum(x["montant"] for x in quotes_rows), 2),
        "order_total": round(sum(x["montant"] for x in orders_rows), 2),
        "quote_count": len(quotes_rows),
        "order_count": len(orders_rows),
    }


@cached()
def get_sales_pipeline(period, date_from=None, date_to=None):
    start, end = get_period_dates(period, date_from, date_to)
    prev_start, prev_end = get_prev_period_dates(period, date_from, date_to)

    current = fetch_sales_pipeline(start, end)
    previous = fetch_sales_pipeline(prev_start, prev_end)

    total_current = round(current["quote_total"] + current["order_total"], 2)
    total_previous = round(previous["quote_total"] + previous["order_total"], 2)

    return {
        "labels": ["Devis en cours", "Bons à facturer"],
        "values": [current["quote_total"], current["order_total"]],
        "prev_labels": ["Devis en cours", "Bons à facturer"],
        "prev_values": [previous["quote_total"], previous["order_total"]],
        "total": total_current,
        "total_prev": total_previous,
        "pct": pct_change(total_current, total_previous),
        "quote_total": current["quote_total"],
        "order_total": current["order_total"],
        "quote_count": current["quote_count"],
        "order_count": current["order_count"],
        "quote_total_prev": previous["quote_total"],
        "order_total_prev": previous["order_total"],
        "quote_count_prev": previous["quote_count"],
        "order_count_prev": previous["order_count"],
        "quote_pct": pct_change(current["quote_total"], previous["quote_total"]),
        "order_pct": pct_change(current["order_total"], previous["order_total"]),
        "quotes": current["quotes"],
        "orders": current["orders"],
        "period_label": make_period_label(start, end, period),
        "prev_period_label": make_period_label(prev_start, prev_end, period),
    }


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
  .header-controls { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }
  .global-filter-wrap { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .filter-label { font-size: 12px; color: var(--text2); margin-right: 4px; }
  .filter-btn { background: var(--bg); border: 1px solid var(--border); color: var(--text2); padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.15s; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active { background: var(--accent); border-color: var(--accent); color: white; }
  .custom-range { display: none; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .custom-range.visible { display: flex; }
  .date-input {
    background: var(--surface2); color: var(--text); border: 1px solid var(--border);
    padding: 7px 10px; border-radius: 6px; font-size: 12px; font-family: 'DM Sans', sans-serif;
  }
  .checkbox-wrap { display: flex; align-items: center; gap: 6px; padding: 8px 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .checkbox-wrap label { font-size: 12px; color: var(--text2); cursor: pointer; }
  .checkbox-wrap input { accent-color: var(--accent); cursor: pointer; }
  .date-badge { font-family: 'DM Mono', monospace; font-size: 11px; color: var(--text2); background: var(--bg); padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border); }
  .refresh-btn, .apply-btn {
    background: var(--surface2); color: var(--text); border: 1px solid var(--border);
    padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 500; cursor: pointer;
    font-family: 'DM Sans', sans-serif; transition: all 0.15s;
  }
  .refresh-btn:hover, .apply-btn:hover { border-color: var(--accent); color: var(--accent); }
  main { padding: 28px 40px; max-width: 1400px; margin: 0 auto; }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 28px; }
  .kpi-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 22px; position: relative; overflow: hidden; }
  .kpi-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; }
  .kpi-card.blue::before { background: var(--accent); }
  .kpi-card.red::before { background: var(--accent2); }
  .kpi-card.green::before { background: var(--accent3); }
  .kpi-top { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 10px; }
  .kpi-label { font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: 1px; font-weight: 500; }
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
  canvas { max-height: 240px; }
  .table-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 22px; margin-bottom: 20px; }
  table { width: 100%; border-collapse: collapse; margin-top: 4px; }
  thead th { text-align: left; font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: 1px; padding: 8px 12px; border-bottom: 1px solid var(--border); font-weight: 500; }
  tbody tr { border-bottom: 1px solid var(--border); transition: background 0.15s; }
  tbody tr:hover { background: var(--surface2); }
  tbody tr:last-child { border-bottom: none; }
  tbody td { padding: 11px 12px; font-size: 13px; vertical-align: middle; }
  .retard-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-family: 'DM Mono', monospace; font-size: 11px; font-weight: 500; }
  .retard-low { background: rgba(247,196,79,0.15); color: var(--warning); }
  .retard-mid { background: rgba(247,99,79,0.15); color: var(--danger); }
  .retard-high { background: rgba(247,99,79,0.3); color: var(--danger); }
  .status-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-family: 'DM Mono', monospace; font-size: 11px; font-weight: 500; background: rgba(79,142,247,0.15); color: var(--accent); }
  .success-badge { background: rgba(79,247,160,0.15); color: var(--success); }
  .danger-badge { background: rgba(247,99,79,0.15); color: var(--danger); }
  .warning-badge { background: rgba(247,196,79,0.15); color: var(--warning); }
  .montant { font-family: 'DM Mono', monospace; font-size: 13px; font-weight: 500; }
  .loading, .empty-state, .error-state { display: flex; align-items: center; justify-content: center; min-height: 160px; color: var(--text2); font-size: 13px; gap: 10px; text-align: center; padding: 20px; }
  .error-state { color: #ffb4a7; }
  .spinner { width: 16px; height: 16px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
  .split-list { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .mini-table-title { font-size: 12px; font-weight: 600; margin-bottom: 8px; color: var(--text); }
  .table-link { color: var(--accent); text-decoration: none; font-size: 12px; font-weight: 500; }
  .table-link:hover { text-decoration: underline; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (max-width: 1100px) {
    .kpi-grid, .charts-grid, .split-list { grid-template-columns: 1fr; }
  }
  @media (max-width: 900px) {
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

  <div class="header-controls">
    <div class="global-filter-wrap">
      <span class="filter-label">Période :</span>
      <button class="filter-btn" data-period="today" onclick="setGlobalPeriod('today')">Aujourd'hui</button>
      <button class="filter-btn" data-period="week" onclick="setGlobalPeriod('week')">Semaine</button>
      <button class="filter-btn active" data-period="month" onclick="setGlobalPeriod('month')">Mois</button>
      <button class="filter-btn" data-period="quarter" onclick="setGlobalPeriod('quarter')">Trimestre</button>
      <button class="filter-btn" data-period="year" onclick="setGlobalPeriod('year')">Année</button>
      <button class="filter-btn" data-period="custom" onclick="setGlobalPeriod('custom')">Personnalisé</button>
    </div>

    <div class="custom-range" id="customRange">
      <input class="date-input" type="date" id="dateFrom">
      <span style="color: var(--text2); font-size: 12px;">→</span>
      <input class="date-input" type="date" id="dateTo">
      <button class="apply-btn" onclick="applyCustomRange()">Appliquer</button>
    </div>

    <div class="checkbox-wrap">
      <input type="checkbox" id="compareGlobal" checked onchange="loadAll()">
      <label for="compareGlobal">Comparer à N-1</label>
    </div>

    <div class="date-badge" id="dateBadge">—</div>
    <button class="refresh-btn" onclick="refreshAll()">↻ Actualiser</button>
  </div>
</header>

<main>
  <div class="kpi-grid">
    <div class="kpi-card blue">
      <div class="kpi-top"><div class="kpi-label">CA</div></div>
      <div class="kpi-value blue" id="kpiCA">—</div>
      <div class="kpi-bottom">
        <div class="kpi-sub" id="kpiCASub">Chargement...</div>
        <div id="kpiCAPct"></div>
      </div>
    </div>
    <div class="kpi-card red">
      <div class="kpi-top"><div class="kpi-label">Factures en retard</div></div>
      <div class="kpi-value red" id="kpiRetard">—</div>
      <div class="kpi-bottom">
        <div class="kpi-sub" id="kpiRetardSub">Chargement...</div>
        <div id="kpiRetardPct"></div>
      </div>
    </div>
    <div class="kpi-card green">
      <div class="kpi-top"><div class="kpi-label">Devis en cours</div></div>
      <div class="kpi-value green" id="kpiQuotes">—</div>
      <div class="kpi-bottom">
        <div class="kpi-sub" id="kpiQuotesSub">Chargement...</div>
        <div id="kpiQuotesPct"></div>
      </div>
    </div>
    <div class="kpi-card green">
      <div class="kpi-top"><div class="kpi-label">Bons à facturer</div></div>
      <div class="kpi-value green" id="kpiOrders">—</div>
      <div class="kpi-bottom">
        <div class="kpi-sub" id="kpiOrdersSub">Chargement...</div>
        <div id="kpiOrdersPct"></div>
      </div>
    </div>
  </div>

  <div class="charts-grid">
    <div class="chart-card">
      <div class="card-header">
        <div>
          <div class="card-title">Chiffre d'affaires</div>
          <div class="card-sub" id="caChartSub">Top 5 clients + autres</div>
        </div>
      </div>
      <canvas id="chartCA"></canvas>
    </div>

    <div class="chart-card">
      <div class="card-header">
        <div>
          <div class="card-title">Devis et bons à facturer</div>
          <div class="card-sub" id="salesChartSub">Montants HTVA</div>
        </div>
      </div>
      <canvas id="chartSales"></canvas>
    </div>
  </div>

  <div class="table-card">
    <div class="card-header">
      <div>
        <div class="card-title">Factures en retard</div>
        <div class="card-sub" id="retardTableSub">Factures impayées dont l'échéance est dépassée</div>
      </div>
    </div>
    <div id="tableRetard"><div class="loading"><div class="spinner"></div> Chargement...</div></div>
  </div>

  <div class="table-card">
    <div class="card-header">
      <div>
        <div class="card-title">Détail commercial</div>
        <div class="card-sub" id="salesTableSub">Devis en cours et bons à facturer</div>
      </div>
    </div>
    <div id="tableSales"><div class="loading"><div class="spinner"></div> Chargement...</div></div>
  </div>
</main>

<script>
let chartCA = null, chartSales = null;
let state = {
  period: 'month',
  dateFrom: null,
  dateTo: null
};

function fmt(n) {
  return new Intl.NumberFormat('fr-BE', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 }).format(n || 0);
}

function pctBadge(pct) {
  if (pct === null || pct === undefined) return '';
  const cls = pct > 0 ? 'pct-up' : pct < 0 ? 'pct-down' : 'pct-neutral';
  const sign = pct > 0 ? '+' : '';
  return `<span class="pct-badge ${cls}">${sign}${pct}% vs N-1</span>`;
}

async function fetchJson(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error || `Erreur HTTP ${response.status}`);
  }
  return data;
}

function showError(targetId, error) {
  document.getElementById(targetId).innerHTML = `<div class="error-state">⚠️ ${error.message}</div>`;
}

function activateGlobalButtons(period) {
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.period === period);
  });
}

function toggleCustomRange(show) {
  document.getElementById('customRange').classList.toggle('visible', show);
}

function buildQuery() {
  const params = new URLSearchParams();
  params.set('period', state.period);
  if (state.period === 'custom') {
    params.set('date_from', state.dateFrom || '');
    params.set('date_to', state.dateTo || '');
  }
  return params.toString();
}

function setGlobalPeriod(period) {
  state.period = period;
  activateGlobalButtons(period);
  toggleCustomRange(period === 'custom');
  if (period !== 'custom') {
    loadAll();
  }
}

function applyCustomRange() {
  const dateFrom = document.getElementById('dateFrom').value;
  const dateTo = document.getElementById('dateTo').value;
  state.dateFrom = dateFrom;
  state.dateTo = dateTo;
  loadAll();
}

function compareEnabled() {
  return document.getElementById('compareGlobal').checked;
}

async function loadCA() {
  try {
    const query = buildQuery();
    const data = await fetchJson(`/api/ca?${query}`);

    document.getElementById('kpiCA').textContent = fmt(data.total);
    document.getElementById('kpiCASub').textContent = `${data.labels.length} poste(s) — ${data.period_label}`;
    document.getElementById('kpiCAPct').innerHTML = compareEnabled() ? pctBadge(data.pct) : '';
    document.getElementById('caChartSub').textContent = `Top 5 clients + autres — ${data.period_label}`;

    if (chartCA) chartCA.destroy();

    const datasets = [{
      label: 'Période actuelle',
      data: data.values,
      backgroundColor: 'rgba(79,142,247,0.7)',
      borderColor: 'rgba(79,142,247,1)',
      borderWidth: 1,
      borderRadius: 4
    }];

    if (compareEnabled()) {
      datasets.push({
        label: 'Année précédente',
        data: data.prev_values,
        backgroundColor: 'rgba(79,142,247,0.2)',
        borderColor: 'rgba(79,142,247,0.5)',
        borderWidth: 1,
        borderRadius: 4
      });
    }

    chartCA = new Chart(document.getElementById('chartCA'), {
      type: 'bar',
      data: { labels: data.labels, datasets },
      options: {
        responsive: true,
        plugins: {
          legend: {
            display: compareEnabled(),
            labels: { color: '#8890a8', font: { size: 11 } }
          }
        },
        scales: {
          x: { ticks: { color: '#8890a8', font: { size: 10 } }, grid: { color: '#2a2f45' } },
          y: { ticks: { color: '#8890a8', font: { size: 10 }, callback: v => fmt(v) }, grid: { color: '#2a2f45' } }
        }
      }
    });
  } catch (error) {
    showError('chartCA', error);
    document.getElementById('kpiCASub').textContent = 'Erreur';
  }
}

async function loadSales() {
  try {
    const query = buildQuery();
    const data = await fetchJson(`/api/sales?${query}`);

    document.getElementById('kpiQuotes').textContent = fmt(data.quote_total);
    document.getElementById('kpiQuotesSub').textContent = `${data.quote_count} devis — ${data.period_label}`;
    document.getElementById('kpiQuotesPct').innerHTML = compareEnabled() ? pctBadge(data.quote_pct) : '';

    document.getElementById('kpiOrders').textContent = fmt(data.order_total);
    document.getElementById('kpiOrdersSub').textContent = `${data.order_count} commande(s) — ${data.period_label}`;
    document.getElementById('kpiOrdersPct').innerHTML = compareEnabled() ? pctBadge(data.order_pct) : '';

    document.getElementById('salesChartSub').textContent = `Montants HTVA — ${data.period_label}`;
    document.getElementById('salesTableSub').textContent = `Devis en cours et bons à facturer — ${data.period_label}`;

    if (chartSales) chartSales.destroy();

    const datasets = [{
      label: 'Période actuelle',
      data: data.values,
      backgroundColor: ['rgba(79,142,247,0.75)','rgba(79,247,160,0.75)'],
      borderRadius: 6
    }];

    if (compareEnabled()) {
      datasets.push({
        label: 'Année précédente',
        data: data.prev_values,
        backgroundColor: ['rgba(79,142,247,0.25)','rgba(79,247,160,0.25)'],
        borderRadius: 6
      });
    }

    chartSales = new Chart(document.getElementById('chartSales'), {
      type: 'bar',
      data: { labels: data.labels, datasets },
      options: {
        responsive: true,
        plugins: {
          legend: {
            display: compareEnabled(),
            labels: { color: '#8890a8', font: { size: 11 } }
          }
        },
        scales: {
          x: { ticks: { color: '#8890a8', font: { size: 10 } }, grid: { color: '#2a2f45' } },
          y: { ticks: { color: '#8890a8', font: { size: 10 }, callback: v => fmt(v) }, grid: { color: '#2a2f45' } }
        }
      }
    });

    const renderSection = (title, rows, statusClass) => {
      if (!rows.length) {
        return `<div><div class="mini-table-title">${title}</div><div class="empty-state">${title === 'Devis en cours' ? 'Aucun devis en cours sur la période' : 'Aucun bon à facturer sur la période'}</div></div>`;
      }
      let html = `<div><div class="mini-table-title">${title}</div><table><thead><tr><th>Numéro</th><th>Client</th><th>Montant</th><th>Date</th><th>Statut</th><th></th></tr></thead><tbody>`;
      for (const r of rows) {
        html += `<tr>
          <td><span style="font-family:monospace;font-size:12px;color:#8890a8">${r.numero}</span></td>
          <td>${r.client}</td>
          <td><span class="montant">${fmt(r.montant)}</span></td>
          <td><span style="font-family:monospace;font-size:12px">${r.date || '—'}</span></td>
          <td><span class="status-badge ${statusClass}">${r.invoice_status_label}</span></td>
          <td>${r.url ? `<a class="table-link" href="${r.url}" target="_blank">Voir</a>` : ''}</td>
        </tr>`;
      }
      html += '</tbody></table></div>';
      return html;
    };

    document.getElementById('tableSales').innerHTML =
      `<div class="split-list">${renderSection('Devis en cours', data.quotes, '')}${renderSection('Bons à facturer', data.orders, 'success-badge')}</div>`;
  } catch (error) {
    showError('chartSales', error);
    showError('tableSales', error);
    document.getElementById('kpiQuotesSub').textContent = 'Erreur';
    document.getElementById('kpiOrdersSub').textContent = 'Erreur';
  }
}

async function loadRetard() {
  try {
    const query = buildQuery();
    const data = await fetchJson(`/api/retard?${query}`);

    document.getElementById('kpiRetard').textContent = fmt(data.total);
    document.getElementById('kpiRetardSub').textContent = `${data.count} facture(s) — ${data.period_label}`;
    document.getElementById('kpiRetardPct').innerHTML = compareEnabled() ? pctBadge(data.pct) : '';
    document.getElementById('retardTableSub').textContent = `Factures en retard — ${data.period_label}`;

    if (data.factures.length === 0) {
      document.getElementById('tableRetard').innerHTML = '<div class="empty-state">✅ Aucune facture en retard</div>';
      return;
    }

    let html = `<table><thead><tr><th>Numéro</th><th>Client</th><th>Montant dû</th><th>Échéance</th><th>Retard</th><th>Priorité</th><th></th></tr></thead><tbody>`;
    for (const f of data.factures) {
      const cls = f.retard_jours > 60 ? 'retard-high' : f.retard_jours > 30 ? 'retard-mid' : 'retard-low';
      const badgeCls = f.priority === 'Critique' ? 'danger-badge' : f.priority === 'Haute' ? 'warning-badge' : '';
      html += `<tr>
        <td><span style="font-family:monospace;font-size:12px;color:#8890a8">${f.numero}</span></td>
        <td>${f.client}</td>
        <td><span class="montant">${fmt(f.montant)}</span></td>
        <td><span style="font-family:monospace;font-size:12px">${f.echeance}</span></td>
        <td><span class="retard-badge ${cls}">+${f.retard_jours}j</span></td>
        <td><span class="status-badge ${badgeCls}">${f.priority}</span></td>
        <td>${f.url ? `<a class="table-link" href="${f.url}" target="_blank">Voir</a>` : ''}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('tableRetard').innerHTML = html;
  } catch (error) {
    showError('tableRetard', error);
    document.getElementById('kpiRetardSub').textContent = 'Erreur';
  }
}

function refreshAll() {
  fetchJson('/api/health?clear_cache=1').finally(() => loadAll());
}

async function loadAll() {
  document.getElementById('dateBadge').textContent = new Date().toLocaleDateString('fr-BE', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  await Promise.all([loadCA(), loadSales(), loadRetard()]);
}

window.addEventListener('DOMContentLoaded', () => {
  const today = new Date();
  const currentYear = today.getFullYear();
  const currentMonth = String(today.getMonth() + 1).padStart(2, '0');
  const currentDay = String(today.getDate()).padStart(2, '0');
  document.getElementById('dateTo').value = `${currentYear}-${currentMonth}-${currentDay}`;
  document.getElementById('dateFrom').value = `${currentYear}-${currentMonth}-01`;
  state.dateFrom = document.getElementById('dateFrom').value;
  state.dateTo = document.getElementById('dateTo').value;
  loadAll();
});
</script>
</body>
</html>
"""


def get_request_period_args():
    period = request.args.get("period", "month")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    return period, date_from, date_to


@app.route("/")
def index():
    return render_template_string(HTML, db=ODOO_DB)


@app.route("/api/health")
def api_health():
    if request.args.get("clear_cache") == "1":
        _cache.clear()
    return jsonify({"ok": True, "cache_entries": len(_cache)})


@app.route("/api/ca")
def api_ca():
    period, date_from, date_to = get_request_period_args()
    return jsonify(get_ca(period, date_from, date_to))


@app.route("/api/retard")
def api_retard():
    period, date_from, date_to = get_request_period_args()
    return jsonify(get_factures_retard(period, date_from, date_to))


@app.route("/api/sales")
def api_sales():
    period, date_from, date_to = get_request_period_args()
    return jsonify(get_sales_pipeline(period, date_from, date_to))


@app.errorhandler(Exception)
def handle_error(error):
    code = getattr(error, "code", 500)
    return jsonify({"error": str(error)}), code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
