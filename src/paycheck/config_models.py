"""Pydantic v2 configuration models for the paycheck calculator."""

from datetime import date
from typing import List, Literal, Optional, Union, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.types import PositiveFloat, PositiveInt


class PersonConfig(BaseModel):
    """Personal information configuration."""
    name: str
    state: str = Field(default="CA", description="State abbreviation")
    filing_status: Literal["single", "married_filing_jointly", "married_filing_separately", "head_of_household"] = "single"
    start_date: date = Field(description="Employment start date")


class CalendarConfig(BaseModel):
    """Calendar and pay frequency configuration."""
    years: List[int] = Field(description="Years to calculate")
    pay_frequency: Literal["semimonthly", "biweekly", "monthly"] = "semimonthly"
    trading_calendar: str = Field(default="XNYS", description="Trading calendar for market data")
    holidays: str = Field(default="US", description="Holiday calendar")

    @field_validator("years")
    @classmethod
    def validate_years(cls, v):
        if not v:
            raise ValueError("At least one year must be specified")
        return sorted(v)


class MarketDataConfig(BaseModel):
    """Market data provider configuration."""
    provider: Literal["yfinance"] = "yfinance"
    symbol: str = Field(default="", description="Stock symbol")
    price_field: str = Field(default="Adj Close", description="Price field to use")
    session_tz: str = Field(default="US/Eastern", description="Trading session timezone")
    cache_dir: str = Field(default=".cache/prices", description="Cache directory for price data")


class LimitsConfig(BaseModel):
    """IRS contribution limits configuration."""
    irs_402g_employee_deferral: PositiveInt = Field(default=23500, description="402(g) employee deferral limit")
    irs_415c_annual_additions: PositiveInt = Field(default=69000, description="415(c) annual additions limit")
    include_employer_in_415c: bool = Field(default=False, description="Include employer match in 415(c) limit")


class MatchTier(BaseModel):
    """Employer match tier configuration."""
    match_rate: float = Field(ge=0.0, le=2.0, description="Match rate (e.g., 1.0 for 100%)")
    up_to_usd: PositiveFloat = Field(description="Dollar amount up to which this rate applies")


class EmployerMatchConfig(BaseModel):
    """Employer match configuration."""
    mode: Literal["tiers", "none"] = "tiers"
    apply_only_to_employee_deferrals: bool = Field(default=True, description="Match only pretax+Roth deferrals")
    tiers: List[MatchTier] = Field(default_factory=list, description="Match tiers")

    @field_validator("tiers")
    @classmethod
    def validate_tiers(cls, v, info):
        if info.data.get("mode") == "tiers" and not v:
            raise ValueError("Tiers must be specified when mode is 'tiers'")
        return v


class ESPPOfferingConfig(BaseModel):
    """ESPP offering configuration."""
    lookback_months: PositiveInt = Field(default=24, description="Lookback period in months")
    first_offer_date: Optional[date] = Field(default=None, description="First offering date")


class ESPPConfig(BaseModel):
    """Employee Stock Purchase Plan configuration."""
    enabled: bool = False
    discount_pct: float = Field(default=0.15, ge=0.0, le=0.5, description="Discount percentage")
    annual_limit_usd: PositiveFloat = Field(default=21250, description="Annual contribution limit")
    limit_mode: Literal["contribution_usd", "fmv_usd"] = "contribution_usd"
    max_contribution_pct: float = Field(default=0.25, ge=0.01, le=1.0, description="Maximum ESPP contribution percentage")
    allow_fractional_shares: bool = False
    purchase_months: List[int] = Field(default=[2, 8], description="Purchase months")
    purchase_day_rule: Literal["last_trading_day", "15th", "last_day"] = "last_trading_day"
    offering: ESPPOfferingConfig = Field(default_factory=ESPPOfferingConfig)

    @field_validator("purchase_months")
    @classmethod
    def validate_purchase_months(cls, v):
        if not all(1 <= month <= 12 for month in v):
            raise ValueError("Purchase months must be between 1 and 12")
        return sorted(v)


class FirstYearAdjustments(BaseModel):
    """First year adjustments (sign-on bonus, relocation, etc.)."""
    sign_on_bonus: float = Field(default=0.0, ge=0.0, description="Sign-on bonus amount")
    sign_on_pretax_401k_pct: float = Field(default=0.0, ge=0.0, le=1.0, description="Pre-tax 401(k) contribution pct for sign-on bonus")
    sign_on_roth_401k_pct: float = Field(default=0.0, ge=0.0, le=1.0, description="Roth 401(k) contribution pct for sign-on bonus")
    sign_on_aftertax_401k_pct: float = Field(default=0.0, ge=0.0, le=1.0, description="After-tax 401(k) contribution pct for sign-on bonus")
    relocation_taxed: float = Field(default=0.0, ge=0.0, description="Taxed relocation amount")
    relocation_itemized: float = Field(default=0.0, ge=0.0, description="Itemized relocation amount")
    relocation_tax_advantaged: float = Field(default=0.0, ge=0.0, description="Tax-advantaged relocation amount")
    espp_eligibility_date: Optional[date] = Field(default=None, description="ESPP eligibility date")


class ContributionSchedule(BaseModel):
    """Contribution schedule configuration."""
    pretax_401k_pct: Union[float, List[float]] = Field(default=0.0, description="Pre-tax 401(k) percentages")
    roth_401k_pct: Union[float, List[float]] = Field(default=0.0, description="Roth 401(k) percentages")
    aftertax_401k_pct: Union[float, List[float]] = Field(default=0.0, description="After-tax 401(k) percentages")
    espp_pct: Union[float, List[float]] = Field(default=0.0, description="ESPP percentages")

    @field_validator("pretax_401k_pct", "roth_401k_pct", "aftertax_401k_pct", "espp_pct")
    @classmethod
    def validate_percentages(cls, v):
        if isinstance(v, (int, float)):
            if not 0.0 <= v <= 1.0:
                raise ValueError("Percentage must be between 0.0 and 1.0")
            return v
        elif isinstance(v, list):
            for pct in v:
                if not 0.0 <= pct <= 1.0:
                    raise ValueError("All percentages must be between 0.0 and 1.0")
            return v
        else:
            raise ValueError("Percentage must be a float or list of floats")


class PayrollConfig(BaseModel):
    """Payroll configuration."""
    salary_annual: PositiveFloat = Field(description="Annual salary")
    state: str = Field(default="CA", description="State for payroll taxes")
    extra_income_per_period: Union[float, List[float]] = Field(default=0.0, description="Extra income per pay period (scalar or per-period list)")
    health_deductions_per_period: Union[float, List[float]] = Field(default=0.0, description="Health insurance deductions per period (scalar or per-period list)")
    group_term_life_per_period: Union[float, List[float]] = Field(default=0.0, description="Group term life insurance per period (scalar or per-period list)")
    contribution_schedule: ContributionSchedule = Field(default_factory=ContributionSchedule)
    employer_match: EmployerMatchConfig = Field(default_factory=EmployerMatchConfig)
    espp: ESPPConfig = Field(default_factory=ESPPConfig)
    first_year_adjustments: FirstYearAdjustments = Field(default_factory=FirstYearAdjustments)


class FederalTaxConfig(BaseModel):
    """Federal tax configuration."""
    method: Literal["python_taxes", "withholding_flat"] = "python_taxes"
    filing_status: Literal["single", "married_filing_jointly", "married_filing_separately", "head_of_household"] = "single"
    extra_withholding_per_period: float = Field(default=0.0, ge=0.0, description="Extra withholding per period")


class StateTaxConfig(BaseModel):
    """State tax configuration."""
    code: str = Field(default="CA", description="State code")
    method: Literal["tenforty_annualized", "withholding_flat"] = "tenforty_annualized"


class FICATaxConfig(BaseModel):
    """FICA tax configuration."""
    social_security: bool = True
    medicare: bool = True


class SupplementalTaxConfig(BaseModel):
    """Supplemental tax rates configuration."""
    rsu_withholding_rate: float = Field(default=0.37, ge=0.0, le=1.0, description="RSU withholding rate")
    bonus_withholding_rate: float = Field(default=0.22, ge=0.0, le=1.0, description="Bonus withholding rate")


class CATaxConfig(BaseModel):
    """California-specific tax configuration."""
    voluntary_pct: float = Field(default=0.01, ge=0.0, le=0.1, description="Voluntary withholding percentage")


class TaxesConfig(BaseModel):
    """Tax configuration."""
    tax_year: int = Field(default=2025, description="Tax year")
    federal: FederalTaxConfig = Field(default_factory=FederalTaxConfig)
    state: StateTaxConfig = Field(default_factory=StateTaxConfig)
    fica: FICATaxConfig = Field(default_factory=FICATaxConfig)
    supplemental: SupplementalTaxConfig = Field(default_factory=SupplementalTaxConfig)
    ca: CATaxConfig = Field(default_factory=CATaxConfig)


class RSUSchedule(BaseModel):
    """RSU vesting schedule configuration."""
    type: Literal["per_year", "per_quarter", "nvidia_quarterly", "custom_dates"] = "per_year"
    percentages: List[float] = Field(description="Vesting percentages")
    frequency: Literal["quarterly", "monthly"] = Field(default="quarterly", description="Vesting frequency within year")
    
    # For nvidia_quarterly schedule type
    first_vest_delay_months: Optional[int] = Field(default=None, description="Months to delay first vest from grant date")
    vest_day_rule: Literal["nvidia_wednesdays", "15th", "custom"] = Field(default="nvidia_wednesdays", description="Vesting day rule")
    
    # For custom_dates type
    custom_vest_dates: Optional[List[date]] = Field(default=None, description="Custom vest dates")

    @field_validator("percentages")
    @classmethod
    def validate_percentages(cls, v):
        if not v:
            raise ValueError("At least one percentage must be specified")
        if not all(0.0 <= pct <= 100.0 for pct in v):
            raise ValueError("All percentages must be between 0.0 and 100.0")
        return v
    
    @field_validator("custom_vest_dates")
    @classmethod
    def validate_custom_dates(cls, v, info):
        if info.data.get("type") == "custom_dates" and not v:
            raise ValueError("custom_vest_dates required when type is 'custom_dates'")
        return v


class RSUTaxConfig(BaseModel):
    """RSU tax configuration (simplified flat rates)."""
    federal_withholding_rate: float = Field(default=0.22, ge=0.0, le=1.0, description="Federal withholding rate")
    state_withholding_rate: float = Field(default=0.10, ge=0.0, le=1.0, description="State withholding rate")
    apply_fica: bool = Field(default=True, description="Apply FICA taxes to RSU income")
    apply_ca_voluntary: bool = Field(default=True, description="Apply CA voluntary tax to RSU income")


class RSUGrant(BaseModel):
    """RSU grant configuration."""
    grant_id: str = Field(description="Unique grant identifier")
    symbol: str = Field(description="Stock symbol")
    
    # Grant specification - either target value OR total shares
    target_value_usd: Optional[PositiveFloat] = Field(default=None, description="Target grant value in USD")
    total_shares: Optional[PositiveFloat] = Field(default=None, description="Total shares granted (if not using target value)")
    
    # Grant date calculation
    grant_date: Optional[date] = Field(default=None, description="Explicit grant date")
    grant_date_rule: Literal["explicit", "sixth_business_day_following_month", "custom"] = Field(default="explicit", description="Grant date calculation rule")
    employment_start_date: Optional[date] = Field(default=None, description="Employment start date for grant date calculation")
    
    # Share calculation (for target value grants)
    share_calculation: Optional[Dict[str, Any]] = Field(default=None, description="Share calculation parameters")
    
    schedule: RSUSchedule = Field(description="Vesting schedule")
    withholding_method: Literal["shares", "cash"] = "shares"
    tax: RSUTaxConfig = Field(default_factory=RSUTaxConfig)
    
    @model_validator(mode="after")
    def validate_grant_specification(self):
        """Validate that either target_value_usd or total_shares is specified."""
        if self.target_value_usd is None and self.total_shares is None:
            raise ValueError("Either target_value_usd or total_shares must be specified")
        
        if self.target_value_usd is not None and self.total_shares is not None:
            raise ValueError("Cannot specify both target_value_usd and total_shares")
        
        if self.grant_date_rule != "explicit" and self.employment_start_date is None:
            raise ValueError("employment_start_date required for non-explicit grant date rules")
        
        if self.target_value_usd is not None and self.share_calculation is None:
            # Set default share calculation parameters
            self.share_calculation = {
                "method": "30_day_average",
                "price_field": "Close",
                "rounding": "down"
            }
        
        return self


class Raise(BaseModel):
    """Mid-year salary raise."""
    effective_date: date = Field(description="Date the raise takes effect")
    salary_annual: PositiveFloat = Field(description="New annual salary")


class PerYearOverrides(BaseModel):
    """Per-year configuration overrides for multi-year projections."""
    salary_annual: Optional[PositiveFloat] = Field(default=None, description="Override annual salary")
    raises: Optional[List["Raise"]] = Field(default=None, description="Mid-year salary raises with effective dates")
    contribution_schedule: Optional[ContributionSchedule] = Field(default=None, description="Override contribution schedule")
    limits: Optional[LimitsConfig] = Field(default=None, description="Override IRS limits")
    tax_year: Optional[int] = Field(default=None, description="Override tax year (defaults to calendar year)")
    espp_annual_limit_usd: Optional[PositiveFloat] = Field(default=None, description="Override ESPP annual limit")
    extra_income_per_period: Optional[Union[float, List[float]]] = Field(default=None, description="Override extra income per period (scalar or per-period list)")
    health_deductions_per_period: Optional[Union[float, List[float]]] = Field(default=None, description="Override health deductions per period (scalar or per-period list)")
    group_term_life_per_period: Optional[Union[float, List[float]]] = Field(default=None, description="Override group term life per period (scalar or per-period list)")
    first_year_adjustments: Optional[FirstYearAdjustments] = Field(default=None, description="Override first year adjustments")


class OutputWriteConfig(BaseModel):
    """Output write configuration."""
    ledger: bool = True
    per_period: bool = True
    per_year: bool = True
    espp: bool = True
    rsu: bool = True


class OutputsConfig(BaseModel):
    """Output configuration."""
    directory: str = Field(default="out", description="Output directory")
    write: OutputWriteConfig = Field(default_factory=OutputWriteConfig)
    include_tax_liability_estimates: bool = Field(default=True, description="Include tax liability estimates")


class FormatConfig(BaseModel):
    """Format configuration."""
    date: str = Field(default="%b %-d, %Y", description="Date format string")
    decimals: int = Field(default=2, ge=0, le=10, description="Decimal places")


class AppConfig(BaseModel):
    """Main application configuration."""
    version: int = Field(default=2, description="Configuration version")
    person: PersonConfig = Field(description="Personal information")
    calendar: CalendarConfig = Field(description="Calendar configuration")
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    payroll: PayrollConfig = Field(description="Payroll configuration")
    taxes: TaxesConfig = Field(default_factory=TaxesConfig)
    rsu_grants: List[RSUGrant] = Field(default_factory=list, description="RSU grants")
    per_year: Optional[Dict[int, PerYearOverrides]] = Field(default=None, description="Per-year configuration overrides")
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    format: FormatConfig = Field(default_factory=FormatConfig)

    @model_validator(mode="after")
    def validate_config(self):
        """Cross-field validation."""
        # Ensure filing status is consistent
        if self.person.filing_status != self.taxes.federal.filing_status:
            raise ValueError("Person filing status must match federal tax filing status")
        
        # Ensure state is consistent
        if self.person.state != self.payroll.state:
            raise ValueError("Person state must match payroll state")
        
        return self
