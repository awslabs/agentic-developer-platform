CREATE OR REPLACE FUNCTION adp_decision_payload_flags_v1(source text)
RETURNS integer LANGUAGE plpgsql IMMUTABLE PARALLEL UNSAFE AS $function$
DECLARE
    normalized text;
    invalid_integer boolean;
    payload json;
    attempt json;
    flags integer := 0;
BEGIN
    IF source IS NULL OR source = '' THEN source := '{}'; END IF;
    SELECT string_agg(
        CASE WHEN token[1] IS NOT NULL THEN (
            SELECT string_agg(
                CASE WHEN part[1] ~ $regex$^\\u(0000|[dD][89a-fA-F][0-9a-fA-F]{2})$$regex$
                    THEN E'\\ufffd' ELSE coalesce(part[1], part[2], part[3]) END,
                '' ORDER BY position)
            FROM regexp_matches(token[1], $regex$(\\(?:u[0-9a-fA-F]{4}|[\s\S]))|([^\\]+)|(\\)$regex$, 'g')
                WITH ORDINALITY AS escapes(part, position)
        ) WHEN token[2] IS NOT NULL THEN 'null' ELSE coalesce(token[3], token[4]) END,
        '' ORDER BY position),
        bool_or(token[3] IS NOT NULL AND token[3] !~ '[.eE]' AND length(ltrim(token[3], '-')) > 4300)
    INTO normalized, invalid_integer
    FROM regexp_matches(source,
        $regex$("(?:[^"\\]|\\.)*")|(-?Infinity|NaN)|(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)|([\s\S])$regex$, 'g')
        WITH ORDINALITY AS tokens(token, position);
    IF invalid_integer THEN RETURN 0; END IF;
    BEGIN
        payload := normalized::json;
        IF json_typeof(payload) <> 'object' THEN RETURN 0; END IF;
        flags := 1;
        IF payload -> 'reason' IS NOT NULL AND payload -> 'criterion_ids' IS NOT NULL
            AND payload -> 'plan_version' IS NOT NULL THEN flags := flags | 2; END IF;
        attempt := payload -> 'attempt';
        IF json_typeof(attempt) = 'boolean' OR
            (json_typeof(attempt) = 'number' AND attempt::text ~ '^-?[0-9]+$') THEN
            flags := flags | 4;
        END IF;
        IF json_typeof(payload -> 'contract') = 'string'
            AND payload ->> 'contract' = 'dispatch-admission-refusal/v1' THEN
            flags := flags | 8;
        END IF;
        RETURN flags;
    EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN
        RETURN 0;
    END;
END
$function$;

CREATE OR REPLACE FUNCTION adp_decision_category_v1(kind text, actor_id text,
    actor_kind text, reason text, rejection_reason text)
RETURNS integer LANGUAGE plpgsql IMMUTABLE PARALLEL UNSAFE AS $function$
BEGIN
    CASE
    WHEN kind IN ('node_stalled', 'node_halted') THEN RETURN 1;
    WHEN kind IN ('gate_approved', 'gate_rejected') THEN RETURN 4;
    WHEN kind = 'node_dispatched' AND (adp_decision_payload_flags_v1(reason) & 1) = 1 THEN RETURN 2;
    WHEN kind = 'result_observed' AND (adp_decision_payload_flags_v1(reason) & 1) = 1 THEN RETURN 3;
    WHEN kind = 'evaluation_waived' AND actor_kind = 'human'
        AND (adp_decision_payload_flags_v1(reason) & 2) = 2 THEN RETURN 5;
    WHEN kind = 'transition_rejected' AND actor_id = 'system:orchestration-dispatch' AND actor_kind = 'service'
        AND (adp_decision_payload_flags_v1(rejection_reason) & 8) = 8 THEN RETURN 6;
    WHEN kind = 'evaluation_evidence_wait' AND actor_id = 'system:orchestration-tick' AND actor_kind = 'service'
        AND (adp_decision_payload_flags_v1(reason) & 1) = 1 THEN RETURN 7;
    WHEN kind = 'developer_retry_checked' AND actor_id = 'system:developer-recovery' AND actor_kind = 'service'
        AND (adp_decision_payload_flags_v1(reason) & 1) = 1 THEN RETURN 8;
    ELSE RETURN NULL;
    END CASE;
END
$function$;

CREATE OR REPLACE FUNCTION adp_decision_clearer_v1(kind text, actor_id text, actor_kind text)
RETURNS integer LANGUAGE sql IMMUTABLE PARALLEL UNSAFE AS $function$
    SELECT CASE WHEN kind = 'node_dispatched' THEN 1
        WHEN kind = 'evaluation_evidence_ready' AND actor_id = 'system:orchestration-tick'
            AND actor_kind = 'service' THEN 2 ELSE NULL END;
$function$;
