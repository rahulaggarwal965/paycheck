# Paycheck Calculator

A multi-year paycheck projection tool with support for ESPP, RSU vesting, 401(k) contributions with employer matching, and tax withholding. Built with Hydra configuration management, Pydantic v2 models, and Polars DataFrames.

## Features

- **Multi-year payroll projection** with semimonthly, biweekly, and monthly pay frequencies
- **Per-period schedules** for contribution rates, extra income, health deductions, and group term life (scalar or per-period lists)
- **Per-year configuration overrides** for salary, contribution schedules, IRS limits, and more
- **ESPP (Employee Stock Purchase Plan)** with 24-month lookback, configurable discount, and purchase limits
- **RSU (Restricted Stock Unit)** vesting with multiple schedule types, target-value grants, whole-share rounding, and FMV at market open
- **401(k) contributions** with IRS limit enforcement (402(g) and 415(c)), configurable employer match tiers, and optional employer-match inclusion in 415(c)
- **Tax withholding** using python-taxes and tenforty for federal/state calculations, plus FICA and CA voluntary
- **First year adjustments** for sign-on bonus (with explicit 401k contribution rates), taxed/untaxed relocation, with proper tax treatment
- **Weekend pay date adjustment** (Saturday/Sunday rolls to preceding Friday)
- **Market data** via yfinance with multi-field local caching (Open, Close, etc.)
- **Money ledger** with unified transaction log, stable sort ordering across event types, and effective tax rate
- **Year summary** derived directly from the money ledger TOTAL row, ensuring consistency between logged output and CSV

## Installation

### Prerequisites

- Python 3.12+
- uv package manager

### Setup

```bash
cd /path/to/paycheck
uv sync
uv run -m paycheck.main --help
```

## Quick Start

### 1. Create a configuration

```bash
cp examples/config_example.yaml conf/my_config.yaml
# Edit conf/my_config.yaml with your details
```

### 2. Run the calculator

```bash
# Run with a named config
uv run -m paycheck.main --config-name=my_config

# Override specific values via CLI
uv run -m paycheck.main --config-name=my_config payroll.salary_annual=300000 calendar.years=[2025,2026]
```

## Configuration

Hydra configuration with modular YAML files and CLI overrides.

### Directory Structure

```
conf/
├── config.yaml              # Base config (defaults + required fields)
├── market_data/
│   └── default.yaml         # Market data provider settings
├── calendar/
│   └── semimonthly.yaml     # Pay frequency settings
├── limits/
│   └── 2025.yaml            # IRS contribution limits
├── payroll/
│   └── default.yaml         # Salary, contributions, ESPP, employer match
├── taxes/
│   └── default.yaml         # Tax withholding settings
└── rsu/
    └── default.yaml         # RSU grant configurations
```

### Key Configuration Sections

#### Personal Information
```yaml
person:
  name: "Your Name"
  state: CA
  filing_status: single
  start_date: "2025-07-28"
```

#### Payroll
```yaml
payroll:
  salary_annual: 170000
  extra_income_per_period: [50]         # Scalar or per-period list
  health_deductions_per_period: [0, 36, 3]  # Per-period list (last value repeats)
  group_term_life_per_period: 9.38      # Scalar applied to every period

  contribution_schedule:
    pretax_401k_pct: [0.28, 0.19, 0.19, 0.20, 0.20, 0.26, 0.26, 0.15]
    roth_401k_pct: [0.00]
    aftertax_401k_pct: [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.14]
    espp_pct: [0.25, 0.25, 0.25, 0.12]

  employer_match:
    mode: tiers
    tiers:
      - match_rate: 1.00
        up_to_usd: 6000
      - match_rate: 0.50
        up_to_usd: 11000

  espp:
    enabled: true
    discount_pct: 0.15
    annual_limit_usd: 21250
    purchase_months: [2, 8]
    offering:
      lookback_months: 24
      first_offer_date: "2025-07-28"

  first_year_adjustments:
    sign_on_bonus: 25000
    sign_on_pretax_401k_pct: 0.28       # 401(k) contribution rates for sign-on
    sign_on_roth_401k_pct: 0.00
    sign_on_aftertax_401k_pct: 0.00
    relocation_taxed: 5000
    relocation_itemized: 280
    relocation_tax_advantaged: 3500
```

#### RSU Grants
```yaml
rsu_grants:
  - grant_id: "RSU-2025"
    symbol: AAPL
    target_value_usd: 140000
    grant_date_rule: sixth_business_day_following_month
    employment_start_date: "2025-07-28"
    share_calculation:
      method: "30_day_average"
      price_field: "Close"
      rounding: "down"
    schedule:
      type: nvidia_quarterly          # or per_year, per_quarter, custom_dates
      percentages: [6.25, 6.25, 6.25, 6.25, 6.25, 6.25, 6.25, 6.25,
                   6.25, 6.25, 6.25, 6.25, 6.25, 6.25, 6.25, 6.25]
      vest_day_rule: nvidia_wednesdays
    withholding_method: shares
    tax:
      federal_withholding_rate: 0.22
      state_withholding_rate: 0.1025
      apply_fica: true
      apply_ca_voluntary: true
```

#### Per-Year Overrides

Override any payroll parameter for specific years. Per-year `limits` overrides are **merged** with the base config — only explicitly set fields are overridden, so unspecified fields (e.g., `include_employer_in_415c`) are preserved.

```yaml
per_year:
  2025:
    salary_annual: 170000
    first_year_adjustments:
      sign_on_bonus: 25000
      sign_on_pretax_401k_pct: 0.28
    contribution_schedule:
      pretax_401k_pct: [0.28, 0.19]
  2026:
    salary_annual: 170000
    health_deductions_per_period: [3]
    limits:
      irs_415c_annual_additions: 72000   # Only this field overridden; others inherited
    contribution_schedule:
      pretax_401k_pct: [0.15, 0.15, 0.15, 0.15, 0.15, 0.14, 0.14, 0.14, 0.14, 0.14, 0.14, 0.14]
```

#### Taxes
```yaml
taxes:
  tax_year: 2025
  federal:
    method: python_taxes
    filing_status: single
  state:
    code: CA
    method: tenforty_annualized
  fica:
    social_security: true
    medicare: true
  supplemental:
    rsu_withholding_rate: 0.37
    bonus_withholding_rate: 0.22
  ca:
    voluntary_pct: 0.01
```

### Per-Period Schedules

Contribution rates and per-period amounts (`extra_income_per_period`, `health_deductions_per_period`, `group_term_life_per_period`, `pretax_401k_pct`, etc.) accept either:

- **A scalar** (e.g., `50.0`) — applied to every period
- **A list** (e.g., `[0, 36, 3]`) — values applied in order; if the list is shorter than the number of periods, the last value repeats

## Outputs

All output files are written to the configured `outputs.directory` as CSVs, one set per year.

### Pay Periods (`pay_periods_YYYY.csv`)

Per-period breakdown with YTD tracking. Includes first-year adjustment rows (sign_on, relocation_taxed, relocation_itemized, relocation_tax_advantaged) merged into the timeline with a `kind` column.

Columns: `pay_date`, `kind`, `gross_pay`, `extra_income`, `group_term_life`, `earnings` (gross + extra + gtl), `health_deductions`, `pretax_401k_pct`, `pretax_401k`, `employer_match`, `taxable_wages`, all tax components, `post_tax_pay`, `roth_401k`, `aftertax_401k`, `espp_contrib`, `final_take_home` (plus `_ytd` variants).

### Money Ledger (`money_ledger_YYYY.csv`)

Unified transaction log for all income events in chronological order:
- Wages (pay periods)
- RSU vests (with FMV, shares, tax breakdown)
- ESPP purchases (with shares acquired — contribution amounts are tracked in wage events only, not duplicated on purchase rows)
- First-year adjustments (sign-on, relocation)

Events on the same date are stably ordered: adjustments first, then wages, then ESPP, then RSU.

The TOTAL row includes an `effective_tax_rate` column (total taxes / gross income). The logged year summary is derived directly from this TOTAL row.

### ESPP Purchases (`espp_YYYY.csv`)

ESPP transaction details: purchase dates, offering/purchase prices, discount amounts, shares purchased, lookback calculations. Pending future purchases are marked with `is_pending=True`.

### RSU Vests (`rsu_vests_YYYY.csv`)

RSU vesting events: vest dates, share counts (whole shares via cumulative floor rounding), FMV (market open price), tax withholding breakdown, net shares delivered. Future projected vests are marked with `is_projected=True`.

## Architecture

```
src/paycheck/
├── main.py                  # Hydra entry point
├── config_models.py         # Pydantic v2 configuration models
├── pipeline.py              # Main orchestrator (year processing, period calculation)
├── payroll/
│   └── calendar.py          # Pay period generation, weekend adjustment
├── contrib/
│   └── engine.py            # 401(k) contribution engine (IRS limits)
├── taxes/
│   └── withholding.py       # Tax engine (python-taxes, tenforty, FICA)
├── espp/
│   └── engine.py            # ESPP engine (lookback, purchases)
├── rsu/
│   └── engine.py            # RSU engine (schedules, target-value grants)
├── prices/
│   └── yahoo.py             # yfinance price fetcher with multi-field caching
├── outputs/
│   └── writers.py           # CSV output (pay periods, money ledger, ESPP, RSU)
└── mappers/
    └── legacy.py            # First-year adjustment processing
```

## Notes

- python-taxes only supports up to 2025 tax year; 2026+ falls back to flat withholding rates
- ESPP cycle contributions span years (not reset on YTD reset); annual limit enforced at purchase time
- RSU FMV uses market open price on vest date; shares are whole numbers via cumulative floor rounding
- Pay dates falling on weekends are rolled to the preceding Friday
- 415(c) enforcement optionally includes employer match (`include_employer_in_415c: true`); per-year limits overrides merge with the base config, preserving unspecified fields
- Sign-on bonus 401(k) contributions use their own explicit rates (`sign_on_pretax_401k_pct`, etc.) rather than inheriting from the first pay period
