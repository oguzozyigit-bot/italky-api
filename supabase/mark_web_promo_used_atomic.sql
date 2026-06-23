-- Atomic web_promo_codes redemption lock.
-- Run once in Supabase SQL Editor (Dashboard -> SQL Editor -> New query).

CREATE OR REPLACE FUNCTION public.mark_web_promo_used_atomic(
    p_code_id    uuid,
    p_user_id    text,
    p_used_count integer,
    p_max_uses   integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_next       integer;
    v_new_status text;
BEGIN
    v_next := p_used_count + 1;

    v_new_status := CASE
        WHEN p_max_uses = 1
          OR (p_max_uses > 0 AND v_next >= p_max_uses)
        THEN 'used'
        ELSE 'active'
    END;

    UPDATE public.web_promo_codes
    SET
        used_count = v_next,
        status     = v_new_status
    WHERE id         = p_code_id
      AND status     = 'active'
      AND used_count = p_used_count
      AND (p_max_uses = 0 OR used_count < p_max_uses);

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'ok',     false,
            'reason', 'PROMO_ALREADY_USED'
        );
    END IF;

    RETURN jsonb_build_object(
        'ok',         true,
        'new_status', v_new_status,
        'used_count', v_next
    );

EXCEPTION WHEN OTHERS THEN
    RAISE;
END;
$$;

GRANT EXECUTE ON FUNCTION public.mark_web_promo_used_atomic(uuid, text, integer, integer)
    TO service_role;
