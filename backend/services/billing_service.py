"""Transactional Credits accounting and recoverable AI operation settlement.

Services flush but never commit: the caller owns the financial transaction.
External AI/payment requests must run outside these transactions.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, localcontext

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend.core.config import get_dynamic_config
from backend.core.time_service import now_utc
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingCreditDebt,
    BillingCreditHold,
    BillingCreditLot,
    BillingDebtFunding,
    BillingNotice,
    BillingOperation,
    BillingPriceProfile,
    BillingReservationEvent,
    BillingTransaction,
    BillingUsageCharge,
    BillingWallet,
)
from backend.services.billing_price_identity import canonical_price_call_kind
from backend.services.billing_pricing import (
    DECIMAL_PRECISION,
    PricingPending,
    calculate_price,
    credits_to_units,
    exact_decimal,
    units_to_credits,
    validate_price_config,
)


class BillingError(Exception):
    def __init__(self, message: str, code: str = "billing_error"):
        super().__init__(message)
        self.code = code


class InsufficientCredits(BillingError):
    def __init__(self, message: str = "Insufficient Credits"):
        super().__init__(message, "insufficient_credits")


class BillingConflict(BillingError):
    def __init__(self):
        super().__init__(
            "Concurrent billing update; retry the transaction", "billing_conflict"
        )


def price_scope_key(account_id: str | None) -> str:
    """Account IDs are stable server routing identifiers, never credentials."""
    if account_id is None:
        return "provider"
    if (
        not isinstance(account_id, str)
        or not account_id
        or account_id.strip() != account_id
        or len(account_id) > 128
    ):
        raise BillingError("Invalid AI account identity", "invalid_price_identity")
    return f"account:{account_id}"


class BillingService:
    def __init__(self, session, *, policy: dict | None = None):
        self.session = session
        self.policy = policy

    async def _setting(self, name):
        if self.policy is not None and name in self.policy:
            return self.policy[name]
        return await get_dynamic_config(name, fresh=True)

    async def assert_can_start(self, user_id):
        """Cheap preflight; authoritative reservation happens at durable admission."""
        if not await self._setting("billing_enabled"):
            return
        wallet = await self.get_wallet(user_id)
        await self._check_legacy_source_readiness(user_id)
        reserve = credits_to_units(
            await self._setting("billing_initial_reserve_credits") or "0"
        )
        available = wallet.balance_units - wallet.reserved_units
        if available <= 0 or available < reserve:
            raise InsufficientCredits()

    async def get_wallet(self, user_id: int) -> BillingWallet:
        wallet = (
            await self.session.execute(
                select(BillingWallet)
                .where(BillingWallet.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if wallet is None:
            try:
                async with self.session.begin_nested():
                    wallet = BillingWallet(
                        user_id=user_id,
                        balance_units=0,
                        reserved_units=0,
                        version=0,
                        low_balance_threshold_units=0,
                        low_balance_notified=False,
                    )
                    self.session.add(wallet)
                    await self.session.flush()
            except IntegrityError:
                wallet = (
                    await self.session.execute(
                        select(BillingWallet)
                        .where(BillingWallet.user_id == user_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if wallet is None:
                    raise
        return wallet

    async def _check_legacy_source_readiness(self, user_id):
        """Newly redeemed old offers cannot silently lose their bought value."""
        from backend.models.legacy_entitlement_models import LegacyEntitlement

        sources = (
            (
                await self.session.execute(
                    select(LegacyEntitlement)
                    .where(
                        LegacyEntitlement.user_id == user_id,
                        LegacyEntitlement.revoked_at.is_(None),
                        LegacyEntitlement.converted_at.is_(None),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for entry in sources:
            if entry.expires_at and entry.expires_at <= now_utc():
                continue
            if exact_decimal(entry.snapshot.get("credit_grant", "0")) > 0:
                continue
            remaining = any(
                getattr(entry, f"{prefix}_remaining") > 0
                for prefix in ("pr", "issue", "agent")
            )
            old_limits = any(
                entry.snapshot.get(f"{prefix}_{period}_add", 0) > 0
                for prefix in ("pr", "issue", "agent")
                for period in ("daily", "weekly", "monthly")
            )
            potentially_paid = (
                entry.snapshot.get(
                    "funding_amount_cents", entry.snapshot.get("price_cents", 0)
                )
                > 0
                or entry.snapshot.get("version", 1) < 2
            )
            if potentially_paid and (remaining or old_limits):
                raise BillingError(
                    "Purchased legacy rights require reviewed Credits conversion",
                    "legacy_migration_required",
                )

    async def _existing(self, key: str) -> BillingTransaction | None:
        if not isinstance(key, str) or not key or len(key) > 191:
            raise BillingError("Invalid billing idempotency key")
        return (
            await self.session.execute(
                select(BillingTransaction)
                .where(BillingTransaction.idempotency_key == key)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _change_wallet(self, wallet, *, delta=0, reserve_delta=0):
        if wallet.reserved_units + reserve_delta < 0:
            raise BillingError("Reservation underflow")
        new_balance = wallet.balance_units + delta
        credits_to_units(units_to_credits(new_balance))
        result = await self.session.execute(
            update(BillingWallet)
            .where(
                BillingWallet.user_id == wallet.user_id,
                BillingWallet.version == wallet.version,
            )
            .values(
                balance_units=new_balance,
                reserved_units=wallet.reserved_units + reserve_delta,
                version=wallet.version + 1,
                updated_at=now_utc(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise BillingConflict()
        # Refresh via a current read; no stale ORM value may overwrite a CAS update.
        return await self.get_wallet(wallet.user_id)

    async def _allocate(self, user_id: int, units: int) -> list[dict]:
        lots = (
            (
                await self.session.execute(
                    select(BillingCreditLot)
                    .where(
                        BillingCreditLot.user_id == user_id,
                        BillingCreditLot.remaining_units > 0,
                    )
                    .order_by(BillingCreditLot.transaction_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        allocated = []
        remaining = units
        for lot in lots:
            amount = min(lot.remaining_units, remaining)
            if amount:
                lot.remaining_units -= amount
                allocated.append(
                    {"transaction_id": lot.transaction_id, "units": amount}
                )
                remaining -= amount
            if remaining == 0:
                break
        # An actual-cost overrun is an explicit debt, not fabricated funded Credits.
        if remaining:
            allocated.append({"transaction_id": None, "units": remaining, "debt": True})
        return allocated

    async def _lot(self, transaction_id):
        return (
            await self.session.execute(
                select(BillingCreditLot)
                .where(BillingCreditLot.transaction_id == transaction_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _fund_debt(self, user_id, units):
        debts = (
            (
                await self.session.execute(
                    select(BillingCreditDebt)
                    .where(
                        BillingCreditDebt.user_id == user_id,
                        BillingCreditDebt.outstanding_units > 0,
                    )
                    .order_by(BillingCreditDebt.transaction_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        allocations = []
        remaining = units
        for debt in debts:
            amount = min(remaining, debt.outstanding_units)
            if amount:
                debt.outstanding_units -= amount
                remaining -= amount
                allocations.append(
                    {"transaction_id": debt.transaction_id, "units": amount}
                )
        if remaining:
            raise BillingError("Outstanding debt does not reconcile with the ledger")
        return allocations

    async def _restore_debt(self, transaction_id, units):
        debt = (
            await self.session.execute(
                select(BillingCreditDebt)
                .where(BillingCreditDebt.transaction_id == transaction_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        unpaid = min(units, debt.outstanding_units)
        debt.outstanding_units -= unpaid
        remaining = units - unpaid
        restored_funding = []
        fundings = (
            (
                await self.session.execute(
                    select(BillingDebtFunding)
                    .where(
                        BillingDebtFunding.debt_transaction_id == transaction_id,
                        BillingDebtFunding.remaining_units > 0,
                    )
                    .order_by(BillingDebtFunding.funding_transaction_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for funding in fundings:
            amount = min(remaining, funding.remaining_units)
            if amount:
                funding.remaining_units -= amount
                remaining -= amount
                history = await self._restore_source(
                    funding.funding_transaction_id,
                    amount,
                    excluded_debt_id=transaction_id,
                )
                restored_funding.append(
                    {
                        "transaction_id": funding.funding_transaction_id,
                        "units": amount,
                        "history": history,
                    }
                )
        if remaining:
            raise BillingError("Debt refund exceeds attributable funding")
        return {
            "debt_transaction_id": transaction_id,
            "unpaid_retired_units": unpaid,
            "funding_restorations": restored_funding,
        }

    async def _restore_source(self, source_id, units, *, excluded_debt_id=None):
        """Returning usage first retires debt caused by refunding that source.

        A refunded purchase must not be resurrected as a refundable lot. If its
        revocation debt was funded, restore the funding source instead.
        """
        lot = await self._lot(source_id)
        if lot is None:
            raise BillingError("Missing credit provenance")
        candidates = (
            (
                await self.session.execute(
                    select(BillingCreditDebt)
                    .join(
                        BillingTransaction,
                        BillingTransaction.id == BillingCreditDebt.transaction_id,
                    )
                    .where(
                        BillingTransaction.reference_transaction_id == source_id,
                        BillingCreditDebt.transaction_id != excluded_debt_id
                        if excluded_debt_id is not None
                        else True,
                    )
                    .order_by(BillingCreditDebt.transaction_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        remaining = units
        history = []
        for debt in candidates:
            funded = (
                (
                    await self.session.execute(
                        select(BillingDebtFunding.remaining_units)
                        .where(
                            BillingDebtFunding.debt_transaction_id
                            == debt.transaction_id
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            amount = min(remaining, debt.outstanding_units + sum(funded))
            if amount:
                history.append(await self._restore_debt(debt.transaction_id, amount))
                remaining -= amount
            if not remaining:
                break
        if remaining:
            lot = await self._lot(source_id)
            lot.remaining_units += remaining
        return {
            "source_transaction_id": source_id,
            "lot_restored_units": remaining,
            "revocation_debt_restorations": history,
        }

    async def _append(self, wallet, units, key, kind, **kwargs):
        # Unique conflicts must roll back all source allocations and the wallet
        # mutation before reading the winning immutable financial event.
        try:
            async with self.session.begin_nested():
                return await self._append_once(wallet, units, key, kind, **kwargs)
        except IntegrityError:
            existing = await self._existing(key)
            expected = (
                wallet.user_id,
                units,
                kind,
                kwargs.get("operation_id"),
                kwargs.get("order_id"),
                kwargs.get("reference_transaction_id"),
            )
            if existing is None:
                raise
            if (
                existing.user_id,
                existing.delta_units,
                existing.kind,
                existing.operation_id,
                existing.order_id,
                existing.reference_transaction_id,
            ) != expected:
                raise BillingError(
                    "Idempotency key conflicts with financial intent",
                    "idempotency_conflict",
                ) from None
            return existing

    async def _append_once(
        self,
        wallet,
        units,
        key,
        kind,
        *,
        operation_id=None,
        order_id=None,
        reference_transaction_id=None,
        actor_id=None,
        reason=None,
        snapshot=None,
        allow_debt=False,
        restore_allocations=None,
        external_revocation=False,
    ):
        existing = await self._existing(key)
        if existing:
            if (
                existing.user_id,
                existing.delta_units,
                existing.kind,
                existing.operation_id,
                existing.order_id,
                existing.reference_transaction_id,
            ) != (
                wallet.user_id,
                units,
                kind,
                operation_id,
                order_id,
                reference_transaction_id,
            ):
                raise BillingError(
                    "Idempotency key conflicts with financial intent",
                    "idempotency_conflict",
                )
            return existing
        if (
            units < 0
            and not allow_debt
            and wallet.balance_units - wallet.reserved_units < -units
        ):
            raise InsufficientCredits()
        details = dict(snapshot or {})
        if units < 0 and reference_transaction_id is not None:
            lot = await self._lot(reference_transaction_id)
            if lot is None or (
                not external_revocation and lot.remaining_units < -units
            ):
                raise BillingError(
                    "Purchased Credits have already been consumed",
                    "credits_already_consumed",
                )
            if external_revocation:
                unspent = min(lot.remaining_units, -units)
                lot.remaining_units -= unspent
                details["allocations"] = (
                    [{"transaction_id": reference_transaction_id, "units": unspent}]
                    if unspent
                    else []
                )
                if -units > unspent:
                    details["allocations"].append(
                        {
                            "transaction_id": None,
                            "units": -units - unspent,
                            "debt": True,
                        }
                    )
                details["external_source_revocation"] = True
            else:
                lot.remaining_units += units
        debt_repayments = []
        if units > 0 and restore_allocations is None:
            debts = (
                (
                    await self.session.execute(
                        select(BillingCreditDebt.outstanding_units)
                        .where(
                            BillingCreditDebt.user_id == wallet.user_id,
                            BillingCreditDebt.outstanding_units > 0,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            repayment = min(units, sum(debts))
            debt_repayments = (
                await self._fund_debt(wallet.user_id, repayment) if repayment else []
            )
            details["debt_repayments"] = debt_repayments
        if units < 0 and reference_transaction_id is None:
            details["allocations"] = await self._allocate(wallet.user_id, -units)
        if units > 0 and restore_allocations is not None:
            restorations = []
            for allocation in restore_allocations:
                if allocation["transaction_id"] is not None:
                    restorations.append(
                        await self._restore_source(
                            allocation["transaction_id"],
                            allocation["units"],
                            excluded_debt_id=reference_transaction_id,
                        )
                    )
                elif allocation.get("debt"):
                    restorations.append(
                        await self._restore_debt(
                            reference_transaction_id, allocation["units"]
                        )
                    )
            details["source_restorations"] = restorations
        transaction = BillingTransaction(
            user_id=wallet.user_id,
            delta_units=units,
            idempotency_key=key,
            kind=kind,
            operation_id=operation_id,
            order_id=order_id,
            reference_transaction_id=reference_transaction_id,
            actor_id=actor_id,
            reason=reason,
            snapshot=details,
        )
        self.session.add(transaction)
        await self.session.flush()
        debt_units = sum(
            item["units"] for item in details.get("allocations", []) if item.get("debt")
        )
        if debt_units:
            self.session.add(
                BillingCreditDebt(
                    transaction_id=transaction.id,
                    user_id=wallet.user_id,
                    outstanding_units=debt_units,
                )
            )
        if units > 0:
            if restore_allocations is None:
                self.session.add(
                    BillingCreditLot(
                        transaction_id=transaction.id,
                        user_id=wallet.user_id,
                        remaining_units=units
                        - sum(item["units"] for item in debt_repayments),
                    )
                )
                for item in debt_repayments:
                    self.session.add(
                        BillingDebtFunding(
                            debt_transaction_id=item["transaction_id"],
                            funding_transaction_id=transaction.id,
                            remaining_units=item["units"],
                        )
                    )
        wallet = await self._change_wallet(wallet, delta=units)
        await self._notice(wallet, transaction)
        await self.session.flush()
        return transaction

    async def _notice(self, wallet, transaction):
        below = (
            wallet.low_balance_threshold_units > 0
            and wallet.balance_units - wallet.reserved_units
            <= wallet.low_balance_threshold_units
        )
        if below and not wallet.low_balance_notified and transaction.delta_units < 0:
            self.session.add(
                BillingNotice(user_id=wallet.user_id, transaction_id=transaction.id)
            )
            wallet.low_balance_notified = True
        elif not below:
            wallet.low_balance_notified = False

    async def grant(
        self,
        user_id,
        credits,
        idempotency_key,
        *,
        kind="grant",
        order_id=None,
        actor_id=None,
        reason=None,
        snapshot=None,
    ):
        units = credits_to_units(credits)
        if units < 0 or kind not in {"grant", "purchase", "migration"}:
            raise BillingError("Invalid credit grant")
        wallet = await self.get_wallet(user_id)
        return await self._append(
            wallet,
            units,
            idempotency_key,
            kind,
            order_id=order_id,
            actor_id=actor_id,
            reason=reason,
            snapshot=snapshot,
        )

    async def adjust(self, user_id, credits, *, idempotency_key, actor_id, reason):
        if not actor_id or not str(reason or "").strip():
            raise BillingError("Adjustments require an administrator and reason")
        return await self._append(
            await self.get_wallet(user_id),
            credits_to_units(credits),
            idempotency_key,
            "adjustment",
            actor_id=actor_id,
            reason=reason,
        )

    async def _reversal_remaining(self, original):
        reversed_rows = (
            (
                await self.session.execute(
                    select(BillingTransaction.delta_units)
                    .where(BillingTransaction.reference_transaction_id == original.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        reversed_units = sum(reversed_rows)
        return abs(original.delta_units) - abs(int(reversed_units))

    async def check_reversible(self, transaction_id, *, units=None, allow_debt=False):
        original = await self.session.get(BillingTransaction, transaction_id)
        if original is None:
            raise BillingError("Transaction not found")
        wallet = await self.get_wallet(original.user_id)
        remaining = await self._reversal_remaining(original)
        requested = remaining if units is None else units
        if (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or requested <= 0
            or requested > remaining
        ):
            raise BillingError("Transaction already reversed", "already_reversed")
        if original.delta_units > 0:
            lot = await self._lot(transaction_id)
            if lot is None or lot.remaining_units < requested:
                raise BillingError(
                    "Purchased Credits have already been consumed",
                    "credits_already_consumed",
                )
            if (
                not allow_debt
                and wallet.balance_units - wallet.reserved_units < requested
            ):
                raise InsufficientCredits(
                    "Credits are reserved or unavailable for refund"
                )
        return original

    async def reverse(
        self,
        transaction_id,
        idempotency_key,
        *,
        actor_id=None,
        reason=None,
        units=None,
        _verified_hold=False,
    ):
        original = await self.session.get(BillingTransaction, transaction_id)
        if original is None:
            raise BillingError("Transaction not found")
        wallet = await self.get_wallet(original.user_id)
        existing = await self._existing(idempotency_key)
        if existing:
            if existing.reference_transaction_id != transaction_id or (
                units is not None and abs(existing.delta_units) != units
            ):
                raise BillingError("Conflicting reversal idempotency key")
            return existing
        await self.check_reversible(
            transaction_id, units=units, allow_debt=_verified_hold
        )
        remaining = await self._reversal_remaining(original)
        requested = remaining if units is None else units
        restored = None
        if original.delta_units < 0:
            skip = abs(original.delta_units) - remaining
            to_restore = requested
            restored = []
            for allocation in original.snapshot.get("allocations", []):
                size = allocation["units"]
                skipped = min(skip, size)
                skip -= skipped
                size -= skipped
                amount = min(size, to_restore)
                if amount:
                    restored.append({**allocation, "units": amount})
                    to_restore -= amount
            if to_restore:
                raise BillingError("Missing credit allocation provenance")
        return await self._append(
            wallet,
            requested if original.delta_units < 0 else -requested,
            idempotency_key,
            "refund" if original.delta_units < 0 else "adjustment",
            operation_id=original.operation_id,
            order_id=original.order_id,
            reference_transaction_id=original.id,
            actor_id=actor_id,
            reason=reason,
            snapshot={
                "original_transaction_id": original.id,
                "restored_allocations": restored or [],
            },
            restore_allocations=restored,
            allow_debt=_verified_hold,
        )

    async def hold_purchase_refund(
        self, transaction_id, idempotency_key, *, units=None
    ):
        original = await self.session.get(BillingTransaction, transaction_id)
        if original is None or original.delta_units <= 0:
            raise BillingError("Refund hold requires an original credit grant")
        wallet = await self.get_wallet(original.user_id)
        existing = (
            await self.session.execute(
                select(BillingCreditHold)
                .where(BillingCreditHold.idempotency_key == idempotency_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing:
            if existing.transaction_id != transaction_id or (
                units is not None and existing.units != units
            ):
                raise BillingError("Conflicting refund hold")
            return existing.units
        await self.check_reversible(transaction_id, units=units)
        requested = await self._reversal_remaining(original) if units is None else units
        lot = await self._lot(transaction_id)
        lot.remaining_units -= requested
        await self._change_wallet(wallet, reserve_delta=requested)
        self.session.add(
            BillingCreditHold(
                idempotency_key=idempotency_key,
                transaction_id=transaction_id,
                user_id=original.user_id,
                units=requested,
                state="active",
            )
        )
        self.session.add(
            BillingReservationEvent(
                user_id=original.user_id,
                operation_id=f"refund:{transaction_id}",
                kind="refund_hold",
                delta_units=requested,
                idempotency_key=f"refund-hold:{idempotency_key}",
            )
        )
        await self.session.flush()
        return requested

    async def release_purchase_refund(
        self, transaction_id, units, idempotency_key, *, repay_debt=True
    ):
        original = await self.session.get(BillingTransaction, transaction_id)
        if original is None:
            raise BillingError("Original grant not found")
        wallet = await self.get_wallet(original.user_id)
        hold = (
            await self.session.execute(
                select(BillingCreditHold)
                .where(BillingCreditHold.idempotency_key == idempotency_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if hold is None or hold.transaction_id != transaction_id or hold.units != units:
            raise BillingError("Refund hold not found or amount conflicts")
        if hold.state == "released":
            return
        lot = await self._lot(transaction_id)
        lot.remaining_units += units
        repayments = []
        if repay_debt:
            debts = (
                (
                    await self.session.execute(
                        select(BillingCreditDebt.outstanding_units)
                        .where(
                            BillingCreditDebt.user_id == wallet.user_id,
                            BillingCreditDebt.outstanding_units > 0,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            repay = min(units, sum(debts))
            repayments = await self._fund_debt(wallet.user_id, repay) if repay else []
            lot.remaining_units -= repay
            for item in repayments:
                funding = (
                    await self.session.execute(
                        select(BillingDebtFunding)
                        .where(
                            BillingDebtFunding.debt_transaction_id
                            == item["transaction_id"],
                            BillingDebtFunding.funding_transaction_id == transaction_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if funding:
                    funding.remaining_units += item["units"]
                else:
                    self.session.add(
                        BillingDebtFunding(
                            debt_transaction_id=item["transaction_id"],
                            funding_transaction_id=transaction_id,
                            remaining_units=item["units"],
                        )
                    )
        await self._change_wallet(wallet, reserve_delta=-units)
        hold.state = "released"
        hold.released_at = now_utc()
        self.session.add(
            BillingReservationEvent(
                user_id=original.user_id,
                operation_id=f"refund:{transaction_id}",
                kind="refund_release",
                delta_units=-units,
                idempotency_key=f"refund-release:{idempotency_key}",
                snapshot={"debt_repayments": repayments},
            )
        )
        await self.session.flush()

    async def finalize_purchase_refund(
        self, transaction_id, units, hold_key, event_key, *, actor_id=None, reason=None
    ):
        """Post a verified upstream refund against its previously protected source.

        Actual in-flight Usage may have overrun other funds while this source
        was held. Keep that debt instead of refusing an already executed refund.
        """
        original = await self.session.get(BillingTransaction, transaction_id)
        if original is None:
            raise BillingError("Original grant not found")
        await self.get_wallet(original.user_id)
        hold = (
            await self.session.execute(
                select(BillingCreditHold)
                .where(BillingCreditHold.idempotency_key == hold_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if hold is None or hold.transaction_id != transaction_id or hold.units != units:
            raise BillingError("Confirmed refund does not match its protected source")
        await self.release_purchase_refund(
            transaction_id, units, hold_key, repay_debt=False
        )
        return await self.reverse(
            transaction_id,
            event_key,
            units=units,
            actor_id=actor_id,
            reason=reason,
            _verified_hold=True,
        )

    async def reverse_external_purchase(
        self, transaction_id, units, event_key, *, reason, snapshot=None
    ):
        """Record a verified already-executed refund without another money request.

        Spent source Credits become explicitly sourced debt. Other purchase lots
        are retained, rather than silently being revoked to cover that refund.
        Only verified payment reconciliation calls this financial primitive.
        """
        original = await self.session.get(BillingTransaction, transaction_id)
        if original is None or original.delta_units <= 0:
            raise BillingError("External refund requires the original credit source")
        wallet = await self.get_wallet(original.user_id)
        existing = await self._existing(event_key)
        if existing:
            if (
                existing.reference_transaction_id != transaction_id
                or existing.delta_units != -units
            ):
                raise BillingError("Conflicting external refund event")
            return existing
        remaining = await self._reversal_remaining(original)
        if (
            isinstance(units, bool)
            or not isinstance(units, int)
            or units <= 0
            or units > remaining
        ):
            raise BillingError("External refund exceeds original source")
        if not reason or not isinstance(snapshot, dict) or not snapshot:
            raise BillingError("Verified external refund evidence is required")
        return await self._append(
            wallet,
            -units,
            event_key,
            "adjustment",
            order_id=original.order_id,
            reference_transaction_id=original.id,
            reason=reason,
            snapshot=snapshot,
            allow_debt=True,
            external_revocation=True,
        )

    async def set_low_balance_threshold(self, user_id, credits):
        units = credits_to_units(credits)
        if units < 0:
            raise BillingError("Threshold cannot be negative")
        wallet = await self.get_wallet(user_id)
        wallet.low_balance_threshold_units = units
        # Changing preferences does not emit a repeated notification.
        wallet.low_balance_notified = (
            units > 0 and wallet.balance_units - wallet.reserved_units <= units
        )
        await self.session.flush()
        return wallet

    async def publish_price(
        self, provider_id, model_id, call_kind, config, *, actor_id, account_id=None
    ):
        config = validate_price_config(config)
        call_kind = canonical_price_call_kind(call_kind)
        scope_key = price_scope_key(account_id)
        provider_id = str(provider_id)
        if (
            not provider_id
            or len(provider_id) > 128
            or not model_id
            or len(model_id) > 255
            or not call_kind
            or len(call_kind) > 32
            or not actor_id
        ):
            raise BillingError("Invalid price identity")
        # Existing immutable profiles retain the original provider/model/kind
        # version constraint. Continue its global audit sequence across scopes:
        # no historic row or guard has to be rewritten for this additive upgrade.
        latest_version = (
            await self.session.execute(
                select(BillingPriceProfile.version)
                .where(
                    BillingPriceProfile.provider_id == provider_id,
                    BillingPriceProfile.model_id == model_id,
                    BillingPriceProfile.call_kind == call_kind,
                )
                .order_by(BillingPriceProfile.version.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        profile = BillingPriceProfile(
            provider_id=provider_id,
            account_id=account_id,
            scope_key=scope_key,
            model_id=model_id,
            call_kind=call_kind,
            version=latest_version + 1 if latest_version else 1,
            config=config,
            actor_id=actor_id,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(profile)
                await self.session.flush()
        except IntegrityError:
            # Multi-worker races are constrained by SQL, rather than a local
            # lock. The caller can retry after refreshing the transaction.
            raise BillingConflict() from None
        return profile

    async def _price(self, provider_id, model_id, call_kind, *, account_id=None):
        scope_key = price_scope_key(account_id)
        # Only explicitly published unified model tariffs admit new calls.
        # Legacy stream-only prices remain available through their pinned IDs.
        call_kind = canonical_price_call_kind(call_kind)
        return (
            await self.session.execute(
                select(BillingPriceProfile)
                .where(
                    BillingPriceProfile.provider_id == str(provider_id),
                    BillingPriceProfile.model_id == model_id,
                    BillingPriceProfile.call_kind == call_kind,
                    BillingPriceProfile.scope_key == scope_key,
                    BillingPriceProfile.account_id == account_id,
                )
                .order_by(BillingPriceProfile.version.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _operation(self, operation_id):
        identity = (
            await self.session.execute(
                select(BillingOperation.user_id).where(
                    BillingOperation.operation_id == operation_id
                )
            )
        ).one_or_none()
        if identity is not None and identity[0] is not None:
            await self.get_wallet(identity[0])
        operation = (
            await self.session.execute(
                select(BillingOperation)
                .where(BillingOperation.operation_id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if operation is None:
            raise BillingError("Billing operation not registered")
        return operation

    async def register_operation(
        self, user_id, operation_id, feature, source=None, platform_reason=None
    ):
        if (
            not operation_id
            or len(operation_id) > 128
            or not feature
            or len(feature) > 32
        ):
            raise BillingError("Invalid operation identity")
        if user_id is None and not platform_reason:
            raise BillingError("Platform operations require a cost ownership reason")
        wallet = await self.get_wallet(user_id) if user_id is not None else None
        existing = (
            await self.session.execute(
                select(BillingOperation)
                .where(BillingOperation.operation_id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing:
            if (
                existing.user_id != user_id
                or existing.feature != feature
                or any(
                    key in existing.source and existing.source[key] != value
                    for key, value in (source or {}).items()
                )
            ):
                raise BillingError("Operation attribution conflict")
            existing.source = {**existing.source, **(source or {})}
            return existing
        enabled = bool(await self._setting("billing_enabled"))
        if user_id is not None:
            await self._check_plan_admission(user_id, feature)
            # SQL row locks serialize native databases. A conditional version
            # write also fences SQLite readers competing for user admission;
            # it changes no balance and creates no financial transaction.
            await self._change_wallet(wallet)
        operation = BillingOperation(
            operation_id=operation_id,
            user_id=user_id,
            feature=feature,
            source=source or {},
            platform_reason=platform_reason,
            charging_enabled=enabled,
            charge_failed=bool(await self._setting("billing_charge_failed_operations")),
            charge_failed_calls=bool(
                await self._setting("billing_charge_failed_calls")
            ),
            status="not_started",
            reserve_units=0,
            reservation_seq=0,
            settled_units=0,
            known_credits="0",
        )
        self.session.add(operation)
        await self.session.flush()
        if enabled and user_id is not None:
            await self._check_legacy_source_readiness(user_id)
            reserve = credits_to_units(
                await self._setting("billing_initial_reserve_credits") or "0"
            )
            if (
                reserve < 0
                or wallet.balance_units - wallet.reserved_units <= 0
                or wallet.balance_units - wallet.reserved_units < reserve
            ):
                raise InsufficientCredits()
            await self._reserve(operation, wallet, reserve)
        ttl = int(await self._setting("billing_reservation_ttl_seconds") or 3600)
        operation.expires_at = now_utc() + timedelta(seconds=ttl)
        await self.session.flush()
        return operation

    async def _check_plan_admission(self, user_id, feature, *, check_daily=True):
        from backend.models.legacy_entitlement_models import LegacyEntitlement
        from backend.services.legacy_entitlement_service import LegacyEntitlementService

        sources = (
            (
                await self.session.execute(
                    select(LegacyEntitlement)
                    .where(*LegacyEntitlementService.active_conditions(user_id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        limits = [
            entry.snapshot.get("concurrency_limit")
            for entry in sources
            if entry.snapshot.get("concurrency_limit")
        ]
        if limits:
            rows = (
                (
                    await self.session.execute(
                        select(BillingOperation.operation_id)
                        .where(
                            BillingOperation.user_id == user_id,
                            BillingOperation.outcome.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            count = len(rows)
            if count >= max(limits):
                raise BillingError(
                    "Concurrent operation rate limit reached", "concurrency_limit"
                )
        if check_daily and feature == "repo_scan":
            scan_limit = sum(
                (entry.rate_limits or {}).get("repo_scan_daily", 0) for entry in sources
            )
            if scan_limit:
                from backend.core.time_service import (
                    get_time_service,
                    start_of_local_day,
                )

                clock = get_time_service()
                day = clock.to_app_timezone(now_utc()).date()
                start = start_of_local_day(day, clock.zone)
                end = start_of_local_day(day + timedelta(days=1), clock.zone)
                rows = (
                    (
                        await self.session.execute(
                            select(BillingOperation.operation_id)
                            .where(
                                BillingOperation.user_id == user_id,
                                BillingOperation.feature == feature,
                                BillingOperation.created_at >= start,
                                BillingOperation.created_at < end,
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                count = len(rows)
                if count >= scan_limit:
                    raise BillingError(
                        "Repository scan daily rate limit reached",
                        "rate_limit_exceeded",
                    )

    async def _reserve(self, operation, wallet, units):
        delta = units - operation.reserve_units
        if delta == 0:
            return
        if delta > 0 and wallet.balance_units - wallet.reserved_units < delta:
            raise InsufficientCredits()
        await self._change_wallet(wallet, reserve_delta=delta)
        operation.reservation_seq += 1
        self.session.add(
            BillingReservationEvent(
                user_id=wallet.user_id,
                operation_id=operation.operation_id,
                kind="reserve" if delta > 0 else "release",
                delta_units=delta,
                idempotency_key=f"reserve:{operation.operation_id}:{operation.reservation_seq}",
            )
        )
        operation.reserve_units = units
        await self.session.flush()

    async def start_call(
        self,
        operation_id,
        call_id,
        provider_id,
        model_id,
        call_kind,
        logical_call_id=None,
        protocol_family=None,
        *,
        account_id=None,
    ):
        operation = await self._operation(operation_id)
        existing = await self.session.get(BillingCallAttempt, call_id)
        if existing:
            # Retrying an upstream send is a new actual attempt, never this path.
            raise BillingError(
                "Actual request identifier was already used", "duplicate_actual_request"
            )
        if operation.outcome is not None or operation.status == "reversed":
            raise BillingError(
                "Operation cannot admit a new request", "operation_closed"
            )
        profile = await self._price(
            provider_id, model_id, call_kind, account_id=account_id
        )
        if operation.charging_enabled and operation.user_id is not None:
            if profile is None:
                raise BillingError(
                    "No exact price for AI account/provider/model/call_kind"
                    if account_id is not None
                    else "No exact price for provider/model/call_kind",
                    "missing_price",
                )
            wallet = await self.get_wallet(operation.user_id)
            accrued = max(
                0,
                credits_to_units(operation.known_credits, round_up=True)
                - operation.settled_units,
            )
            if (
                wallet.balance_units - wallet.reserved_units + operation.reserve_units
                <= accrued
            ):
                raise InsufficientCredits()
            if operation.pending_reason:
                unresolved = (
                    (
                        await self.session.execute(
                            select(AIUsageRecord)
                            .where(AIUsageRecord.operation_id == operation_id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                attempts = (
                    (
                        await self.session.execute(
                            select(BillingCallAttempt)
                            .where(BillingCallAttempt.operation_id == operation_id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                active_keys = {
                    attempt.call_id: attempt.usage_record_key for attempt in attempts
                }
                quoted_keys = set(
                    (
                        await self.session.execute(
                            select(BillingUsageCharge.usage_record_key)
                            .where(BillingUsageCharge.operation_id == operation_id)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                unknown_payable = any(
                    (operation.charge_failed_calls or row.outcome == "completed")
                    and (
                        not row.actual_call_id
                        or active_keys.get(row.actual_call_id) == row.record_key
                    )
                    and (
                        not row.usage_reported
                        or getattr(row, "usage_complete", True) is False
                        or row.record_key not in quoted_keys
                    )
                    for row in unresolved
                )
                if unknown_payable or operation.charge_failed_calls:
                    raise BillingError(
                        "Previous request needs reconciliation",
                        "pending_reconciliation",
                    )
        attempt = BillingCallAttempt(
            call_id=call_id,
            operation_id=operation_id,
            logical_call_id=logical_call_id,
            provider_id=str(provider_id),
            account_id=account_id,
            model_id=model_id,
            call_kind=call_kind,
            protocol_family=protocol_family,
            price_profile_id=profile.id if profile else None,
            state="started",
        )
        self.session.add(attempt)
        operation.status = "running"
        operation.expires_at = now_utc() + timedelta(
            seconds=int(await self._setting("billing_reservation_ttl_seconds") or 3600)
        )
        await self.session.flush()
        return attempt

    async def finish_operation(self, operation_id, status):
        if status not in {"completed", "succeeded", "failed", "cancelled"}:
            raise BillingError("Invalid terminal operation outcome")
        operation = await self._operation(operation_id)
        outcome = "completed" if status == "succeeded" else status
        if operation.outcome is not None and operation.outcome != outcome:
            raise BillingError("Conflicting terminal operation outcome")
        operation.outcome = outcome
        return await self.settle_operation(operation_id)

    async def resume_operation(self, operation_id):
        """Trusted worker lifecycle extends a settled automatic continuation."""
        operation = await self._operation(operation_id)
        if (
            operation.outcome in {"completed", "failed", "cancelled"}
            and not operation.pending_reason
        ):
            if operation.user_id is not None:
                # Resuming an ended execution occupies user headroom again, but
                # keeps its identity and must not reapply its daily admission.
                await self._check_plan_admission(
                    operation.user_id, operation.feature, check_daily=False
                )
                await self._change_wallet(await self.get_wallet(operation.user_id))
            operation.outcome = None
            operation.status = "running"
            await self.session.flush()
        elif operation.outcome is not None:
            raise BillingError("Operation needs reconciliation before continuation")
        ttl = int(await self._setting("billing_reservation_ttl_seconds") or 3600)
        operation.expires_at = now_utc() + timedelta(seconds=ttl)
        await self.session.flush()
        return operation

    async def settle_operation(self, operation_id):
        operation = await self._operation(operation_id)
        usages = (
            (
                await self.session.execute(
                    select(AIUsageRecord)
                    .where(AIUsageRecord.operation_id == operation_id)
                    .order_by(AIUsageRecord.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        attempts = (
            (
                await self.session.execute(
                    select(BillingCallAttempt)
                    .where(BillingCallAttempt.operation_id == operation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        by_call = {attempt.call_id: attempt for attempt in attempts}
        by_record = {
            attempt.usage_record_key: attempt
            for attempt in attempts
            if attempt.usage_record_key
        }
        # Reconciliation appends a replacement usage row without deleting the
        # original unknown/partial evidence. Only the resolved row is priced.
        usages = [
            usage
            for usage in usages
            if not usage.actual_call_id
            or usage.actual_call_id not in by_call
            or by_call[usage.actual_call_id].usage_record_key == usage.record_key
        ]
        pending = None
        for usage in usages:
            frozen = (
                await self.session.execute(
                    select(BillingUsageCharge)
                    .where(BillingUsageCharge.usage_record_key == usage.record_key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if frozen:
                continue
            attempt = (
                await self.session.get(
                    BillingCallAttempt, getattr(usage, "actual_call_id", None)
                )
                if getattr(usage, "actual_call_id", None)
                else None
            )
            attempt = attempt or by_record.get(usage.record_key)
            profile = (
                await self.session.get(BillingPriceProfile, attempt.price_profile_id)
                if attempt and attempt.price_profile_id
                else None
            )
            # Never reinterpret a historical request using a newly published tariff.
            if profile is None:
                pending = "missing_price"
                continue
            if (
                profile.provider_id != usage.provider_id
                or profile.model_id != usage.model_id
                or canonical_price_call_kind(profile.call_kind)
                != canonical_price_call_kind(usage.call_kind)
                or profile.account_id != usage.account_id
                or (attempt and attempt.account_id != usage.account_id)
                or (attempt and attempt.call_kind != usage.call_kind)
            ):
                pending = "price_identity_mismatch"
                continue
            try:
                quote = calculate_price(usage, profile.config)
            except PricingPending as exc:
                pending = str(exc)[:128]
                continue
            self.session.add(
                BillingUsageCharge(
                    operation_id=operation_id,
                    usage_record_key=usage.record_key,
                    price_profile_id=profile.id,
                    provider_cost=str(quote.provider_cost),
                    provider_currency=profile.config["currency"],
                    settlement_amount=str(quote.settlement_amount),
                    settlement_currency=profile.config["settlement_currency"],
                    credits=str(quote.credits),
                    snapshot={
                        **quote.snapshot,
                        "price_version": profile.version,
                        "usage_record_key": usage.record_key,
                        "provider_id": usage.provider_id,
                        "account_id": usage.account_id,
                        "price_scope_key": profile.scope_key,
                        "model_id": usage.model_id,
                        "call_kind": usage.call_kind,
                        "pricing_call_kind": canonical_price_call_kind(
                            profile.call_kind
                        ),
                        "price_profile_call_kind": profile.call_kind,
                    },
                )
            )
        await self.session.flush()
        charges = (
            (
                await self.session.execute(
                    select(BillingUsageCharge)
                    .where(BillingUsageCharge.operation_id == operation_id)
                    .order_by(BillingUsageCharge.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        with localcontext() as ctx:
            ctx.prec = DECIMAL_PRECISION
            payable_keys = {
                usage.record_key
                for usage in usages
                if operation.charge_failed_calls or usage.outcome == "completed"
            }
            total = sum(
                (
                    exact_decimal(charge.credits)
                    for charge in charges
                    if charge.usage_record_key in payable_keys
                ),
                Decimal(0),
            )
        operation.known_credits = str(total)
        attempts = (
            (
                await self.session.execute(
                    select(BillingCallAttempt)
                    .where(BillingCallAttempt.operation_id == operation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        if any(
            attempt.state in {"started", "pending_usage", "pending_reconciliation"}
            for attempt in attempts
        ):
            pending = "pending_reconciliation"
        operation.pending_reason = pending
        wallet = (
            await self.get_wallet(operation.user_id)
            if operation.user_id is not None
            else None
        )
        terminal = operation.outcome is not None
        if terminal:
            # Unknown amounts remain pending; known costs still have immutable quotes.
            charge_user = (
                operation.charging_enabled
                and wallet is not None
                and (operation.outcome == "completed" or operation.charge_failed)
            )
            target = credits_to_units(total, round_up=True) if charge_user else 0
            delta = target - operation.settled_units
            if operation.reserve_units and wallet:
                await self._reserve(operation, wallet, 0)
                wallet = await self.get_wallet(operation.user_id)
            if delta > 0:
                await self._append(
                    wallet,
                    -delta,
                    f"settle:{operation_id}:{target}",
                    "consumption",
                    operation_id=operation_id,
                    allow_debt=True,
                    snapshot={
                        "feature": operation.feature,
                        "source": operation.source,
                        "outcome": operation.outcome,
                        "charge_failed": operation.charge_failed,
                        "cumulative_units": target,
                        "known_unrounded_credits": str(total),
                        "usage_charges": [charge.id for charge in charges],
                        "partial": bool(pending),
                    },
                )
                operation.settled_units = target
            operation.status = (
                "pending_reconciliation"
                if pending == "pending_reconciliation"
                else (
                    "pending_pricing"
                    if pending
                    else (
                        "settled"
                        if operation.outcome == "completed"
                        else operation.outcome
                    )
                )
            )
        else:
            operation.status = "usage_known" if charges else "running"
            if pending:
                operation.status = (
                    "pending_reconciliation"
                    if pending == "pending_reconciliation"
                    else "pending_pricing"
                )
            if wallet and operation.charging_enabled:
                target_reserve = max(
                    operation.reserve_units,
                    max(
                        0,
                        credits_to_units(total, round_up=True)
                        - operation.settled_units,
                    ),
                )
                available_for_op = (
                    wallet.balance_units
                    - wallet.reserved_units
                    + operation.reserve_units
                )
                # Known overrun is retained; never invent a reservation or erase cost.
                await self._reserve(
                    operation,
                    wallet,
                    min(target_reserve, max(available_for_op, operation.reserve_units)),
                )
        await self.session.flush()
        return operation

    async def reconcile_wallet(self, user_id):
        wallet = await self.get_wallet(user_id)
        ledger_units = sum(
            (
                await self.session.execute(
                    select(BillingTransaction.delta_units)
                    .where(BillingTransaction.user_id == user_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        active_reserved = sum(
            (
                await self.session.execute(
                    select(BillingOperation.reserve_units)
                    .where(BillingOperation.user_id == user_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        refund_reserved = sum(
            (
                await self.session.execute(
                    select(BillingCreditHold.units)
                    .where(
                        BillingCreditHold.user_id == user_id,
                        BillingCreditHold.state == "active",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        source_units = sum(
            (
                await self.session.execute(
                    select(BillingCreditLot.remaining_units)
                    .where(BillingCreditLot.user_id == user_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        debt_units = sum(
            (
                await self.session.execute(
                    select(BillingCreditDebt.outstanding_units)
                    .where(BillingCreditDebt.user_id == user_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        return {
            "user_id": user_id,
            "balance_units": wallet.balance_units,
            "ledger_units": int(ledger_units),
            "reserved_units": wallet.reserved_units,
            "operation_reserved_units": int(active_reserved),
            "refund_reserved_units": int(refund_reserved),
            "source_units": int(source_units),
            "outstanding_debt_units": int(debt_units),
            "consistent": wallet.balance_units == ledger_units
            and wallet.reserved_units == active_reserved + refund_reserved
            and wallet.balance_units == source_units + refund_reserved - debt_units,
        }

    async def recover_operations(self, *, limit=100, dry_run=True):
        from sqlalchemy import exists

        from backend.models.service_execution_models import ServiceExecutionOwnership
        from backend.services.service_execution_capacity import (
            has_live_service_execution_ownership,
        )

        live_owner = exists().where(
            ServiceExecutionOwnership.operation_id == BillingOperation.operation_id,
            ServiceExecutionOwnership.state.in_(("queued", "running")),
            ServiceExecutionOwnership.expires_at > now_utc(),
        )
        operations = (
            (
                await self.session.execute(
                    select(BillingOperation)
                    .where(
                        BillingOperation.expires_at <= now_utc(),
                        BillingOperation.outcome.is_(None),
                        ~live_owner,
                    )
                    .order_by(BillingOperation.user_id, BillingOperation.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        report = []
        operation_ids = [operation.operation_id for operation in operations]
        for operation_id in operation_ids:
            operation = (
                await self._operation(operation_id)
                if not dry_run
                else next(row for row in operations if row.operation_id == operation_id)
            )
            if operation.outcome is not None or operation.expires_at > now_utc():
                continue
            if await has_live_service_execution_ownership(
                self.session,
                operation.operation_id,
                for_update=not dry_run,
            ):
                continue
            report.append(
                {
                    "operation_id": operation.operation_id,
                    "status": operation.status,
                    "reserved_units": operation.reserve_units,
                }
            )
            if dry_run:
                continue
            attempts = (
                (
                    await self.session.execute(
                        select(BillingCallAttempt).where(
                            BillingCallAttempt.operation_id == operation.operation_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for attempt in attempts:
                if attempt.state == "started":
                    attempt.state = "pending_reconciliation"
            operation.outcome = "failed"
            await self.settle_operation(operation.operation_id)
        return report
