"""Monetary values.

FIN-POL-002 §5 requires tolerance calculations to use decimal arithmetic in the invoice
currency, and forbids treating model-generated arithmetic as authoritative. Two design
choices follow.

First, a float in a monetary field is rejected at the schema boundary rather than coerced.
Binary floating point cannot represent 0.1, so ``0.1 + 0.2 != 0.3``; a tolerance test built
on floats fails unpredictably at the threshold, which is exactly where it matters. Rejecting
the type is stricter than converting it, because a float that reached the boundary means a
caller computed something outside the decimal engine.

Second, currency travels with the amount. Comparing two ``Money`` values of different
currencies raises rather than returning a misleading answer, so an unnoticed
cross-currency subtraction cannot produce a variance that looks within tolerance.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

#: Number of minor units retained. Two is correct for every currency in the corpus; a
#: production system would carry the exponent per currency from a reference data service.
MONEY_EXPONENT = Decimal("0.01")

#: Rounding mode recorded on every calculation. FIN-POL-002 §5 requires the method to be
#: stored; half-up matches the convention used by the finance team's spreadsheets.
MONEY_ROUNDING = ROUND_HALF_UP


def reject_float_value(value: object, name: str) -> None:
    """Raise if ``value`` is a float.

    Takes ``object`` rather than the declared parameter type on purpose. Guarding a
    parameter already annotated ``Decimal`` reads to a type checker as an unreachable
    branch, because ``Decimal`` and ``float`` have disjoint bases. The guard is still
    worth having: annotations are not enforced at runtime, and the callers that matter
    most here are the ones passing values from JSON, a spreadsheet or a model response.
    Widening the parameter keeps the check honest and the type checker satisfied.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{name} must be a Decimal, an int or a string, never a float: binary floating "
            "point cannot represent 0.1, so a threshold comparison built on it fails "
            "unpredictably at exactly the boundary where it matters"
        )


def _reject_float(value: Any) -> Any:
    """Refuse binary floating point in monetary positions.

    Raises ``ValueError`` rather than ``TypeError`` because pydantic v2 converts
    ``ValueError`` and ``AssertionError`` raised inside a validator into a
    ``ValidationError`` with field context, while a ``TypeError`` propagates uncaught and
    would surface as a 500 rather than a 422.
    """
    if isinstance(value, float):
        raise ValueError(  # noqa: TRY004 - pydantic only traps ValueError
            "monetary amounts must not be floats; pass a Decimal, an int, or a string "
            "such as '18400.00' so that decimal arithmetic is exact"
        )
    return value


MoneyAmount = Annotated[Decimal, BeforeValidator(_reject_float)]
"""A Decimal that refuses float input. Use for every monetary field in the domain."""

CurrencyCode = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
"""ISO 4217 alphabetic code. Validated by shape only; there is no currency reference data
in this system, and inventing one would imply a completeness we do not have."""


def quantize(amount: Decimal) -> Decimal:
    """Round to minor units using the recorded rounding mode."""
    return amount.quantize(MONEY_EXPONENT, rounding=MONEY_ROUNDING)


class Money(BaseModel):
    """An amount in a single currency.

    Frozen so that a value cannot be mutated after a calculation has cited it. Arithmetic
    returns new instances and refuses to mix currencies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: MoneyAmount
    currency: CurrencyCode

    @model_validator(mode="after")
    def _quantize_amount(self) -> Self:
        quantized = quantize(self.amount)
        if quantized != self.amount:
            # Rebuild rather than mutate: the model is frozen, and rounding at construction
            # keeps every stored amount at the same scale so equality is meaningful.
            object.__setattr__(self, "amount", quantized)
        return self

    # ---- construction ---------------------------------------------------------------

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str) -> Money:
        return cls(amount=Decimal(str(amount)), currency=currency.upper())

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(amount=Decimal("0.00"), currency=currency.upper())

    # ---- arithmetic -----------------------------------------------------------------

    def _require_same_currency(self, other: Money, operation: str) -> None:
        if self.currency != other.currency:
            raise ValueError(
                f"cannot {operation} {self.currency} and {other.currency}: convert under "
                "FIN-POL-009 §2 with a cited rate before comparing"
            )

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other, "add")
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other, "subtract")
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        reject_float_value(factor, "factor")
        return Money(amount=self.amount * Decimal(str(factor)), currency=self.currency)

    def __neg__(self) -> Money:
        return Money(amount=-self.amount, currency=self.currency)

    def __abs__(self) -> Money:
        return Money(amount=abs(self.amount), currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount >= other.amount

    # ---- rendering ------------------------------------------------------------------

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"

    @property
    def is_zero(self) -> bool:
        return self.amount == 0


def percent_of(value: Money, percent: Decimal) -> Money:
    """``percent`` of ``value``, quantized. ``percent`` is expressed as e.g. Decimal("1") for 1%."""
    reject_float_value(percent, "percent")
    return Money(amount=value.amount * percent / Decimal("100"), currency=value.currency)


def minimum(*values: Money) -> Money:
    """Smallest of several amounts. Used for the ``min(absolute, percentage)`` tolerance
    limits in FIN-POL-002 §2, where the policy wording is "the lower of these limits"."""
    if not values:
        raise ValueError("minimum() requires at least one value")
    currencies = {value.currency for value in values}
    if len(currencies) > 1:
        raise ValueError(f"cannot take the minimum across currencies {sorted(currencies)}")
    return min(values, key=lambda value: value.amount)
