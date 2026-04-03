-- FILE: supabase_sql/nfc_token_cards.sql

create table if not exists public.nfc_cards (
  id bigserial primary key,
  uid text unique not null,
  token_amount integer not null default 0,
  status text not null default 'active', -- active | used | blocked
  expire_at timestamptz null,
  assigned_user_id text null,
  redeemed_at timestamptz null,
  redeemed_by_user_id text null,
  note text null,
  created_at timestamptz not null default now()
);

create index if not exists idx_nfc_cards_uid on public.nfc_cards(uid);
create index if not exists idx_nfc_cards_status on public.nfc_cards(status);

create or replace function public.redeem_nfc_token_card(
  p_uid text,
  p_user_id text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_card record;
  v_tokens integer := 0;
  v_new_tokens integer := 0;
begin
  if trim(coalesce(p_uid, '')) = '' then
    return jsonb_build_object(
      'ok', false,
      'reason', 'UID_REQUIRED'
    );
  end if;

  if trim(coalesce(p_user_id, '')) = '' then
    return jsonb_build_object(
      'ok', false,
      'reason', 'USER_REQUIRED'
    );
  end if;

  select *
    into v_card
  from public.nfc_cards
  where uid = trim(p_uid)
  limit 1;

  if not found then
    return jsonb_build_object(
      'ok', false,
      'reason', 'CARD_NOT_FOUND'
    );
  end if;

  if v_card.status = 'blocked' then
    return jsonb_build_object(
      'ok', false,
      'reason', 'CARD_BLOCKED'
    );
  end if;

  if v_card.status = 'used' then
    return jsonb_build_object(
      'ok', false,
      'reason', 'CARD_ALREADY_USED'
    );
  end if;

  if v_card.expire_at is not null and v_card.expire_at <= now() then
    return jsonb_build_object(
      'ok', false,
      'reason', 'CARD_EXPIRED'
    );
  end if;

  if coalesce(v_card.token_amount, 0) <= 0 then
    return jsonb_build_object(
      'ok', false,
      'reason', 'INVALID_TOKEN_AMOUNT'
    );
  end if;

  select coalesce(tokens, 0)
    into v_tokens
  from public.profiles
  where id = p_user_id
  limit 1;

  v_new_tokens := v_tokens + v_card.token_amount;

  update public.profiles
  set tokens = v_new_tokens
  where id = p_user_id;

  update public.nfc_cards
  set
    status = 'used',
    redeemed_at = now(),
    redeemed_by_user_id = p_user_id,
    assigned_user_id = p_user_id
  where id = v_card.id;

  insert into public.wallet_tx (
    user_id,
    delta,
    reason,
    note,
    created_at
  ) values (
    p_user_id,
    v_card.token_amount,
    'nfc_token_load',
    'NFC token card: ' || v_card.uid,
    now()
  );

  return jsonb_build_object(
    'ok', true,
    'reason', 'SUCCESS',
    'uid', v_card.uid,
    'loaded_tokens', v_card.token_amount,
    'tokens_after', v_new_tokens
  );
end;
$$;

grant execute on function public.redeem_nfc_token_card(text, text)
to anon, authenticated, service_role;
