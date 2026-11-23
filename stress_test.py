"""Stress test geopolitico per un'azienda manifatturiera (scenario inventato).

Invece di assegnare punteggi di rischio ai paesi, questo strumento simula
cosa succede all'azienda quando uno shock colpisce forniture o clienti.

Per ogni scenario simula 12 mesi e calcola, mese per mese, quanta produzione
e quanti ricavi si perdono rispetto alla situazione normale. Il modello tiene
conto di tre difese dell'azienda: le scorte di magazzino, i fornitori
alternativi (che però richiedono tempo per essere qualificati) e la quota di
domanda che arriva da paesi non colpiti.

Produce:
  - un report CSV con una riga per scenario;
  - un CSV con l'andamento mensile di ogni scenario;
  - un grafico dei ricavi mensili in percentuale del normale;
  - un rapporto sulla dipendenza da pochi paesi fornitori (indice HHI);
  - un'analisi di sensibilità: quanto cambiano le perdite con più scorte;
  - un riepilogo a terminale.

Uso:
    python stress_test.py
    python stress_test.py --data data --out output
    python stress_test.py --extra-stock 2     # e se avessimo 2 mesi di scorte in più?
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # nessuna finestra: salva soltanto il file
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

# --- Parametri del metodo -------------------------------------------------
# Un mese è "in difficoltà" se i ricavi scendono sotto questa quota del normale.
IMPACT_THRESHOLD = 0.95

SCENARIO_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"


# --- Lettura e controllo dei dati ----------------------------------------
def load_csv(path: Path) -> list[dict]:
    """Legge un CSV e restituisce una lista di dizionari (una riga = un dizionario)."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _number(errors: list[str], where: str, value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{where}: '{value}' non è un numero")
        return float("nan")


def validate(params: dict, components: list[dict], sources: list[dict],
             customers: list[dict], scenarios: list[dict]) -> None:
    """Controlla i dati e, se trova errori, li elenca tutti insieme."""
    errors = []

    revenue = _number(errors, "monthly_revenue_meur", params.get("monthly_revenue_meur"))
    horizon = _number(errors, "horizon_months", params.get("horizon_months"))
    if revenue <= 0:
        errors.append("monthly_revenue_meur deve essere maggiore di 0")
    if horizon < 1 or horizon != int(horizon):
        errors.append("horizon_months deve essere un numero intero di almeno 1")

    names = [c["component"] for c in components]
    if not names:
        errors.append("components.csv è vuoto")
    for c in components:
        name = c["component"]
        for field in ("inventory_months", "alt_lead_time_months", "alt_capacity_pct"):
            if _number(errors, f"{name}/{field}", c[field]) < 0:
                errors.append(f"{name}/{field} non può essere negativo")
        if _number(errors, f"{name}/alt_capacity_pct", c["alt_capacity_pct"]) > 100:
            errors.append(f"{name}/alt_capacity_pct non può superare 100")

    totals = defaultdict(float)
    for s in sources:
        if s["component"] not in names:
            errors.append(f"sources.csv: componente '{s['component']}' non presente in components.csv")
        totals[s["component"]] += _number(errors, f"{s['component']}/{s['country']}", s["share_pct"])
    for name in names:
        if abs(totals[name] - 100) > 0.01:
            errors.append(f"{name}: le quote dei fornitori sommano {totals[name]:g}, non 100")

    total_customers = sum(_number(errors, f"cliente {c['country']}", c["share_pct"]) for c in customers)
    if abs(total_customers - 100) > 0.01:
        errors.append(f"customers.csv: le quote sommano {total_customers:g}, non 100")

    supplier_countries = {s["country"] for s in sources}
    customer_countries = {c["country"] for c in customers}
    for e in scenarios:
        where = f"{e['scenario_id']}/{e['country']}"
        if e["kind"] not in ("supply", "demand"):
            errors.append(f"{where}: kind '{e['kind']}' non valido (usare supply o demand)")
        known = supplier_countries if e["kind"] == "supply" else customer_countries
        if e["country"] not in known:
            errors.append(f"{where}: paese non presente tra i {'fornitori' if e['kind'] == 'supply' else 'clienti'}")
        if not 0 <= _number(errors, f"{where}/loss_pct", e["loss_pct"]) <= 100:
            errors.append(f"{where}: loss_pct fuori dal range 0-100")
        if _number(errors, f"{where}/start_month", e["start_month"]) < 1:
            errors.append(f"{where}: start_month deve essere almeno 1")
        if _number(errors, f"{where}/duration_months", e["duration_months"]) < 1:
            errors.append(f"{where}: duration_months deve essere almeno 1")

    if errors:
        raise ValueError("Dati non validi:\n  - " + "\n  - ".join(errors))


# --- Simulazione ----------------------------------------------------------
def active_loss(events: list[dict], kind: str, country: str, month: int) -> float:
    """Quota persa (da 0 a 1) per un paese in un dato mese. Se più eventi si
    sovrappongono sullo stesso paese vale il più grave, non la somma."""
    losses = [
        float(e["loss_pct"])
        for e in events
        if e["kind"] == kind and e["country"] == country
        and int(e["start_month"]) <= month < int(e["start_month"]) + int(e["duration_months"])
    ]
    return max(losses, default=0.0) / 100


def simulate(events: list[dict], components: list[dict], sources: list[dict],
             customers: list[dict], horizon: int, extra_stock: float = 0.0) -> list[dict]:
    """Simula mese per mese e restituisce, per ogni mese, produzione e ricavi
    in percentuale del normale (1.0 = 100%).

    Per ogni componente, ogni mese:
      1. fornitura regolare = somma delle quote dei paesi, ridotta dalle perdite;
      2. se manca qualcosa e passa abbastanza tempo dal primo mese di carenza,
         entra un fornitore alternativo (fino alla sua capacità);
      3. quello che manca ancora viene coperto dalle scorte, finché ce ne sono.
    L'azienda produce quanto permette il componente più scarso (servono tutti)
    e vende al massimo quanto chiede il mercato.
    """
    by_component = defaultdict(list)
    for s in sources:
        by_component[s["component"]].append(s)

    stock = {c["component"]: float(c["inventory_months"]) + extra_stock for c in components}
    first_shortage = {c["component"]: None for c in components}

    monthly = []
    for month in range(1, horizon + 1):
        availability = {}
        for c in components:
            name = c["component"]
            regular = sum(
                float(s["share_pct"]) / 100 * (1 - active_loss(events, "supply", s["country"], month))
                for s in by_component[name]
            )
            shortage = max(0.0, 1 - regular)
            if shortage > 0 and first_shortage[name] is None:
                first_shortage[name] = month

            alternative = 0.0
            if first_shortage[name] is not None and month >= first_shortage[name] + float(c["alt_lead_time_months"]):
                alternative = min(float(c["alt_capacity_pct"]) / 100, shortage)

            supply = regular + alternative
            deficit = max(0.0, 1 - supply)
            used = min(stock[name], deficit)
            stock[name] -= used
            availability[name] = min(1.0, supply + used)

        production = min(availability.values())
        demand = 1 - sum(
            float(c["share_pct"]) / 100 * active_loss(events, "demand", c["country"], month)
            for c in customers
        )
        revenue = min(production, demand)

        if revenue >= 1 - 1e-9:
            bottleneck = "-"
        elif demand < production:
            bottleneck = "Domanda"
        else:
            bottleneck = min(availability, key=availability.get)

        monthly.append({"month": month, "revenue": revenue, "production": production,
                        "demand": demand, "bottleneck": bottleneck})
    return monthly


def summarize(scenario_id: str, name: str, monthly: list[dict], monthly_revenue: float) -> dict:
    """Riassume un'intera simulazione in una riga di report."""
    revenues = [m["revenue"] for m in monthly]
    worst = min(monthly, key=lambda m: m["revenue"])
    lost = monthly_revenue * sum(1 - r for r in revenues)

    below = [m["month"] for m in monthly if m["revenue"] < IMPACT_THRESHOLD]
    if not below:
        recovery = "Nessun impatto"
    elif below[-1] == monthly[-1]["month"]:
        recovery = "Non recuperato entro l'orizzonte"
    else:
        recovery = f"Mese {below[-1] + 1}"

    return {
        "scenario_id": scenario_id,
        "scenario": name,
        "min_revenue_pct": round(worst["revenue"] * 100, 1),
        "worst_month": worst["month"] if worst["revenue"] < 1 else "-",
        "months_in_difficulty": len(below),
        "revenue_lost_meur": round(lost, 2),
        "revenue_lost_pct": round(100 * lost / (monthly_revenue * len(monthly)), 1),
        "main_bottleneck": worst["bottleneck"],
        "back_to_normal": recovery,
    }


def run_all(params: dict, components: list[dict], sources: list[dict], customers: list[dict],
            scenarios: list[dict], extra_stock: float = 0.0) -> tuple[list[dict], dict]:
    """Esegue tutti gli scenari. Restituisce (report ordinato, andamento mensile per scenario)."""
    horizon = int(float(params["horizon_months"]))
    revenue = float(params["monthly_revenue_meur"])

    grouped, names = defaultdict(list), {}
    for e in scenarios:
        grouped[e["scenario_id"]].append(e)
        names[e["scenario_id"]] = e["name"]

    report, series = [], {}
    for sid, events in grouped.items():
        monthly = simulate(events, components, sources, customers, horizon, extra_stock)
        series[sid] = monthly
        report.append(summarize(sid, names[sid], monthly, revenue))

    # Dallo scenario più costoso al meno costoso.
    report.sort(key=lambda row: (-row["revenue_lost_meur"], row["scenario_id"]))
    return report, series


def concentration(components: list[dict], sources: list[dict]) -> list[dict]:
    """Quanto ogni componente dipende da pochi paesi, a prescindere dallo shock.

    - hhi: indice di Herfindahl-Hirschman sulle quote dei paesi (da 0 a 1;
      1 = un solo fornitore). Sopra 0,25 la fornitura è molto concentrata.
    - uncovered_months: mesi di carenza se la fornitura si fermasse del tutto,
      cioè il tempo per qualificare un fornitore alternativo meno le scorte
      (0 = le scorte bastano fino all'arrivo dell'alternativo).
    """
    by_component = defaultdict(list)
    for s in sources:
        by_component[s["component"]].append(s)

    rows = []
    for c in components:
        name = c["component"]
        shares = {s["country"]: float(s["share_pct"]) / 100 for s in by_component[name]}
        top = max(shares, key=shares.get)
        stock, lead = float(c["inventory_months"]), float(c["alt_lead_time_months"])
        rows.append({
            "component": name,
            "countries": len(shares),
            "top_country": top,
            "top_share_pct": round(shares[top] * 100),
            "hhi": round(sum(v * v for v in shares.values()), 2),
            "inventory_months": stock,
            "alt_lead_time_months": lead,
            "uncovered_months": round(max(0.0, lead - stock), 1),
        })
    rows.sort(key=lambda r: (-r["hhi"], -r["uncovered_months"], r["component"]))
    return rows


# Mesi di scorte aggiuntive provati nell'analisi di sensibilità.
SENSITIVITY_STEPS = range(0, 7)


def sensitivity(params: dict, components: list[dict], sources: list[dict], customers: list[dict],
                scenarios: list[dict], steps=SENSITIVITY_STEPS) -> list[dict]:
    """Ricavi persi per ogni scenario al crescere delle scorte di ogni componente."""
    rows = []
    for extra in steps:
        report, _ = run_all(params, components, sources, customers, scenarios, float(extra))
        for r in report:
            rows.append({"extra_stock_months": extra, "scenario_id": r["scenario_id"],
                         "scenario": r["scenario"], "revenue_lost_meur": r["revenue_lost_meur"]})
    rows.sort(key=lambda r: (r["scenario_id"], r["extra_stock_months"]))
    return rows


# --- Output ---------------------------------------------------------------
def write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def timeseries_rows(report: list[dict], series: dict) -> list[dict]:
    rows = []
    for r in report:
        for m in series[r["scenario_id"]]:
            rows.append({
                "scenario_id": r["scenario_id"], "scenario": r["scenario"], "month": m["month"],
                "revenue_pct": round(m["revenue"] * 100, 1),
                "production_pct": round(m["production"] * 100, 1),
                "demand_pct": round(m["demand"] * 100, 1),
                "bottleneck": m["bottleneck"],
            })
    return rows


def draw_chart(report: list[dict], series: dict, path: Path, subtitle: str = "") -> None:
    """Ricavi mensili in % del normale, una linea per scenario."""
    fig, ax = plt.subplots(figsize=(9, 6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)

    ax.axhline(100, color="#c9c8c3", linewidth=1, linestyle=(0, (4, 3)), zorder=1)

    # Il colore segue lo scenario (ordine degli identificativi), non la sua posizione in classifica.
    colors = {sid: SCENARIO_COLORS[i % len(SCENARIO_COLORS)] for i, sid in enumerate(sorted(series))}

    handles = []
    for r in report:
        color = colors[r["scenario_id"]]
        monthly = series[r["scenario_id"]]
        months = [m["month"] for m in monthly]
        values = [m["revenue"] * 100 for m in monthly]
        ax.plot(months, values, color=color, linewidth=2.2, zorder=3,
                solid_capstyle="round", solid_joinstyle="round")
        handles.append(Line2D([0], [0], color=color, linewidth=2.2, label=r["scenario"]))

        # Etichetta diretta solo sul punto peggiore (testo in inchiostro, non nel colore della serie).
        low = min(monthly, key=lambda m: m["revenue"])
        if low["revenue"] < 1:
            ax.scatter([low["month"]], [low["revenue"] * 100], s=46, color=color,
                       edgecolor=SURFACE, linewidth=2, zorder=4)
            ax.annotate(f"{low['revenue'] * 100:.0f}%", (low["month"], low["revenue"] * 100),
                        xytext=(0, -13), textcoords="offset points", ha="center", va="top",
                        fontsize=9, color=INK, zorder=5)

    horizon = len(next(iter(series.values())))
    ax.set_xlim(1, horizon)
    ax.set_ylim(0, 108)
    ax.set_xticks(range(1, horizon + 1))
    ax.set_xlabel("Mese della simulazione", color=INK_SECONDARY)
    ax.set_ylabel("Ricavi mensili (% del normale)", color=INK_SECONDARY)
    ax.set_title("Cosa succede ai ricavi dopo uno shock", color=INK, loc="left", fontsize=13)
    if subtitle:
        ax.text(0, 1.015, subtitle, transform=ax.transAxes, fontsize=9, color=INK_SECONDARY)
    ax.tick_params(colors=INK_SECONDARY)
    ax.grid(axis="y", color="#e4e3df", linewidth=0.8, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c9c8c3")
    ax.legend(handles=handles, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.13),
              ncol=2, labelcolor=INK_SECONDARY, fontsize=9.5)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def draw_sensitivity(rows: list[dict], path: Path) -> None:
    """Ricavi persi in funzione dei mesi di scorte aggiuntive, una linea per scenario."""
    fig, ax = plt.subplots(figsize=(9, 6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)

    sids = sorted({r["scenario_id"] for r in rows})
    colors = {sid: SCENARIO_COLORS[i % len(SCENARIO_COLORS)] for i, sid in enumerate(sids)}
    handles = []
    for sid in sids:
        sel = [r for r in rows if r["scenario_id"] == sid]
        ax.plot([r["extra_stock_months"] for r in sel], [r["revenue_lost_meur"] for r in sel],
                color=colors[sid], linewidth=2.2, marker="o", markersize=5, zorder=3,
                markeredgecolor=SURFACE, markeredgewidth=1.5, solid_capstyle="round")
        handles.append(Line2D([0], [0], color=colors[sid], linewidth=2.2, label=sel[0]["scenario"]))

    steps = sorted({r["extra_stock_months"] for r in rows})
    ax.set_xlim(steps[0], steps[-1])
    ax.set_ylim(bottom=0)
    ax.set_xticks(steps)
    ax.set_xlabel("Mesi di scorte aggiuntive per ogni componente", color=INK_SECONDARY)
    ax.set_ylabel("Ricavi persi (milioni di euro)", color=INK_SECONDARY)
    ax.set_title("Quanto servono le scorte?", color=INK, loc="left", fontsize=13)
    ax.tick_params(colors=INK_SECONDARY)
    ax.grid(axis="y", color="#e4e3df", linewidth=0.8, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c9c8c3")
    ax.legend(handles=handles, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.13),
              ncol=2, labelcolor=INK_SECONDARY, fontsize=9.5)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def print_concentration(rows: list[dict]) -> None:
    print("\nDipendenza dai fornitori (dal più concentrato):\n")
    for r in rows:
        print(f"  {r['component']}: {r['top_country']} {r['top_share_pct']}% "
              f"(HHI {r['hhi']:.2f}), scoperto {r['uncovered_months']:g} mesi se la fornitura si ferma")


def print_summary(report: list[dict], extra_stock: float) -> None:
    title = "Risultati" + (f" (con {extra_stock:g} mesi di scorte in più)" if extra_stock else "")
    print(f"{title} - dal più costoso al meno costoso:\n")
    for r in report:
        print(f"  {r['scenario_id']} {r['scenario']}")
        print(f"     ricavi minimi: {r['min_revenue_pct']:.0f}% del normale (mese {r['worst_month']})")
        print(f"     ricavi persi: {r['revenue_lost_meur']:.1f} M euro ({r['revenue_lost_pct']:.1f}% dell'anno)")
        print(f"     collo di bottiglia: {r['main_bottleneck']}  |  di nuovo normale: {r['back_to_normal']}")


# --- Programma principale -------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Stress test geopolitico per un'azienda")
    parser.add_argument("--data", default="data", help="cartella con i file CSV (default: data)")
    parser.add_argument("--out", default="output", help="cartella di output (default: output)")
    parser.add_argument("--extra-stock", type=float, default=0.0,
                        help="mesi di scorte aggiuntive per ogni componente (default: 0)")
    args = parser.parse_args()
    if args.extra_stock < 0:
        parser.error("--extra-stock non può essere negativo")

    data_dir, out_dir = Path(args.data), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    params = {r["parameter"]: r["value"] for r in load_csv(data_dir / "company.csv")}
    components = load_csv(data_dir / "components.csv")
    sources = load_csv(data_dir / "sources.csv")
    customers = load_csv(data_dir / "customers.csv")
    scenarios = load_csv(data_dir / "scenarios.csv")

    validate(params, components, sources, customers, scenarios)
    report, series = run_all(params, components, sources, customers, scenarios, args.extra_stock)

    write_csv(report, out_dir / "scenario_report.csv")
    write_csv(timeseries_rows(report, series), out_dir / "monthly_results.csv")
    subtitle = f"Scorte aumentate di {args.extra_stock:g} mesi per ogni componente" if args.extra_stock else ""
    draw_chart(report, series, out_dir / "revenue_chart.png", subtitle)

    dependency = concentration(components, sources)
    write_csv(dependency, out_dir / "dependency_report.csv")
    sens = sensitivity(params, components, sources, customers, scenarios)
    write_csv(sens, out_dir / "stock_sensitivity.csv")
    draw_sensitivity(sens, out_dir / "stock_sensitivity.png")

    print_summary(report, args.extra_stock)
    print_concentration(dependency)
    print(f"\nFile creati in '{out_dir}': scenario_report.csv, monthly_results.csv, revenue_chart.png, "
          "dependency_report.csv, stock_sensitivity.csv, stock_sensitivity.png")


if __name__ == "__main__":
    main()
