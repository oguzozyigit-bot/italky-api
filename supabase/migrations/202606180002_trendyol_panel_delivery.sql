alter table public.promo_codes add column if not exists delivery_payload jsonb null;
alter table public.promo_codes add column if not exists invoice_status text null;
alter table public.promo_codes add column if not exists activated_at timestamptz null;
alter table public.promo_codes add column if not exists activated_by uuid null;
alter table public.promo_codes add column if not exists bound_user_id uuid null;

do $$
declare
  constraint_record record;
begin
  if to_regclass('public.marketplace_delivery_jobs') is null then
    return;
  end if;

  if not exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'marketplace_delivery_jobs'
      and column_name = 'status'
  ) then
    return;
  end if;

  for constraint_record in
    select conname
    from pg_constraint
    where conrelid = 'public.marketplace_delivery_jobs'::regclass
      and contype = 'c'
      and pg_get_constraintdef(oid) ilike '%status%'
  loop
    execute format('alter table public.marketplace_delivery_jobs drop constraint %I', constraint_record.conname);
  end loop;

  alter table public.marketplace_delivery_jobs
    add constraint marketplace_delivery_jobs_status_check
    check (
      status in (
        'pending',
        'processing',
        'delivered',
        'failed',
        'skipped',
        'cancelled',
        'sent',
        'manual_deliver_scheduled',
        'processing_manual_deliver',
        'manual_deliver_failed',
        'panel_manual_delivery_required'
      )
    );
end $$;
