"""Shared fixtures.

Every test runs against a temporary database and index directory. No test touches the
developer's ``data/`` directory, so a failed run cannot leave state that makes the next run
pass for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ap_agent.config.settings import Settings
from ap_agent.domain.enums import LineType, VendorStatus
from ap_agent.domain.evidence import (
    GoodsReceipt,
    InvoiceHistoryStatus,
    POLine,
    PurchaseOrder,
    VendorRecord,
)
from ap_agent.domain.request import ProcessingRequest, RequestLine

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Fixed clock. Every date-dependent assertion is expressed relative to this instant so
#: that the suite does not start failing when a vendor crosses the 30-day threshold.
NOW = datetime(2026, 9, 11, 9, 30, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider="fake",
        corpus_dir=REPO_ROOT / "finance_rag_corpus",
        index_dir=tmp_path / "index",
        db_path=tmp_path / "runtime" / "ap_agent.db",
        tool_timeout_seconds=1.0,
        tool_max_retries=2,
        tool_retry_backoff_seconds=0.0,
        max_steps=12,
        max_tool_calls=10,
    )


@pytest.fixture
def matched_request() -> ProcessingRequest:
    """A clean PO-backed invoice: the FIN-001 shape, used as the baseline in unit tests."""
    return ProcessingRequest(
        case_id="TEST-CLEAN",
        invoice_reference="INV-2026-0451",
        vendor="Brightline Industrial Supplies Pty Ltd",
        vendor_id="V-1001",
        amount=Decimal("18400.00"),
        net_amount=Decimal("16727.27"),
        tax_amount=Decimal("1672.73"),
        currency="AUD",
        invoice_date=NOW.date(),
        po_reference="PO-88121",
        lines=[
            RequestLine(
                line_number=1,
                description="Industrial bearing assembly, 40mm",
                line_type=LineType.GOODS,
                quantity=Decimal("120"),
                unit_price=Decimal("96.00"),
                line_total=Decimal("11520.00"),
                po_line_number=1,
            ),
            RequestLine(
                line_number=2,
                description="Sealed drive coupling, type C",
                line_type=LineType.GOODS,
                quantity=Decimal("46"),
                unit_price=Decimal("113.20"),
                line_total=Decimal("5207.20"),
                po_line_number=2,
            ),
        ],
    )


@pytest.fixture
def matched_po() -> PurchaseOrder:
    return PurchaseOrder(
        po_reference="PO-88121",
        vendor_id="V-1001",
        currency="AUD",
        total_value=Decimal("16727.20"),
        approval_status="APPROVED",
        approved_by="U-3081",
        payment_terms_days=30,
        lines=[
            POLine(
                line_number=1,
                description="Industrial bearing assembly, 40mm",
                line_type=LineType.GOODS,
                quantity_ordered=Decimal("120"),
                unit_price=Decimal("96.00"),
                line_value=Decimal("11520.00"),
            ),
            POLine(
                line_number=2,
                description="Sealed drive coupling, type C",
                line_type=LineType.GOODS,
                quantity_ordered=Decimal("46"),
                unit_price=Decimal("113.20"),
                line_value=Decimal("5207.20"),
            ),
        ],
        receipts=[
            GoodsReceipt(
                receipt_id="GR-55010",
                po_line_number=1,
                quantity_received=Decimal("120"),
                received_date=NOW.date(),
                receipted_by="U-2210",
            ),
            GoodsReceipt(
                receipt_id="GR-55011",
                po_line_number=2,
                quantity_received=Decimal("46"),
                received_date=NOW.date(),
                receipted_by="U-2210",
            ),
        ],
    )


@pytest.fixture
def active_vendor() -> VendorRecord:
    return VendorRecord(
        vendor_id="V-1001",
        legal_name="Brightline Industrial Supplies Pty Ltd",
        status=VendorStatus.ACTIVE,
        bank_account_last4="4417",
        bank_country="AU",
        bank_details_changed_at=datetime(2024, 3, 2, tzinfo=UTC),
        created_at=datetime(2019, 6, 14, tzinfo=UTC),
        last_updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        created_by="U-1140",
    )


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> Iterator[None]:
    """Ensure no test observes another test's cached settings."""
    from ap_agent.config.settings import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


__all__ = ["NOW", "REPO_ROOT", "InvoiceHistoryStatus"]
