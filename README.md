# Geopolitical Stress Test

A Python simulation tool designed to model the operational and financial impact of geopolitical shocks on a manufacturing company's supply chain and customer demand.

Rather than assigning static risk scores to countries, this tool simulates a month-by-month scenario (e.g., "a 3-month export ban from China") to measure concrete effects on production capacity, bottleneck components, and lost revenue.

## Overview & Methodology

The simulation models a 12-month horizon for a hypothetical manufacturing enterprise. Each month, for every component, the system evaluates:

1. **Regular Supply:** Total volume from standard suppliers, adjusted for active shock disruptions.
2. **Alternative Suppliers:** Contingency suppliers step in up to their maximum capacity once their qualification lead time elapses.
3. **Inventory Assisting:** Remaining supply deficits are covered by safety stock until depleted.

Assembly requires all necessary components. Therefore, **monthly production is constrained by the most critical bottleneck component**, while **monthly revenue equals the minimum of total production capacity and market demand**.

## Key Features

- **Quantified Impact Analysis:** Measures revenue losses in both absolute terms (€ M) and as a percentage of annual turnover.
- **Bottleneck Identification:** Pinpoints the exact component or demand drop driving production downtime.
- **Buffer Testing (`--extra-stock`):** Simulates how additional months of inventory mitigate supply disruptions versus demand shocks.
- **Automated Reporting & Visualization:** Generates detailed CSV reports and visual trend charts via `matplotlib`.
- **Data Validation:** Built-in checks to ensure supplier and customer share percentages equal 100%, valid country codes, and correct parameters.

## Project Structure

```text
geopolitical-stress-test/
├── stress_test.py        # Core simulation engine, reporting, and visualization
├── data/                 # Input CSV configuration files (company, components, sources, customers, scenarios)
├── output/               # Generated reports and chart outputs
├── tests/                # Automated unit tests
└── requirements.txt      # Project dependencies
