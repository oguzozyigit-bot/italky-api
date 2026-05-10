-- Corporate promo code system for italkyAI
-- Apply in Supabase SQL Editor before using the admin corporate promo UI.
-- This intentionally uses corporate_promo_codes so the old promo_codes/QR/jeton schema is not disturbed.

create extension if not exists pgcrypto;

create table if not exists public.corporate_promo_codes (
  id uuid primary key default gen_random_uuid(),
  code text unique not null,
  company_name text not null,
  campaign_name text null,
  duration_months int not null,
  duration_days int not null,
  valid_until timestamptz not null,
  status text not null default 'active',
  activated_by uuid null,
  activated_email text null,
  activated_phone text null,
  phone_verified boolean not null default false,
  sms_consent boolean not null default false,
  email_consent boolean not null default false,
  consent_at timestamptz null,
  activated_at timestamptz null,
  membership_starts_at timestamptz null,
  membership_ends_at timestamptz null,
  created_by uuid null,
  created_at timestamptz not null default now(),
  note text null,
  constraint corporate_promo_codes_status_check check (status in ('active','activated','expired','cancelled')),
  constraint corporate_promo_codes_duration_check check (duration_months in (1,3,6,12) and duration_days > 0),
  constraint corporate_promo_codes_format_check check (
    code ~ '^[A-Z0-9]{8}$'
    and length(regexp_replace(code, '[^A-Z]', '', 'g')) = 2
    and length(regexp_replace(code, '[^0-9]', '', 'g')) = 6
    and regexp_replace(code, '[^A-Z]', '', 'g') not in ('AK','FG','FB','GS')
  )
);

create index if not exists corporate_promo_codes_company_idx on public.corporate_promo_codes(company_name);
create index if not exists corporate_promo_codes_campaign_idx on public.corporate_promo_codes(campaign_name);
create index if not exists corporate_promo_codes_status_idx on public.corporate_promo_codes(status);
create index if not exists corporate_promo_codes_activated_at_idx on public.corporate_promo_codes(activated_at);
create index if not exists corporate_promo_codes_membership_ends_idx on public.corporate_promo_codes(membership_ends_at);

create table if not exists public.promo_phone_otps (
  id uuid primary key default gen_random_uuid(),
  code text not null,
  phone text not null,
  otp_code text not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  verified_at timestamptz null
);

create index if not exists promo_phone_otps_lookup_idx on public.promo_phone_otps(code, phone, otp_code, verified_at);
create index if not exists promo_phone_otps_expires_idx on public.promo_phone_otps(expires_at);

alter table public.profiles add column if not exists package_active boolean not null default false;
alter table public.profiles add column if not exists package_started_at timestamptz null;
alter table public.profiles add column if not exists package_ends_at timestamptz null;
alter table public.profiles add column if not exists selected_package_code text null;
alter table public.profiles add column if not exists app_access_mode text null;
alter table public.profiles add column if not exists promo_used_at timestamptz null;
alter table public.profiles add column if not exists promo_code_used text null;

-- Optional RLS hardening. Service-role backend endpoints are expected to write these tables.
alter table public.corporate_promo_codes enable row level security;
alter table public.promo_phone_otps enable row level security;
