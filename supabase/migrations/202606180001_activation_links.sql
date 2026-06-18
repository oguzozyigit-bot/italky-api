create extension if not exists pgcrypto;

create table if not exists public.activation_links (
  id uuid primary key default gen_random_uuid(),
  token text not null unique,
  code_value text not null,
  marketplace text null,
  marketplace_order_number text null,
  marketplace_package_id bigint null,
  marketplace_line_id bigint null,
  marketplace_quantity_index integer null,
  marketplace_barcode text null,
  marketplace_stock_code text null,
  days integer null,
  created_at timestamptz not null default now(),
  clicked_at timestamptz null,
  used_at timestamptz null,
  expires_at timestamptz null,
  is_active boolean not null default true
);

create unique index if not exists activation_links_token_uidx on public.activation_links(token);
create index if not exists activation_links_code_value_idx on public.activation_links(code_value);
create index if not exists activation_links_marketplace_order_idx
  on public.activation_links(marketplace, marketplace_order_number, marketplace_package_id);
create index if not exists activation_links_active_expires_idx on public.activation_links(is_active, expires_at);

alter table public.activation_links enable row level security;

alter table public.promo_codes add column if not exists marketplace text null;
alter table public.promo_codes add column if not exists marketplace_order_number text null;
alter table public.promo_codes add column if not exists marketplace_package_id bigint null;
alter table public.promo_codes add column if not exists marketplace_line_id bigint null;
alter table public.promo_codes add column if not exists marketplace_quantity_index integer null;
alter table public.promo_codes add column if not exists marketplace_barcode text null;
alter table public.promo_codes add column if not exists marketplace_stock_code text null;
alter table public.promo_codes add column if not exists delivery_status text null;
alter table public.promo_codes add column if not exists reserved_at timestamptz null;
alter table public.promo_codes add column if not exists delivered_at timestamptz null;
alter table public.promo_codes add column if not exists delivery_attempt_count integer null;
alter table public.promo_codes add column if not exists delivery_error text null;
alter table public.promo_codes add column if not exists delivery_response jsonb null;

create index if not exists promo_codes_marketplace_order_idx
  on public.promo_codes(marketplace, marketplace_order_number, marketplace_package_id);
