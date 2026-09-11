"""Simulated systems of record, with deterministic fault injection.

Four integrations are simulated here: the vendor master, the purchasing and receipting
system, the invoice history, and the authority register. The README labels each one, and
nothing in this module reaches the network.

## Relative dates

Fixture dates may be written as ``@as_of``, ``@as_of-2d`` or ``@as_of+20d`` and are resolved
against the run clock. This is not convenience. A literal date would make the FIN-003 bank
change stop being recent the moment the calendar moved past its 30-day window, so the case
would quietly stop testing what it was written to test, and it would still pass. Relative
dates keep a fixture's *intent* stable rather than its values.

## Fault injection

Faults are declarative, keyed by case and tool in ``fault_profiles.json``. A reviewer can see
which failure mode each case exercises without reading code, and the failures are produced by
the same code path a real outage would take, rather than by patching a function inside a
test.

The fault counter is per backend instance, so ``transient_then_success`` means the first
attempt of a given run fails and the next succeeds. A fresh instance resets it, which is
what makes a resumed run behave like a new caller rather than inheriting the earlier run's
failure count.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from ap_agent.domain.errors import PermanentToolError, ToolTimeoutError, TransientToolError
from ap_agent.domain.evidence import (
    DelegationRecord,
    GoodsReceipt,
    InvoiceHistoryMatch,
    POLine,
    PurchaseOrder,
    VendorRecord,
)

DEFAULT_MOCK_DATA_DIR: Final = Path(__file__).resolve().parents[3] / "fixtures" / "mock_data"

_RELATIVE_DATE = re.compile(r"^@as_of(?:(?P<sign>[+-])(?P<days>\d+)d)?$")

#: Profile names understood by the backend. An unrecognised profile is an error rather than a
#: silent no-op: a typo in a fixture must not turn a fault-injection case into a happy path.
KNOWN_PROFILES: Final[frozenset[str]] = frozenset(
    {"timeout_always", "transient_then_success", "permanent_error", "not_found"}
)


def _resolve_temporal(value: Any, as_of: datetime) -> Any:
    """Resolve a relative date token against the run clock, leaving other values alone."""
    if not isinstance(value, str):
        return value
    match = _RELATIVE_DATE.match(value)
    if match is None:
        return value
    days = int(match.group("days") or 0)
    if match.group("sign") == "-":
        days = -days
    return (as_of + timedelta(days=days)).isoformat()


def _resolve_dates(payload: Any, as_of: datetime) -> Any:
    """Recursively resolve relative date tokens and drop comment keys."""
    if isinstance(payload, dict):
        return {
            key: _resolve_dates(value, as_of)
            for key, value in payload.items()
            if not key.startswith("_")
        }
    if isinstance(payload, list):
        return [_resolve_dates(item, as_of) for item in payload]
    return _resolve_temporal(payload, as_of)


def _to_date(value: str) -> str:
    """Trim a resolved timestamp to a date string for date-typed fields."""
    return value[:10]


class MockBackends:
    """In-memory stand-ins for the four external systems of record."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        case_id: str = "",
        as_of: datetime | None = None,
    ) -> None:
        self._data_dir = data_dir or DEFAULT_MOCK_DATA_DIR
        self._case_id = case_id
        self._as_of = as_of or datetime.now(tz=UTC)
        self._attempts: dict[str, int] = {}

        self._vendors = self._load("vendors.json")["vendors"]
        self._purchase_orders = self._load("purchase_orders.json")["purchase_orders"]
        self._history = self._load("invoice_history.json")["records"]
        delegation_payload = self._load("delegations.json")
        self._delegations = delegation_payload["delegations"]
        self._faults = self._load("fault_profiles.json")["cases"].get(case_id, {})

        unknown = set(self._faults.values()) - KNOWN_PROFILES
        if unknown:
            raise ValueError(
                f"case {case_id} declares unknown fault profile(s) {sorted(unknown)}; "
                f"known profiles are {sorted(KNOWN_PROFILES)}"
            )

    def _load(self, filename: str) -> dict[str, Any]:
        path = self._data_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"mock data file not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        resolved = _resolve_dates(raw, self._as_of)
        if not isinstance(resolved, dict):
            raise TypeError(f"mock data file {path.name} must contain a JSON object")
        return resolved

    # ---- fault injection ---------------------------------------------------------------

    def _apply_fault(self, tool_name: str) -> None:
        """Raise the failure this case declares for this tool, if any."""
        profile = self._faults.get(tool_name)
        if profile is None:
            return
        attempt = self._attempts.get(tool_name, 0) + 1
        self._attempts[tool_name] = attempt

        if profile == "timeout_always":
            raise ToolTimeoutError(tool_name, "simulated upstream timeout")
        if profile == "transient_then_success" and attempt == 1:
            raise TransientToolError(tool_name, "simulated upstream 503, retryable")
        if profile == "permanent_error":
            raise PermanentToolError(tool_name, "simulated upstream 400, not retryable")
        # "not_found" is handled by the lookup itself returning nothing.

    def _is_not_found_profile(self, tool_name: str) -> bool:
        # bool() rather than a bare comparison: the profile map is loaded from JSON, so its
        # values are untyped as far as a checker is concerned.
        return bool(self._faults.get(tool_name) == "not_found")

    @property
    def attempt_counts(self) -> dict[str, int]:
        """Attempts per tool. Used by tests to assert the retry ceiling was respected."""
        return dict(self._attempts)

    # ---- vendor master -----------------------------------------------------------------

    def get_vendor(self, vendor_id: str) -> VendorRecord | None:
        self._apply_fault("get_vendor_record")
        if self._is_not_found_profile("get_vendor_record"):
            return None
        record = self._vendors.get(vendor_id)
        if record is None:
            return None
        return VendorRecord.model_validate(record)

    # ---- purchasing and receipting ------------------------------------------------------

    def get_purchase_order(self, po_reference: str) -> PurchaseOrder | None:
        self._apply_fault("get_purchase_order")
        if self._is_not_found_profile("get_purchase_order"):
            return None
        record = self._purchase_orders.get(po_reference)
        if record is None:
            return None
        return PurchaseOrder(
            po_reference=record["po_reference"],
            vendor_id=record["vendor_id"],
            currency=record["currency"],
            total_value=Decimal(record["total_value"]),
            approval_status=record["approval_status"],
            approved_by=record.get("approved_by"),
            permits_currency_conversion=record.get("permits_currency_conversion", False),
            payment_terms_days=record.get("payment_terms_days"),
            lines=[
                POLine(
                    line_number=line["line_number"],
                    description=line["description"],
                    line_type=line["line_type"],
                    quantity_ordered=Decimal(line["quantity_ordered"]),
                    unit_price=Decimal(line["unit_price"]),
                    line_value=Decimal(line["line_value"]),
                    permits_freight=line.get("permits_freight", False),
                    service_completion_confirmed=line.get("service_completion_confirmed", False),
                )
                for line in record["lines"]
            ],
            receipts=[
                GoodsReceipt(
                    receipt_id=receipt["receipt_id"],
                    po_line_number=receipt["po_line_number"],
                    quantity_received=Decimal(receipt["quantity_received"]),
                    received_date=date.fromisoformat(_to_date(receipt["received_date"])),
                    receipted_by=receipt["receipted_by"],
                )
                for receipt in record.get("receipts", [])
            ],
        )

    # ---- invoice history ----------------------------------------------------------------

    def find_history(self, *, vendor_id: str) -> list[InvoiceHistoryMatch]:
        """Candidate prior records for a vendor.

        Returns everything on file for the vendor rather than pre-filtering to likely
        matches. Classification is the rule engine's job, and a backend that decided what
        counted as a candidate would put part of the duplicate policy in a mock.
        """
        self._apply_fault("check_invoice_history")
        if self._is_not_found_profile("check_invoice_history"):
            return []
        return [
            InvoiceHistoryMatch(
                record_id=record["record_id"],
                invoice_reference=record["invoice_reference"],
                vendor_id=record["vendor_id"],
                currency=record["currency"],
                gross_amount=Decimal(record["gross_amount"]),
                invoice_date=date.fromisoformat(_to_date(record["invoice_date"])),
                status=record["status"],
                po_reference=record.get("po_reference"),
                attachment_hashes=record.get("attachment_hashes", []),
                match_type="candidate",
            )
            for record in self._history
            if record["vendor_id"] == vendor_id
        ]

    # ---- authority register -------------------------------------------------------------

    def get_delegation(self, delegation_id: str) -> DelegationRecord | None:
        self._apply_fault("get_authority_delegation")
        if self._is_not_found_profile("get_authority_delegation"):
            return None
        record = self._delegations.get(delegation_id)
        if record is None:
            return None
        return DelegationRecord(
            delegation_id=record["delegation_id"],
            register_version=record["register_version"],
            delegate_id=record["delegate_id"],
            delegate_role=record["delegate_role"],
            delegator_id=record["delegator_id"],
            delegator_role=record["delegator_role"],
            scope=record["scope"],
            starts_on=date.fromisoformat(_to_date(record["starts_on"])),
            ends_on=date.fromisoformat(_to_date(record["ends_on"])),
            revoked=record.get("revoked", False),
        )
