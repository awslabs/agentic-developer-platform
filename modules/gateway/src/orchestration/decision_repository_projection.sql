-- V1 and its immutable expression indexes remain unchanged for rolling readers.
CREATE OR REPLACE FUNCTION adp_decision_category_v2(kind text, actor_id text,
    actor_kind text, reason text, rejection_reason text)
RETURNS integer LANGUAGE plpgsql IMMUTABLE PARALLEL UNSAFE AS $function$
DECLARE previous integer;
BEGIN
    IF kind = 'result_observed' AND actor_id = 'system:repository-evaluation' AND actor_kind = 'service'
        AND (adp_decision_payload_flags_v1(reason) & 1) = 1 THEN RETURN 10; END IF;
    previous := adp_decision_category_v1(kind, actor_id, actor_kind, reason, rejection_reason);
    IF previous IS NOT NULL THEN RETURN previous; END IF;
    IF kind = 'transition_rejected' AND actor_id = 'system:repository-evaluation' AND actor_kind = 'service'
        AND (adp_decision_payload_flags_v1(rejection_reason) & 1) = 1 THEN RETURN 9; END IF;
    RETURN NULL;
END
$function$;
