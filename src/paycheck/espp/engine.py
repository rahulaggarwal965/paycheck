"""ESPP (Employee Stock Purchase Plan) accrual and purchase engine."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class ESPPPurchase:
    """Represents an ESPP purchase transaction."""
    cycle_id: str
    offering_start_date: date
    contributions: float

    # Optional fields (None for pending/future purchases)
    purchase_date: Optional[date] = None
    offering_price: Optional[float] = None
    purchase_day_price: Optional[float] = None
    purchase_price: Optional[float] = None
    discount_applied: Optional[float] = None
    shares_purchased: Optional[float] = None

    # Additional metadata
    lookback_months: int = 0
    fractional_shares_allowed: bool = False
    is_pending: bool = False
    carryforward: Optional[float] = None


class ESPPEngine:
    """Engine for ESPP contribution accrual and share purchases."""

    def __init__(self, config, price_fetcher):
        """Initialize the ESPP engine.

        Args:
            config: Application configuration object
            price_fetcher: Price fetcher for market data
        """
        self.config = config.payroll.espp
        self.price_fetcher = price_fetcher

        # Tracking
        self.ytd_contributions = 0.0
        self.cycle_contributions = 0.0
        self.ytd_purchase_amount = 0.0
        self.purchases = []
        self.current_cycle_start = None

        # Store annual limit as instance attribute so it can be updated per year
        self.annual_limit = self.config.annual_limit_usd

        # Track current offering/subscription state (for reset logic)
        self._offering_start_date = None
        self._offering_price = None

        # Validation
        if self.config.enabled and not self.config.offering.first_offer_date:
            raise ValueError("ESPP enabled but no first_offer_date specified")

        logger.info(f"Initialized ESPP engine: enabled={self.config.enabled}, "
                   f"discount={self.config.discount_pct:.1%}, "
                   f"annual_limit=${self.annual_limit:,.0f}")

    def accrue_contribution(self, gross_pay: float, espp_pct: float,
                          period_date: date) -> float:
        """Accrue ESPP contribution for a pay period.

        If there is still a purchase remaining in the current calendar year,
        contributions are capped so that cycle_contributions + ytd_purchase_amount
        never exceeds the annual limit.  Once the limit is reached, withholding
        stops and the money stays in the paycheck.

        After the last purchase month of the year, contributions accrue freely
        because they belong to the *next* year's purchase cycle.

        Args:
            gross_pay: Gross pay for the period
            espp_pct: ESPP contribution percentage
            period_date: Date of the pay period

        Returns:
            Actual ESPP contribution amount (may be less than desired when
            the annual limit is reached)
        """
        if not self.config.enabled:
            return 0.0

        # Check if eligible (after eligibility date if specified)
        if (self.config.offering.first_offer_date and
            period_date < self.config.offering.first_offer_date):
            logger.debug(f"Period {period_date} before ESPP eligibility")
            return 0.0

        desired_contribution = gross_pay * espp_pct

        if desired_contribution <= 0:
            return 0.0

        actual_contribution = desired_contribution

        # If there is a purchase still ahead in this calendar year, cap
        # contributions so the next purchase won't exceed the annual limit.
        # After the last purchase month, contributions flow freely into the
        # next year's cycle.
        has_remaining_purchase = any(
            m >= period_date.month for m in self.config.purchase_months
        )
        if has_remaining_purchase:
            remaining_limit = max(
                0, self.annual_limit - self.ytd_purchase_amount - self.cycle_contributions
            )
            actual_contribution = min(actual_contribution, remaining_limit)

        if actual_contribution <= 0:
            return 0.0

        # Track contributions
        self.ytd_contributions += actual_contribution
        self.cycle_contributions += actual_contribution

        logger.debug(f"ESPP contribution: desired=${desired_contribution:,.2f}, "
                    f"actual=${actual_contribution:,.2f}, "
                    f"YTD=${self.ytd_contributions:,.2f}")

        return actual_contribution

    def process_purchase(self, purchase_month: int, purchase_year: int) -> Optional[ESPPPurchase]:
        """Process ESPP purchase at end of offering period.

        Args:
            purchase_month: Month of purchase (1-12)
            purchase_year: Year of purchase

        Returns:
            ESPPPurchase object if purchase occurred, None otherwise
        """
        if not self.config.enabled or self.cycle_contributions <= 0:
            return None

        if purchase_month not in self.config.purchase_months:
            return None

        # Cap at annual purchase limit — contributions accrue freely (they may
        # be for next year's purchase), so this is where the IRS limit is enforced.
        usable_contributions = min(
            self.cycle_contributions,
            max(0, self.annual_limit - self.ytd_purchase_amount)
        )

        if usable_contributions <= 0:
            logger.debug(f"ESPP annual purchase limit reached for {purchase_year}")
            return None

        import calendar
        today = date.today()

        # Check if this is a future purchase (purchase month hasn't ended yet)
        last_day = calendar.monthrange(purchase_year, purchase_month)[1]
        purchase_month_end = date(purchase_year, purchase_month, last_day)
        if today < purchase_month_end:
            # Future purchase — create pending record
            purchase = ESPPPurchase(
                cycle_id=f"{purchase_year}-{purchase_month:02d}",
                offering_start_date=self._offering_start_date or self.config.offering.first_offer_date or date(purchase_year, 1, 1),
                contributions=usable_contributions,
                lookback_months=self.config.offering.lookback_months,
                fractional_shares_allowed=self.config.allow_fractional_shares,
                is_pending=True,
            )

            self.purchases.append(purchase)
            self.ytd_purchase_amount += usable_contributions
            self.cycle_contributions = max(0, self.cycle_contributions - usable_contributions)

            logger.info(f"ESPP purchase pending: {purchase.cycle_id}, "
                       f"contributions=${usable_contributions:,.2f}")
            return purchase

        try:
            # Get purchase date and price
            purchase_date, purchase_day_price = self._get_purchase_date_and_price(
                purchase_year, purchase_month
            )

            # Get offering start price
            offering_start_date, offering_price = self._get_offering_start_price(
                purchase_date
            )

            # Calculate purchase price with lookback and discount
            purchase_price = self._calculate_purchase_price(
                offering_price, purchase_day_price
            )

            # Calculate shares purchased using usable contributions
            shares_purchased = self._calculate_shares_purchased(purchase_price, usable_contributions)

            # Create purchase record
            purchase = ESPPPurchase(
                cycle_id=f"{purchase_year}-{purchase_month:02d}",
                offering_start_date=offering_start_date,
                contributions=usable_contributions,
                purchase_date=purchase_date,
                offering_price=offering_price,
                purchase_day_price=purchase_day_price,
                purchase_price=purchase_price,
                discount_applied=min(offering_price, purchase_day_price) - purchase_price,
                shares_purchased=shares_purchased,
                lookback_months=self.config.offering.lookback_months,
                fractional_shares_allowed=self.config.allow_fractional_shares
            )

            self.purchases.append(purchase)
            self.ytd_purchase_amount += usable_contributions

            # Whole-share remainder carries forward to the next purchase period
            actual_cost = shares_purchased * purchase_price
            self.cycle_contributions = usable_contributions - actual_cost
            purchase.carryforward = self.cycle_contributions

            logger.info(f"ESPP purchase: {shares_purchased:.4f} shares @ "
                       f"${purchase_price:.2f} on {purchase_date}")

            # Check if the offering/subscription price should reset
            self._check_offering_reset(purchase_date)

            return purchase

        except Exception as e:
            logger.error(f"Error processing ESPP purchase for {purchase_year}-{purchase_month:02d}: {e}")
            return None

    def _get_purchase_date_and_price(self, year: int, month: int) -> tuple[date, float]:
        """Get purchase date and price based on purchase day rule.

        Args:
            year: Purchase year
            month: Purchase month

        Returns:
            Tuple of (purchase_date, price)
        """
        if self.config.purchase_day_rule == "last_trading_day":
            return self.price_fetcher.get_last_trading_day_price(year, month)

        elif self.config.purchase_day_rule == "15th":
            purchase_date = date(year, month, 15)
            price = self.price_fetcher.get_price_on_or_before(purchase_date)
            if price is None:
                raise ValueError(f"No price available for {purchase_date}")
            return purchase_date, price

        elif self.config.purchase_day_rule == "last_day":
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            purchase_date = date(year, month, last_day)
            price = self.price_fetcher.get_price_on_or_before(purchase_date)
            if price is None:
                raise ValueError(f"No price available for {purchase_date}")
            return purchase_date, price

        else:
            raise ValueError(f"Unknown purchase day rule: {self.config.purchase_day_rule}")

    def _get_offering_start_price(self, purchase_date: date) -> tuple[date, float]:
        """Get offering start date and price, accounting for resets.

        On the first resolved purchase the subscription price is initialized
        from the configured first_offer_date (or lookback).  After each
        purchase, _check_offering_reset may lower the subscription price if the
        stock opened the next period at a lower level.

        Args:
            purchase_date: Date of purchase

        Returns:
            Tuple of (offering_start_date, offering_price)
        """
        if self._offering_price is not None:
            return self._offering_start_date, self._offering_price

        # First resolved purchase — initialize from config
        if self.config.offering.first_offer_date:
            offering_start_date = self.config.offering.first_offer_date
        else:
            from dateutil.relativedelta import relativedelta
            offering_start_date = purchase_date - relativedelta(
                months=self.config.offering.lookback_months
            )

        offering_price = self.price_fetcher.get_price_on_or_before(offering_start_date)
        if offering_price is None:
            raise ValueError(f"No price available for offering start date {offering_start_date}")

        self._offering_start_date = offering_start_date
        self._offering_price = offering_price
        return offering_start_date, offering_price

    def _check_offering_reset(self, purchase_date: date) -> None:
        """Check if the subscription price should reset after a purchase.

        If the closing price on the first trading day after a purchase is lower
        than the current subscription price, the offering resets to the new
        lower price.
        """
        today = date.today()

        for i in range(1, 10):
            check_date = purchase_date + timedelta(days=i)
            if check_date > today:
                return  # Can't check future dates
            price = self.price_fetcher.get_price(check_date)
            if price is not None:
                if price < self._offering_price:
                    logger.info(
                        f"ESPP offering reset: {check_date} price ${price:.2f} < "
                        f"subscription price ${self._offering_price:.2f}"
                    )
                    self._offering_start_date = check_date
                    self._offering_price = price
                return

    def _calculate_purchase_price(self, offering_price: float,
                                purchase_day_price: float) -> float:
        """Calculate purchase price with lookback and discount.

        Args:
            offering_price: Price at offering start
            purchase_day_price: Price on purchase day

        Returns:
            Purchase price after discount
        """
        # Lookback: use lower of offering price or purchase day price
        base_price = min(offering_price, purchase_day_price)

        # Apply discount
        purchase_price = base_price * (1 - self.config.discount_pct)

        logger.debug(f"Purchase price calculation: offering=${offering_price:.2f}, "
                    f"purchase_day=${purchase_day_price:.2f}, "
                    f"base=${base_price:.2f}, "
                    f"discount={self.config.discount_pct:.1%}, "
                    f"final=${purchase_price:.2f}")

        return purchase_price

    def _calculate_shares_purchased(self, purchase_price: float, contributions: float) -> float:
        """Calculate number of shares purchased.

        Args:
            purchase_price: Price per share
            contributions: Contribution amount to use for purchase

        Returns:
            Number of shares purchased
        """
        if purchase_price <= 0:
            return 0.0

        shares = contributions / purchase_price

        if not self.config.allow_fractional_shares:
            shares = int(shares)  # Truncate to whole shares

        return shares

    def get_cycle_status(self) -> Dict[str, Any]:
        """Get current cycle status.

        Returns:
            Dictionary with cycle information
        """
        return {
            "cycle_contributions": self.cycle_contributions,
            "ytd_contributions": self.ytd_contributions,
            "ytd_purchase_amount": self.ytd_purchase_amount,
            "remaining_annual_limit": max(0, self.annual_limit - self.ytd_purchase_amount),
            "annual_limit_reached": self.ytd_purchase_amount >= self.annual_limit,
            "next_purchase_months": [
                month for month in self.config.purchase_months
            ]
        }

    def get_purchases_for_year(self, year: int) -> List[ESPPPurchase]:
        """Get all purchases for a specific year.

        Uses cycle_id for year filtering (not purchase_date which may be None).

        Args:
            year: Year to get purchases for

        Returns:
            List of purchases in the specified year
        """
        return [p for p in self.purchases if p.cycle_id.startswith(f"{year}-")]

    def reset_ytd(self) -> None:
        """Reset year-to-date tracking (for new tax year).
        Note: cycle_contributions are NOT reset because ESPP purchase cycles
        span across calendar years (e.g., Aug 2025 contributions used for Feb 2026 purchase).
        """
        self.ytd_contributions = 0.0
        self.ytd_purchase_amount = 0.0

    def get_ytd_summary(self) -> Dict[str, Any]:
        """Get year-to-date summary.

        Returns:
            Dictionary with YTD totals and statistics
        """
        actual_purchases = [p for p in self.purchases if not p.is_pending]
        total_shares = sum(p.shares_purchased for p in actual_purchases if p.shares_purchased is not None)
        total_discount = sum(
            p.discount_applied * p.shares_purchased
            for p in actual_purchases
            if p.discount_applied is not None and p.shares_purchased is not None
        )

        return {
            "ytd_contributions": self.ytd_contributions,
            "cycle_contributions": self.cycle_contributions,
            "ytd_purchase_amount": self.ytd_purchase_amount,
            "total_purchases": len(actual_purchases),
            "total_shares_purchased": total_shares,
            "total_discount_value": total_discount,
            "remaining_annual_limit": max(0, self.annual_limit - self.ytd_purchase_amount),
            "annual_limit_reached": self.ytd_purchase_amount >= self.annual_limit
        }
