import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stress_test as st  # noqa: E402


# --- Dati di prova: un solo componente, due paesi fornitori -----------------
def component(stock="1", lead="2", capacity="70", name="X"):
    return {"component": name, "description": "", "inventory_months": stock,
            "alt_lead_time_months": lead, "alt_capacity_pct": capacity}


def source(country, share, name="X"):
    return {"component": name, "country": country, "share_pct": share}


SOURCES = [source("A", "70"), source("B", "30")]
CUSTOMERS = [{"country": "C", "share_pct": "50"}, {"country": "D", "share_pct": "50"}]


def event(kind="supply", country="A", loss="100", start="2", duration="2", sid="S1"):
    return {"scenario_id": sid, "name": "prova", "kind": kind, "country": country,
            "loss_pct": loss, "start_month": start, "duration_months": duration,
            "description": ""}


def revenues(events, components=None, sources=None, customers=None, horizon=6, extra=0.0):
    monthly = st.simulate(events, components or [component()], sources or SOURCES,
                          customers or CUSTOMERS, horizon, extra)
    return [round(m["revenue"], 4) for m in monthly]


class SimulationTests(unittest.TestCase):
    def test_no_events_means_no_loss(self):
        self.assertEqual(revenues([]), [1.0] * 6)

    def test_stock_absorbs_a_short_shock(self):
        # Fornitore unico, un mese di stop totale, un mese di scorte: nessun impatto.
        result = revenues([event(country="A", start="2", duration="1")],
                          sources=[source("A", "100")])
        self.assertEqual(result, [1.0] * 6)

    def test_hand_calculated_case(self):
        # Il paese A (70%) si ferma nei mesi 2 e 3. Fornitura regolare = 0,3.
        # Mese 2: manca 0,7, le scorte (1,0) ne coprono 0,7 -> ricavi 100%.
        # Mese 3: manca 0,7, restano 0,3 di scorte -> 0,3 + 0,3 = 60%.
        # Il fornitore alternativo arriverebbe al mese 4, ma lo stop è finito.
        self.assertEqual(revenues([event()]), [1.0, 1.0, 0.6, 1.0, 1.0, 1.0])

    def test_alternative_supplier_kicks_in_after_lead_time(self):
        # Nessuna scorta, stop di 6 mesi da subito. Alternativo dopo 2 mesi, 50% di capacità.
        comp = component(stock="0", lead="2", capacity="50")
        result = revenues([event(country="A", loss="100", start="1", duration="6")],
                          components=[comp], sources=[source("A", "100")], horizon=8)
        self.assertEqual(result, [0.0, 0.0, 0.5, 0.5, 0.5, 0.5, 1.0, 1.0])

    def test_alternative_never_exceeds_the_shortage(self):
        # Capacità alternativa 90% ma manca solo il 30%: la fornitura non supera il 100%.
        comp = component(stock="0", lead="0", capacity="90")
        result = revenues([event(country="A", loss="100", start="1", duration="2")],
                          components=[comp], horizon=3)
        self.assertEqual(result, [1.0, 1.0, 1.0])

    def test_demand_shock(self):
        # Il cliente C vale il 50% e dimezza gli ordini: ricavi al 75%.
        result = revenues([event(kind="demand", country="C", loss="50", start="1", duration="1")],
                          horizon=2)
        self.assertEqual(result, [0.75, 1.0])

    def test_production_is_limited_by_scarcest_component(self):
        comps = [component(name="X", stock="0"), component(name="Y", stock="0")]
        srcs = [source("A", "100", "X"), source("B", "100", "Y")]
        monthly = st.simulate([event(country="A", loss="40", start="1", duration="1")],
                              comps, srcs, CUSTOMERS, 2)
        self.assertAlmostEqual(monthly[0]["revenue"], 0.6)
        self.assertEqual(monthly[0]["bottleneck"], "X")

    def test_bottleneck_can_be_demand(self):
        monthly = st.simulate([event(kind="demand", country="C", loss="100", start="1", duration="1")],
                              [component()], SOURCES, CUSTOMERS, 1)
        self.assertEqual(monthly[0]["bottleneck"], "Domanda")

    def test_overlapping_events_take_the_worst_not_the_sum(self):
        events = [event(loss="40", start="1", duration="1", sid="S1"),
                  event(loss="30", start="1", duration="1", sid="S1")]
        self.assertAlmostEqual(st.active_loss(events, "supply", "A", 1), 0.4)

    def test_event_is_inactive_outside_its_window(self):
        e = [event(start="2", duration="2")]
        self.assertEqual(st.active_loss(e, "supply", "A", 1), 0)
        self.assertEqual(st.active_loss(e, "supply", "A", 3), 1)
        self.assertEqual(st.active_loss(e, "supply", "A", 4), 0)

    def test_extra_stock_reduces_the_loss(self):
        base = revenues([event()])
        more = revenues([event()], extra=2.0)
        self.assertLess(sum(1 - r for r in more), sum(1 - r for r in base))
        self.assertEqual(more, [1.0] * 6)


class SummaryTests(unittest.TestCase):
    def summary(self, events):
        monthly = st.simulate(events, [component()], SOURCES, CUSTOMERS, 6)
        return st.summarize("S1", "prova", monthly, monthly_revenue=10)

    def test_summary_numbers(self):
        s = self.summary([event()])
        self.assertEqual(s["min_revenue_pct"], 60.0)
        self.assertEqual(s["worst_month"], 3)
        self.assertEqual(s["revenue_lost_meur"], 4.0)  # 10 x 0,4
        self.assertEqual(s["revenue_lost_pct"], 6.7)   # 4 su 60
        self.assertEqual(s["main_bottleneck"], "X")
        self.assertEqual(s["back_to_normal"], "Mese 4")

    def test_no_impact(self):
        s = self.summary([])
        self.assertEqual(s["back_to_normal"], "Nessun impatto")
        self.assertEqual(s["worst_month"], "-")
        self.assertEqual(s["revenue_lost_meur"], 0)

    def test_not_recovered(self):
        # Senza scorte né fornitore alternativo, uno stop che dura tutto l'orizzonte non si recupera.
        comp = component(stock="0", capacity="0")
        monthly = st.simulate([event(country="A", start="1", duration="6")],
                              [comp], SOURCES, CUSTOMERS, 6)
        s = st.summarize("S1", "prova", monthly, monthly_revenue=10)
        self.assertEqual(s["back_to_normal"], "Non recuperato entro l'orizzonte")

    def test_alternative_supplier_allows_recovery_during_the_shock(self):
        # Stop di 6 mesi, ma l'alternativo (70%) copre tutto il buco dal mese 3.
        s = self.summary([event(country="A", start="1", duration="6")])
        self.assertEqual(s["back_to_normal"], "Mese 3")


class ValidationTests(unittest.TestCase):
    PARAMS = {"monthly_revenue_meur": "10", "horizon_months": "12"}

    def check(self, params=None, components=None, sources=None, customers=None, scenarios=None):
        st.validate(params or self.PARAMS, components or [component()], sources or SOURCES,
                    customers or CUSTOMERS, scenarios or [event()])

    def test_valid_data_passes(self):
        self.check()

    def test_supplier_shares_must_sum_to_100(self):
        with self.assertRaises(ValueError):
            self.check(sources=[source("A", "70"), source("B", "20")])

    def test_customer_shares_must_sum_to_100(self):
        with self.assertRaises(ValueError):
            self.check(customers=[{"country": "C", "share_pct": "50"}])

    def test_unknown_component_in_sources(self):
        with self.assertRaises(ValueError):
            self.check(sources=SOURCES + [source("Z", "10", name="Altro")])

    def test_unknown_country_in_scenario(self):
        with self.assertRaises(ValueError):
            self.check(scenarios=[event(country="Z")])

    def test_demand_event_must_name_a_customer(self):
        with self.assertRaises(ValueError):
            self.check(scenarios=[event(kind="demand", country="A")])

    def test_invalid_kind(self):
        with self.assertRaises(ValueError):
            self.check(scenarios=[event(kind="altro")])

    def test_loss_out_of_range(self):
        with self.assertRaises(ValueError):
            self.check(scenarios=[event(loss="120")])

    def test_not_a_number(self):
        with self.assertRaises(ValueError):
            self.check(scenarios=[event(loss="tanto")])

    def test_invalid_start_and_duration(self):
        with self.assertRaises(ValueError):
            self.check(scenarios=[event(start="0")])
        with self.assertRaises(ValueError):
            self.check(scenarios=[event(duration="0")])

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            self.check(params={"monthly_revenue_meur": "0", "horizon_months": "12"})
        with self.assertRaises(ValueError):
            self.check(params={"monthly_revenue_meur": "10", "horizon_months": "2.5"})

    def test_missing_parameter(self):
        with self.assertRaises(ValueError):
            self.check(params={"monthly_revenue_meur": "10"})

    def test_errors_are_reported_together(self):
        with self.assertRaises(ValueError) as ctx:
            self.check(sources=[source("A", "10")], customers=[{"country": "C", "share_pct": "10"}])
        self.assertIn("fornitori", str(ctx.exception))
        self.assertIn("customers.csv", str(ctx.exception))


class ConcentrationTests(unittest.TestCase):
    def test_hand_calculated_hhi(self):
        # Quote 70/30: 0,7^2 + 0,3^2 = 0,58. Scorte 1 mese, alternativo in 2: scoperti 1 mese.
        row = st.concentration([component()], SOURCES)[0]
        self.assertEqual(row["top_country"], "A")
        self.assertEqual(row["top_share_pct"], 70)
        self.assertEqual(row["hhi"], 0.58)
        self.assertEqual(row["countries"], 2)
        self.assertEqual(row["uncovered_months"], 1.0)

    def test_single_supplier_has_hhi_one(self):
        row = st.concentration([component()], [source("A", "100")])[0]
        self.assertEqual(row["hhi"], 1.0)

    def test_stock_longer_than_lead_time_leaves_nothing_uncovered(self):
        row = st.concentration([component(stock="3", lead="2")], SOURCES)[0]
        self.assertEqual(row["uncovered_months"], 0)

    def test_sorted_from_most_concentrated(self):
        comps = [component(name="X"), component(name="Y")]
        srcs = [source("A", "50", "X"), source("B", "50", "X"), source("A", "100", "Y")]
        rows = st.concentration(comps, srcs)
        self.assertEqual([r["component"] for r in rows], ["Y", "X"])


class SensitivityTests(unittest.TestCase):
    PARAMS = {"monthly_revenue_meur": "10", "horizon_months": "6"}

    def test_more_stock_never_costs_more(self):
        rows = st.sensitivity(self.PARAMS, [component()], SOURCES, CUSTOMERS, [event()], steps=range(0, 4))
        lost = [r["revenue_lost_meur"] for r in rows]
        self.assertEqual(lost, sorted(lost, reverse=True))

    def test_hand_calculated_values(self):
        # Senza scorte extra si perdono 4,0 M euro (vedi SummaryTests); con 2 mesi in più nessuna perdita.
        rows = st.sensitivity(self.PARAMS, [component()], SOURCES, CUSTOMERS, [event()], steps=(0, 2))
        self.assertEqual([r["revenue_lost_meur"] for r in rows], [4.0, 0.0])

    def test_demand_shock_is_not_helped_by_stock(self):
        shock = [event(kind="demand", country="C", loss="50", start="1", duration="2")]
        rows = st.sensitivity(self.PARAMS, [component()], SOURCES, CUSTOMERS, shock, steps=(0, 5))
        self.assertEqual(rows[0]["revenue_lost_meur"], rows[1]["revenue_lost_meur"])


class ShippedDataTests(unittest.TestCase):
    """I dati inclusi nel repository devono essere validi e dare risultati sensati."""

    data_dir = "data"

    @classmethod
    def setUpClass(cls):
        data = ROOT / cls.data_dir
        cls.params = {r["parameter"]: r["value"] for r in st.load_csv(data / "company.csv")}
        cls.components = st.load_csv(data / "components.csv")
        cls.sources = st.load_csv(data / "sources.csv")
        cls.customers = st.load_csv(data / "customers.csv")
        cls.scenarios = st.load_csv(data / "scenarios.csv")

    def test_shipped_data_is_valid(self):
        st.validate(self.params, self.components, self.sources, self.customers, self.scenarios)

    def test_report_is_sorted_by_cost(self):
        report, _ = st.run_all(self.params, self.components, self.sources,
                               self.customers, self.scenarios)
        lost = [r["revenue_lost_meur"] for r in report]
        self.assertEqual(lost, sorted(lost, reverse=True))
        self.assertEqual(len(report), len({e["scenario_id"] for e in self.scenarios}))

    def test_every_scenario_has_some_impact(self):
        report, _ = st.run_all(self.params, self.components, self.sources,
                               self.customers, self.scenarios)
        self.assertTrue(all(r["revenue_lost_meur"] > 0 for r in report))

    def test_outputs_are_written(self):
        report, series = st.run_all(self.params, self.components, self.sources,
                                    self.customers, self.scenarios)
        with tempfile.TemporaryDirectory() as tmp:
            st.write_csv(report, Path(tmp) / "report.csv")
            st.write_csv(st.timeseries_rows(report, series), Path(tmp) / "monthly.csv")
            st.draw_chart(report, series, Path(tmp) / "chart.png")
            st.write_csv(st.concentration(self.components, self.sources), Path(tmp) / "dependency.csv")
            sens = st.sensitivity(self.params, self.components, self.sources,
                                  self.customers, self.scenarios)
            st.write_csv(sens, Path(tmp) / "sensitivity.csv")
            st.draw_sensitivity(sens, Path(tmp) / "sensitivity.png")
            for name in ("report.csv", "monthly.csv", "chart.png",
                         "dependency.csv", "sensitivity.csv", "sensitivity.png"):
                self.assertGreater((Path(tmp) / name).stat().st_size, 0)

    def test_dependency_covers_every_component(self):
        rows = st.concentration(self.components, self.sources)
        self.assertEqual({r["component"] for r in rows}, {c["component"] for c in self.components})
        self.assertTrue(all(0 < r["hhi"] <= 1 for r in rows))


class DefenceDataTests(ShippedDataTests):
    """Stesse verifiche sul caso della difesa (data_defence/)."""

    data_dir = "data_defence"


if __name__ == "__main__":
    unittest.main()
