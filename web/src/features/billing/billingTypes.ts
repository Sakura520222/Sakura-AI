export interface Wallet {
  balance: string;
  reserved: string;
  available: string;
  low_balance_threshold: string;
  low_balance: boolean;
}
export interface BillingContext {
  payment_enabled: boolean;
  providers: Array<{ id: string; label: string }>;
  currencies: string[];
  currency_minor_exponents: Record<string, number>;
}
export interface OffsetPage<T> {
  items: T[];
  total?: number;
  offset: number;
  limit: number;
  has_more?: boolean;
}
export interface Transaction {
  id: number;
  kind: string;
  credits: string;
  feature: string | null;
  operation_id: string | null;
  created_at: string;
  source: Source;
}
export interface Operation {
  operation_id: string;
  feature: string;
  status: string;
  outcome: string | null;
  known_credits: string | null;
  reserved_credits: string;
  settled_credits: string;
  created_at: string;
  source: Source;
}
export interface Source {
  review_id?: number;
  issue_analysis_id?: number;
  agent_task_id?: number;
  scan_id?: number;
  order_id?: number;
  repo?: string;
  url?: string;
}
export interface Plan {
  id: number;
  name: string;
  plan_type: string;
  price_cents: number;
  currency: string;
  formatted_price: string | null;
  duration_days: number | null;
  credit_grant: string;
  rate_limits: Record<string, number> | null;
  concurrency_limit: number | null;
  is_active: boolean;
  description: string | null;
  sort_order: number;
  pr_quota_bonus: number;
  pr_daily_add: number;
  pr_weekly_add: number;
  pr_monthly_add: number;
  issue_quota_bonus: number;
  issue_daily_add: number;
  issue_weekly_add: number;
  issue_monthly_add: number;
  agent_quota_bonus: number;
  agent_daily_add: number;
  agent_weekly_add: number;
  agent_monthly_add: number;
}
export interface Order {
  id: number;
  order_no: string;
  plan_name: string | null;
  amount_cents: number;
  formatted_amount: string | null;
  formatted_refunded_amount: string | null;
  currency: string;
  status: string;
  payment_provider: string;
  checkout_url?: string;
  created_at: string;
  fulfilled_at: string | null;
}
export interface RefundRequest {
  id: number;
  order_id: number;
  order_no: string | null;
  user_id: number;
  status: string;
  reason: string | null;
  review_note: string | null;
  amount_cents: number;
  formatted_amount: string | null;
  currency: string;
  requested_at: string;
  reviewed_at: string | null;
}

/** Never round or parse Credits as binary floating point. */
export function exactCredits(value: string | null | undefined): string {
  if (value == null) return "—";
  if (!/^[+-]?\d+(?:\.\d+)?$/.test(value)) return value;
  return value.includes(".")
    ? value.replace(/0+$/, "").replace(/\.$/, "")
    : value;
}

export function parseIntegerInput(value: string, minimum = 0): number {
  if (!/^\d+$/.test(value)) throw new Error("invalid_integer");
  const result = Number(value);
  if (!Number.isSafeInteger(result) || result < minimum)
    throw new Error("invalid_integer");
  return result;
}

export function validateCreditInput(value: string, signed = false): string {
  const pattern = signed ? /^-?\d+(?:\.\d{1,6})?$/ : /^\d+(?:\.\d{1,6})?$/;
  if (!pattern.test(value.trim())) throw new Error("invalid_credits");
  return value.trim();
}

export function moneyToMinor(value: string, exponent: number): number {
  if (!Number.isInteger(exponent) || exponent < 0 || exponent > 6)
    throw new Error("unknown_currency");
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match || (match[2]?.length ?? 0) > exponent)
    throw new Error("invalid_money");
  const units =
    BigInt(match[1]) * 10n ** BigInt(exponent) +
    BigInt((match[2] ?? "").padEnd(exponent, "0") || "0");
  if (units > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("invalid_money");
  return Number(units);
}
