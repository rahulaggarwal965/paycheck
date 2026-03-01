"""Legacy compatibility adapters for sign-on bonus and relocation handling."""

from typing import List, Tuple, Optional

from paycheck.contrib.engine import ContributionResult
from paycheck.taxes.withholding import TaxEvent, TaxResult, UnifiedTaxEngine
from paycheck.contrib.engine import ContributionEngine
from paycheck.config_models import AppConfig, FirstYearAdjustments
import logging

logger = logging.getLogger(__name__)


def process_first_year_adjustments(config: AppConfig, tax_engine: UnifiedTaxEngine, contrib_engine: ContributionEngine,
                                   adjustments_override: Optional[FirstYearAdjustments] = None) -> List[Tuple[str, Optional[ContributionResult], TaxResult]]:
    """Process sign-on bonus and relocation per legacy behavior.

    This function replicates the behavior of the original take-home-pay script
    for handling first-year adjustments:
    - Sign-on bonus: allows 401k contributions, no ESPP
    - Relocation taxed: taxed like wages, no 401k/ESPP contributions
    - Relocation itemized/tax-advantaged: untaxed income (handled in output writers)

    Args:
        config: Application configuration object
        tax_engine: Unified tax engine
        contrib_engine: Contribution engine
        adjustments_override: Optional override for first year adjustments (from per-year config)

    Returns:
        List of tuples: (adjustment_type, contrib_result, tax_result)
    """
    results = []
    adjustments = adjustments_override if adjustments_override is not None else config.payroll.first_year_adjustments
    
    # Process sign-on bonus
    if adjustments.sign_on_bonus > 0:
        logger.info(f"Processing sign-on bonus: ${adjustments.sign_on_bonus:,.2f}")
        
        # Sign-on bonus allows 401k contributions but no ESPP
        contrib_result = contrib_engine.process_period(
            gross_pay=adjustments.sign_on_bonus,
            pretax_pct=adjustments.sign_on_pretax_401k_pct,
            roth_pct=adjustments.sign_on_roth_401k_pct,
            aftertax_pct=adjustments.sign_on_aftertax_401k_pct,
            espp_pct=0.0  # No ESPP on sign-on bonus
        )
        
        # Create tax event for sign-on bonus
        tax_event = TaxEvent(
            kind="sign_on",
            gross_amount=adjustments.sign_on_bonus,
            event_date=config.person.start_date,
            pre_tax_deductions=contrib_result.pretax_401k,
            health_deductions=0.0,  # No health deductions on bonus
            group_term_life=0.0,    # No group term life on bonus
            extra_income=0.0,       # Sign-on bonus is the gross amount
            apply_fica=True,
            method="inherit"
        )
        
        tax_result = tax_engine.process_event(tax_event)
        results.append(("sign_on_bonus", contrib_result, tax_result))
        
        logger.info(f"Sign-on bonus processed: gross=${adjustments.sign_on_bonus:,.2f}, "
                   f"401k=${contrib_result.pretax_401k:,.2f}, "
                   f"tax=${tax_result.total_tax_paid:,.2f}, "
                   f"net=${tax_result.net_cash:,.2f}")
    
    # Process relocation (taxed portion)
    if adjustments.relocation_taxed > 0:
        logger.info(f"Processing taxed relocation: ${adjustments.relocation_taxed:,.2f}")
        
        # Relocation taxed like wages but no 401k or ESPP contributions
        tax_event = TaxEvent(
            kind="relocation_taxed",
            gross_amount=adjustments.relocation_taxed,
            event_date=config.person.start_date,
            pre_tax_deductions=0.0,  # No 401k contributions on relocation
            health_deductions=0.0,   # No health deductions on relocation
            group_term_life=0.0,     # No group term life on relocation
            extra_income=0.0,        # Relocation is the gross amount
            apply_fica=True,
            method="inherit"
        )
        
        tax_result = tax_engine.process_event(tax_event)
        results.append(("relocation_taxed", None, tax_result))
        
        logger.info(f"Taxed relocation processed: gross=${adjustments.relocation_taxed:,.2f}, "
                   f"tax=${tax_result.total_tax_paid:,.2f}, "
                   f"net=${tax_result.net_cash:,.2f}")
    
    # Note: relocation_itemized and relocation_tax_advantaged are handled
    # in the output writers as untaxed income lines
    
    for r in results:
        logger.info(f"Result: {r}")
    return results


def validate_legacy_compatibility(config, year_results: dict) -> dict:
    """Validate that results match legacy take-home-pay script expectations.
    
    This function performs validation checks to ensure the new implementation
    produces results consistent with the original take-home-pay script.
    
    Args:
        config: Application configuration object
        year_results: Year results from the new implementation
        
    Returns:
        Dictionary with validation results
    """
    validation_results = {
        "compatible": True,
        "warnings": [],
        "errors": []
    }
    
    # Check that CA voluntary tax is applied when state is CA
    if config.taxes.state.code.upper() == "CA":
        ca_voluntary_expected = config.taxes.ca.voluntary_pct > 0
        ca_voluntary_found = any(
            p.get("ca_voluntary_tax", 0) > 0 
            for p in year_results.get("periods_data", [])
        )
        
        if ca_voluntary_expected and not ca_voluntary_found:
            validation_results["errors"].append(
                "CA voluntary tax expected but not found in results"
            )
            validation_results["compatible"] = False
    
    # Check that employer match is calculated
    if config.payroll.employer_match.mode == "tiers":
        match_found = any(
            p.get("employer_match", 0) > 0 
            for p in year_results.get("periods_data", [])
        )
        
        if not match_found:
            validation_results["warnings"].append(
                "Employer match configured but no match found in results"
            )
    
    # Check 402(g) cap enforcement
    total_deferrals = sum(
        p.get("pretax_401k", 0) + p.get("roth_401k", 0)
        for p in year_results.get("periods_data", [])
    )
    
    if total_deferrals > config.limits.irs_402g_employee_deferral + 100:  # Allow small rounding
        validation_results["errors"].append(
            f"402(g) cap exceeded: ${total_deferrals:,.2f} > "
            f"${config.limits.irs_402g_employee_deferral:,.2f}"
        )
        validation_results["compatible"] = False
    
    # Check that ESPP purchases only occur in configured months
    if config.payroll.espp.enabled:
        espp_purchases = year_results.get("espp_purchases", [])
        for purchase in espp_purchases:
            if purchase.purchase_date.month not in config.payroll.espp.purchase_months:
                validation_results["errors"].append(
                    f"ESPP purchase in unexpected month: {purchase.purchase_date.month}"
                )
                validation_results["compatible"] = False
    
    # Log validation results
    if validation_results["compatible"]:
        logger.info("Legacy compatibility validation passed")
    else:
        logger.error("Legacy compatibility validation failed")
        for error in validation_results["errors"]:
            logger.error(f"  Error: {error}")
    
    for warning in validation_results["warnings"]:
        logger.warning(f"  Warning: {warning}")
    
    return validation_results


def convert_legacy_csv_to_config(csv_path: str) -> dict:
    """Convert legacy take-home-pay CSV output to new configuration format.
    
    This function can be used to migrate existing CSV outputs to the new
    configuration format for comparison or continued processing.
    
    Args:
        csv_path: Path to legacy CSV file
        
    Returns:
        Dictionary with configuration parameters
    """
    # This is a placeholder for CSV conversion functionality
    # Implementation would depend on the specific format of legacy CSV files
    
    logger.warning("Legacy CSV conversion not yet implemented")
    return {}


def generate_legacy_comparison_report(new_results: dict, legacy_results: dict = None) -> dict:
    """Generate a comparison report between new and legacy results.
    
    Args:
        new_results: Results from new implementation
        legacy_results: Results from legacy implementation (if available)
        
    Returns:
        Dictionary with comparison report
    """
    report = {
        "timestamp": str(pd.Timestamp.now()),
        "new_implementation": {
            "total_income": new_results.get("year_summary", {}).get("totals", {}).get("total_income", 0),
            "total_taxes": new_results.get("year_summary", {}).get("totals", {}).get("total_taxes", 0),
            "total_net": new_results.get("year_summary", {}).get("totals", {}).get("total_net", 0),
            "pay_periods": len(new_results.get("periods_data", [])),
            "rsu_vests": len(new_results.get("rsu_vests", [])),
            "espp_purchases": len(new_results.get("espp_purchases", []))
        }
    }
    
    if legacy_results:
        report["legacy_implementation"] = legacy_results
        
        # Calculate differences
        new_totals = report["new_implementation"]
        legacy_totals = legacy_results
        
        report["differences"] = {
            "income_diff": new_totals["total_income"] - legacy_totals.get("total_income", 0),
            "tax_diff": new_totals["total_taxes"] - legacy_totals.get("total_taxes", 0),
            "net_diff": new_totals["total_net"] - legacy_totals.get("total_net", 0)
        }
        
        # Flag significant differences (>$1)
        report["significant_differences"] = any(
            abs(diff) > 1.0 for diff in report["differences"].values()
        )
    
    return report


# Legacy constants and mappings for compatibility
LEGACY_PAY_PERIODS_SEMIMONTHLY = 24
LEGACY_PAY_PERIODS_BIWEEKLY = 26
LEGACY_PAY_PERIODS_MONTHLY = 12

LEGACY_CA_VOLUNTARY_RATE = 0.01  # 1% CA voluntary withholding

# Legacy employer match configuration (example from original script)
LEGACY_EMPLOYER_MATCH_TIERS = [
    {"match_rate": 1.00, "up_to_usd": 6000},   # 100% of first $6,000
    {"match_rate": 0.50, "up_to_usd": 11000}   # 50% of next $11,000
]

LEGACY_401K_CAP_2025 = 23500  # 402(g) limit
LEGACY_415C_CAP_2025 = 69000  # 415(c) limit
