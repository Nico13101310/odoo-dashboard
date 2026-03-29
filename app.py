from flask import Flask, render_template_string, jsonify, request
import os, ssl, time, xmlrpc.client
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

app = Flask(__name__)

ODOO_URL      = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_DB       = os.environ.get("ODOO_DB", "")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_API_KEY  = os.environ.get("ODOO_API_KEY", "")
CACHE_TTL     = int(os.environ.get("CACHE_TTL", "120"))
PAGE_SIZE     = int(os.environ.get("ODOO_PAGE_SIZE", "200"))

_cache = {}

def cached(ttl=CACHE_TTL):
    def dec(fn):
        def wrap(*a):
            k = (fn.__name__, a)
            now = time.time()
            it = _cache.get(k)
            if it and now - it["ts"] < ttl:
                return it["value"]
            v = fn(*a)
            _cache[k] = {"ts": now, "value": v}
            return v
        wrap.__name__ = fn.__name__
        return wrap
    return dec

class OdooError(Exception): pass

def get_conn():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", context=ctx, allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
    if not uid:
        raise OdooError("Authentification refusée")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", context=ctx, allow_none=True)
    return models, uid

def xkw(model, method, args=None, kwargs=None):
    m, uid = get_conn()
    try:
        return m.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args or [], kwargs or {})
    except xmlrpc.client.Fault as e:
        raise OdooError(str(e))

def search_all(model, domain, fields, order=None):
    out, offset = [], 0
    while True:
        kw = {"fields": fields, "limit": PAGE_SIZE, "offset": offset}
        if order: kw["order"] = order
        batch = xkw(model, "search_read", [domain], kw)
        out.extend(batch)
        if len(batch) < PAGE_SIZE: break
        offset += PAGE_SIZE
    return out

def pct(cur, prev):
    if not prev: return None
    return round((cur - prev) / prev * 100, 1)

def period_dates(period):
    today = date.today()
    if period == "month":
        return today.replace(day=1), today
    return today.replace(month=1, day=1), today

def odoo_url(model, rid):
    return f"{ODOO_URL}/web#id={rid}&model={model}&view_type=form"

# ── CLIENTS ───────────────────────────────────────────────
@cached()
def get_clients():
    rows = search_all("res.partner",
        [["is_company","=",True],["customer_rank",">",0],["active","=",True]],
        ["name"], order="name asc")
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append({"id": r["id"], "name": (r.get("name") or "").strip()})
    return out

def partner_domain(pid):
    if not pid: return []
    return [["partner_id","child_of",int(pid)]]

# ── FACTURES ──────────────────────────────────────────────
@cached()
def fetch_invoices(d_start, d_end, pid):
    domain = [
        ["move_type","=","out_invoice"],
        ["state","=","posted"],
        ["invoice_date",">=",str(d_start)],
        ["invoice_date","<=",str(d_end)],
    ] + partner_domain(pid)
    return search_all("account.move", domain,
        ["id","name","partner_id","amount_untaxed","amount_total",
         "amount_residual","payment_state","invoice_date","invoice_date_due"],
        order="invoice_date desc")

def classify(inv, today):
    ps  = inv.get("payment_state","")
    due = inv.get("invoice_date_due")
    if ps in ("paid","in_payment"): return "paid"
    if due and datetime.strptime(due,"%Y-%m-%d").date() < today: return "overdue"
    return "pending"

# ── COMMANDES À FACTURER ───────────────────────────────────
@cached()
def fetch_to_invoice(d_start, d_end, pid):
    """Commandes confirmées avec invoice_status = to invoice ou partiellement facturées"""
    domain = [
        ["state","in",["sale","done"]],
        ["invoice_status","in",["to invoice"]],
        ["date_order",">=",f"{d_start} 00:00:00"],
        ["date_order","<=",f"{d_end} 23:59:59"],
    ] + partner_domain(pid)
    return search_all("sale.order", domain,
        ["id","name","partner_id","amount_untaxed","amount_total",
         "date_order","invoice_status","invoice_ids"],
        order="date_order desc")

@cached()
def get_invoice_data(period, pid):
    start, end = period_dates(period)
    ps, pe     = start - relativedelta(years=1), end - relativedelta(years=1)
    today      = date.today()

    cur  = fetch_invoices(start, end, pid)
    prev = fetch_invoices(ps, pe, pid)
    cur_toinv  = fetch_to_invoice(start, end, pid)
    prev_toinv = fetch_to_invoice(ps, pe, pid)

    def stats(rows):
        paid=pending=overdue=0
        paid_n=pending_n=overdue_n=0
        ca=0
        detail={"paid":[],"pending":[],"overdue":[]}
        for inv in rows:
            amt  = inv.get("amount_total") or 0
            res  = inv.get("amount_residual") or 0
            htva = inv.get("amount_untaxed") or 0
            ca  += htva
            cls  = classify(inv, today)
            due  = inv.get("invoice_date_due","")
            retard = None
            if due and cls=="overdue":
                retard = (today - datetime.strptime(due,"%Y-%m-%d").date()).days
            row = {
                "id":      inv.get("id"),
                "numero":  inv.get("name","—"),
                "client":  inv["partner_id"][1] if inv.get("partner_id") else "Inconnu",
                "htva":    round(htva,2),
                "ttc":     round(amt,2),
                "restant": round(res,2),
                "echeance":due,
                "date":    inv.get("invoice_date",""),
                "retard":  retard,
                "url":     odoo_url("account.move", inv.get("id")),
            }
            detail[cls].append(row)
            if cls=="paid":    paid+=amt; paid_n+=1
            elif cls=="pending": pending+=res; pending_n+=1
            else:              overdue+=res; overdue_n+=1
        return {
            "ca":round(ca,2), "paid":round(paid,2), "pending":round(pending,2), "overdue":round(overdue,2),
            "paid_n":paid_n, "pending_n":pending_n, "overdue_n":overdue_n,
            "total_n":len(rows), "detail":detail,
        }

    def stats_toinv(rows):
        total = 0
        detail = []
        for so in rows:
            htva = so.get("amount_untaxed") or 0
            total += htva
            d_order = so.get("date_order","")
            if d_order and " " in d_order:
                d_order = d_order.split(" ")[0]
            detail.append({
                "id":     so.get("id"),
                "numero": so.get("name","—"),
                "client": so["partner_id"][1] if so.get("partner_id") else "Inconnu",
                "htva":   round(htva,2),
                "ttc":    round(so.get("amount_total") or 0, 2),
                "date":   d_order,
                "url":    odoo_url("sale.order", so.get("id")),
            })
        return {"total": round(total,2), "count": len(detail), "detail": detail}

    c = stats(cur)
    p = stats(prev)
    ti_cur  = stats_toinv(cur_toinv)
    ti_prev = stats_toinv(prev_toinv)

    c["ca_pct"]       = pct(c["ca"], p["ca"])
    c["paid_pct"]     = pct(c["paid"], p["paid"])
    c["pending_pct"]  = pct(c["pending"], p["pending"])
    c["overdue_pct"]  = pct(c["overdue"], p["overdue"])
    c["toinv"]        = ti_cur["total"]
    c["toinv_n"]      = ti_cur["count"]
    c["toinv_pct"]    = pct(ti_cur["total"], ti_prev["total"])
    c["toinv_detail"] = ti_cur["detail"]
    c["period"]       = f"{start} → {end}"
    c["prev_period"]  = f"{ps} → {pe}"

    # Top clients CA
    by_client={}
    for inv in cur:
        nom = inv["partner_id"][1] if inv.get("partner_id") else "Inconnu"
        by_client[nom] = by_client.get(nom,0) + (inv.get("amount_untaxed") or 0)
    top = sorted(by_client.items(), key=lambda x:x[1], reverse=True)[:8]
    c["top_labels"] = [x[0] for x in top]
    c["top_values"] = [round(x[1],2) for x in top]

    # Évolution mensuelle 12 mois
    monthly={}
    base = date.today().replace(day=1)
    for i in range(11,-1,-1):
        m = base - relativedelta(months=i)
        monthly[m.strftime("%b %Y")] = 0
    for inv in fetch_invoices(base-relativedelta(months=11), end, pid):
        d = inv.get("invoice_date","")
        if not d: continue
        key = datetime.strptime(d,"%Y-%m-%d").date().replace(day=1).strftime("%b %Y")
        if key in monthly:
            monthly[key] = monthly.get(key,0) + (inv.get("amount_untaxed") or 0)
    c["monthly_labels"] = list(monthly.keys())
    c["monthly_values"] = [round(v,2) for v in monthly.values()]

    return c

HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ConiMind · Dashboard Facturation</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root {
  --bg:        #0c0e1a;
  --bg2:       #111422;
  --surface:   #161929;
  --surface2:  #1c2035;
  --border:    #252840;
  --border2:   #2e3350;
  --orange:    #f5a623;
  --orange2:   #ffbe4f;
  --orange-glow: rgba(245,166,35,.15);
  --orange-dim:  rgba(245,166,35,.08);
  --success:   #34d399;
  --warning:   #fbbf24;
  --danger:    #f87171;
  --info:      #60a5fa;
  --purple:    #a78bfa;
  --text:      #f0f2fa;
  --text2:     #7c84a8;
  --text3:     #404666;
  --radius:    16px;
  --radius-sm: 10px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Outfit', sans-serif; min-height: 100vh; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }

/* HEADER */
header {
  position: sticky; top: 0; z-index: 100;
  background: rgba(12,14,26,.9); backdrop-filter: blur(24px);
  border-bottom: 1px solid var(--border);
  padding: 0 36px; height: 66px;
  display: flex; align-items: center; justify-content: space-between; gap: 20px;
}
.logo { display: flex; align-items: center; gap: 11px; text-decoration: none; flex-shrink: 0; }
.logo-mark {
  width: 38px; height: 38px;
  background: linear-gradient(135deg, #f5a623, #ffbe4f);
  border-radius: 10px; display: flex; align-items: center; justify-content: center;
  font-size: 18px; box-shadow: 0 0 24px var(--orange-glow);
}
.logo-name { font-size: 17px; font-weight: 800; letter-spacing: -.4px; color: var(--text); }
.logo-tag  { font-size: 10px; color: var(--text2); font-family: 'JetBrains Mono', monospace; letter-spacing: .5px; }
.controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pill-group {
  display: flex; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden; padding: 3px;
}
.pill {
  padding: 6px 18px; font-size: 12px; font-weight: 600; cursor: pointer;
  border: none; background: transparent; color: var(--text2);
  font-family: 'Outfit', sans-serif; border-radius: 8px; transition: all .15s;
}
.pill:hover { color: var(--text); }
.pill.active { background: var(--orange); color: #0c0e1a; box-shadow: 0 2px 10px var(--orange-glow); }
.select-ctrl {
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  padding: 8px 12px; border-radius: var(--radius-sm); font-size: 12px;
  font-family: 'Outfit', sans-serif; cursor: pointer; min-width: 170px;
}
.select-ctrl:focus { outline: none; border-color: var(--orange); }
.cmp-label { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--text2); cursor: pointer; user-select: none; }
.cmp-label input { accent-color: var(--orange); cursor: pointer; }
.header-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.date-chip {
  font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text2);
  background: var(--surface2); padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border);
}
.btn-refresh {
  background: transparent; border: 1px solid var(--border); color: var(--text2);
  padding: 7px 14px; border-radius: var(--radius-sm); font-size: 13px; cursor: pointer; transition: all .15s;
}
.btn-refresh:hover { border-color: var(--orange); color: var(--orange); }

/* MAIN */
main { padding: 30px 36px; max-width: 1440px; margin: 0 auto; }

.sec-label {
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 2px;
  color: var(--orange); margin-bottom: 16px; display: flex; align-items: center; gap: 10px;
}
.sec-label::after { content:''; flex:1; height:1px; background:var(--border); }

/* KPIs — grille 5 colonnes */
.kpi-grid { display: grid; grid-template-columns: repeat(5,1fr); gap: 14px; margin-bottom: 30px; }

.kpi {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 22px 18px;
  position: relative; overflow: hidden; transition: border-color .2s, transform .2s;
}
.kpi:hover { border-color: var(--border2); transform: translateY(-2px); }
.kpi::before {
  content:''; position:absolute; top:0; left:0; right:0; height:2px;
  border-radius:var(--radius) var(--radius) 0 0;
}
.kpi.k-orange::before { background: linear-gradient(90deg,var(--orange),var(--orange2)); }
.kpi.k-green::before  { background: linear-gradient(90deg,var(--success),#6ee7b7); }
.kpi.k-blue::before   { background: linear-gradient(90deg,var(--info),#93c5fd); }
.kpi.k-red::before    { background: linear-gradient(90deg,var(--danger),#fca5a5); }
.kpi.k-purple::before { background: linear-gradient(90deg,var(--purple),#c4b5fd); }

.kpi-icon { width:38px; height:38px; border-radius:9px; display:flex; align-items:center; justify-content:center; font-size:17px; margin-bottom:14px; }
.k-orange .kpi-icon { background: var(--orange-dim); }
.k-green  .kpi-icon { background: rgba(52,211,153,.1); }
.k-blue   .kpi-icon { background: rgba(96,165,250,.1); }
.k-red    .kpi-icon { background: rgba(248,113,113,.1); }
.k-purple .kpi-icon { background: rgba(167,139,250,.1); }

.kpi-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--text2); margin-bottom: 7px; }
.kpi-value { font-size: 23px; font-weight: 800; letter-spacing: -.8px; line-height: 1; margin-bottom: 9px; }
.k-orange .kpi-value { color: var(--orange); }
.k-green  .kpi-value { color: var(--success); }
.k-blue   .kpi-value { color: var(--info); }
.k-red    .kpi-value { color: var(--danger); }
.k-purple .kpi-value { color: var(--purple); }

.kpi-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kpi-sub  { font-size: 10px; color: var(--text2); font-family: 'JetBrains Mono', monospace; }

.badge {
  font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 100px;
  font-family: 'JetBrains Mono', monospace;
}
.b-up   { background: rgba(52,211,153,.12); color: var(--success); }
.b-down { background: rgba(248,113,113,.12); color: var(--danger); }
.b-flat { background: rgba(124,132,168,.12); color: var(--text2); }

/* CHARTS */
.charts-row { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; margin-bottom: 30px; }
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 26px;
}
.card-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 22px; }
.card-title { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
.card-sub   { font-size: 11px; color: var(--text2); font-family: 'JetBrains Mono', monospace; }
canvas { max-height: 220px; }

/* INVOICE TABS */
.inv-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }
.inv-tabs { display: flex; gap: 3px; background: var(--surface2); border: 1px solid var(--border); border-radius: 12px; padding: 4px; }
.inv-tab {
  padding: 7px 16px; border-radius: 9px; font-size: 12px; font-weight: 600;
  cursor: pointer; border: none; background: transparent; color: var(--text2);
  font-family: 'Outfit', sans-serif; transition: all .15s; white-space: nowrap;
}
.inv-tab:hover { color: var(--text); }
.inv-tab.active { background: var(--orange); color: #0c0e1a; box-shadow: 0 2px 10px var(--orange-glow); }
.tab-cnt { display: inline-block; margin-left: 5px; background: rgba(255,255,255,.15); padding: 1px 6px; border-radius: 100px; font-size: 10px; }
.inv-tab.active .tab-cnt { background: rgba(12,14,26,.2); }

/* TABLE */
.tbl-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1.2px; color: var(--text3); padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
tbody tr { border-bottom: 1px solid var(--border); transition: background .12s; }
tbody tr:hover { background: var(--surface2); }
tbody tr:last-child { border-bottom: none; }
tbody td { padding: 12px 14px; font-size: 13px; }

.mono  { font-family: 'JetBrains Mono', monospace; font-size: 12px; }
.muted { color: var(--text2); }
.bold  { font-weight: 600; }

.s-pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 100px; font-size: 11px; font-weight: 600; }
.s-pill::before { content:''; width:5px; height:5px; border-radius:50%; background:currentColor; }
.s-paid    { background: rgba(52,211,153,.1);  color: var(--success); }
.s-pending { background: rgba(245,166,35,.1);  color: var(--orange); }
.s-overdue { background: rgba(248,113,113,.1); color: var(--danger); }
.s-toinv   { background: rgba(167,139,250,.1); color: var(--purple); }

.d-pill { display: inline-block; padding: 2px 9px; border-radius: 7px; font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; }
.d-low  { background: rgba(251,191,36,.1);  color: var(--warning); }
.d-mid  { background: rgba(248,113,113,.1); color: var(--danger); }
.d-high { background: rgba(248,113,113,.2); color: var(--danger); }

.link-btn {
  color: var(--orange); text-decoration: none; border: 1px solid var(--border);
  padding: 4px 11px; border-radius: 7px; font-size: 11px; font-weight: 600; transition: all .15s;
}
.link-btn:hover { border-color: var(--orange); background: var(--orange-dim); }

/* À facturer — banner info */
.toinv-banner {
  background: rgba(167,139,250,.07); border: 1px solid rgba(167,139,250,.2);
  border-radius: 10px; padding: 12px 16px; margin-bottom: 16px;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.toinv-banner .bi-label { font-size: 11px; color: var(--text2); }
.toinv-banner .bi-val   { font-size: 15px; font-weight: 800; color: var(--purple); font-family: 'JetBrains Mono', monospace; }
.toinv-banner .bi-sep   { color: var(--border2); font-size: 18px; }

.state-box { min-height: 140px; display: flex; align-items: center; justify-content: center; color: var(--text2); font-size: 13px; gap: 10px; }
.state-box.err { color: #fca5a5; }
.spin { width: 16px; height: 16px; border: 2px solid var(--border2); border-top-color: var(--orange); border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 1200px) {
  .kpi-grid { grid-template-columns: repeat(3,1fr); }
}
@media (max-width: 900px) {
  .kpi-grid { grid-template-columns: repeat(2,1fr); }
  .charts-row { grid-template-columns: 1fr; }
}
@media (max-width: 600px) {
  .kpi-grid { grid-template-columns: 1fr; }
  main, header { padding-left: 16px; padding-right: 16px; }
  header { height: auto; padding-top: 12px; padding-bottom: 12px; flex-wrap: wrap; }
}
</style>
</head>
<body>

<header>
  <a class="logo" href="https://www.conimind.com" target="_blank">
    <div class="logo-mark">🧠</div>
    <div>
      <div class="logo-name">ConiMind</div>
      <div class="logo-tag">{{ db }} · dashboard</div>
    </div>
  </a>
  <div class="controls">
    <div class="pill-group">
      <button class="pill active" data-p="month" onclick="setPeriod('month',this)">Ce mois</button>
      <button class="pill"        data-p="year"  onclick="setPeriod('year',this)">Cette année</button>
    </div>
    <select class="select-ctrl" id="clientFilter" onchange="load()">
      <option value="">Tous les clients</option>
    </select>
    <label class="cmp-label">
      <input type="checkbox" id="cmpToggle" onchange="load()">
      Comparer N-1
    </label>
  </div>
  <div class="header-right">
    <div class="date-chip" id="dateChip">—</div>
    <button class="btn-refresh" onclick="hardRefresh()" title="Actualiser">↻</button>
  </div>
</header>

<main>
  <div class="sec-label">Vue globale</div>
  <div class="kpi-grid">
    <div class="kpi k-orange">
      <div class="kpi-icon">💶</div>
      <div class="kpi-label">CA facturé HTVA</div>
      <div class="kpi-value" id="kCA">—</div>
      <div class="kpi-foot"><span class="kpi-sub" id="kCAsub">…</span><span id="kCApct"></span></div>
    </div>
    <div class="kpi k-purple">
      <div class="kpi-icon">📋</div>
      <div class="kpi-label">À facturer</div>
      <div class="kpi-value" id="kToInv">—</div>
      <div class="kpi-foot"><span class="kpi-sub" id="kToInvsub">…</span><span id="kToInvpct"></span></div>
    </div>
    <div class="kpi k-green">
      <div class="kpi-icon">✅</div>
      <div class="kpi-label">Factures payées</div>
      <div class="kpi-value" id="kPaid">—</div>
      <div class="kpi-foot"><span class="kpi-sub" id="kPaidsub">…</span><span id="kPaidpct"></span></div>
    </div>
    <div class="kpi k-blue">
      <div class="kpi-icon">⏳</div>
      <div class="kpi-label">En attente</div>
      <div class="kpi-value" id="kPending">—</div>
      <div class="kpi-foot"><span class="kpi-sub" id="kPendingsub">…</span><span id="kPendingpct"></span></div>
    </div>
    <div class="kpi k-red">
      <div class="kpi-icon">🚨</div>
      <div class="kpi-label">En retard</div>
      <div class="kpi-value" id="kOverdue">—</div>
      <div class="kpi-foot"><span class="kpi-sub" id="kOverduesub">…</span><span id="kOverduepct"></span></div>
    </div>
  </div>

  <div class="charts-row">
    <div class="card">
      <div class="card-head">
        <div>
          <div class="card-title">Évolution du CA</div>
          <div class="card-sub">12 derniers mois · HTVA</div>
        </div>
      </div>
      <canvas id="chartMonthly"></canvas>
    </div>
    <div class="card">
      <div class="card-head">
        <div>
          <div class="card-title">Top clients</div>
          <div class="card-sub">CA HTVA · période sélectionnée</div>
        </div>
      </div>
      <canvas id="chartTop"></canvas>
    </div>
  </div>

  <div class="inv-header">
    <div class="sec-label" style="margin-bottom:0;flex:1">Suivi des factures client</div>
    <div class="inv-tabs">
      <button class="inv-tab active" onclick="showTab('all',this)">
        Toutes <span class="tab-cnt" id="cnt-all">—</span>
      </button>
      <button class="inv-tab" onclick="showTab('toinv',this)">
        À facturer <span class="tab-cnt" id="cnt-toinv">—</span>
      </button>
      <button class="inv-tab" onclick="showTab('pending',this)">
        En attente <span class="tab-cnt" id="cnt-pending">—</span>
      </button>
      <button class="inv-tab" onclick="showTab('overdue',this)">
        En retard <span class="tab-cnt" id="cnt-overdue">—</span>
      </button>
      <button class="inv-tab" onclick="showTab('paid',this)">
        Payées <span class="tab-cnt" id="cnt-paid">—</span>
      </button>
    </div>
  </div>
  <div class="card" style="padding:16px 20px">
    <div id="invTable"><div class="state-box"><div class="spin"></div> Chargement…</div></div>
  </div>
</main>

<script>
let period = 'month';
let activeTab = 'all';
let invData = null;
let chM = null, chT = null;

const EUR = n => new Intl.NumberFormat('fr-BE',{style:'currency',currency:'EUR',maximumFractionDigits:0}).format(n||0);

function badge(p) {
  if(p===null||p===undefined) return '';
  const cls = p>0?'b-up':p<0?'b-down':'b-flat';
  return `<span class="badge ${cls}">${p>0?'+':''}${p}% N-1</span>`;
}

function setPeriod(p, btn) {
  period = p;
  document.querySelectorAll('.pill').forEach(b=>b.classList.toggle('active', b.dataset.p===p));
  load();
}

function showTab(tab, btn) {
  activeTab = tab;
  document.querySelectorAll('.inv-tab').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  renderTable();
}

function buildQ() {
  const p = new URLSearchParams({period});
  const pid = document.getElementById('clientFilter').value;
  if(pid) p.set('partner_id',pid);
  return p.toString();
}

async function fetchJ(url) {
  const r = await fetch(url);
  const d = await r.json();
  if(!r.ok||d.error) throw new Error(d.error||`HTTP ${r.status}`);
  return d;
}

async function loadClients() {
  try {
    const d = await fetchJ('/api/clients');
    const sel = document.getElementById('clientFilter');
    const cur = sel.value;
    sel.innerHTML = '<option value="">Tous les clients</option>';
    d.clients.forEach(c=>{
      const o=document.createElement('option');
      o.value=c.id; o.textContent=c.name; sel.appendChild(o);
    });
    sel.value = cur;
  } catch(e){ console.error(e); }
}

async function load() {
  document.getElementById('dateChip').textContent =
    new Date().toLocaleDateString('fr-BE',{weekday:'short',year:'numeric',month:'long',day:'numeric'});
  document.getElementById('invTable').innerHTML =
    '<div class="state-box"><div class="spin"></div> Chargement…</div>';

  try {
    invData = await fetchJ(`/api/invoices?${buildQ()}`);
    const d = invData;
    const cmp = document.getElementById('cmpToggle').checked;

    // KPIs
    document.getElementById('kCA').textContent      = EUR(d.ca);
    document.getElementById('kCAsub').textContent   = `${d.total_n} facture(s) — ${d.period}`;
    document.getElementById('kCApct').innerHTML     = cmp ? badge(d.ca_pct) : '';

    document.getElementById('kToInv').textContent    = EUR(d.toinv);
    document.getElementById('kToInvsub').textContent = `${d.toinv_n} commande(s) à facturer`;
    document.getElementById('kToInvpct').innerHTML   = cmp ? badge(d.toinv_pct) : '';

    document.getElementById('kPaid').textContent     = EUR(d.paid);
    document.getElementById('kPaidsub').textContent  = `${d.paid_n} facture(s)`;
    document.getElementById('kPaidpct').innerHTML    = cmp ? badge(d.paid_pct) : '';

    document.getElementById('kPending').textContent    = EUR(d.pending);
    document.getElementById('kPendingsub').textContent = `${d.pending_n} facture(s)`;
    document.getElementById('kPendingpct').innerHTML   = cmp ? badge(d.pending_pct) : '';

    document.getElementById('kOverdue').textContent    = EUR(d.overdue);
    document.getElementById('kOverduesub').textContent = `${d.overdue_n} facture(s)`;
    document.getElementById('kOverduepct').innerHTML   = cmp ? badge(d.overdue_pct) : '';

    // Tab counts
    const allInv = [...d.detail.overdue,...d.detail.pending,...d.detail.paid];
    document.getElementById('cnt-all').textContent    = allInv.length;
    document.getElementById('cnt-toinv').textContent  = d.toinv_n;
    document.getElementById('cnt-pending').textContent= d.detail.pending.length;
    document.getElementById('cnt-overdue').textContent= d.detail.overdue.length;
    document.getElementById('cnt-paid').textContent   = d.detail.paid.length;

    // Monthly chart
    if(chM) chM.destroy();
    chM = new Chart(document.getElementById('chartMonthly'),{
      type:'line',
      data:{
        labels:d.monthly_labels,
        datasets:[{
          label:'CA HTVA',
          data:d.monthly_values,
          borderColor:'#f5a623',
          backgroundColor:'rgba(245,166,35,.07)',
          borderWidth:2.5, fill:true, tension:.4,
          pointBackgroundColor:'#f5a623', pointRadius:3, pointHoverRadius:6,
        }]
      },
      options:{
        responsive:true,
        plugins:{legend:{display:false}},
        scales:{
          x:{ticks:{color:'#7c84a8',font:{size:10}},grid:{color:'#252840'}},
          y:{ticks:{color:'#7c84a8',font:{size:10},callback:v=>EUR(v)},grid:{color:'#252840'}}
        }
      }
    });

    // Top clients
    if(chT) chT.destroy();
    chT = new Chart(document.getElementById('chartTop'),{
      type:'bar',
      data:{
        labels:d.top_labels,
        datasets:[{
          data:d.top_values,
          backgroundColor:['rgba(245,166,35,.8)','rgba(52,211,153,.7)','rgba(96,165,250,.7)','rgba(248,113,113,.7)','rgba(167,139,250,.7)','rgba(251,191,36,.7)','rgba(52,211,153,.5)','rgba(245,166,35,.5)'],
          borderRadius:5, borderWidth:0,
        }]
      },
      options:{
        indexAxis:'y', responsive:true,
        plugins:{legend:{display:false}},
        scales:{
          x:{ticks:{color:'#7c84a8',font:{size:10},callback:v=>EUR(v)},grid:{color:'#252840'}},
          y:{ticks:{color:'#7c84a8',font:{size:10}},grid:{display:false}}
        }
      }
    });

    renderTable();
  } catch(e) {
    document.getElementById('invTable').innerHTML =
      `<div class="state-box err">⚠️ ${e.message}</div>`;
  }
}

function renderTable() {
  if(!invData) return;
  const d = invData;

  // Onglet "À facturer" — commandes SO
  if(activeTab === 'toinv') {
    const rows = d.toinv_detail || [];
    if(!rows.length) {
      document.getElementById('invTable').innerHTML =
        '<div class="state-box">✅ Aucune commande à facturer sur la période</div>';
      return;
    }
    const total = rows.reduce((s,r)=>s+r.htva,0);
    let h = `
      <div class="toinv-banner">
        <span class="bi-label">Total à facturer sur la période :</span>
        <span class="bi-val">${EUR(total)}</span>
        <span class="bi-sep">·</span>
        <span class="bi-label">${rows.length} commande(s) · HTVA</span>
      </div>
      <div class="tbl-wrap"><table>
      <thead><tr>
        <th>N° Commande</th><th>Client</th><th>Date commande</th>
        <th>Montant HTVA</th><th>Montant TTC</th><th>Statut</th><th></th>
      </tr></thead><tbody>`;
    for(const r of rows) {
      h += `<tr>
        <td class="mono muted">${r.numero}</td>
        <td class="bold">${r.client}</td>
        <td class="mono muted">${r.date||'—'}</td>
        <td class="mono bold">${EUR(r.htva)}</td>
        <td class="mono">${EUR(r.ttc)}</td>
        <td><span class="s-pill s-toinv">À facturer</span></td>
        <td><a class="link-btn" href="${r.url}" target="_blank">Voir ↗</a></td>
      </tr>`;
    }
    h += '</tbody></table></div>';
    document.getElementById('invTable').innerHTML = h;
    return;
  }

  // Autres onglets — factures
  const allInv = [...d.detail.overdue,...d.detail.pending,...d.detail.paid];
  const map = {all:allInv, pending:d.detail.pending, overdue:d.detail.overdue, paid:d.detail.paid};
  const rows = map[activeTab] || allInv;

  if(!rows.length) {
    document.getElementById('invTable').innerHTML =
      '<div class="state-box">✅ Aucune facture dans cette catégorie</div>';
    return;
  }

  const overdueSet = new Set(d.detail.overdue.map(x=>x.id));
  const pendingSet = new Set(d.detail.pending.map(x=>x.id));

  let h = `<div class="tbl-wrap"><table>
    <thead><tr>
      <th>N° Facture</th><th>Client</th><th>Date</th><th>Échéance</th>
      <th>Montant TTC</th><th>Restant dû</th><th>Statut</th><th>Retard</th><th></th>
    </tr></thead><tbody>`;

  for(const f of rows) {
    const type = overdueSet.has(f.id)?'overdue':pendingSet.has(f.id)?'pending':'paid';
    const statusMap = {
      overdue:`<span class="s-pill s-overdue">En retard</span>`,
      pending:`<span class="s-pill s-pending">En attente</span>`,
      paid:   `<span class="s-pill s-paid">Payée</span>`,
    };
    const dCls = f.retard?(f.retard>60?'d-high':f.retard>30?'d-mid':'d-low'):'';
    const dCell = f.retard?`<span class="d-pill ${dCls}">+${f.retard}j</span>`:'<span class="muted">—</span>';
    h += `<tr>
      <td class="mono muted">${f.numero}</td>
      <td class="bold">${f.client}</td>
      <td class="mono muted">${f.date||'—'}</td>
      <td class="mono muted">${f.echeance||'—'}</td>
      <td class="mono bold">${EUR(f.ttc)}</td>
      <td class="mono">${EUR(f.restant)}</td>
      <td>${statusMap[type]||''}</td>
      <td>${dCell}</td>
      <td><a class="link-btn" href="${f.url}" target="_blank">Voir ↗</a></td>
    </tr>`;
  }
  h += '</tbody></table></div>';
  document.getElementById('invTable').innerHTML = h;
}

async function hardRefresh() {
  await fetch('/api/health?clear_cache=1').catch(()=>{});
  await loadClients();
  await load();
}

window.addEventListener('DOMContentLoaded', async () => {
  await loadClients();
  await load();
});
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML, db=ODOO_DB)

@app.route("/api/health")
def api_health():
    if request.args.get("clear_cache") == "1":
        _cache.clear()
    return jsonify({"ok": True})

@app.route("/api/clients")
def api_clients():
    return jsonify({"clients": get_clients()})

@app.route("/api/invoices")
def api_invoices():
    period     = request.args.get("period","month")
    partner_id = request.args.get("partner_id","")
    return jsonify(get_invoice_data(period, partner_id))

@app.errorhandler(Exception)
def handle_err(e):
    return jsonify({"error": str(e)}), getattr(e,"code",500)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
