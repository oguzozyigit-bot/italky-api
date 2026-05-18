-- Activation codes are lightweight access credentials for code-only users.
-- Apply this in Supabase before enabling the Kod ile devam et production flow.

create extension if not exists pgcrypto;

create table if not exists public.activation_codes (
  id uuid primary key default gen_random_uuid(),
  code text unique not null,
  is_active boolean not null default true,
  starts_at timestamptz,
  expires_at timestamptz,
  activated_at timestamptz,
  active_session_key text,
  last_device_id text,
  last_user_agent text,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists activation_codes_code_idx on public.activation_codes (code);
create index if not exists activation_codes_active_session_key_idx on public.activation_codes (active_session_key);

alter table public.activation_codes enable row level security;

-- No public table policies are created intentionally. The API uses the service-role key,
-- and optional SECURITY DEFINER RPCs below expose only activation/check behavior.

drop function if exists public.normalize_activation_code(text);
create function public.normalize_activation_code(p_code text)
returns text
language sql
immutable
as $$
  select regexp_replace(upper(trim(coalesce(p_code, ''))), '[[:space:]-]+', '', 'g')
$$;

drop function if exists public.activate_code(text, text, text);
create function public.activate_code(
  p_code text,
  p_device_id text,
  p_user_agent text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_code text := public.normalize_activation_code(p_code);
  v_row public.activation_codes%rowtype;
  v_session_key text := gen_random_uuid()::text;
  v_now timestamptz := now();
begin
  if v_code = '' then
    return jsonb_build_object('ok', false, 'error', 'CODE_INVALID');
  end if;

  select * into v_row
  from public.activation_codes
  where code = v_code
  limit 1;

  if not found then
    return jsonb_build_object('ok', false, 'error', 'CODE_NOT_FOUND');
  end if;

  if v_row.is_active is not true then
    return jsonb_build_object('ok', false, 'error', 'CODE_INACTIVE');
  end if;

  if v_row.starts_at is not null and v_row.starts_at > v_now then
    return jsonb_build_object('ok', false, 'error', 'CODE_NOT_STARTED');
  end if;

  if v_row.expires_at is not null and v_row.expires_at < v_now then
    return jsonb_build_object('ok', false, 'error', 'CODE_EXPIRED');
  end if;

  update public.activation_codes
  set active_session_key = v_session_key,
      last_device_id = nullif(trim(coalesce(p_device_id, '')), ''),
      last_user_agent = left(coalesce(p_user_agent, ''), 600),
      activated_at = v_now,
      last_seen_at = v_now,
      updated_at = v_now
  where id = v_row.id;

  return jsonb_build_object(
    'ok', true,
    'access', true,
    'code', v_code,
    'active_session_key', v_session_key,
    'expires_at', v_row.expires_at
  );
end;
$$;

drop function if exists public.check_code_session(text, text, text);
create function public.check_code_session(
  p_code text,
  p_active_session_key text,
  p_device_id text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_code text := public.normalize_activation_code(p_code);
  v_row public.activation_codes%rowtype;
  v_now timestamptz := now();
begin
  select * into v_row
  from public.activation_codes
  where code = v_code
  limit 1;

  if not found then
    return jsonb_build_object('ok', true, 'active', false, 'reason', 'code_not_found');
  end if;

  if v_row.is_active is not true then
    return jsonb_build_object('ok', true, 'active', false, 'reason', 'code_inactive');
  end if;

  if v_row.starts_at is not null and v_row.starts_at > v_now then
    return jsonb_build_object('ok', true, 'active', false, 'reason', 'code_not_started');
  end if;

  if v_row.expires_at is not null and v_row.expires_at < v_now then
    return jsonb_build_object('ok', true, 'active', false, 'reason', 'code_expired');
  end if;

  if coalesce(v_row.active_session_key, '') <> coalesce(p_active_session_key, '') then
    return jsonb_build_object('ok', true, 'active', false, 'reason', 'session_replaced');
  end if;

  update public.activation_codes
  set last_seen_at = v_now,
      last_device_id = nullif(trim(coalesce(p_device_id, '')), ''),
      updated_at = v_now
  where id = v_row.id;

  return jsonb_build_object('ok', true, 'active', true);
end;
$$;

revoke all on table public.activation_codes from anon, authenticated;
grant execute on function public.activate_code(text, text, text) to anon, authenticated;
grant execute on function public.check_code_session(text, text, text) to anon, authenticated;
