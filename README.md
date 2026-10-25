# Electric-Bill Defender: APS savings validation

[![tests](https://github.com/klauscreates/electric-bill-defender/actions/workflows/tests.yml/badge.svg)](https://github.com/klauscreates/electric-bill-defender/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

A local tool that estimates how much an APS household on **TOU-E** (Time-of-Use 4pm–7pm Weekdays, no demand charge) could save by shifting load out of On-Peak hours. It reads your own usage export and bills and writes an HTML report and CSV files you can audit.

Everything runs on your machine. The tool uses no utility credentials, makes no network calls, and uploads nothing.

> Independent project, not affiliated with or endorsed by Arizona Public Service. Results are model estimates, not a guarantee of savings or billing advice.

## Try it in a minute (synthetic data)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"   # on macOS/Linux: .venv/bin/python
python -m ebd.synthetic demo
ebd import demo/synthetic_usage.csv --workspace demo/ws --timestamp-col "Interval Start" --timestamp-format "%m/%d/%Y %H:%M" --timestamp-marks start --interval-col "Duration (min)" --kwh-col kWh --confirm
ebd analyze --workspace demo/ws --household demo/synthetic_household.yaml --out demo/report
```

Open `demo/report/report.html`. The synthetic household is fake and labeled as such in the report.

## Scope

| Supported | Rejected, with an explanation |
|---|---|
| APS TOU-E, non-solar, no battery | Demand-charge plans (R-2, R-3/R3-47, Saver Choice Plus/Max) |
| Interval data at 60 minutes or finer | Solar, net billing, exports (negative kWh) |
| Tariff revisions you add to the config | Batteries, daily data, non-TOU plans |

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"
```

## Workflow

### 1. Download your data

Sign in to aps.com and download your interval usage ([APS usage help](https://www.aps.com/en/Utility/Help/Usage-help)). Also keep the PDF bills that cover the same period. Store them under `data/`, which git ignores.

### 2. Import with an explicit column mapping

The tool doesn't guess the export format. Open the CSV, find the columns, and preview how they're read:

```bash
ebd import data/aps_usage.csv --workspace workspaces/home --timestamp-col "Date/Time" --timestamp-format "%m/%d/%Y %I:%M %p" --timestamp-marks start --interval-minutes 15 --kwh-col "kWh"
```

The preview shows the raw rows next to how they were interpreted (Phoenix start and end time, minutes, kWh), plus counts and any import errors. Check it carefully, especially whether a timestamp marks the **start or end** of its interval. Then run the same command again with `--confirm` to write the data.

Other options:
- `--date-col`/`--time-col` for separate date and time columns.
- `--interval-col` with `--interval-unit` when the duration is in a column.
- `--skip-rows` for preamble lines above the header.
- `--source-tz` when timestamps are in a zone that uses daylight saving time. Ambiguous or nonexistent times are flagged, not guessed.

### 3. Record the household

Copy [examples/household.example.yaml](examples/household.example.yaml) to `households/home.yaml`. Fill it in from your bills:
- The rate schedule.
- The solar, battery and demand-charge flags. All three are required.
- Each billing cycle's first and last service day and its billing month. The billing month sets the season.
- The bill's energy line items, used for reconciliation.

### 4. Validate

```bash
ebd validate --workspace workspaces/home --household households/home.yaml
```

Validation reports these problems:
- Missing periods, duplicates and overlaps.
- Negative, blank or implausible kWh.
- Intervals coarser than 60 minutes.
- Intervals that straddle a TOU, day or season boundary.
- Data outside any billing cycle.
- Dates no configured tariff version covers.

Nothing is filled in or prorated. A cycle with any problem is marked **incomplete** and left out of the savings figures. The exit code is 1 when there are errors.

### 5. Analyze

```bash
ebd analyze --workspace workspaces/home --household households/home.yaml --out reports/home-2026-10
```

Outputs:

| File | Contents |
|---|---|
| `report.html` | Standalone report: coverage, tariff, reconciliation, scenarios, ceiling, assumptions |
| `interval_calculations.csv` | Every analyzed interval: period, rate, baseline cost, and each scenario's kWh out/in, kWh, cost and savings |
| `cycle_scenario_summary.csv` | Per billing cycle × scenario: baseline, On-Peak kWh, shifted kWh, destination-load change, savings |
| `cycle_coverage.csv` | Coverage and status for each recorded cycle |
| `validation_issues.csv` | Every validation issue |
| `bill_reconciliation.csv` | Modeled and billed energy line items, with differences |
| `manifest.json` | SHA-256 of every input and output, import mapping, tariff versions, scenario settings, assumptions |

## Method

- **Baseline:** each interval's kWh × the bundled TOU-E rate for its period. The season comes from the billing cycle and the tariff version from the interval's date.
- **Shift scenarios (10%, 25%, 50%):** move that share of every weekday's On-Peak kWh into 7–11 p.m. the same day, spread evenly by time. Daily kWh is unchanged, and each scenario is priced under the same tariff as its baseline. These are **sensitivity scenarios**. Whole-home data does not show which appliances could move, what they draw, or whether the home stays comfortable.
- **Theoretical ceiling:** move all On-Peak kWh into the same day's cheapest eligible hours. Those are Super Off-Peak on winter weekdays and all Off-Peak hours otherwise. This is **physically unvalidated**.
- **Annual totals** appear only for 12 consecutive complete cycles. Otherwise the report gives the observed period without extrapolation.
- Zero and negative results are reported as they are.
- **Excluded charges:** basic service, adjustors (PSA, REAC, TCA, DSMAC, LFCR, TEAM, CRS, SRB), riders, taxes and fees. The report lists them.

### Reproducing a figure

All of these columns are in `interval_calculations.csv`:
- `baseline_cost = kwh × rate_per_kwh`
- `k_kwh = kwh − k_kwh_out + k_kwh_in`
- `k_savings = baseline_cost − k_kwh × rate_per_kwh`

Summing by `cycle_id` gives `cycle_scenario_summary.csv`. A test checks this.

## Tariff configuration

[ebd/tariffs/aps_tou_e.yaml](ebd/tariffs/aps_tou_e.yaml) holds TOU-E **Revision 3**:
- A.C.C. No. 6154, ACC Decision No. 79293, effective 2024-03-08.
- Transcribed from the [official tariff PDF](https://www.aps.com/-/media/APS/APSCOM-PDFs/Utility/Regulatory-and-Legal/Regulatory-Plan-Details-Tariffs/Residential/Service-Plans/Time-of-Use-4pm-7pmWeekdays_R-TOU-E.pdf), verified 2026-10-24.

Dates before 2024-03-08 are rejected until the earlier revision (A.C.C. No. 6122) is added. When APS files a new revision:
1. Set `effective_end` on the current version.
2. Add a new version with its own source and verification date.
3. Never edit a version that a report has already used.

**Open question:** the tariff doesn't say whether winter Super Off-Peak applies on a weekday holiday. The default (`holiday_super_off_peak: false`) makes the whole holiday Off-Peak. Check it against a real winter bill that includes a weekday holiday.

## Acceptance status

Synthetic fixtures (`python -m ebd.synthetic OUT`, files marked `synthetic: true`) are for testing only. Household findings stay **pending** until a real summer bill and a real winter bill reconcile within tolerance. Record any discrepancy in that cycle's `notes`.

Out of scope for now: EIA integration, device control, and savings-based billing. Modeled savings are not attributable savings.

## Tests

```bash
.venv/Scripts/python -m pytest
```

## License

MIT. See [LICENSE](LICENSE).
