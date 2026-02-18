"""Main computation pipeline integrating all engines."""

import logging
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union

import polars as pl

from paycheck.config_models import AppConfig, FirstYearAdjustments
from paycheck.contrib.engine import ContributionEngine, ContributionResult
from paycheck.espp.engine import ESPPEngine
from paycheck.mappers.legacy import process_first_year_adjustments
from paycheck.outputs.writers import OutputWriter
from paycheck.payroll.calendar import PayPeriodCalendar
from paycheck.prices.yahoo import YahooPriceFetcher
from paycheck.rsu.engine import RSUEngine
from paycheck.taxes.withholding import TaxEvent, TaxResult, UnifiedTaxEngine

logger = logging.getLogger(__name__)


class Engines(TypedDict):
    price_fetcher: YahooPriceFetcher
    calendar: PayPeriodCalendar
    tax_engine: UnifiedTaxEngine
    contrib_engine: ContributionEngine
    espp_engine: ESPPEngine
    rsu_engine: RSUEngine
    output_writer: OutputWriter


class YearConfig:
    """Resolved configuration for a single year."""

    def __init__(self, config: AppConfig, year: int):
        self.salary_annual = config.payroll.salary_annual
        self.contribution_schedule = config.payroll.contribution_schedule
        self.limits = config.limits
        self.tax_year = year  # Default to calendar year
        self.espp_annual_limit_usd = config.payroll.espp.annual_limit_usd
        self.extra_income_per_period = config.payroll.extra_income_per_period
        self.health_deductions_per_period = config.payroll.health_deductions_per_period
        self.group_term_life_per_period = config.payroll.group_term_life_per_period
        self.first_year_adjustments = config.payroll.first_year_adjustments


def resolve_year_config(config: AppConfig, year: int) -> YearConfig:
    """Merge base config with per-year overrides for the given year.

    Args:
        config: Application configuration object
        year: Calendar year being processed

    Returns:
        YearConfig with merged values
    """
    yc = YearConfig(config, year)

    if config.per_year and year in config.per_year:
        overrides = config.per_year[year]
        if overrides.salary_annual is not None:
            yc.salary_annual = overrides.salary_annual
        if overrides.contribution_schedule is not None:
            yc.contribution_schedule = overrides.contribution_schedule
        if overrides.limits is not None:
            yc.limits = overrides.limits
        if overrides.tax_year is not None:
            yc.tax_year = overrides.tax_year
        if overrides.espp_annual_limit_usd is not None:
            yc.espp_annual_limit_usd = overrides.espp_annual_limit_usd
        if overrides.extra_income_per_period is not None:
            yc.extra_income_per_period = overrides.extra_income_per_period
        if overrides.health_deductions_per_period is not None:
            yc.health_deductions_per_period = overrides.health_deductions_per_period
        if overrides.group_term_life_per_period is not None:
            yc.group_term_life_per_period = overrides.group_term_life_per_period
        if overrides.first_year_adjustments is not None:
            yc.first_year_adjustments = overrides.first_year_adjustments

    return yc


def _apply_year_config(engines: Dict[str, Any], year_config: YearConfig) -> None:
    """Push resolved year config to engines.

    Args:
        engines: Dictionary of engines
        year_config: Resolved config for the year
    """
    # Update contribution limits
    engines["contrib_engine"].update_limits(
        cap_402g=year_config.limits.irs_402g_employee_deferral,
        cap_415c=year_config.limits.irs_415c_annual_additions,
        include_employer=year_config.limits.include_employer_in_415c,
    )

    # Update tax year
    engines["tax_engine"].update_tax_year(year_config.tax_year)

    # Update ESPP annual limit
    engines["espp_engine"].annual_limit = year_config.espp_annual_limit_usd

def run_paycheck_pipeline(config: AppConfig) -> None:
    """Run the complete paycheck calculation pipeline.
    
    Args:
        config: Application configuration object
    """
    logger.info("Starting paycheck calculation pipeline")
    
    # Initialize all engines
    engines = _initialize_engines(config)
    
    # Process each year
    for year in config.calendar.years:
        logger.info(f"Processing year {year}")

        # Reset engines for new year if needed
        if year > min(config.calendar.years):
            _reset_engines_for_new_year(engines)

        # Resolve and apply per-year configuration (for every year, including first)
        year_config = resolve_year_config(config, year)
        _apply_year_config(engines, year_config)

        # Process the year
        year_results = _process_year(config, engines, year, year_config)
        
        # Write outputs
        written_files = engines["output_writer"].write_all_outputs(
            year=year,
            periods_data=year_results["periods_data"],
            espp_purchases=year_results["espp_purchases"],
            rsu_vests=year_results["rsu_vests"],
            year_summary=year_results["year_summary"],
            first_year_adjustments=year_results.get("first_year_adjustments"),
            first_year_config=year_config.first_year_adjustments,
            start_date=config.person.start_date,
        )
        
        # Log summary
        _log_year_summary(year, year_results, written_files)
    
    logger.info("Paycheck calculation pipeline completed")


def _initialize_engines(config) -> Dict[str, Any]:
    """Initialize all computation engines.
    
    Args:
        config: Application configuration object
        
    Returns:
        Dictionary of initialized engines
    """
    logger.info("Initializing engines")
    
    # Price fetcher
    price_fetcher = YahooPriceFetcher(
        symbol=config.market_data.symbol,
        cache_dir=config.market_data.cache_dir,
        price_field=config.market_data.price_field
    )
    
    # Calendar
    calendar = PayPeriodCalendar(
        start_date=config.person.start_date,
        years=config.calendar.years,
        pay_frequency=config.calendar.pay_frequency
    )
    
    # Tax engine
    tax_engine = UnifiedTaxEngine(config)
    
    # Contribution engine
    contrib_engine = ContributionEngine(config)
    
    # ESPP engine
    espp_engine = ESPPEngine(config, price_fetcher)
    
    # RSU engine
    rsu_engine = RSUEngine(config, price_fetcher, tax_engine)
    
    # Output writer
    output_writer = OutputWriter(config)
    
    return {
        "price_fetcher": price_fetcher,
        "calendar": calendar,
        "tax_engine": tax_engine,
        "contrib_engine": contrib_engine,
        "espp_engine": espp_engine,
        "rsu_engine": rsu_engine,
        "output_writer": output_writer
    }


def _reset_engines_for_new_year(engines: Dict[str, Any]) -> None:
    """Reset engines for a new tax year.
    
    Args:
        engines: Dictionary of engines
    """
    logger.info("Resetting engines for new tax year")
    
    engines["tax_engine"].reset_ytd()
    engines["contrib_engine"].reset_ytd()
    engines["espp_engine"].reset_ytd()


def _process_year(config, engines: Dict[str, Any], year: int,
                  cfg: YearConfig = None) -> Dict[str, Any]:
    """Process a single year of paycheck calculations.

    Args:
        config: Application configuration object
        engines: Dictionary of engines
        year: Year to process
        cfg: Resolved per-year configuration

    Returns:
        Dictionary with year results
    """
    if cfg is None:
        cfg = resolve_year_config(config, year)

    # Get pay periods for the year
    periods_df = engines["calendar"].generate_periods()
    year_periods = periods_df.filter(pl.col("year") == year)

    if year_periods.height == 0:
        logger.warning(f"No pay periods found for year {year}")
        return {
            "periods_data": [],
            "espp_purchases": [],
            "rsu_vests": [],
            "year_summary": {},
            "first_year_adjustments": None
        }

    # Expand per-period schedules for the year (contributions + payroll amounts)
    num_periods = year_periods.height
    contribution_pcts = _expand_per_period_schedules(
        cfg.contribution_schedule, cfg, num_periods
    )

    # Process first year adjustments if this year has adjustments configured
    first_year_adjustments = None
    has_adjustments = (
        cfg.first_year_adjustments.sign_on_bonus > 0 or
        cfg.first_year_adjustments.relocation_taxed > 0
    )
    if has_adjustments:
        first_year_adjustments = process_first_year_adjustments(
            config, engines["tax_engine"], engines["contrib_engine"],
            adjustments_override=cfg.first_year_adjustments
        )

    # Process pay periods with interleaved ESPP purchases
    periods_data, espp_purchases = _process_periods_and_espp(
        config, engines, year_periods, contribution_pcts, year, cfg
    )

    # Process RSU vests
    rsu_vests = _process_rsu_vests(engines, year)

    # Generate year summary
    year_summary = _generate_year_summary(
        config, engines, periods_data, espp_purchases, rsu_vests, first_year_adjustments, cfg=cfg
    )
    
    return {
        "periods_data": periods_data,
        "espp_purchases": espp_purchases,
        "rsu_vests": rsu_vests,
        "year_summary": year_summary,
        "first_year_adjustments": first_year_adjustments
    }


def _expand_per_period_schedules(contribution_schedule, cfg, num_periods: int) -> Dict[str, List[float]]:
    """Expand per-period schedules (contributions and payroll amounts) to match number of periods.

    Each field can be a single float (applied to every period) or a list of floats
    (one per period). Lists shorter than num_periods are extended with the last value.

    Args:
        contribution_schedule: Contribution schedule configuration
        cfg: Resolved YearConfig with per-period amount fields
        num_periods: Number of pay periods

    Returns:
        Dictionary of expanded per-period lists
    """
    def expand_list(input_val: Union[float, List[float]]) -> List[float]:
        if isinstance(input_val, (int, float)):
            return [float(input_val)] * num_periods

        val_list = [float(v) for v in input_val]
        if len(val_list) < num_periods:
            val_list += [val_list[-1]] * (num_periods - len(val_list))
        return val_list[:num_periods]

    return {
        "pretax_401k_pct": expand_list(contribution_schedule.pretax_401k_pct),
        "roth_401k_pct": expand_list(contribution_schedule.roth_401k_pct),
        "aftertax_401k_pct": expand_list(contribution_schedule.aftertax_401k_pct),
        "espp_pct": expand_list(contribution_schedule.espp_pct),
        "extra_income": expand_list(cfg.extra_income_per_period),
        "health_deductions": expand_list(cfg.health_deductions_per_period),
        "group_term_life": expand_list(cfg.group_term_life_per_period),
    }


def _process_periods_and_espp(config, engines: Dict[str, Any], year_periods: pl.DataFrame,
                              contribution_pcts: Dict[str, List[float]],
                              year: int,
                              cfg: YearConfig = None) -> tuple[List[Dict[str, Any]], List]:
    """Process pay periods with ESPP purchases triggered at month boundaries.

    This ensures ESPP purchases use only contributions accrued up to the purchase
    month, rather than the entire year's contributions.

    Args:
        config: Application configuration object
        engines: Dictionary of engines
        year_periods: DataFrame of pay periods for the year
        contribution_pcts: Expanded contribution percentages
        year: Year being processed
        cfg: Resolved per-year configuration

    Returns:
        Tuple of (periods_data, espp_purchases)
    """
    periods_data = []
    espp_purchases = []
    purchase_months = set(config.payroll.espp.purchase_months) if config.payroll.espp.enabled else set()
    prev_month = None

    for idx, period_row in enumerate(year_periods.iter_rows(named=True)):
        current_month = period_row["pay_date"].month

        # When crossing into a new month, trigger ESPP purchase for the previous month
        if prev_month is not None and current_month != prev_month and prev_month in purchase_months:
            try:
                purchase = engines["espp_engine"].process_purchase(prev_month, year)
                if purchase:
                    espp_purchases.append(purchase)
            except Exception as e:
                logger.error(f"Error processing ESPP purchase for {year}-{prev_month:02d}: {e}")

        period_data = _process_single_period(config, engines, period_row, contribution_pcts, idx, cfg)
        periods_data.append(period_data)
        prev_month = current_month

    # Handle purchase for the last month of the year
    if prev_month is not None and prev_month in purchase_months:
        try:
            purchase = engines["espp_engine"].process_purchase(prev_month, year)
            if purchase:
                espp_purchases.append(purchase)
        except Exception as e:
            logger.error(f"Error processing ESPP purchase for {year}-{prev_month:02d}: {e}")

    return periods_data, espp_purchases


def _process_single_period(config, engines: Dict[str, Any], period_row: Dict[str, Any],
                           contribution_pcts: Dict[str, List[float]], idx: int,
                           cfg: YearConfig = None) -> Dict[str, Any]:
    """Process a single pay period.

    Args:
        config: Application configuration object
        engines: Dictionary of engines
        period_row: Row from the pay periods DataFrame
        contribution_pcts: Expanded contribution percentages
        idx: Index into the contribution percentages lists
        cfg: Resolved per-year configuration

    Returns:
        Pay period result dictionary
    """
    # Use resolved salary from year config if available
    salary_annual = cfg.salary_annual if cfg else config.payroll.salary_annual

    # Calculate gross pay (salary portion only)
    if config.calendar.pay_frequency == "semimonthly":
        base_gross_pay = salary_annual / 24
    elif config.calendar.pay_frequency == "biweekly":
        base_gross_pay = salary_annual / 26
    elif config.calendar.pay_frequency == "monthly":
        base_gross_pay = salary_annual / 12
    else:
        raise ValueError(f"Unsupported pay frequency: {config.calendar.pay_frequency}")

    fractional_multiplier = period_row.get("fractional_multiplier", 1.0)
    gross_pay = base_gross_pay * fractional_multiplier

    # Additional income and deduction components (from expanded per-period schedules)
    extra_income = contribution_pcts["extra_income"][idx]
    health_deductions = contribution_pcts["health_deductions"][idx]
    group_term_life = contribution_pcts["group_term_life"][idx]

    # Contributions (calculated on salary, i.e. gross_pay)
    contrib_result = engines["contrib_engine"].process_period(
        gross_pay=gross_pay,
        pretax_pct=contribution_pcts["pretax_401k_pct"][idx],
        roth_pct=contribution_pcts["roth_401k_pct"][idx],
        aftertax_pct=contribution_pcts["aftertax_401k_pct"][idx],
        espp_pct=contribution_pcts["espp_pct"][idx]
    )

    # ESPP accrual
    espp_contrib = engines["espp_engine"].accrue_contribution(
        gross_pay=gross_pay,
        espp_pct=contribution_pcts["espp_pct"][idx],
        period_date=period_row["pay_date"]
    )

    # Taxes
    tax_event = TaxEvent(
        kind="wages",
        gross_amount=gross_pay,
        event_date=period_row["pay_date"],
        pre_tax_deductions=contrib_result.pretax_401k,
        health_deductions=health_deductions,
        group_term_life=group_term_life,
        extra_income=extra_income,
        apply_fica=True,
        method="inherit"
    )
    tax_result = engines["tax_engine"].process_event(tax_event)

    # Computed columns matching legacy definitions
    taxable_wages = gross_pay - contrib_result.pretax_401k
    post_tax_pay = taxable_wages - tax_result.total_tax_paid

    # final_take_home: post-tax cash minus post-tax deductions (Roth, AT, ESPP)
    final_take_home = tax_result.net_cash - contrib_result.roth_401k - contrib_result.aftertax_401k - espp_contrib

    period_data = {
        "period_index": period_row["period_index"],
        "period_start": period_row["period_start"],
        "period_end": period_row["period_end"],
        "pay_date": period_row["pay_date"],
        "year": period_row["year"],
        "gross_pay": gross_pay,
        "extra_income": extra_income,
        "group_term_life": group_term_life,
        "health_deductions": health_deductions,
        "total_earnings": gross_pay + extra_income + group_term_life,
        "fractional_multiplier": fractional_multiplier,
        "pretax_401k_pct": contribution_pcts["pretax_401k_pct"][idx],
        "pretax_401k": contrib_result.pretax_401k,
        "roth_401k_pct": contribution_pcts["roth_401k_pct"][idx],
        "roth_401k": contrib_result.roth_401k,
        "aftertax_401k_pct": contribution_pcts["aftertax_401k_pct"][idx],
        "aftertax_401k": contrib_result.aftertax_401k,
        "espp_pct": contribution_pcts["espp_pct"][idx],
        "employer_match": contrib_result.employer_match,
        "espp_contrib": espp_contrib,
        "taxable_wages": taxable_wages,
        "federal_withholding": tax_result.federal_withholding,
        "state_withholding": tax_result.state_withholding,
        "social_security": tax_result.social_security,
        "medicare": tax_result.medicare,
        "ca_voluntary_tax": tax_result.ca_voluntary_tax,
        "total_tax_paid": tax_result.total_tax_paid,
        "post_tax_pay": post_tax_pay,
        "net_cash": tax_result.net_cash,
        "final_take_home": final_take_home,
        "ytd_gross": tax_result.ytd_wages,
        "ytd_401k": contrib_result.ytd_employee_deferrals,
        "ytd_federal_withheld": tax_result.ytd_federal_withheld,
        "ytd_state_withheld": tax_result.ytd_state_withheld,
        "cap_402g_reached": contrib_result.cap_402g_reached,
        "cap_415c_reached": contrib_result.cap_415c_reached
    }

    logger.debug(f"Processed period {period_row['period_index']}: "
                f"gross=${gross_pay:,.2f}, take_home=${final_take_home:,.2f}")

    return period_data


def _process_rsu_vests(engines: Dict[str, Any], year: int) -> List:
    """Process RSU vests for a year.
    
    Args:
        engines: Dictionary of engines
        year: Year to process
        
    Returns:
        List of RSU vest objects for the year
    """
    return engines["rsu_engine"].process_all_grants_for_year(year)


def _generate_year_summary(config, engines: Dict[str, Any], periods_data: List[Dict[str, Any]],
                          espp_purchases: List, rsu_vests: List,
                          first_year_adjustments: Optional[List[Tuple[str, Optional[ContributionResult], TaxResult]]] = None,
                          cfg: YearConfig = None) -> Dict[str, Any]:
    """Generate year summary statistics.

    Args:
        config: Application configuration object
        engines: Dictionary of engines
        periods_data: Pay period data
        espp_purchases: ESPP purchase objects
        rsu_vests: RSU vest objects
        first_year_adjustments: First year adjustment data
        cfg: Resolved per-year configuration

    Returns:
        Year summary dictionary
    """
    # Aggregate payroll totals (include extra income, group term life)
    total_gross_pay = 0.0
    total_extra_income = 0.0
    total_group_term_life = 0.0

    for p in periods_data:
        total_gross_pay += p["gross_pay"]
        total_extra_income += p["extra_income"]
        total_group_term_life += p["group_term_life"]
    
    # Total income includes all components
    total_income = total_gross_pay + total_extra_income + total_group_term_life
    total_pretax_401k = sum(p["pretax_401k"] for p in periods_data)
    total_roth_401k = sum(p["roth_401k"] for p in periods_data)
    total_aftertax_401k = sum(p["aftertax_401k"] for p in periods_data)
    total_employer_match = sum(p["employer_match"] for p in periods_data)
    total_espp_contrib = sum(p["espp_contrib"] for p in periods_data)
    
    # Tax totals
    total_federal = sum(p["federal_withholding"] for p in periods_data)
    total_state = sum(p["state_withholding"] for p in periods_data)
    total_ss = sum(p["social_security"] for p in periods_data)
    total_medicare = sum(p["medicare"] for p in periods_data)
    total_ca_voluntary = sum(p["ca_voluntary_tax"] for p in periods_data)
    total_tax_paid = sum(p["total_tax_paid"] for p in periods_data)
    total_net_cash = sum(p["net_cash"] for p in periods_data)
    total_final_take_home = sum(p["final_take_home"] for p in periods_data)

    # Add first year adjustments
    if first_year_adjustments:
        for adjustment_type, contrib_result, tax_result in first_year_adjustments:
            if adjustment_type == "sign_on_bonus":
                total_gross_pay += tax_result.federal_tax_base + (contrib_result.pretax_401k if contrib_result else 0)
                if contrib_result:
                    total_pretax_401k += contrib_result.pretax_401k
                total_federal += tax_result.federal_withholding
                total_state += tax_result.state_withholding
                total_ss += tax_result.social_security
                total_medicare += tax_result.medicare
                total_ca_voluntary += tax_result.ca_voluntary_tax
                total_tax_paid += tax_result.total_tax_paid
                total_net_cash += tax_result.net_cash
                total_final_take_home += tax_result.net_cash
            elif adjustment_type == "relocation_taxed":
                total_gross_pay += tax_result.federal_tax_base
                total_federal += tax_result.federal_withholding
                total_state += tax_result.state_withholding
                total_ss += tax_result.social_security
                total_medicare += tax_result.medicare
                total_ca_voluntary += tax_result.ca_voluntary_tax
                total_tax_paid += tax_result.total_tax_paid
                total_net_cash += tax_result.net_cash
                total_final_take_home += tax_result.net_cash

    # Add untaxed relocation amounts to net (use resolved first_year_adjustments)
    adj = cfg.first_year_adjustments if cfg else config.payroll.first_year_adjustments
    if first_year_adjustments and adj.relocation_itemized > 0:
        total_net_cash += adj.relocation_itemized
        total_final_take_home += adj.relocation_itemized
    if first_year_adjustments and adj.relocation_tax_advantaged > 0:
        total_net_cash += adj.relocation_tax_advantaged
        total_final_take_home += adj.relocation_tax_advantaged
    
    # RSU totals (only actual, non-projected vests)
    actual_rsu_vests = [v for v in rsu_vests if not v.is_projected]
    projected_rsu_vests = [v for v in rsu_vests if v.is_projected]
    rsu_gross = sum(v.gross_amount for v in actual_rsu_vests)
    rsu_tax_paid = sum(v.total_tax_paid for v in actual_rsu_vests)
    rsu_net_value = sum(v.net_cash_value for v in actual_rsu_vests)

    # ESPP totals (only actual, non-pending purchases)
    actual_espp = [p for p in espp_purchases if not p.is_pending]
    pending_espp = [p for p in espp_purchases if p.is_pending]
    espp_total_contrib = sum(p.contributions for p in actual_espp)
    espp_total_shares = sum(p.shares_purchased for p in actual_espp if p.shares_purchased is not None)
    espp_total_discount = sum(
        p.discount_applied * p.shares_purchased
        for p in actual_espp
        if p.discount_applied is not None and p.shares_purchased is not None
    )
    
    # Get engine summaries
    contrib_summary = engines["contrib_engine"].get_ytd_summary()
    tax_summary = engines["tax_engine"].get_ytd_summary()
    espp_summary = engines["espp_engine"].get_ytd_summary()
    
    # Calculate effective tax rate
    total_income_with_rsu = total_income + rsu_gross
    total_taxes = total_tax_paid + rsu_tax_paid
    effective_tax_rate = total_taxes / total_income_with_rsu if total_income_with_rsu > 0 else 0.0
    
    return {
        "payroll": {
            "total_gross_pay": total_gross_pay,
            "total_pretax_401k": total_pretax_401k,
            "total_roth_401k": total_roth_401k,
            "total_aftertax_401k": total_aftertax_401k,
            "total_employer_match": total_employer_match,
            "total_espp_contrib": total_espp_contrib,
            "total_net_cash": total_net_cash,
            "total_final_take_home": total_final_take_home
        },
        "taxes": {
            "total_federal": total_federal,
            "total_state": total_state,
            "total_social_security": total_ss,
            "total_medicare": total_medicare,
            "total_ca_voluntary": total_ca_voluntary,
            "total_tax_paid": total_tax_paid
        },
        "rsu": {
            "total_gross_value": rsu_gross,
            "total_tax_paid": rsu_tax_paid,
            "total_net_value": rsu_net_value,
            "vests_count": len(actual_rsu_vests),
            "projected_vests_count": len(projected_rsu_vests),
            "projected_shares": sum(v.shares_vested for v in projected_rsu_vests),
        },
        "espp": {
            "total_contributions": espp_total_contrib,
            "total_shares": espp_total_shares,
            "total_discount_value": espp_total_discount,
            "purchases_count": len(actual_espp),
            "pending_purchases_count": len(pending_espp),
            "pending_contributions": sum(p.contributions for p in pending_espp),
        },
        "totals": {
            "total_income": total_income_with_rsu,
            "total_taxes": total_taxes,
            "total_net": total_net_cash + rsu_net_value,
            "effective_tax_rate": effective_tax_rate
        },
        "engine_summaries": {
            "contributions": contrib_summary,
            "taxes": tax_summary,
            "espp": espp_summary
        }
    }


def _log_year_summary(year: int, year_results: Dict[str, Any], 
                     written_files: Dict[str, Any]) -> None:
    """Log year summary information.
    
    Args:
        year: Year processed
        year_results: Year results dictionary
        written_files: Dictionary of written file paths
    """
    summary = year_results["year_summary"]
    
    logger.info(f"Year {year} Summary:")
    logger.info(f"  Total Income: ${summary['totals']['total_income']:,.2f}")
    logger.info(f"  Total Taxes: ${summary['totals']['total_taxes']:,.2f}")
    logger.info(f"  Total Net: ${summary['totals']['total_net']:,.2f}")
    logger.info(f"  Effective Tax Rate: {summary['totals']['effective_tax_rate']:.1%}")
    logger.info(f"  Pay Periods: {len(year_results['periods_data'])}")
    logger.info(f"  RSU Vests: {len(year_results['rsu_vests'])}")
    logger.info(f"  ESPP Purchases: {len(year_results['espp_purchases'])}")
    
    # Log written files
    files_written = [f for f in written_files.values() if f is not None]
    logger.info(f"  Files Written: {len(files_written)}")
    for file_path in files_written:
        logger.info(f"    {file_path}")


if __name__ == "__main__":
    # This allows the pipeline to be run directly for testing
    from .main import main
    main()
