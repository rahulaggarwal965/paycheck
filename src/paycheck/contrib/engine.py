"""401(k) contribution engine with caps and employer match tiers."""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class ContributionResult:
    """Result of contribution calculation for a pay period."""
    pretax_401k: float
    roth_401k: float
    aftertax_401k: float
    espp: float
    employer_match: float
    
    # Year-to-date tracking
    ytd_pretax: float
    ytd_roth: float
    ytd_aftertax: float
    ytd_employee_deferrals: float
    ytd_employer_match: float
    
    # Cap status
    cap_402g_reached: bool
    cap_415c_reached: bool
    
    # Additional metadata
    desired_pretax: float = 0.0
    desired_roth: float = 0.0
    desired_aftertax: float = 0.0
    remaining_402g: float = 0.0
    remaining_415c: float = 0.0


class ContributionEngine:
    """Engine for calculating 401(k) contributions with IRS limits and employer matching."""
    
    def __init__(self, config):
        """Initialize the contribution engine.
        
        Args:
            config: Application configuration object
        """
        self.config = config
        
        # Year-to-date tracking
        self.ytd_pretax = 0.0
        self.ytd_roth = 0.0
        self.ytd_aftertax = 0.0
        self.ytd_employer_match = 0.0
        
        # IRS limits
        self.cap_402g = config.limits.irs_402g_employee_deferral
        self.cap_415c = config.limits.irs_415c_annual_additions
        self.include_employer_in_415c = config.limits.include_employer_in_415c
        
        # Employer match configuration
        self.match_config = config.payroll.employer_match
        
        logger.info(f"Initialized ContributionEngine with 402(g) cap: ${self.cap_402g:,}, "
                   f"415(c) cap: ${self.cap_415c:,}")
    
    def process_period(self, gross_pay: float, pretax_pct: float, roth_pct: float, 
                       aftertax_pct: float, espp_pct: float) -> ContributionResult:
        """Process contributions for a pay period.
        
        Args:
            gross_pay: Gross pay for the period
            pretax_pct: Pre-tax 401(k) percentage
            roth_pct: Roth 401(k) percentage
            aftertax_pct: After-tax 401(k) percentage
            espp_pct: ESPP percentage (handled separately, no IRS limits here)
            
        Returns:
            ContributionResult with actual contributions and YTD totals
        """
        logger.debug(f"Processing period: gross=${gross_pay:,.2f}, "
                    f"pretax={pretax_pct:.1%}, roth={roth_pct:.1%}, "
                    f"aftertax={aftertax_pct:.1%}")
        
        # Calculate desired contributions
        desired_pretax = gross_pay * pretax_pct
        desired_roth = gross_pay * roth_pct
        desired_aftertax = gross_pay * aftertax_pct
        
        # 1. Process employee deferrals (pretax + Roth) with 402(g) cap
        actual_pretax, actual_roth = self._process_employee_deferrals(
            desired_pretax, desired_roth
        )
        
        # 2. Calculate employer match based on employee deferrals
        employer_match = self._calculate_employer_match(actual_pretax + actual_roth)
        
        # 3. Process after-tax contributions with 415(c) cap
        actual_aftertax = self._process_aftertax_contributions(
            desired_aftertax, actual_pretax + actual_roth, employer_match
        )
        
        # 4. ESPP (no IRS caps applied here)
        actual_espp = gross_pay * espp_pct
        
        # 5. Update YTD tracking
        self._update_ytd(actual_pretax, actual_roth, actual_aftertax, employer_match)
        
        # 6. Calculate remaining caps
        remaining_402g = max(0, self.cap_402g - (self.ytd_pretax + self.ytd_roth))
        remaining_415c = self._calculate_remaining_415c()
        
        return ContributionResult(
            pretax_401k=actual_pretax,
            roth_401k=actual_roth,
            aftertax_401k=actual_aftertax,
            espp=actual_espp,
            employer_match=employer_match,
            ytd_pretax=self.ytd_pretax,
            ytd_roth=self.ytd_roth,
            ytd_aftertax=self.ytd_aftertax,
            ytd_employee_deferrals=self.ytd_pretax + self.ytd_roth,
            ytd_employer_match=self.ytd_employer_match,
            cap_402g_reached=(self.ytd_pretax + self.ytd_roth >= self.cap_402g),
            cap_415c_reached=(remaining_415c <= 0),
            desired_pretax=desired_pretax,
            desired_roth=desired_roth,
            desired_aftertax=desired_aftertax,
            remaining_402g=remaining_402g,
            remaining_415c=remaining_415c
        )
    
    def _process_employee_deferrals(self, desired_pretax: float, 
                                  desired_roth: float) -> tuple[float, float]:
        """Process employee deferrals with 402(g) cap enforcement.
        
        Args:
            desired_pretax: Desired pre-tax contribution
            desired_roth: Desired Roth contribution
            
        Returns:
            Tuple of (actual_pretax, actual_roth)
        """
        current_deferrals = self.ytd_pretax + self.ytd_roth
        remaining_402g = self.cap_402g - current_deferrals
        
        if remaining_402g <= 0:
            logger.debug("402(g) cap already reached")
            return 0.0, 0.0
        
        total_desired = desired_pretax + desired_roth
        
        if total_desired <= remaining_402g:
            # Can contribute full desired amounts
            return desired_pretax, desired_roth
        else:
            # Need to pro-rate to fit within cap
            if total_desired > 0:
                ratio = remaining_402g / total_desired
                actual_pretax = desired_pretax * ratio
                actual_roth = desired_roth * ratio
                logger.debug(f"Pro-rating deferrals by {ratio:.3f} to fit 402(g) cap")
            else:
                actual_pretax = actual_roth = 0.0
            
            return actual_pretax, actual_roth
    
    def _calculate_employer_match(self, employee_deferrals_this_period: float) -> float:
        """Calculate employer match for this period.
        
        Args:
            employee_deferrals_this_period: Employee deferrals for this period
            
        Returns:
            Employer match amount for this period
        """
        if self.match_config.mode == "none":
            return 0.0
        
        # Calculate total match based on YTD employee deferrals
        new_ytd_deferrals = (self.ytd_pretax + self.ytd_roth + 
                            employee_deferrals_this_period)
        
        total_match = self._compute_total_match(new_ytd_deferrals)
        period_match = total_match - self.ytd_employer_match
        
        logger.debug(f"Employer match: YTD deferrals=${new_ytd_deferrals:,.2f}, "
                    f"total match=${total_match:,.2f}, period match=${period_match:,.2f}")
        
        return max(0.0, period_match)
    
    def _compute_total_match(self, ytd_employee_deferrals: float) -> float:
        """Compute total employer match based on YTD employee deferrals and tiers.
        
        Args:
            ytd_employee_deferrals: Year-to-date employee deferrals
            
        Returns:
            Total employer match amount
        """
        if self.match_config.mode == "none" or not self.match_config.tiers:
            return 0.0
        
        total_match = 0.0
        remaining_deferrals = ytd_employee_deferrals
        
        for tier in self.match_config.tiers:
            if remaining_deferrals <= 0:
                break
            
            # Amount eligible for this tier
            tier_amount = min(remaining_deferrals, tier.up_to_usd)
            tier_match = tier_amount * tier.match_rate
            
            total_match += tier_match
            remaining_deferrals -= tier_amount
            
            logger.debug(f"Tier: ${tier_amount:,.2f} @ {tier.match_rate:.1%} = "
                        f"${tier_match:,.2f}")
        
        return total_match
    
    def _process_aftertax_contributions(self, desired_aftertax: float,
                                      employee_deferrals: float,
                                      employer_match: float) -> float:
        """Process after-tax contributions with 415(c) cap enforcement.
        
        Args:
            desired_aftertax: Desired after-tax contribution
            employee_deferrals: Employee deferrals for this period
            employer_match: Employer match for this period
            
        Returns:
            Actual after-tax contribution amount
        """
        # Calculate current 415(c) usage
        current_employee_total = self.ytd_pretax + self.ytd_roth + self.ytd_aftertax
        current_employer_total = self.ytd_employer_match
        
        if self.include_employer_in_415c:
            current_415c_usage = current_employee_total + current_employer_total
            period_additions = employee_deferrals + employer_match
        else:
            current_415c_usage = current_employee_total
            period_additions = employee_deferrals
        
        remaining_415c = self.cap_415c - current_415c_usage - period_additions
        
        if remaining_415c <= 0:
            logger.debug("415(c) cap reached, no after-tax contributions allowed")
            return 0.0
        
        actual_aftertax = min(desired_aftertax, remaining_415c)
        
        if actual_aftertax < desired_aftertax:
            logger.debug(f"After-tax contribution limited by 415(c): "
                        f"desired=${desired_aftertax:,.2f}, "
                        f"actual=${actual_aftertax:,.2f}")
        
        return actual_aftertax
    
    def _calculate_remaining_415c(self) -> float:
        """Calculate remaining 415(c) headroom.
        
        Returns:
            Remaining 415(c) capacity
        """
        current_employee_total = self.ytd_pretax + self.ytd_roth + self.ytd_aftertax
        
        if self.include_employer_in_415c:
            current_total = current_employee_total + self.ytd_employer_match
        else:
            current_total = current_employee_total
        
        return max(0, self.cap_415c - current_total)
    
    def _update_ytd(self, pretax: float, roth: float, aftertax: float, 
                   employer_match: float) -> None:
        """Update year-to-date tracking.
        
        Args:
            pretax: Pre-tax contribution for this period
            roth: Roth contribution for this period
            aftertax: After-tax contribution for this period
            employer_match: Employer match for this period
        """
        self.ytd_pretax += pretax
        self.ytd_roth += roth
        self.ytd_aftertax += aftertax
        self.ytd_employer_match += employer_match
    
    def get_match_tier_status(self) -> List[Dict[str, Any]]:
        """Get status of employer match tiers.
        
        Returns:
            List of tier status dictionaries
        """
        if self.match_config.mode == "none":
            return []
        
        tier_status = []
        ytd_deferrals = self.ytd_pretax + self.ytd_roth
        remaining_deferrals = ytd_deferrals
        
        for i, tier in enumerate(self.match_config.tiers):
            tier_used = min(remaining_deferrals, tier.up_to_usd)
            tier_remaining = max(0, tier.up_to_usd - tier_used)
            tier_exhausted = tier_used >= tier.up_to_usd
            
            tier_status.append({
                "tier_index": i,
                "match_rate": tier.match_rate,
                "up_to_usd": tier.up_to_usd,
                "used": tier_used,
                "remaining": tier_remaining,
                "exhausted": tier_exhausted
            })
            
            remaining_deferrals = max(0, remaining_deferrals - tier.up_to_usd)
        
        return tier_status
    
    def update_limits(self, cap_402g: int, cap_415c: int, include_employer: bool) -> None:
        """Update IRS contribution limits (called per year for per-year config).

        Args:
            cap_402g: 402(g) employee deferral limit
            cap_415c: 415(c) annual additions limit
            include_employer: Whether to include employer match in 415(c) limit
        """
        self.cap_402g = cap_402g
        self.cap_415c = cap_415c
        self.include_employer_in_415c = include_employer
        logger.info(f"Updated contribution limits: 402(g)=${cap_402g:,}, 415(c)=${cap_415c:,}")

    def reset_ytd(self) -> None:
        """Reset year-to-date tracking (for new tax year)."""
        self.ytd_pretax = 0.0
        self.ytd_roth = 0.0
        self.ytd_aftertax = 0.0
        self.ytd_employer_match = 0.0
    
    def get_ytd_summary(self) -> Dict[str, Any]:
        """Get year-to-date summary.
        
        Returns:
            Dictionary with YTD totals and cap status
        """
        employee_deferrals = self.ytd_pretax + self.ytd_roth
        total_employee_contributions = employee_deferrals + self.ytd_aftertax
        
        if self.include_employer_in_415c:
            total_415c_usage = total_employee_contributions + self.ytd_employer_match
        else:
            total_415c_usage = total_employee_contributions
        
        return {
            "ytd_pretax": self.ytd_pretax,
            "ytd_roth": self.ytd_roth,
            "ytd_aftertax": self.ytd_aftertax,
            "ytd_employee_deferrals": employee_deferrals,
            "ytd_employer_match": self.ytd_employer_match,
            "ytd_total_employee_contributions": total_employee_contributions,
            "ytd_415c_usage": total_415c_usage,
            "remaining_402g": max(0, self.cap_402g - employee_deferrals),
            "remaining_415c": max(0, self.cap_415c - total_415c_usage),
            "cap_402g_reached": employee_deferrals >= self.cap_402g,
            "cap_415c_reached": total_415c_usage >= self.cap_415c,
            "match_tier_status": self.get_match_tier_status()
        }
