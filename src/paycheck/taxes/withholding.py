"""Unified tax withholding engine using python-taxes for federal/FICA and tenforty for state."""

from dataclasses import dataclass
from datetime import date
from typing import Optional, Literal, Dict, Any
from decimal import Decimal, ROUND_HALF_UP
import logging

try:
    from python_taxes.federal import income, social_security, medicare
    from tenforty import evaluate_return
except ImportError as e:
    logging.error(f"Required tax libraries not available: {e}")
    # Fallback implementations for testing
    class MockIncome:
        @staticmethod
        def employer_withholding(amount, pay_frequency="semimonthly", tax_year=2025):
            return float(amount) * 0.12  # Rough estimate
    
    class MockSS:
        @staticmethod
        def withholding(amount, tax_year=2025):
            return float(amount) * 0.062
    
    class MockMedicare:
        @staticmethod
        def required_withholding(amount):
            return float(amount) * 0.0145
    
    def evaluate_return(**kwargs):
        class MockResult:
            state_total_tax = kwargs.get('w2_income', 0) * 0.08
        return MockResult()
    
    income = MockIncome()
    social_security = MockSS()
    medicare = MockMedicare()

logger = logging.getLogger(__name__)


def round_decimal(value: float) -> Decimal:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class TaxEvent:
    """Represents a taxable event (wages, RSU, bonus, etc.)."""
    kind: Literal["wages", "rsu", "bonus", "sign_on", "relocation_taxed"]
    gross_amount: float
    event_date: date
    pre_tax_deductions: float = 0.0
    health_deductions: float = 0.0
    group_term_life: float = 0.0
    extra_income: float = 0.0
    apply_fica: bool = True
    method: Literal["inherit", "python_taxes", "tenforty_annualized", "withholding_flat"] = "inherit"
    override_flat_rate: Optional[float] = None
    supplemental_withholding: Optional[Dict[str, float]] = None  # {federal_rate, state_rate}


@dataclass
class TaxResult:
    """Result of tax withholding calculation."""
    federal_withholding: float
    state_withholding: float
    social_security: float
    medicare: float
    ca_voluntary_tax: float
    total_tax_paid: float
    net_cash: float
    
    # Tax bases for transparency
    federal_tax_base: float
    state_tax_base: float
    fica_tax_base: float
    ca_voluntary_tax_base: float
    
    # Additional metadata
    ytd_wages: float
    ytd_federal_withheld: float
    ytd_state_withheld: float


class UnifiedTaxEngine:
    """Unified tax engine using python-taxes for federal/FICA and tenforty for state."""
    
    def __init__(self, config):
        """Initialize the tax engine with configuration.
        
        Args:
            config: Application configuration object
        """
        self.config = config
        
        # Year-to-date tracking
        self.ytd_wages = 0.0
        self.ytd_ss_wages = 0.0
        self.ytd_medicare_wages = 0.0
        self.ytd_federal_withheld = 0.0
        self.ytd_state_withheld = 0.0
        
        # Tax year
        self.tax_year = config.taxes.tax_year

        # Resolved tax year for python-taxes (may differ if current year unsupported)
        self._resolved_python_taxes_year: Optional[int] = None

        # Pay frequency for python-taxes
        self.pay_frequency = self._get_python_taxes_frequency(config.calendar.pay_frequency)

        logger.info(f"Initialized tax engine: federal=python-taxes, state=tenforty, year={self.tax_year}")
    
    def _get_python_taxes_frequency(self, pay_frequency: str) -> str:
        """Map pay frequency to python-taxes format.
        
        Args:
            pay_frequency: Calendar pay frequency
            
        Returns:
            python-taxes compatible frequency string
        """
        frequency_map = {
            "semimonthly": "semimonthly",
            "biweekly": "biweekly", 
            "monthly": "monthly",
            "weekly": "weekly"
        }
        return frequency_map.get(pay_frequency, "semimonthly")
    
    def _resolve_python_taxes_year(self) -> int:
        """Find the best available tax year for python-taxes.

        Tries the configured tax_year first, then falls back to previous years.
        Caches the result so the probe only runs once per tax year.

        Returns:
            The best available tax year supported by python-taxes.
        """
        if self._resolved_python_taxes_year is not None:
            return self._resolved_python_taxes_year

        # Probe up to 3 years back
        for year in range(self.tax_year, self.tax_year - 4, -1):
            try:
                income.employer_withholding(
                    Decimal("1000.00"),
                    pay_frequency=self.pay_frequency,
                    tax_year=year,
                )
                if year != self.tax_year:
                    logger.warning(
                        f"python-taxes does not support tax year {self.tax_year}, "
                        f"falling back to {year} tax tables"
                    )
                self._resolved_python_taxes_year = year
                return year
            except Exception:
                continue

        # Nothing worked — fall back to configured year (will use flat rates)
        logger.warning(
            f"No supported python-taxes year found near {self.tax_year}, "
            f"flat-rate fallback will be used"
        )
        self._resolved_python_taxes_year = self.tax_year
        return self.tax_year

    def _get_annual_periods(self) -> int:
        """Get number of pay periods per year based on frequency.
        
        Returns:
            Number of pay periods per year
        """
        if self.config.calendar.pay_frequency == "semimonthly":
            return 24
        elif self.config.calendar.pay_frequency == "biweekly":
            return 26
        elif self.config.calendar.pay_frequency == "monthly":
            return 12
        elif self.config.calendar.pay_frequency == "weekly":
            return 52
        else:
            return 24  # Default fallback
    
    def process_event(self, event: TaxEvent) -> TaxResult:
        """Process a taxable event and compute withholding.
        
        Args:
            event: Tax event to process
            
        Returns:
            Tax result with all withholding components
        """
        logger.debug(f"Processing {event.kind} event: ${event.gross_amount:,.2f}")
        
        # Calculate tax bases according to the rules
        tax_bases = self._calculate_tax_bases(event)
        
        # Compute each tax component
        federal_withholding = self._compute_federal_tax(event, tax_bases["federal"])
        state_withholding = self._compute_state_tax(event, tax_bases["state"])
        social_security = self._compute_social_security(event, tax_bases["fica"])
        medicare = self._compute_medicare(event, tax_bases["fica"])
        ca_voluntary_tax = self._compute_ca_voluntary_tax(event, tax_bases["ca_voluntary"])
        
        # Calculate totals
        total_tax_paid = (federal_withholding + state_withholding +
                         social_security + medicare + ca_voluntary_tax)
        # Exclude group_term_life from net_cash: it's imputed income (increases
        # taxes but is not actual cash received by the employee).
        net_cash = tax_bases["federal"] - total_tax_paid - event.group_term_life
        
        # Update YTD tracking
        self._update_ytd(event, tax_bases, federal_withholding, state_withholding)
        
        return TaxResult(
            federal_withholding=federal_withholding,
            state_withholding=state_withholding,
            social_security=social_security,
            medicare=medicare,
            ca_voluntary_tax=ca_voluntary_tax,
            total_tax_paid=total_tax_paid,
            net_cash=net_cash,
            federal_tax_base=tax_bases["federal"],
            state_tax_base=tax_bases["state"],
            fica_tax_base=tax_bases["fica"],
            ca_voluntary_tax_base=tax_bases["ca_voluntary"],
            ytd_wages=self.ytd_wages,
            ytd_federal_withheld=self.ytd_federal_withheld,
            ytd_state_withheld=self.ytd_state_withheld
        )
    
    def _calculate_tax_bases(self, event: TaxEvent) -> Dict[str, float]:
        """Calculate tax bases for different tax types according to the rules.
        
        Tax base rules:
        - Federal/State tax: salary + extra_income + group_term_life - pretax_deductions - health_deductions
        - FICA tax: salary + extra_income + group_term_life - health_deductions  
        - CA voluntary tax: salary + extra_income + group_term_life - health_deductions - group_term_life
        
        Args:
            event: Tax event to process
            
        Returns:
            Dictionary with tax bases for each tax type
        """
        # Base income components
        base_income = event.gross_amount + event.extra_income + event.group_term_life
        
        # Federal and State tax base: base_income - pretax_deductions - health_deductions
        federal_state_base = base_income - event.pre_tax_deductions - event.health_deductions
        
        # FICA tax base: base_income - health_deductions (no pretax 401k deduction for FICA)
        fica_base = base_income - event.health_deductions
        
        # CA voluntary tax base: base_income - health_deductions - group_term_life
        ca_voluntary_base = base_income - event.health_deductions - event.group_term_life
        
        return {
            "federal": max(0.0, federal_state_base),
            "state": max(0.0, federal_state_base),
            "fica": max(0.0, fica_base),
            "ca_voluntary": max(0.0, ca_voluntary_base)
        }
    
    def _compute_federal_tax(self, event: TaxEvent, tax_base: float) -> float:
        """Compute federal income tax withholding using python-taxes.
        
        Args:
            event: Tax event
            tax_base: Federal tax base
            
        Returns:
            Federal withholding amount
        """
        if tax_base <= 0:
            return 0.0
        
        # Handle override flat rate
        if event.override_flat_rate is not None:
            return tax_base * event.override_flat_rate * 0.8  # Assume 80% federal
        
        # Handle explicit supplemental rates
        if event.supplemental_withholding is not None:
            return tax_base * event.supplemental_withholding.get("federal_rate", 0.0)
        
        # Determine method
        method = event.method
        if method == "inherit":
            method = self.config.taxes.federal.method
        
        if method == "python_taxes":
            try:
                resolved_year = self._resolve_python_taxes_year()
                withholding = float(
                    income.employer_withholding(
                        round_decimal(tax_base),
                        pay_frequency=self.pay_frequency,
                        tax_year=resolved_year,
                    )
                )

                # Add extra withholding if configured
                withholding += self.config.taxes.federal.extra_withholding_per_period

                return max(0.0, withholding)

            except Exception as e:
                logger.error(f"Error in python-taxes federal calculation: {e}")
                # Fallback to flat rate
                return tax_base * 0.12
        
        elif method == "withholding_flat":
            # Use supplemental rates
            if event.kind == "rsu":
                rate = self.config.taxes.supplemental.rsu_withholding_rate
            else:
                rate = self.config.taxes.supplemental.bonus_withholding_rate
            
            # Apply tiered supplemental rates (22% up to $1M, 37% above)
            if tax_base <= 1_000_000:
                return tax_base * 0.22
            else:
                return 1_000_000 * 0.22 + (tax_base - 1_000_000) * 0.37
        
        else:
            raise ValueError(f"Unknown federal tax method: {method}")
    
    def _compute_state_tax(self, event: TaxEvent, tax_base: float) -> float:
        """Compute state income tax withholding using tenforty annualization.
        
        Args:
            event: Tax event
            tax_base: State tax base
            
        Returns:
            State withholding amount
        """
        if tax_base <= 0:
            return 0.0
        
        # Handle override flat rate
        if event.override_flat_rate is not None:
            return tax_base * event.override_flat_rate * 0.2  # Assume 20% state
        
        # Handle explicit supplemental rates
        if event.supplemental_withholding is not None:
            return tax_base * event.supplemental_withholding.get("state_rate", 0.0)
        
        # Determine method
        method = event.method
        if method == "inherit":
            method = self.config.taxes.state.method
        
        if method == "tenforty_annualized":
            try:
                # Annualize the current paycheck value and compute state tax
                annual_income = tax_base * self._get_annual_periods()
                
                result = evaluate_return(
                    w2_income=annual_income,
                    state=self.config.taxes.state.code,
                    filing_status=self._get_tenforty_filing_status()
                )
                
                # Divide annual tax by number of periods
                period_state_tax = result.state_total_tax / self._get_annual_periods()
                
                return max(0.0, period_state_tax)
                
            except Exception as e:
                logger.error(f"Error in tenforty state calculation: {e}")
                # Fallback to flat rate (CA estimate)
                return tax_base * 0.08
        
        elif method == "withholding_flat":
            # Use flat state rate (10% for supplemental)
            return tax_base * 0.10
        
        else:
            raise ValueError(f"Unknown state tax method: {method}")
    
    def _compute_social_security(self, event: TaxEvent, tax_base: float) -> float:
        """Compute Social Security tax using python-taxes.
        
        Args:
            event: Tax event
            tax_base: FICA tax base
            
        Returns:
            Social Security tax amount
        """
        if not event.apply_fica or not self.config.taxes.fica.social_security or tax_base <= 0:
            return 0.0
        
        try:
            resolved_year = self._resolve_python_taxes_year()
            return float(social_security.withholding(round_decimal(tax_base), tax_year=resolved_year))
        except Exception as e:
            logger.error(f"Error in python-taxes Social Security calculation: {e}")
            # Fallback calculation
            ss_wage_base = 176100  # 2025 limit
            if self.ytd_ss_wages < ss_wage_base:
                ss_taxable = min(tax_base, ss_wage_base - self.ytd_ss_wages)
                return ss_taxable * 0.062
            return 0.0
    
    def _compute_medicare(self, event: TaxEvent, tax_base: float) -> float:
        """Compute Medicare tax using python-taxes.
        
        Args:
            event: Tax event
            tax_base: FICA tax base
            
        Returns:
            Medicare tax amount
        """
        if not event.apply_fica or not self.config.taxes.fica.medicare or tax_base <= 0:
            return 0.0
        
        try:
            return float(medicare.required_withholding(round_decimal(tax_base)))
        except Exception as e:
            logger.error(f"Error in python-taxes Medicare calculation: {e}")
            # Fallback calculation
            medicare_tax = tax_base * 0.0145
            
            # Additional Medicare tax (0.9% above threshold)
            additional_medicare_threshold = 200000  # Single filer
            new_ytd_medicare = self.ytd_medicare_wages + tax_base
            if new_ytd_medicare > additional_medicare_threshold:
                if self.ytd_medicare_wages < additional_medicare_threshold:
                    additional_base = new_ytd_medicare - additional_medicare_threshold
                else:
                    additional_base = tax_base
                medicare_tax += additional_base * 0.009
            
            return medicare_tax
    
    def _compute_ca_voluntary_tax(self, event: TaxEvent, tax_base: float) -> float:
        """Compute California voluntary withholding tax.
        
        Args:
            event: Tax event
            tax_base: CA voluntary tax base
            
        Returns:
            CA voluntary tax amount
        """
        if self.config.taxes.state.code.upper() != "CA" or tax_base <= 0:
            return 0.0
        
        return tax_base * self.config.taxes.ca.voluntary_pct
    
    def _get_tenforty_filing_status(self) -> str:
        """Map filing status to tenforty format.
        
        Returns:
            Filing status in tenforty format
        """
        filing_status_map = {
            "single": "Single",
            "married_filing_jointly": "Married/Joint",
            "married_filing_separately": "Married/Sep",
            "head_of_household": "Head_of_House"
        }
        return filing_status_map.get(self.config.taxes.federal.filing_status, "Single")
    
    def _update_ytd(self, event: TaxEvent, tax_bases: Dict[str, float], 
                   federal_withholding: float, state_withholding: float) -> None:
        """Update year-to-date tracking.
        
        Args:
            event: Tax event
            tax_bases: Tax bases dictionary
            federal_withholding: Federal withholding amount
            state_withholding: State withholding amount
        """
        self.ytd_wages += tax_bases["federal"]  # Use federal tax base for YTD wages
        self.ytd_federal_withheld += federal_withholding
        self.ytd_state_withheld += state_withholding
        
        if event.apply_fica:
            self.ytd_ss_wages += tax_bases["fica"]
            self.ytd_medicare_wages += tax_bases["fica"]
    
    def update_tax_year(self, year: int) -> None:
        """Update the tax year (called per year for per-year config).

        Args:
            year: Tax year to use for calculations
        """
        self.tax_year = year
        self._resolved_python_taxes_year = None  # Reset cache for new year
        logger.info(f"Updated tax year to {year}")

    def reset_ytd(self) -> None:
        """Reset year-to-date tracking (for new tax year)."""
        self.ytd_wages = 0.0
        self.ytd_ss_wages = 0.0
        self.ytd_medicare_wages = 0.0
        self.ytd_federal_withheld = 0.0
        self.ytd_state_withheld = 0.0
    
    def get_ytd_summary(self) -> Dict[str, float]:
        """Get year-to-date summary.
        
        Returns:
            Dictionary with YTD totals
        """
        return {
            "ytd_wages": self.ytd_wages,
            "ytd_ss_wages": self.ytd_ss_wages,
            "ytd_medicare_wages": self.ytd_medicare_wages,
            "ytd_federal_withheld": self.ytd_federal_withheld,
            "ytd_state_withheld": self.ytd_state_withheld,
        }