"""Helpers shared by the rule modules.

Kept separate so that a rule module imports only what it uses, and so the business-day
calculation has one definition. Public holidays are out of scope: the corpus references them
(FIN-POL-006 §2) but supplies no calendar, and inventing one would produce review dates that
look authoritative and are not. The limitation is recorded in the README.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ap_agent.domain.enums import ExceptionCategory
from ap_agent.domain.money import Money

#: FIN-POL-007 §4: standard exceptions are reviewed within three business days.
EXCEPTION_REVIEW_BUSINESS_DAYS = 3

#: The currency in which every monetary threshold in the corpus is expressed.
POLICY_CURRENCY = "AUD"


def add_business_days(start: date, days: int) -> date:
    """Advance a date by whole business days, skipping Saturday and Sunday."""
    if days < 0:
        raise ValueError("days must not be negative")
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def previous_business_day(start: date) -> date:
    """The latest business day on or before ``start``.

    FIN-POL-006 §2 pays a non-business-day due date on the *preceding* business day. Moving
    forward instead would make the payment late, which is the whole point of the rule.
    """
    current = start
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def next_review_date(as_of: date) -> date:
    """The review date placed on an exception raised now."""
    return add_business_days(as_of, EXCEPTION_REVIEW_BUSINESS_DAYS)


def threshold(amount: str, currency: str) -> Money:
    """Express a policy threshold in a working currency.

    The corpus states every limit in AUD. When the invoice is in another currency the limit
    is applied numerically in that currency and the substitution is recorded as an
    assumption by the caller, because converting a threshold requires a cited rate under
    FIN-POL-009 §2 and this engine does not invent rates.
    """
    return Money.of(Decimal(amount), currency)


#: Exception categories that describe a reconciliation variance rather than a missing
#: document. Used when deciding whether a hold is a data problem or a control problem.
VARIANCE_CATEGORIES = frozenset(
    {ExceptionCategory.PRICE_VARIANCE, ExceptionCategory.QUANTITY_VARIANCE}
)
