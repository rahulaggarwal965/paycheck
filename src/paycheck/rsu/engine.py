"""RSU (Restricted Stock Unit) engine with target value grants and quarterly vesting."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Dict, Any, Tuple, Optional
from dateutil.relativedelta import relativedelta
import math
import logging

logger = logging.getLogger(__name__)


@dataclass
class RSUVest:
    """Represents an RSU vesting event."""
    grant_id: str
    vest_date: date
    shares_vested: float

    # Financial fields (None for projected/future vests)
    fmv: Optional[float] = None
    gross_amount: Optional[float] = None

    # Tax components (None for projected)
    federal_withholding: Optional[float] = None
    state_withholding: Optional[float] = None
    social_security: Optional[float] = None
    medicare: Optional[float] = None
    ca_voluntary_tax: Optional[float] = None
    total_tax_paid: Optional[float] = None

    # Share delivery (None for projected)
    withheld_shares: Optional[float] = None
    net_shares: Optional[float] = None
    net_cash_value: Optional[float] = None

    # Metadata
    withholding_method: str = "shares"
    vest_percentage: float = 0.0
    is_projected: bool = False


@dataclass
class RSUGrantInfo:
    """Information about a processed RSU grant."""
    grant_id: str
    symbol: str
    target_value_usd: Optional[float]
    calculated_shares: float
    grant_date: date
    average_price_for_calculation: Optional[float]
    calculation_period_start: Optional[date]
    calculation_period_end: Optional[date]


class RSUEngine:
    """Engine for RSU grant processing with target value calculation and flexible vesting."""
    
    def __init__(self, config, price_fetcher, tax_engine):
        """Initialize the RSU engine.
        
        Args:
            config: Application configuration object
            price_fetcher: Price fetcher for market data
            tax_engine: Tax engine for withholding calculations (not used with simplified tax)
        """
        self.config = config
        self.price_fetcher = price_fetcher
        self.tax_engine = tax_engine  # Keep for compatibility but use simplified tax
        self.vests = []
        self.processed_grants = []
        
        logger.info(f"Initialized RSU engine with {len(config.rsu_grants)} grants")
    
    def process_grants(self) -> List[RSUGrantInfo]:
        """Process all RSU grants to calculate shares and grant dates.
        
        Returns:
            List of processed grant information
        """
        processed_grants = []
        
        for grant in self.config.rsu_grants:
            try:
                grant_info = self._process_grant(grant)
                processed_grants.append(grant_info)
                logger.info(f"Processed grant {grant.grant_id}: {grant_info.calculated_shares:.0f} shares")
            except Exception as e:
                logger.error(f"Error processing grant {grant.grant_id}: {e}")
        
        self.processed_grants = processed_grants
        return processed_grants
    
    def _process_grant(self, grant) -> RSUGrantInfo:
        """Process a single RSU grant to calculate shares and grant date.
        
        Args:
            grant: RSU grant configuration
            
        Returns:
            RSUGrantInfo with calculated values
        """
        # Calculate grant date
        if grant.grant_date_rule == "explicit":
            grant_date = grant.grant_date
            if grant_date is None:
                raise ValueError(f"Explicit grant date required for grant {grant.grant_id}")
        elif grant.grant_date_rule == "sixth_business_day_following_month":
            grant_date = self._calculate_sixth_business_day_following_month(grant.employment_start_date)
        else:
            raise ValueError(f"Unknown grant date rule: {grant.grant_date_rule}")
        
        # Calculate shares
        if grant.target_value_usd is not None:
            # Calculate shares from target value
            calculated_shares, avg_price, calc_start, calc_end = self._calculate_shares_from_target_value(
                grant, grant_date
            )
        else:
            # Use explicit share count
            calculated_shares = grant.total_shares
            avg_price = None
            calc_start = None
            calc_end = None
        
        return RSUGrantInfo(
            grant_id=grant.grant_id,
            symbol=grant.symbol,
            target_value_usd=grant.target_value_usd,
            calculated_shares=calculated_shares,
            grant_date=grant_date,
            average_price_for_calculation=avg_price,
            calculation_period_start=calc_start,
            calculation_period_end=calc_end
        )
    
    def _calculate_sixth_business_day_following_month(self, employment_start_date: date) -> date:
        """Calculate the 6th business day of the month following employment start.
        
        Args:
            employment_start_date: Employment start date
            
        Returns:
            Grant date (6th business day of following month)
        """
        # Get first day of following month
        if employment_start_date.month == 12:
            following_month = date(employment_start_date.year + 1, 1, 1)
        else:
            following_month = date(employment_start_date.year, employment_start_date.month + 1, 1)
        
        # Count business days to find 6th business day
        current_date = following_month
        business_days_count = 0
        
        while business_days_count < 6:
            if current_date.weekday() < 5:  # Monday-Friday
                business_days_count += 1
                if business_days_count == 6:
                    return current_date
            current_date += timedelta(days=1)
        
        return current_date
    
    def _calculate_shares_from_target_value(self, grant, grant_date: date) -> Tuple[float, float, date, date]:
        """Calculate shares from target value using 30-day average.
        
        Args:
            grant: RSU grant configuration
            grant_date: Calculated grant date
            
        Returns:
            Tuple of (calculated_shares, average_price, calc_period_start, calc_period_end)
        """
        if grant.employment_start_date is None:
            raise ValueError("employment_start_date required for target value calculation")
        
        # Calculate 30-day period ending on last day of employment start month
        start_month_last_day = date(
            grant.employment_start_date.year,
            grant.employment_start_date.month,
            self._get_last_day_of_month(grant.employment_start_date.year, grant.employment_start_date.month)
        )
        
        calc_period_end = start_month_last_day
        calc_period_start = calc_period_end - timedelta(days=29)  # 30 days total
        
        # Get prices for the 30-day period
        prices = []
        current_date = calc_period_start
        
        while current_date <= calc_period_end:
            price = self.price_fetcher.get_price(current_date)
            if price is not None:
                prices.append(price)
            current_date += timedelta(days=1)
        
        if not prices:
            raise ValueError(f"No price data available for 30-day period ending {calc_period_end}")
        
        # Calculate average price
        average_price = sum(prices) / len(prices)
        
        # Calculate shares (rounded down to nearest whole share)
        calculated_shares = math.floor(grant.target_value_usd / average_price)
        
        logger.info(f"Target value calculation for {grant.grant_id}: "
                   f"${grant.target_value_usd:,.0f} ÷ ${average_price:.2f} = "
                   f"{calculated_shares:.0f} shares ({len(prices)} price points)")
        
        return calculated_shares, average_price, calc_period_start, calc_period_end
    
    def _get_last_day_of_month(self, year: int, month: int) -> int:
        """Get the last day of a month.
        
        Args:
            year: Year
            month: Month
            
        Returns:
            Last day of the month
        """
        import calendar
        return calendar.monthrange(year, month)[1]
    
    def generate_vest_schedule(self, grant) -> List[Tuple[date, int, float]]:
        """Generate vest dates and share amounts for a grant.
        
        Args:
            grant: RSU grant configuration
            
        Returns:
            List of (vest_date, shares, percentage) tuples
        """
        # Get processed grant info
        grant_info = None
        for info in self.processed_grants:
            if info.grant_id == grant.grant_id:
                grant_info = info
                break
        
        if grant_info is None:
            raise ValueError(f"Grant {grant.grant_id} not processed. Call process_grants() first.")
        
        total_shares = grant_info.calculated_shares
        
        if grant.schedule.type == "nvidia_quarterly":
            return self._generate_nvidia_quarterly_schedule(grant, grant_info, total_shares)
        elif grant.schedule.type == "per_quarter":
            return self._generate_per_quarter_schedule(grant, grant_info, total_shares)
        elif grant.schedule.type == "per_year":
            return self._generate_per_year_schedule(grant, grant_info, total_shares)
        elif grant.schedule.type == "custom_dates":
            return self._generate_custom_dates_schedule(grant, grant_info, total_shares)
        else:
            raise ValueError(f"Unknown schedule type: {grant.schedule.type}")
    
    def _generate_nvidia_quarterly_schedule(self, grant, grant_info: RSUGrantInfo,
                                          total_shares: float) -> List[Tuple[date, int, float]]:
        """Generate quarterly vesting schedule with whole shares (nvidia_quarterly type).

        Uses cumulative floor rounding: tracks cumulative expected shares and
        floors each cumulative total, so each vest gets whole shares and the
        total across all vests equals total_shares exactly.

        Args:
            grant: RSU grant configuration
            grant_info: Processed grant information
            total_shares: Total shares to vest

        Returns:
            List of (vest_date, shares, percentage) tuples
        """
        vests = []
        grant_date = grant_info.grant_date

        # Determine first vest date based on grant month
        first_vest_date = self._calculate_nvidia_first_vest_date(grant_date)

        current_vest_date = first_vest_date
        cumulative_pct = 0.0
        cumulative_shares_issued = 0

        for i, percentage in enumerate(grant.schedule.percentages):
            cumulative_pct += percentage
            cumulative_expected = total_shares * (cumulative_pct / 100.0)
            shares_this_vest = math.floor(cumulative_expected) - cumulative_shares_issued
            cumulative_shares_issued += shares_this_vest
            vests.append((current_vest_date, shares_this_vest, percentage))

            current_vest_date = self._get_next_nvidia_vest_date(current_vest_date)

        return vests
    
    def _calculate_nvidia_first_vest_date(self, grant_date: date) -> date:
        """Calculate first vest date for nvidia_quarterly schedule.
        
        Args:
            grant_date: Grant date
            
        Returns:
            First vest date
        """
        grant_month = grant_date.month
        grant_year = grant_date.year
        
        if 1 <= grant_month <= 3:  # Jan-Mar
            # First vest on 3rd Wednesday of following June
            return self._get_third_wednesday(grant_year, 6)
        elif 4 <= grant_month <= 6:  # Apr-Jun
            # First vest on 3rd Wednesday of following September
            return self._get_third_wednesday(grant_year, 9)
        elif 7 <= grant_month <= 9:  # Jul-Sep
            # First vest on 2nd Wednesday of following December
            return self._get_second_wednesday(grant_year, 12)
        else:  # Oct-Dec
            # First vest on 3rd Wednesday of following March
            return self._get_third_wednesday(grant_year + 1, 3)
    
    def _get_third_wednesday(self, year: int, month: int) -> date:
        """Get the 3rd Wednesday of a month.
        
        Args:
            year: Year
            month: Month
            
        Returns:
            Date of 3rd Wednesday
        """
        # Find first Wednesday
        first_day = date(year, month, 1)
        days_until_wednesday = (2 - first_day.weekday()) % 7  # Wednesday = 2
        first_wednesday = first_day + timedelta(days=days_until_wednesday)
        
        # Add 2 weeks to get 3rd Wednesday
        third_wednesday = first_wednesday + timedelta(weeks=2)
        
        # Handle fallback to 15th for dates after September 2028
        if year > 2028 or (year == 2028 and month > 9):
            return date(year, month, 15)
        
        return third_wednesday
    
    def _get_second_wednesday(self, year: int, month: int) -> date:
        """Get the 2nd Wednesday of a month.
        
        Args:
            year: Year
            month: Month
            
        Returns:
            Date of 2nd Wednesday
        """
        # Find first Wednesday
        first_day = date(year, month, 1)
        days_until_wednesday = (2 - first_day.weekday()) % 7  # Wednesday = 2
        first_wednesday = first_day + timedelta(days=days_until_wednesday)
        
        # Add 1 week to get 2nd Wednesday
        second_wednesday = first_wednesday + timedelta(weeks=1)
        
        # Handle fallback to 15th for dates after September 2028
        if year > 2028 or (year == 2028 and month > 9):
            return date(year, month, 15)
        
        return second_wednesday
    
    def _get_next_nvidia_vest_date(self, current_vest_date: date) -> date:
        """Get the next quarterly vest date (nvidia_quarterly cycle).
        
        Args:
            current_vest_date: Current vest date
            
        Returns:
            Next vest date
        """
        year = current_vest_date.year
        month = current_vest_date.month
        
        # Quarterly cycle: Mar (3rd Wed), Jun (3rd Wed), Sep (3rd Wed), Dec (2nd Wed)
        if month == 3:  # March -> June
            return self._get_third_wednesday(year, 6)
        elif month == 6:  # June -> September
            return self._get_third_wednesday(year, 9)
        elif month == 9:  # September -> December
            return self._get_second_wednesday(year, 12)
        elif month == 12:  # December -> March next year
            return self._get_third_wednesday(year + 1, 3)
        else:
            # Handle edge cases - default to next quarter
            if month < 3:
                return self._get_third_wednesday(year, 3)
            elif month < 6:
                return self._get_third_wednesday(year, 6)
            elif month < 9:
                return self._get_third_wednesday(year, 9)
            else:
                return self._get_second_wednesday(year, 12)
    
    def _generate_per_quarter_schedule(self, grant, grant_info: RSUGrantInfo,
                                     total_shares: float) -> List[Tuple[date, int, float]]:
        """Generate per-quarter vesting schedule with whole shares.

        Args:
            grant: RSU grant configuration
            grant_info: Processed grant information
            total_shares: Total shares to vest

        Returns:
            List of (vest_date, shares, percentage) tuples
        """
        vests = []
        grant_date = grant_info.grant_date
        cumulative_pct = 0.0
        cumulative_shares_issued = 0

        for quarter_idx, percentage in enumerate(grant.schedule.percentages):
            cumulative_pct += percentage
            cumulative_expected = total_shares * (cumulative_pct / 100.0)
            shares_this_vest = math.floor(cumulative_expected) - cumulative_shares_issued
            cumulative_shares_issued += shares_this_vest
            vest_date = grant_date + relativedelta(months=quarter_idx * 3)
            vests.append((vest_date, shares_this_vest, percentage))

        return vests
    
    def _generate_per_year_schedule(self, grant, grant_info: RSUGrantInfo,
                                  total_shares: float) -> List[Tuple[date, int, float]]:
        """Generate per-year vesting schedule with whole shares.

        Args:
            grant: RSU grant configuration
            grant_info: Processed grant information
            total_shares: Total shares to vest

        Returns:
            List of (vest_date, shares, percentage) tuples
        """
        # First, flatten all vest events with their cumulative percentage
        flat_pcts = []
        grant_date = grant_info.grant_date

        for year_idx, year_pct in enumerate(grant.schedule.percentages):
            if grant.schedule.frequency == "quarterly":
                sub_pct = year_pct / 4.0
                for quarter in range(4):
                    vest_date = grant_date + relativedelta(years=year_idx, months=quarter * 3)
                    flat_pcts.append((vest_date, sub_pct))
            elif grant.schedule.frequency == "monthly":
                sub_pct = year_pct / 12.0
                for month in range(12):
                    vest_date = grant_date + relativedelta(years=year_idx, months=month)
                    flat_pcts.append((vest_date, sub_pct))

        # Cumulative floor rounding
        vests = []
        cumulative_pct = 0.0
        cumulative_shares_issued = 0

        for vest_date, pct in flat_pcts:
            cumulative_pct += pct
            cumulative_expected = total_shares * (cumulative_pct / 100.0)
            shares_this_vest = math.floor(cumulative_expected) - cumulative_shares_issued
            cumulative_shares_issued += shares_this_vest
            vests.append((vest_date, shares_this_vest, pct))

        return vests
    
    def _generate_custom_dates_schedule(self, grant, grant_info: RSUGrantInfo,
                                      total_shares: float) -> List[Tuple[date, int, float]]:
        """Generate custom dates vesting schedule with whole shares.

        Args:
            grant: RSU grant configuration
            grant_info: Processed grant information
            total_shares: Total shares to vest

        Returns:
            List of (vest_date, shares, percentage) tuples
        """
        if not grant.schedule.custom_vest_dates:
            raise ValueError("Custom vest dates required for custom_dates schedule type")

        vests = []
        cumulative_pct = 0.0
        cumulative_shares_issued = 0

        for vest_date, percentage in zip(grant.schedule.custom_vest_dates, grant.schedule.percentages):
            cumulative_pct += percentage
            cumulative_expected = total_shares * (cumulative_pct / 100.0)
            shares_this_vest = math.floor(cumulative_expected) - cumulative_shares_issued
            cumulative_shares_issued += shares_this_vest
            vests.append((vest_date, shares_this_vest, percentage))

        return vests
    
    def process_vest(self, grant, vest_date: date, shares: float, percentage: float) -> RSUVest:
        """Process a single RSU vest event with simplified tax calculation.

        Args:
            grant: RSU grant configuration
            vest_date: Date of vesting
            shares: Number of shares vesting
            percentage: Vesting percentage for this event

        Returns:
            RSUVest object with all calculated values
        """
        logger.debug(f"Processing vest: {grant.grant_id}, {shares:.4f} shares on {vest_date}")

        # Get FMV at market open on vest date (strict 5-day lookback only)
        fmv = self.price_fetcher.get_price_on_or_before(vest_date, max_lookback_days=5, price_field="Open")

        if fmv is None:
            # No price available — create projected vest with known fields only
            vest = RSUVest(
                grant_id=grant.grant_id,
                vest_date=vest_date,
                shares_vested=shares,
                withholding_method=grant.withholding_method,
                vest_percentage=percentage,
                is_projected=True,
            )
            self.vests.append(vest)
            logger.info(f"RSU vest projected: {grant.grant_id}, {shares:.4f} shares on {vest_date} (no price data)")
            return vest

        gross_amount = fmv * shares

        # Calculate taxes using simplified flat rates
        federal_withholding = gross_amount * grant.tax.federal_withholding_rate
        state_withholding = gross_amount * grant.tax.state_withholding_rate

        # Calculate FICA taxes if applicable
        social_security = 0.0
        medicare = 0.0

        if grant.tax.apply_fica:
            social_security = self._calculate_social_security(gross_amount)
            medicare = self._calculate_medicare(gross_amount)

        # Calculate CA voluntary tax if applicable
        ca_voluntary_tax = 0.0
        if (grant.tax.apply_ca_voluntary and
            self.config.taxes.state.code.upper() == "CA"):
            ca_voluntary_tax = gross_amount * self.config.taxes.ca.voluntary_pct

        # Calculate totals
        total_tax_paid = (federal_withholding + state_withholding +
                         social_security + medicare + ca_voluntary_tax)

        # Calculate share withholding based on method
        withheld_shares, net_shares, net_cash_value = self._calculate_share_delivery(
            grant, shares, fmv, total_tax_paid
        )

        # Create vest record
        vest = RSUVest(
            grant_id=grant.grant_id,
            vest_date=vest_date,
            shares_vested=shares,
            fmv=fmv,
            gross_amount=gross_amount,
            federal_withholding=federal_withholding,
            state_withholding=state_withholding,
            social_security=social_security,
            medicare=medicare,
            ca_voluntary_tax=ca_voluntary_tax,
            total_tax_paid=total_tax_paid,
            withheld_shares=withheld_shares,
            net_shares=net_shares,
            net_cash_value=net_cash_value,
            withholding_method=grant.withholding_method,
            vest_percentage=percentage
        )

        self.vests.append(vest)

        logger.info(f"RSU vest processed: {grant.grant_id}, {shares:.4f} shares @ "
                   f"${fmv:.2f}, tax=${total_tax_paid:,.2f}, "
                   f"net_shares={net_shares:.4f}")

        return vest
    
    def _calculate_social_security(self, gross_amount: float) -> float:
        """Calculate Social Security tax for RSU.
        
        Args:
            gross_amount: Gross RSU amount
            
        Returns:
            Social Security tax amount
        """
        try:
            from python_taxes.federal import social_security
            return float(social_security.withholding(
                round_decimal(gross_amount), 
                tax_year=self.config.taxes.tax_year
            ))
        except Exception as e:
            logger.error(f"Error calculating Social Security: {e}")
            # Fallback calculation
            return gross_amount * 0.062
    
    def _calculate_medicare(self, gross_amount: float) -> float:
        """Calculate Medicare tax for RSU.
        
        Args:
            gross_amount: Gross RSU amount
            
        Returns:
            Medicare tax amount
        """
        try:
            from python_taxes.federal import medicare
            return float(medicare.required_withholding(round_decimal(gross_amount)))
        except Exception as e:
            logger.error(f"Error calculating Medicare: {e}")
            # Fallback calculation
            return gross_amount * 0.0145
    
    def _calculate_share_delivery(self, grant, shares_vested: float, fmv: float, 
                                total_tax: float) -> Tuple[float, float, float]:
        """Calculate share withholding and delivery.
        
        Args:
            grant: RSU grant configuration
            shares_vested: Number of shares vesting
            fmv: Fair market value per share
            total_tax: Total tax amount to be withheld
            
        Returns:
            Tuple of (withheld_shares, net_shares, net_cash_value)
        """
        if grant.withholding_method == "shares":
            # Withhold shares to cover taxes
            if fmv > 0:
                withheld_shares = math.ceil(total_tax / fmv)
                # Ensure we don't withhold more shares than vested
                withheld_shares = min(withheld_shares, shares_vested)
            else:
                withheld_shares = shares_vested  # Withhold all if no value
            
            net_shares = shares_vested - withheld_shares
            net_cash_value = net_shares * fmv
            
        elif grant.withholding_method == "cash":
            # No shares withheld, employee pays cash
            withheld_shares = 0.0
            net_shares = shares_vested
            net_cash_value = (shares_vested * fmv) - total_tax
            
        else:
            raise ValueError(f"Unknown withholding method: {grant.withholding_method}")
        
        return withheld_shares, net_shares, net_cash_value
    
    def process_all_grants_for_year(self, year: int) -> List[RSUVest]:
        """Process all RSU vests for a specific year.
        
        Args:
            year: Year to process vests for
            
        Returns:
            List of RSUVest objects for the year
        """
        year_vests = []
        
        # Ensure grants are processed
        if not self.processed_grants:
            self.process_grants()
        
        for grant in self.config.rsu_grants:
            try:
                vest_schedule = self.generate_vest_schedule(grant)
                
                for vest_date, shares, percentage in vest_schedule:
                    if vest_date.year == year:
                        vest = self.process_vest(grant, vest_date, shares, percentage)
                        year_vests.append(vest)
            except Exception as e:
                logger.error(f"Error processing vests for {grant.grant_id} in {year}: {e}")
        
        return year_vests
    
    def get_grant_summaries(self) -> List[Dict[str, Any]]:
        """Get summaries for all processed grants.

        Only actual (non-projected) vests are included in financial sums.

        Returns:
            List of grant summary dictionaries
        """
        summaries = []

        for grant_info in self.processed_grants:
            grant_vests = [v for v in self.vests if v.grant_id == grant_info.grant_id]
            actual_vests = [v for v in grant_vests if not v.is_projected]
            projected_vests = [v for v in grant_vests if v.is_projected]

            summary = {
                "grant_id": grant_info.grant_id,
                "symbol": grant_info.symbol,
                "target_value_usd": grant_info.target_value_usd,
                "calculated_shares": grant_info.calculated_shares,
                "grant_date": grant_info.grant_date,
                "average_price_for_calculation": grant_info.average_price_for_calculation,
                "vests_processed": len(actual_vests),
                "vests_projected": len(projected_vests),
                "total_shares_vested": sum(v.shares_vested for v in actual_vests),
                "total_gross_value": sum(v.gross_amount for v in actual_vests),
                "total_tax_paid": sum(v.total_tax_paid for v in actual_vests),
                "total_net_shares": sum(v.net_shares for v in actual_vests),
                "total_net_value": sum(v.net_cash_value for v in actual_vests)
            }

            summaries.append(summary)

        return summaries


def round_decimal(value: float):
    """Round to 2 decimal places for tax calculations."""
    from decimal import Decimal, ROUND_HALF_UP
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)