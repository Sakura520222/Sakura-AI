"""Validate billing activation before saving configuration, without paid probes."""

from backend.core.config import get_dynamic_config
from backend.services.billing_pricing import credits_to_units, exact_decimal
from backend.services.billing_service import BillingError, BillingService


class BillingConfigurationError(BillingError):
    """Safe machine-readable field failures; no submitted values or secrets."""

    def __init__(
        self,
        message,
        code,
        field,
        translation_key,
        *,
        params=None,
        details=None,
        help_url=None,
        help_label_key=None,
        issues=None,
    ):
        super().__init__(message, code)
        self.issues = issues or [
            {
                "field": field,
                "code": code,
                "message_key": translation_key,
                "params": params or {},
                "details": details or [],
                "help_url": help_url,
                "help_label_key": help_label_key,
            }
        ]


def config_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "1", "false", "0"}:
        return value.lower() in {"true", "1"}
    raise BillingError(
        "Billing switches must be explicit booleans", "invalid_billing_config"
    )


async def validate_billing_configuration(session, changes):
    issues = []
    for key in (
        "billing_enabled",
        "billing_charge_failed_operations",
        "billing_charge_failed_calls",
    ):
        if key in changes:
            try:
                config_bool(changes[key])
            except BillingError:
                issues.append(
                    {
                        "field": key,
                        "code": "invalid_billing_config",
                        "message_key": "billing.validation.boolean",
                    }
                )
    if "billing_initial_reserve_credits" in changes:
        try:
            if credits_to_units(changes["billing_initial_reserve_credits"]) < 0:
                raise ValueError
        except ValueError, ArithmeticError:
            issues.append(
                {
                    "field": "billing_initial_reserve_credits",
                    "code": "invalid_billing_config",
                    "message_key": "billing.validation.credits",
                }
            )
    if "billing_reservation_ttl_seconds" in changes:
        raw = changes["billing_reservation_ttl_seconds"]
        try:
            if isinstance(raw, bool | float) or int(raw) < 60:
                raise ValueError
        except ValueError, TypeError:
            issues.append(
                {
                    "field": "billing_reservation_ttl_seconds",
                    "code": "invalid_billing_config",
                    "message_key": "billing.validation.reservation_ttl",
                }
            )
    if "payment_partial_refund_policy" in changes and changes[
        "payment_partial_refund_policy"
    ] not in {"reject", "proportional_unused_credits"}:
        issues.append(
            {
                "field": "payment_partial_refund_policy",
                "code": "invalid_billing_config",
                "message_key": "billing.validation.refund_policy",
            }
        )
    if issues:
        raise BillingConfigurationError(
            "Invalid billing parameters",
            "invalid_billing_config",
            issues[0]["field"],
            issues[0]["message_key"],
            issues=issues,
        )
    route_keys = {
        "billing_enabled",
        "enable_context_compression",
        "embedding_provider",
        "embedding_model",
        "rerank_provider",
        "rerank_model",
        "ai_role_bindings",
    }
    if not route_keys.intersection(changes) and not any(
        key.startswith("ai_account.") for key in changes
    ):
        return
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from backend.core.config import get_settings
    from backend.core.time_service import now_utc
    from backend.models.database import AppConfig
    from backend.models.legacy_entitlement_models import LegacyEntitlement
    from backend.models.payment_models import Order, Plan

    # All route writes and activation serialize on this persisted config row.
    # Bootstrap once for installations that have not saved the default yet.
    gate_query = (
        select(AppConfig)
        .where(AppConfig.key_name == "billing_enabled")
        .execution_options(populate_existing=True)
    )
    gate = (await session.execute(gate_query.with_for_update())).scalar_one_or_none()
    if gate is None:
        try:
            async with session.begin_nested():
                session.add(
                    AppConfig(
                        key_name="billing_enabled",
                        key_value=str(get_settings().billing_enabled).lower(),
                    )
                )
                await session.flush()
        except IntegrityError:
            pass  # Another writer bootstrapped it; the locking read follows.
        gate = (await session.execute(gate_query.with_for_update())).scalar_one()
    if not config_bool(changes.get("billing_enabled", gate.key_value)):
        return

    # The rollout must not turn an already purchased request allowance into
    # unusable rate-limit headroom without an explicitly reviewed conversion.
    sources = (
        (
            await session.execute(
                select(LegacyEntitlement).where(LegacyEntitlement.revoked_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    by_order = {source.order_id: source for source in sources if source.order_id}
    paid_orders = (
        (
            await session.execute(
                select(Order).where(Order.status == "fulfilled", Order.amount_cents > 0)
            )
        )
        .scalars()
        .all()
    )
    unreviewed = []
    for order in paid_orders:
        source = by_order.get(order.id)
        if not order.plan_snapshot and source is None:
            unreviewed.append(order.id)
            continue
        if (
            source is None
            or source.converted_at
            or (source.expires_at and source.expires_at <= now_utc())
        ):
            continue
        has_bonus = any(
            getattr(source, f"{prefix}_remaining") > 0
            for prefix in ("pr", "issue", "agent")
        )
        legacy_period = bool(source.rate_limits) and (
            source.snapshot.get("version", 1) < 2
            or any(
                source.snapshot.get(f"{prefix}_{period}_add", 0) > 0
                for prefix in ("pr", "issue", "agent")
                for period in ("daily", "weekly", "monthly")
            )
        )
        if (has_bonus or legacy_period) and exact_decimal(
            source.snapshot.get("credit_grant", "0")
        ) == 0:
            unreviewed.append(order.id)
    if unreviewed:
        raise BillingConfigurationError(
            "Review and convert purchased legacy sources before enabling billing; orders: "
            + ", ".join(map(str, unreviewed[:20])),
            "legacy_migration_required",
            "billing_enabled",
            "billing.validation.legacy_sources",
            params={"orders": ", ".join(map(str, unreviewed[:20]))},
            help_url="/billing/admin/orders",
            help_label_key="billing.admin_orders",
        )
    offers = (
        (
            await session.execute(
                select(Plan).where(Plan.is_active, Plan.price_cents > 0)
            )
        )
        .scalars()
        .all()
    )
    for plan in offers:
        legacy_fields = [
            getattr(plan, f"{prefix}_{suffix}") or 0
            for prefix in ("pr", "issue", "agent")
            for suffix in ("quota_bonus", "daily_add", "weekly_add", "monthly_add")
        ]
        if not plan.credit_grant and any(legacy_fields):
            raise BillingConfigurationError(
                "Configure Credits or disable active legacy paid plan: " + str(plan.id),
                "legacy_plan_requires_credits",
                "billing_enabled",
                "billing.validation.legacy_plan",
                params={"plan_id": plan.id},
                help_url="/billing/admin/plans",
                help_label_key="billing.admin_plans",
            )
    from backend.core.ai_protocol.role_config import (
        ALL_ROLES,
        resolve_role_from_config,
        resolve_role_from_snapshot,
    )

    snapshot = None
    if "ai_role_bindings" in changes or any(
        key.startswith("ai_account.") for key in changes
    ):
        from backend.core.ai_protocol import account_store

        rows = (
            (
                await session.execute(
                    select(AppConfig)
                    .where(
                        AppConfig.key_name.startswith("ai_account.")
                        | (AppConfig.key_name == "ai_role_bindings")
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        values = {row.key_name: row.key_value for row in rows}
        values.update(changes)
        accounts, bindings = {}, {}
        for key, raw in values.items():
            data = account_store._safe_json_loads(raw)
            if not isinstance(data, dict):
                continue
            if key.startswith("ai_account."):
                account = account_store._account_from_dict(data)
                account.id = account.id or key.removeprefix("ai_account.")
                accounts[account.id] = account
            elif key == "ai_role_bindings":
                for role, entry in data.items():
                    if isinstance(entry, dict):
                        binding = account_store._role_binding_from_dict(entry)
                        if binding is not None:
                            bindings[role] = binding
        snapshot = accounts, bindings

    service = BillingService(session)
    routes = set()
    compression = config_bool(
        changes.get(
            "enable_context_compression",
            await get_dynamic_config("enable_context_compression", fresh=True),
        )
    )
    for role in ALL_ROLES:
        chain = (
            resolve_role_from_snapshot(role, *snapshot)
            if snapshot is not None
            else await resolve_role_from_config(role)
        )
        if role == "main" and (chain is None or not chain.candidates):
            raise BillingConfigurationError(
                "Configure an AI main role before enabling Credits billing",
                "missing_price",
                "billing_enabled",
                "billing.validation.main_role",
                help_url="/config/ai",
                help_label_key="config.ai_title",
            )
        for candidate in chain.candidates if chain else ():
            account_id = getattr(candidate, "account_id", None)
            routes.add(
                (
                    str(candidate.provider.id),
                    candidate.model.model_id,
                    "chat",
                    account_id,
                )
            )
            if compression:
                routes.add(
                    (
                        str(candidate.provider.id),
                        candidate.model.model_id,
                        "context_compression",
                        account_id,
                    )
                )
    for feature in ("embedding", "rerank"):
        provider = changes.get(
            f"{feature}_provider",
            await get_dynamic_config(f"{feature}_provider", fresh=True),
        )
        if provider not in {None, "", "none", "local"}:
            model = changes.get(
                f"{feature}_model",
                await get_dynamic_config(f"{feature}_model", fresh=True),
            )
            routes.add((str(provider), str(model), feature, None))
    missing = []
    for provider, model, kind, account_id in sorted(
        routes, key=lambda item: tuple(value or "" for value in item)
    ):
        if await service._price(provider, model, kind, account_id=account_id) is None:
            prefix = f"account:{account_id}/" if account_id is not None else ""
            missing.append(f"{prefix}{provider}/{model}/{kind}")
    if missing:
        raise BillingConfigurationError(
            "Missing exact pricing profiles: " + ", ".join(missing),
            "missing_price",
            "billing_enabled",
            "billing.validation.missing_prices",
            params={"count": len(missing)},
            details=missing,
            help_url="/billing/admin/pricing",
            help_label_key="billing.model_pricing",
        )
