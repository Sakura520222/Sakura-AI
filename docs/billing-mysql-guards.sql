-- Billing 2.0 immutable guards for MySQL 8.0.29+ / 8.4.
-- Review and run as an authorized database administrator in the Sakura database.
-- The installer account becomes DEFINER and must remain valid.
-- Runtime accounts need table TRIGGER privileges for metadata verification.
-- Existing guards are verified by application startup; conflicting definitions fail closed.
-- This file changes no balances, grants no privileges and changes no global settings.

CREATE TRIGGER IF NOT EXISTS billing_transactions_no_update BEFORE UPDATE ON billing_transactions FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_transactions_no_delete BEFORE DELETE ON billing_transactions FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_price_profiles_no_update BEFORE UPDATE ON billing_price_profiles FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_price_profiles_no_delete BEFORE DELETE ON billing_price_profiles FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_usage_charges_no_update BEFORE UPDATE ON billing_usage_charges FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_usage_charges_no_delete BEFORE DELETE ON billing_usage_charges FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_reservation_events_no_update BEFORE UPDATE ON billing_reservation_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_reservation_events_no_delete BEFORE DELETE ON billing_reservation_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_legacy_entitlement_events_no_update BEFORE UPDATE ON billing_legacy_entitlement_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_legacy_entitlement_events_no_delete BEFORE DELETE ON billing_legacy_entitlement_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_refund_attempt_events_no_update BEFORE UPDATE ON payment_refund_attempt_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_refund_attempt_events_no_delete BEFORE DELETE ON payment_refund_attempt_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_reconciliation_events_no_update BEFORE UPDATE ON billing_reconciliation_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_reconciliation_events_no_delete BEFORE DELETE ON billing_reconciliation_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_rate_limit_admissions_no_update BEFORE UPDATE ON billing_rate_limit_admissions FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS billing_rate_limit_admissions_no_delete BEFORE DELETE ON billing_rate_limit_admissions FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_code_snapshot_audits_no_update BEFORE UPDATE ON payment_code_snapshot_audits FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_code_snapshot_audits_no_delete BEFORE DELETE ON payment_code_snapshot_audits FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_receipts_no_update BEFORE UPDATE ON payment_receipts FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_receipts_no_delete BEFORE DELETE ON payment_receipts FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_refund_inbox_audits_no_update BEFORE UPDATE ON payment_refund_inbox_audits FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_refund_inbox_audits_no_delete BEFORE DELETE ON payment_refund_inbox_audits FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_refund_references_no_update BEFORE UPDATE ON payment_refund_references FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';

CREATE TRIGGER IF NOT EXISTS payment_refund_references_no_delete BEFORE DELETE ON payment_refund_references FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only';
