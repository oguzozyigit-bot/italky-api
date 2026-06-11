create table if not exists store_purchases (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  platform text not null check (platform in ('ios', 'android')),
  product_id text not null,
  transaction_id text null,
  original_transaction_id text null,
  purchase_token text null,
  order_id text null,
  purchase_time timestamptz null,
  granted_days integer not null default 0,
  entitlement_start timestamptz null,
  entitlement_end timestamptz null,
  status text not null default 'active' check (
    status in ('active', 'refunded', 'voided', 'revoked', 'cancelled', 'expired', 'pending', 'failed')
  ),
  refund_time timestamptz null,
  voided_time timestamptz null,
  revoke_reason text null,
  raw_payload jsonb null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists store_purchases_android_purchase_token_uq
  on store_purchases (platform, purchase_token)
  where platform = 'android' and purchase_token is not null;

create unique index if not exists store_purchases_ios_transaction_id_uq
  on store_purchases (platform, transaction_id)
  where platform = 'ios' and transaction_id is not null;

create unique index if not exists store_purchases_order_id_uq
  on store_purchases (platform, order_id)
  where order_id is not null;

create table if not exists purchase_audit_logs (
  id uuid primary key default gen_random_uuid(),
  purchase_id uuid null references store_purchases(id),
  user_id uuid null,
  platform text null,
  action text not null,
  reason text null,
  old_status text null,
  new_status text null,
  old_entitlement_end timestamptz null,
  new_entitlement_end timestamptz null,
  raw_payload jsonb null,
  created_at timestamptz not null default now()
);
