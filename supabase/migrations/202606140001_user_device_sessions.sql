create table if not exists public.user_device_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  device_id text not null,
  platform text,
  user_agent text,
  is_active boolean not null default true,
  active_since timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  revoked_at timestamptz,
  revoked_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_user_device_sessions_user_id
on public.user_device_sessions(user_id);

create index if not exists idx_user_device_sessions_user_active
on public.user_device_sessions(user_id, is_active);

create unique index if not exists uq_user_device_sessions_user_device
on public.user_device_sessions(user_id, device_id);

do $$
begin
  if to_regprocedure('public.set_updated_at()') is null then
    execute $fn$
      create function public.set_updated_at()
      returns trigger
      language plpgsql
      as $body$
      begin
        new.updated_at = now();
        return new;
      end;
      $body$;
    $fn$;
  end if;
end;
$$;

drop trigger if exists trg_user_device_sessions_updated_at on public.user_device_sessions;

create trigger trg_user_device_sessions_updated_at
before update on public.user_device_sessions
for each row
execute function public.set_updated_at();

do $$
begin
  if to_regprocedure('public.is_current_admin()') is null then
    execute $fn$
      create function public.is_current_admin()
      returns boolean
      language sql
      security definer
      set search_path = public
      as $body$
        select exists (
          select 1
          from public.profiles p
          where p.id = auth.uid()::text
            and (
              lower(coalesce(p.email, '')) = 'oguzozyigit@gmail.com'
              or coalesce(p.is_admin, false) = true
              or lower(coalesce(p.role, '')) in ('admin', 'superadmin')
            )
        );
      $body$;
    $fn$;
  end if;
end;
$$;

grant execute on function public.is_current_admin() to authenticated;

create or replace function public.register_active_device_session(
  p_device_id text,
  p_platform text default null,
  p_user_agent text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_user_id text := auth.uid()::text;
  v_device_id text := nullif(btrim(p_device_id), '');
  v_is_admin boolean := false;
begin
  if v_user_id is null then
    raise exception 'not_authenticated';
  end if;

  if v_device_id is null then
    raise exception 'missing_device_id';
  end if;

  v_is_admin := public.is_current_admin();

  if not v_is_admin then
    update public.user_device_sessions
    set
      is_active = false,
      revoked_at = now(),
      revoked_reason = 'new_device_login'
    where user_id = v_user_id
      and device_id <> v_device_id
      and is_active = true;
  end if;

  insert into public.user_device_sessions (
    user_id,
    device_id,
    platform,
    user_agent,
    is_active,
    active_since,
    last_seen_at,
    revoked_at,
    revoked_reason
  )
  values (
    v_user_id,
    v_device_id,
    nullif(btrim(p_platform), ''),
    nullif(btrim(p_user_agent), ''),
    true,
    now(),
    now(),
    null,
    null
  )
  on conflict (user_id, device_id)
  do update set
    platform = excluded.platform,
    user_agent = excluded.user_agent,
    is_active = true,
    active_since = case
      when public.user_device_sessions.is_active = true
        and public.user_device_sessions.revoked_at is null
      then public.user_device_sessions.active_since
      else now()
    end,
    last_seen_at = now(),
    revoked_at = null,
    revoked_reason = null;

  return jsonb_build_object(
    'active', true,
    'admin', v_is_admin
  );
end;
$$;

grant execute on function public.register_active_device_session(text, text, text) to authenticated;

create or replace function public.check_active_device_session(p_device_id text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_user_id text := auth.uid()::text;
  v_device_id text := nullif(btrim(p_device_id), '');
  v_is_admin boolean := false;
  v_is_active boolean := false;
begin
  if v_user_id is null then
    raise exception 'not_authenticated';
  end if;

  if v_device_id is null then
    raise exception 'missing_device_id';
  end if;

  v_is_admin := public.is_current_admin();

  if v_is_admin then
    update public.user_device_sessions
    set last_seen_at = now()
    where user_id = v_user_id
      and device_id = v_device_id;

    return jsonb_build_object(
      'active', true,
      'admin', true
    );
  end if;

  update public.user_device_sessions
  set last_seen_at = now()
  where user_id = v_user_id
    and device_id = v_device_id
    and is_active = true
    and revoked_at is null
  returning true into v_is_active;

  if coalesce(v_is_active, false) then
    return jsonb_build_object(
      'active', true,
      'admin', false
    );
  end if;

  return jsonb_build_object(
    'active', false,
    'admin', false,
    'reason', 'new_device_login'
  );
end;
$$;

grant execute on function public.check_active_device_session(text) to authenticated;

alter table public.user_device_sessions enable row level security;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'user_device_sessions'
      and policyname = 'user_device_sessions_select_own'
  ) then
    create policy user_device_sessions_select_own
    on public.user_device_sessions
    for select
    to authenticated
    using (user_id = auth.uid()::text);
  end if;

  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'user_device_sessions'
      and policyname = 'user_device_sessions_insert_own'
  ) then
    create policy user_device_sessions_insert_own
    on public.user_device_sessions
    for insert
    to authenticated
    with check (user_id = auth.uid()::text);
  end if;

  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'user_device_sessions'
      and policyname = 'user_device_sessions_update_own'
  ) then
    create policy user_device_sessions_update_own
    on public.user_device_sessions
    for update
    to authenticated
    using (user_id = auth.uid()::text)
    with check (user_id = auth.uid()::text);
  end if;

  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'user_device_sessions'
      and policyname = 'user_device_sessions_admin_select_all'
  ) then
    create policy user_device_sessions_admin_select_all
    on public.user_device_sessions
    for select
    to authenticated
    using (public.is_current_admin());
  end if;

  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'user_device_sessions'
      and policyname = 'user_device_sessions_admin_update_all'
  ) then
    create policy user_device_sessions_admin_update_all
    on public.user_device_sessions
    for update
    to authenticated
    using (public.is_current_admin())
    with check (public.is_current_admin());
  end if;
end;
$$;
