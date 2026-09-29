"""The order status vocabulary orders and reconcile share.

Both read the status a Trade carries (openapi.json OrderStatusInfo.status),
which may be spelled with the enum's qualified name, so each normalizes
every status it reads with normalize_status before comparing it.
"""

from __future__ import annotations

# Values of the OpenAPI Status enum that p1rec compares against
STATUS_FAILED = "Failed"
STATUS_FILLED = "Filled"
STATUS_CANCELLED = "Cancelled"
STATUS_PART_FILLED = "PartFilled"
STATUS_SUBMITTED = "Submitted"

_QUALIFIER = "Status."


def normalize_status(status: str) -> str:
    """Return status without the enum's "Status." qualifier, so both spellings compare equal."""
    return status.removeprefix(_QUALIFIER)
