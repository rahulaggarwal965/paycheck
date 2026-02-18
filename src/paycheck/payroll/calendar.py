"""Pay period calendar generator for different pay frequencies."""

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from typing import List
import polars as pl
import calendar


def _adjust_for_weekend(d: date) -> date:
    """If date falls on a weekend, roll back to the preceding Friday."""
    weekday = d.weekday()
    if weekday == 5:  # Saturday
        return d - timedelta(days=1)
    elif weekday == 6:  # Sunday
        return d - timedelta(days=2)
    return d


class PayPeriodCalendar:
    """Generate pay periods for different pay frequencies."""

    def __init__(self, start_date: date, years: List[int], pay_frequency: str):
        """Initialize the pay period calendar.
        
        Args:
            start_date: Employment start date
            years: List of years to generate periods for
            pay_frequency: Pay frequency ('semimonthly', 'biweekly', 'monthly')
        """
        self.start_date = start_date
        self.years = sorted(years)
        self.pay_frequency = pay_frequency
    
    def generate_periods(self) -> pl.DataFrame:
        """Generate pay periods for all years.
        
        Returns:
            DataFrame with columns: period_start, period_end, pay_date, year, period_index
        """
        if self.pay_frequency == "semimonthly":
            return self._generate_semimonthly()
        elif self.pay_frequency == "biweekly":
            return self._generate_biweekly()
        elif self.pay_frequency == "monthly":
            return self._generate_monthly()
        else:
            raise ValueError(f"Unsupported pay frequency: {self.pay_frequency}")
    
    def _generate_semimonthly(self) -> pl.DataFrame:
        """Generate semimonthly pay periods (15th and last day of month)."""
        periods = []
        period_counter = 0
        first_paycheck_processed = False
        
        for year in self.years:
            for month in range(1, 13):
                # First period: 1st to 15th, paid on 15th (adjusted for weekends)
                period_start = date(year, month, 1)
                period_end = date(year, month, 15)
                pay_date = _adjust_for_weekend(date(year, month, 15))
                
                # Skip periods where start_date falls in the middle (but not on boundaries)
                if period_start < self.start_date <= period_end:
                    # Start date is in the middle of this period, skip it and add fractional to next period
                    continue
                
                if pay_date >= self.start_date:
                    # Check if this is the first paycheck and if start_date is in a previous period
                    fractional_multiplier = 1.0
                    if not first_paycheck_processed:
                        fractional_multiplier = self._calculate_fractional_multiplier_semimonthly(
                            self.start_date, pay_date, period_start, period_end
                        )
                        first_paycheck_processed = True
                    
                    period_counter += 1
                    periods.append({
                        "period_start": period_start,
                        "period_end": period_end,
                        "pay_date": pay_date,
                        "year": year,
                        "period_index": period_counter,
                        "period_type": "first_half",
                        "fractional_multiplier": fractional_multiplier
                    })
                
                # Second period: 16th to end of month, paid on last day (adjusted for weekends)
                period_start = date(year, month, 16)
                last_day = calendar.monthrange(year, month)[1]
                period_end = date(year, month, last_day)
                pay_date = _adjust_for_weekend(period_end)
                
                # Skip periods where start_date falls in the middle (but not on boundaries)
                if period_start < self.start_date <= period_end:
                    # Start date is in the middle of this period, skip it and add fractional to next period
                    continue
                
                if pay_date >= self.start_date:
                    # Check if this is the first paycheck and if start_date is in a previous period
                    fractional_multiplier = 1.0
                    if not first_paycheck_processed:
                        fractional_multiplier = self._calculate_fractional_multiplier_semimonthly(
                            self.start_date, pay_date, period_start, period_end
                        )
                        first_paycheck_processed = True
                    
                    period_counter += 1
                    periods.append({
                        "period_start": period_start,
                        "period_end": period_end,
                        "pay_date": pay_date,
                        "year": year,
                        "period_index": period_counter,
                        "period_type": "second_half",
                        "fractional_multiplier": fractional_multiplier
                    })
        
        return pl.DataFrame(periods)
    
    def _calculate_fractional_multiplier_semimonthly(self, start_date: date, pay_date: date, 
                                                   period_start: date, period_end: date) -> float:
        """Calculate fractional multiplier for first paycheck in semimonthly schedule.
        
        Args:
            start_date: Employment start date
            pay_date: Date of the paycheck
            period_start: Start of the current pay period
            period_end: End of the current pay period
            
        Returns:
            Multiplier for the first paycheck (e.g., 1.333 for partial period)
        """
        # If start_date is within the current period, no fractional adjustment needed
        if period_start <= start_date <= period_end:
            return 1.0
        
        # Calculate days in current period
        current_period_days = (period_end - period_start).days + 1
        
        # Find the previous period that contains the start_date
        previous_period_start = None
        previous_period_end = None
        
        # For semimonthly, we need to find which period the start_date falls in
        if period_start.day == 1:  # Current period is 1st-15th
            # Previous period is 16th-end of previous month
            if period_start.month == 1:
                prev_month = 12
                prev_year = period_start.year - 1
            else:
                prev_month = period_start.month - 1
                prev_year = period_start.year
            
            last_day = calendar.monthrange(prev_year, prev_month)[1]
            previous_period_start = date(prev_year, prev_month, 16)
            previous_period_end = date(prev_year, prev_month, last_day)
        else:  # Current period is 16th-end
            # Previous period is 1st-15th of same month
            previous_period_start = date(period_start.year, period_start.month, 1)
            previous_period_end = date(period_start.year, period_start.month, 15)
        
        # If start_date is not in the previous period either, just return 1.0
        if not (previous_period_start <= start_date <= previous_period_end):
            return 1.0
        
        # Calculate partial business days from start_date to end of previous period
        partial_business_days = self._count_business_days(start_date, previous_period_end)
        previous_period_business_days = self._count_business_days(previous_period_start, previous_period_end)
        
        # Calculate fractional amount: (partial business days / previous period business days) + 1.0
        if previous_period_business_days > 0:
            fractional_amount = (partial_business_days / previous_period_business_days) + 1.0
        else:
            fractional_amount = 1.0
        
        return fractional_amount
    
    def _count_business_days(self, start_date: date, end_date: date) -> int:
        """Count business days (Monday-Friday) between two dates, inclusive.
        
        Args:
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            
        Returns:
            Number of business days
        """
        if start_date > end_date:
            return 0
        
        business_days = 0
        current_date = start_date
        
        while current_date <= end_date:
            # Monday = 0, Sunday = 6
            if current_date.weekday() < 5:  # Monday-Friday
                business_days += 1
            current_date += timedelta(days=1)
        
        return business_days
    
    def _generate_biweekly(self) -> pl.DataFrame:
        """Generate biweekly pay periods (every 2 weeks)."""
        periods = []
        period_counter = 0
        
        # Start from the first pay date on or after start_date
        current_start = self.start_date
        
        # Find the first Friday on or after start_date for pay date
        days_until_friday = (4 - current_start.weekday()) % 7
        if days_until_friday == 0 and current_start.weekday() != 4:
            days_until_friday = 7
        first_pay_date = current_start + timedelta(days=days_until_friday)
        
        current_pay_date = first_pay_date
        max_date = date(max(self.years), 12, 31)
        
        while current_pay_date <= max_date:
            # Period is 2 weeks ending on pay date
            period_end = current_pay_date
            period_start = period_end - timedelta(days=13)  # 14 days total
            
            if period_end.year in self.years:
                period_counter += 1
                periods.append({
                    "period_start": period_start,
                    "period_end": period_end,
                    "pay_date": current_pay_date,
                    "year": period_end.year,
                    "period_index": period_counter,
                    "period_type": "biweekly"
                })
            
            # Next pay date is 2 weeks later
            current_pay_date += timedelta(days=14)
        
        return pl.DataFrame(periods)
    
    def _generate_monthly(self) -> pl.DataFrame:
        """Generate monthly pay periods (last day of month)."""
        periods = []
        period_counter = 0
        
        for year in self.years:
            for month in range(1, 13):
                period_start = date(year, month, 1)
                last_day = calendar.monthrange(year, month)[1]
                period_end = date(year, month, last_day)
                pay_date = _adjust_for_weekend(period_end)

                if pay_date >= self.start_date:
                    period_counter += 1
                    periods.append({
                        "period_start": period_start,
                        "period_end": period_end,
                        "pay_date": pay_date,
                        "year": year,
                        "period_index": period_counter,
                        "period_type": "monthly"
                    })
        
        return pl.DataFrame(periods)
    
    def get_periods_for_year(self, year: int) -> pl.DataFrame:
        """Get pay periods for a specific year.
        
        Args:
            year: Year to get periods for
            
        Returns:
            DataFrame with periods for the specified year
        """
        all_periods = self.generate_periods()
        return all_periods.filter(pl.col("year") == year)
    
    def get_period_count_for_year(self, year: int) -> int:
        """Get the number of pay periods in a specific year.
        
        Args:
            year: Year to count periods for
            
        Returns:
            Number of pay periods in the year
        """
        year_periods = self.get_periods_for_year(year)
        return year_periods.height
    
    def get_annual_periods(self) -> int:
        """Get the typical number of pay periods per year for this frequency.
        
        Returns:
            Typical annual pay periods
        """
        if self.pay_frequency == "semimonthly":
            return 24
        elif self.pay_frequency == "biweekly":
            return 26  # Sometimes 27 in leap years
        elif self.pay_frequency == "monthly":
            return 12
        else:
            raise ValueError(f"Unsupported pay frequency: {self.pay_frequency}")
    
    def is_partial_first_period(self, period_start: date) -> bool:
        """Check if a period is a partial first period due to start date.
        
        Args:
            period_start: Start date of the period
            
        Returns:
            True if this is a partial first period
        """
        return period_start < self.start_date <= period_start + timedelta(days=14)
    
    def get_period_days(self, period_start: date, period_end: date) -> int:
        """Get the number of days in a pay period.
        
        Args:
            period_start: Start date of the period
            period_end: End date of the period
            
        Returns:
            Number of days in the period
        """
        return (period_end - period_start).days + 1
