"""Cooperative boundaries shared by optional Issue and PR relation work."""

from backend.core.ai_protocol.errors import ReviewCancelledError


class RelationDeadlineExceeded(TimeoutError):
    """The optional relation phase exhausted its soft deadline."""


def check_relation_boundary(cancel_event=None, deadline=None):
    """Preserve domain cancellation before observing soft deadline expiry.

    A soft deadline is checked between operations, never used to cancel a
    provider or a blocking GitHub/vector-store operation that is still running.
    Genuine task cancellation propagates unchanged through normal awaits.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ReviewCancelledError()
    if deadline is not None and deadline.is_expired():
        raise RelationDeadlineExceeded("Issue relation deadline exceeded")
