create or replace function public.charge_cultural_translation_tokens(
  p_user_id uuid,
  p_cost int,
  p_source_text text,
  p_target_lang text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cost int := greatest(0, coalesce(p_cost, 0));
  v_tokens_before bigint;
  v_tokens_after bigint;
  v_source_text text := coalesce(p_source_text, '');
  v_target_lang text := nullif(btrim(coalesce(p_target_lang, '')), '');
begin
  if p_user_id is null then
    raise exception 'user_id_required';
  end if;

  if v_cost <= 0 then
    return jsonb_build_object(
      'ok', true,
      'charged', false,
      'reason', 'zero_cost',
      'tokens_charged', 0
    );
  end if;

  update public.profiles
  set tokens = coalesce(tokens, 0) - v_cost
  where id::text = p_user_id::text
    and coalesce(tokens, 0) >= v_cost
  returning coalesce(tokens, 0) + v_cost, coalesce(tokens, 0)
  into v_tokens_before, v_tokens_after;

  if v_tokens_after is null then
    select coalesce(tokens, 0)
    into v_tokens_before
    from public.profiles
    where id::text = p_user_id::text
    limit 1;

    return jsonb_build_object(
      'ok', false,
      'reason', 'insufficient_tokens',
      'tokens_before', coalesce(v_tokens_before, 0),
      'tokens_after', coalesce(v_tokens_before, 0),
      'tokens_charged', 0,
      'required_tokens', v_cost
    );
  end if;

  insert into public.wallet_tx (
    user_id,
    type,
    amount,
    reason,
    meta
  )
  values (
    p_user_id,
    'usage_cultural_translate',
    -v_cost,
    'Kulturel ceviri kullanimi'
      || case when v_target_lang is not null then ' -> ' || v_target_lang else '' end,
    jsonb_build_object(
      'source_module', 'usage_cultural_translate',
      'usage_kind', 'text',
      'target_lang', v_target_lang,
      'source_text_length', char_length(v_source_text),
      'source_text_preview', left(v_source_text, 160),
      'tokens_before', v_tokens_before,
      'tokens_after', v_tokens_after,
      'tokens_charged', v_cost,
      'balance_before', v_tokens_before,
      'balance_after', v_tokens_after,
      'charge_type', 'cultural_translation_per_10_chars'
    )
  );

  return jsonb_build_object(
    'ok', true,
    'charged', true,
    'source_module', 'usage_cultural_translate',
    'tokens_before', v_tokens_before,
    'tokens_after', v_tokens_after,
    'tokens_charged', v_cost,
    'cost', v_cost
  );
end;
$$;

grant execute on function public.charge_cultural_translation_tokens(uuid, int, text, text) to authenticated;
grant execute on function public.charge_cultural_translation_tokens(uuid, int, text, text) to service_role;
