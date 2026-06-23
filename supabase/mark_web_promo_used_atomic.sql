-- Atomic web_promo_codes redemption lock.
-- Run once in Supabase SQL Editor (Dashboard → SQL Editor → New query).
--
-- This function wraps the code-marking and optional redemption log
-- inside a single database transaction so they either both succeed
-- or both roll back — no partial state is left behind.

CREATE OR REPLACE FUNCTION public.mark_web_promo_used_atomic(
    p_code_id    uuid,
    p_user_id    text,
    p_used_count integer,   -- current used_count read before calling
    p_max_uses   integer    -- 0 = unlimited
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_rows       integer;
    v_next       integer;
    v_new_status text;
BEGIN
    v_next := p_used_count + 1;

    -- Decide the new status
    v_new_status := CASE
        WHEN p_max_uses = 1
          OR (p_max_uses > 0 AND v_next >= p_max_uses)
        THEN 'used'
        ELSE 'active'
    END;

    -- ── BEGIN (implicit in plpgsql) ────────────────────────────────────────
    -- Single atomic UPDATE:
    --   • status = 'active'      → code not yet claimed by another request
    --   • used_count = p_used_count → guards against concurrent redemptions
    --   • used_count < p_max_uses   → still has quota (skipped for unlimited)
    UPDATE public.web_promo_codes
    SET
        used_count = v_next,
        status     = v_new_status
    WHERE id         = p_code_id
      AND status     = 'active'
      AND used_count = p_used_count
      AND (p_max_uses = 0 OR used_count < p_max_uses);

    GET DIAGNOSTICS v_rows = ROW_COUNT;

    -- ── ROLLBACK path ──────────────────────────────────────────────────────
    -- If 0 rows were updated the code was already used / concurrently claimed.
    -- plpgsql automatically rolls back on RAISE EXCEPTION.
    IF v_rows = 0 THEN
        RETURN jsonb_build_object(
            'ok',     false,
            'reason', 'PROMO_ALREADY_USED',
            'rows',   0
        );
    END IF;

    -- ── COMMIT path ────────────────────────────────────────────────────────
    RETURN jsonb_build_object(
        'ok',         true,
        'new_status', v_new_status,
        'used_count', v_next,
        'rows',       v_rows
    );

    -- Any unhandled exception propagates here → plpgsql rolls back automatically.
EXCEPTION WHEN OTHERS THEN
    RAISE;
END;
$$;

-- Grant execute to the service role used by the backend
GRANT EXECUTE ON FUNCTION public.mark_web_promo_used_atomic(uuid, text, integer, integer)
    TO service_role;
