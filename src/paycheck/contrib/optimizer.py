"""Optimal 401k and ESPP contribution schedule optimizer.

Computes balanced contribution rates that maximize per-period take-home pay
while hitting annual contribution targets, subject to 1% increment constraint.
"""

import math
import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def compute_optimal_401k_schedule(
    periods_data: List[Dict[str, Any]],
    first_year_adjustments: Optional[List],
    year_config: Any,
    config: Any,
    today: date,
) -> Optional[Dict[str, Any]]:
    """Compute optimal balanced 401k contribution schedules.

    Spreads contributions evenly across modifiable periods (1% increments)
    to maximize per-period take-home pay consistency.

    Args:
        periods_data: List of period result dicts from the pipeline.
        first_year_adjustments: First-year adjustment results (sign-on, etc.).
        year_config: Resolved YearConfig for this year.
        config: Full AppConfig.
        today: Current date (determines which periods are modifiable).

    Returns:
        Dict with optimal schedule and metadata, or None if nothing to optimize.
    """
    if not periods_data:
        return None

    # ── Step 1: Classify periods ──────────────────────────────────────
    past_periods = [p for p in periods_data if p["pay_date"] <= today]
    future_periods = [p for p in periods_data if p["pay_date"] > today]

    if len(future_periods) < 2:
        # Need at least 1 locked + 1 modifiable
        logger.info("Not enough future periods to optimize")
        return None

    locked_period = future_periods[0]
    modifiable_periods = future_periods[1:]
    fixed_periods = past_periods + [locked_period]

    logger.info(
        f"Optimizer: {len(past_periods)} past, 1 locked "
        f"({locked_period['pay_date']}), {len(modifiable_periods)} modifiable"
    )

    # ── Step 2: Compute fixed YTD ─────────────────────────────────────
    fixed_ytd = _compute_fixed_ytd(fixed_periods, first_year_adjustments)

    # ── Step 3: Compute annual targets from config intent ─────────────
    fya_pretax = _get_first_year_pretax(first_year_adjustments)
    targets = _compute_annual_targets(
        periods_data, fya_pretax, year_config, config
    )

    # ── Step 4: Compute remaining to distribute ───────────────────────
    remaining = _compute_remaining(targets, fixed_ytd, year_config, config)

    # ── Step 5: Assign rates with 1% constraint ──────────────────────
    mod_gross = [p["gross_pay"] for p in modifiable_periods]

    pretax_rates, pretax_assigned = _assign_rates(remaining["pretax"], mod_gross)
    roth_rates, roth_assigned = _assign_rates(remaining["roth"], mod_gross)
    aftertax_rates, aftertax_assigned = _assign_rates(remaining["aftertax"], mod_gross)

    # ── Step 6: Build full per-period lists ───────────────────────────
    full_pretax = [p["pretax_401k_pct"] for p in fixed_periods] + pretax_rates
    full_roth = [p["roth_401k_pct"] for p in fixed_periods] + roth_rates
    full_aftertax = [p["aftertax_401k_pct"] for p in fixed_periods] + aftertax_rates

    # Compute overshoot (assigned >= remaining due to front-loading)
    # The contribution engine's cap enforcement handles the final period adjustment.
    pretax_overshoot = pretax_assigned - remaining["pretax"]
    roth_overshoot = roth_assigned - remaining["roth"]
    aftertax_overshoot = aftertax_assigned - remaining["aftertax"]

    metadata = {
        "today": today.isoformat(),
        "num_past": len(past_periods),
        "locked_pay_date": locked_period["pay_date"].isoformat(),
        "num_modifiable": len(modifiable_periods),
        "first_modifiable_pay_date": modifiable_periods[0]["pay_date"].isoformat(),
        "last_modifiable_pay_date": modifiable_periods[-1]["pay_date"].isoformat(),
        "targets": targets,
        "fixed_ytd": fixed_ytd,
        "remaining": remaining,
        "assigned": {
            "pretax": pretax_assigned,
            "roth": roth_assigned,
            "aftertax": aftertax_assigned,
        },
        "overshoot": {
            "pretax": pretax_overshoot,
            "roth": roth_overshoot,
            "aftertax": aftertax_overshoot,
        },
    }

    logger.info(
        f"Optimal schedule: pretax overshoot=${pretax_overshoot:,.2f}, "
        f"roth overshoot=${roth_overshoot:,.2f}, aftertax overshoot=${aftertax_overshoot:,.2f}"
    )

    return {
        "pretax_401k_pct": full_pretax,
        "roth_401k_pct": full_roth,
        "aftertax_401k_pct": full_aftertax,
        "metadata": metadata,
    }


def _compute_fixed_ytd(
    fixed_periods: List[Dict[str, Any]],
    first_year_adjustments: Optional[List],
) -> Dict[str, float]:
    """Sum actual contributions from fixed (past + locked) periods."""
    ytd = {
        "pretax": sum(p["pretax_401k"] for p in fixed_periods),
        "roth": sum(p["roth_401k"] for p in fixed_periods),
        "aftertax": sum(p["aftertax_401k"] for p in fixed_periods),
        "employer_match": sum(p["employer_match"] for p in fixed_periods),
    }

    # Include first-year adjustment contributions (sign-on 401k)
    if first_year_adjustments:
        for item in first_year_adjustments:
            if len(item) >= 2 and item[1] is not None:
                contrib_result = item[1]
                ytd["pretax"] += contrib_result.pretax_401k
                ytd["roth"] += contrib_result.roth_401k
                ytd["aftertax"] += contrib_result.aftertax_401k
                ytd["employer_match"] += contrib_result.employer_match

    return ytd


def _get_first_year_pretax(first_year_adjustments: Optional[List]) -> float:
    """Get total pretax contribution from first-year adjustments."""
    total = 0.0
    if first_year_adjustments:
        for item in first_year_adjustments:
            if len(item) >= 2 and item[1] is not None:
                total += item[1].pretax_401k
    return total


def _compute_annual_match(deferrals: float, config: Any) -> float:
    """Compute total annual employer match for a given deferral amount.

    Mirrors ContributionEngine._compute_total_match logic.
    """
    match_config = config.payroll.employer_match
    if match_config.mode == "none" or not match_config.tiers:
        return 0.0

    total_match = 0.0
    remaining = deferrals
    for tier in match_config.tiers:
        if remaining <= 0:
            break
        tier_amount = min(remaining, tier.up_to_usd)
        total_match += tier_amount * tier.match_rate
        remaining -= tier_amount
    return total_match


def _compute_annual_targets(
    periods_data: List[Dict[str, Any]],
    fya_pretax: float,
    year_config: Any,
    config: Any,
) -> Dict[str, float]:
    """Compute annual contribution targets from config intent.

    Uses desired (rate × gross) amounts, then caps to IRS limits.
    """
    cap_402g = year_config.limits.irs_402g_employee_deferral
    cap_415c = year_config.limits.irs_415c_annual_additions
    include_employer = year_config.limits.include_employer_in_415c

    # Desired annual totals (what the config rates would produce without caps)
    desired_pretax = fya_pretax + sum(
        p["gross_pay"] * p["pretax_401k_pct"] for p in periods_data
    )
    desired_roth = sum(p["gross_pay"] * p["roth_401k_pct"] for p in periods_data)
    desired_aftertax = sum(
        p["gross_pay"] * p["aftertax_401k_pct"] for p in periods_data
    )

    # Cap pretax + roth to 402(g)
    desired_deferral = desired_pretax + desired_roth
    if desired_deferral > cap_402g:
        ratio = cap_402g / desired_deferral
        target_pretax = desired_pretax * ratio
        target_roth = desired_roth * ratio
    else:
        target_pretax = desired_pretax
        target_roth = desired_roth

    # Project annual employer match based on target deferrals
    projected_match = _compute_annual_match(target_pretax + target_roth, config)

    # Cap aftertax to fill 415(c)
    if include_employer:
        available_415c = cap_415c - target_pretax - target_roth - projected_match
    else:
        available_415c = cap_415c - target_pretax - target_roth
    target_aftertax = min(desired_aftertax, max(0, available_415c))

    return {
        "pretax": target_pretax,
        "roth": target_roth,
        "aftertax": target_aftertax,
        "employer_match": projected_match,
        "cap_402g": cap_402g,
        "cap_415c": cap_415c,
        "desired_pretax": desired_pretax,
        "desired_roth": desired_roth,
        "desired_aftertax": desired_aftertax,
    }


def _compute_remaining(
    targets: Dict[str, float],
    fixed_ytd: Dict[str, float],
    year_config: Any,
    config: Any,
) -> Dict[str, float]:
    """Compute remaining contributions to distribute over modifiable periods."""
    cap_402g = year_config.limits.irs_402g_employee_deferral
    cap_415c = year_config.limits.irs_415c_annual_additions
    include_employer = year_config.limits.include_employer_in_415c

    remaining_pretax = max(0.0, targets["pretax"] - fixed_ytd["pretax"])
    remaining_roth = max(0.0, targets["roth"] - fixed_ytd["roth"])

    # Re-check against remaining 402(g) space
    remaining_402g = max(0.0, cap_402g - fixed_ytd["pretax"] - fixed_ytd["roth"])
    if remaining_pretax + remaining_roth > remaining_402g:
        total = remaining_pretax + remaining_roth
        if total > 0:
            ratio = remaining_402g / total
            remaining_pretax *= ratio
            remaining_roth *= ratio

    # For aftertax: account for projected remaining employer match
    # The match for fixed periods is already in fixed_ytd["employer_match"].
    # We need to project the match that will be earned in modifiable periods.
    total_deferrals = targets["pretax"] + targets["roth"]
    total_match = _compute_annual_match(total_deferrals, config)
    remaining_match = total_match - fixed_ytd["employer_match"]

    if include_employer:
        remaining_415c = max(
            0.0,
            cap_415c
            - fixed_ytd["pretax"]
            - fixed_ytd["roth"]
            - fixed_ytd["aftertax"]
            - fixed_ytd["employer_match"]
            - remaining_pretax
            - remaining_roth
            - remaining_match,
        )
    else:
        remaining_415c = max(
            0.0,
            cap_415c
            - fixed_ytd["pretax"]
            - fixed_ytd["roth"]
            - fixed_ytd["aftertax"]
            - remaining_pretax
            - remaining_roth,
        )

    remaining_aftertax = min(
        max(0.0, targets["aftertax"] - fixed_ytd["aftertax"]),
        remaining_415c,
    )

    return {
        "pretax": remaining_pretax,
        "roth": remaining_roth,
        "aftertax": remaining_aftertax,
    }


def _assign_rates(
    remaining: float,
    gross_list: List[float],
    min_pct: float = 0.0,
    max_pct: float = 1.0,
    prefer_overshoot: bool = True,
) -> Tuple[List[float], float]:
    """Assign per-period rates in 1% increments to distribute remaining dollars.

    Uses base_pct for most periods and base_pct + 1% for some periods at the
    START. Front-loads higher rates.

    Args:
        remaining: Dollar amount to distribute.
        gross_list: Gross pay for each modifiable period.
        min_pct: Minimum allowed percentage (e.g. 0.01 for ESPP).
        max_pct: Maximum allowed percentage (e.g. 0.25 for ESPP).
        prefer_overshoot: If True (401k), ensure total >= remaining so cap
            enforcement handles the final adjustment. If False (ESPP),
            minimize |total - remaining| since there's no per-period cap
            enforcement and overflow is wasted.

    Returns:
        Tuple of (per-period rate list, total dollars assigned).
    """
    n = len(gross_list)
    if n == 0 or remaining <= 0:
        floor_rates = [min_pct] * n
        floor_total = sum(min_pct * g for g in gross_list)
        return floor_rates, floor_total

    total_gross = sum(gross_list)
    ideal_pct = remaining / total_gross
    base_pct = math.floor(ideal_pct * 100) / 100.0  # Round down to nearest 1%
    base_pct = max(min_pct, min(base_pct, max_pct))
    high_pct = base_pct + 0.01

    if high_pct > max_pct:
        # Can't go above max — use max for all periods
        return [max_pct] * n, sum(max_pct * g for g in gross_list)

    # Compute total at base rate
    total_at_base = sum(base_pct * g for g in gross_list)

    # How much shortfall do we need to fill with high_pct periods?
    shortfall = remaining - total_at_base

    # Greedily assign high_pct to periods at the START (front-load).
    # Count how many high periods we need.
    rates = [base_pct] * n
    assigned_extra = 0.0
    high_count = 0

    for i in range(n):
        if assigned_extra >= shortfall:
            break
        rates[i] = high_pct
        assigned_extra += 0.01 * gross_list[i]
        high_count = i + 1

    total_with_overshoot = total_at_base + assigned_extra

    if prefer_overshoot:
        # 401k mode: always overshoot, cap enforcement handles final period
        return rates, total_with_overshoot

    # ESPP mode: pick whichever of undershoot/overshoot is closer to target.
    # Undershoot = one fewer high period than overshoot.
    if high_count > 0:
        undershoot_extra = assigned_extra - 0.01 * gross_list[high_count - 1]
        total_with_undershoot = total_at_base + undershoot_extra
        overshoot_gap = total_with_overshoot - remaining
        undershoot_gap = remaining - total_with_undershoot

        if undershoot_gap <= overshoot_gap:
            # Undershoot is closer — drop the last high period back to base
            rates_under = [base_pct] * n
            for i in range(high_count - 1):
                rates_under[i] = high_pct
            return rates_under, total_with_undershoot

    return rates, total_with_overshoot


# ── ESPP Optimizer ────────────────────────────────────────────────────────


def _get_cycle_for_period(
    pay_date: date, purchase_months: List[int]
) -> Tuple[int, int]:
    """Map a pay period to its ESPP purchase cycle.

    Returns (purchase_year, purchase_month) for the next purchase that this
    period's contributions feed into.
    """
    month = pay_date.month
    for pm in purchase_months:
        if month <= pm:
            return (pay_date.year, pm)
    # Wraps to first purchase month of next year
    return (pay_date.year + 1, purchase_months[0])


def compute_optimal_espp_schedule(
    periods_data: List[Dict[str, Any]],
    year_config: Any,
    config: Any,
    today: date,
    espp_purchases: Optional[List] = None,
) -> Optional[Dict[str, Any]]:
    """Compute optimal ESPP contribution schedule per purchase cycle.

    Within each cycle, rates can only decrease once (front-loads high rate,
    then steps down to base rate). Rates are in 1% increments, minimum 1%.

    Key ESPP mechanics:
    - Annual limit ($21,250) applies only to in-year purchase cycles (cycles
      whose purchase date falls in the current year).
    - Cross-year cycles (e.g., Sep–Dec 2026 for a Feb 2027 purchase) accrue
      freely — no annual limit cap during accrual. The next year's limit
      applies at purchase time.
    - Overshoot within an in-year cycle causes the ESPP engine to cap later
      periods' contributions (inconsistent take-home), so we minimize the gap.
    - Whole-share remainder (carryforward) carries across cycles/years, but
      the annual limit does NOT carry forward.

    Args:
        periods_data: List of period result dicts from the pipeline.
        year_config: Resolved YearConfig for this year.
        config: Full AppConfig.
        today: Current date (determines which periods are modifiable).

    Returns:
        Dict with optimal ESPP schedule and metadata, or None if nothing to optimize.
    """
    espp_config = config.payroll.espp
    if not periods_data or not espp_config.enabled:
        return None

    purchase_months = sorted(espp_config.purchase_months)
    max_pct = espp_config.max_contribution_pct
    annual_limit = year_config.espp_annual_limit_usd
    # Determine which year we're processing
    process_year = periods_data[0]["pay_date"].year

    # ── Step 1: Classify periods ──────────────────────────────────────
    past_periods = [p for p in periods_data if p["pay_date"] <= today]
    future_periods = [p for p in periods_data if p["pay_date"] > today]

    if len(future_periods) < 2:
        logger.info("ESPP optimizer: not enough future periods")
        return None

    locked_period = future_periods[0]
    modifiable_periods = future_periods[1:]
    fixed_periods = past_periods + [locked_period]

    # ── Step 2: Group ALL periods by cycle ────────────────────────────
    cycle_periods: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for p in periods_data:
        cycle_key = _get_cycle_for_period(p["pay_date"], purchase_months)
        cycle_periods.setdefault(cycle_key, [])
        cycle_periods[cycle_key].append(p)

    # ── Step 3: Separate in-year vs cross-year cycles ─────────────────
    # In-year cycles: purchase happens in process_year → subject to annual limit
    # Cross-year cycles: purchase in a future year → accrue freely
    in_year_keys = sorted(k for k in cycle_periods if k[0] == process_year)
    cross_year_keys = sorted(k for k in cycle_periods if k[0] != process_year)

    # ── Step 4: Compute targets per cycle ──────────────────────────────
    # Build purchase lookup: cycle_key → ESPPPurchase
    purchase_by_cycle: Dict[Tuple[int, int], Any] = {}
    if espp_purchases:
        for p in espp_purchases:
            # cycle_id is "YYYY-MM"
            parts = p.cycle_id.split("-")
            key = (int(parts[0]), int(parts[1]))
            purchase_by_cycle[key] = p

    # For in-year cycles, compute effective annual limit after prior purchases
    # and whole-share carryforward. The ESPP engine enforces:
    #   remaining_limit = annual_limit - ytd_purchase_amount - cycle_contributions
    # where cycle_contributions includes carryforward from previous purchase.
    # So max new contributions for a cycle = annual_limit - ytd_purchase_before - carryforward
    in_year_cycle_desired: Dict[Tuple[int, int], float] = {}
    in_year_cycle_effective_limit: Dict[Tuple[int, int], float] = {}
    ytd_purchase_amount = 0.0
    desired_in_year = 0.0

    for k in in_year_keys:
        desired = sum(p["gross_pay"] * p["espp_pct"] for p in cycle_periods[k])
        in_year_cycle_desired[k] = desired
        desired_in_year += desired

        # Get carryforward into this cycle from the previous purchase
        prev_carryforward = 0.0
        # Find the purchase just before this cycle (could be from prior in-year
        # cycle, or from the last cycle of previous year)
        all_sorted_keys = sorted(purchase_by_cycle.keys())
        for pk in all_sorted_keys:
            if pk < k and purchase_by_cycle[pk].carryforward is not None:
                prev_carryforward = purchase_by_cycle[pk].carryforward

        effective_limit = max(0.0, annual_limit - ytd_purchase_amount - prev_carryforward)
        in_year_cycle_effective_limit[k] = effective_limit

        logger.info(
            f"ESPP cycle {k[0]}-{k[1]:02d}: desired=${desired:,.2f}, "
            f"ytd_purchase=${ytd_purchase_amount:,.2f}, "
            f"carryforward=${prev_carryforward:,.2f}, "
            f"effective_limit=${effective_limit:,.2f}"
        )

        # After this cycle's purchase, update ytd_purchase_amount
        if k in purchase_by_cycle:
            ytd_purchase_amount += purchase_by_cycle[k].contributions

    target_in_year = min(desired_in_year, annual_limit)

    # Cross-year: desired from config rates, no annual limit cap from this year
    cross_year_cycle_desired: Dict[Tuple[int, int], float] = {}
    desired_cross_year = 0.0
    for k in cross_year_keys:
        d = sum(p["gross_pay"] * p["espp_pct"] for p in cycle_periods[k])
        cross_year_cycle_desired[k] = d
        desired_cross_year += d

    # ── Step 5: For each cycle, compute fixed and assign modifiable ───
    period_rates: Dict[int, float] = {}  # period index → rate
    cycle_metadata: List[Dict[str, Any]] = []

    for cycle_key in sorted(cycle_periods.keys()):
        periods = cycle_periods[cycle_key]
        is_in_year = cycle_key[0] == process_year

        if is_in_year:
            # Cap to effective annual limit for this cycle (accounts for
            # prior purchases' ytd_purchase_amount and carryforward)
            target = min(
                in_year_cycle_desired[cycle_key],
                in_year_cycle_effective_limit[cycle_key],
            )
        else:
            # Cross-year: use full desired (no annual limit from this year)
            target = cross_year_cycle_desired[cycle_key]

        # Split into fixed vs modifiable within this cycle
        cycle_fixed = [p for p in periods if p in fixed_periods]
        cycle_modifiable = [p for p in periods if p in modifiable_periods]

        fixed_contrib = sum(p["espp_contrib"] for p in cycle_fixed)

        # For fixed periods, keep their original pct
        for p in cycle_fixed:
            idx = periods_data.index(p)
            period_rates[idx] = p["espp_pct"]

        if not cycle_modifiable:
            cycle_metadata.append({
                "cycle": f"{cycle_key[0]}-{cycle_key[1]:02d}",
                "is_cross_year": not is_in_year,
                "num_fixed": len(cycle_fixed),
                "num_modifiable": 0,
                "target": target,
                "fixed_contrib": fixed_contrib,
                "remaining": 0.0,
                "assigned": 0.0,
                "gap": 0.0,
            })
            continue

        remaining = max(0.0, target - fixed_contrib)
        mod_gross = [p["gross_pay"] for p in cycle_modifiable]

        # Overshoot mode: ensure optimal contributions >= original config's
        # contributions. The ESPP engine's annual limit cap enforcement will
        # adjust the final period if we slightly exceed the limit.
        rates, assigned = _assign_rates(
            remaining, mod_gross, min_pct=0.01, max_pct=max_pct,
            prefer_overshoot=True,
        )

        for p, rate in zip(cycle_modifiable, rates):
            idx = periods_data.index(p)
            period_rates[idx] = rate

        gap = assigned - remaining  # positive = overshoot, negative = undershoot

        cycle_metadata.append({
            "cycle": f"{cycle_key[0]}-{cycle_key[1]:02d}",
            "is_cross_year": not is_in_year,
            "num_fixed": len(cycle_fixed),
            "num_modifiable": len(cycle_modifiable),
            "target": target,
            "fixed_contrib": fixed_contrib,
            "remaining": remaining,
            "assigned": assigned,
            "gap": gap,
        })

    # Build the full ordered rate list
    full_espp_pct = [period_rates[i] for i in range(len(periods_data))]

    logger.info(
        f"ESPP optimal schedule: {len(cycle_metadata)} cycles, "
        f"in-year target=${target_in_year:,.2f} (limit ${annual_limit:,.0f}), "
        f"cross-year desired=${desired_cross_year:,.2f}"
    )
    for cm in cycle_metadata:
        direction = "over" if cm["gap"] >= 0 else "under"
        logger.info(
            f"  {cm['cycle']}{' (carry-fwd)' if cm['is_cross_year'] else ''}: "
            f"target=${cm['target']:,.2f}, fixed=${cm['fixed_contrib']:,.2f}, "
            f"assigned=${cm['assigned']:,.2f} ({direction} ${abs(cm['gap']):,.2f})"
        )

    return {
        "espp_pct": full_espp_pct,
        "metadata": {
            "today": today.isoformat(),
            "annual_limit": annual_limit,
            "desired_in_year": desired_in_year,
            "target_in_year": target_in_year,
            "desired_cross_year": desired_cross_year,
            "max_pct": max_pct,
            "cycles": cycle_metadata,
        },
    }
