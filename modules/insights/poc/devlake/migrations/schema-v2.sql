CREATE SCHEMA IF NOT EXISTS devlake_meta;
REVOKE ALL ON SCHEMA devlake_meta FROM PUBLIC;
SELECT pg_advisory_xact_lock(hashtextextended(:'cell_id' || ':' || :'db_resource_id', 0));

CREATE TABLE IF NOT EXISTS devlake_meta.schema_receipts (
    cell_id text NOT NULL,
    db_resource_id text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version = 2),
    manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('applied', 'retired')),
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cell_id, db_resource_id, schema_version)
);
CREATE OR REPLACE FUNCTION devlake_meta.require_compatible_schema(
    expected_cell text, expected_resource text, expected_digest char(64)
) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, devlake_meta AS $function$
BEGIN
    IF EXISTS (SELECT 1 FROM devlake_meta.schema_receipts
        WHERE cell_id <> expected_cell OR db_resource_id <> expected_resource
           OR manifest_sha256 NOT IN (expected_digest, 'fb8dc9cce5f944bb0b4e089c0363bb4ef3c476081e248c2d85ca7160740c5424',
                 '7d540b58c73101396f3d7035060d87345ec8e780f2d7a50551a3e4fc1010fdb1',
                 '8425fcbb6dc3994b35cea6ca8ac2a1dd15b141f2695df460361022f90285a846',
                 '39b8bd6176045459b68132f13e78e1b8f2dda12b8ec7d116c0689936f50daa56',
                 '3f8cc17bfe276a19061bcccb576875ae0db3999f6eea5dd7ef1daf4d9b698dbf',
                 '99db0aefb66490a76ce11d55ddc531c5b190b51c9ad65c773281584585df47f3',
                 'db902ed429de0cd914efe352bb192bab72bcb410b6441fef8a8e207d7554b8d6',
                 '063c5665b420f80b123d8580fc56fe7ca2d30b85454feb6053b3f65e8c5878ff')
           OR status <> 'applied') THEN
        RAISE EXCEPTION 'incompatible schema receipt' USING ERRCODE = '55000';
    END IF;
END;
$function$;
SELECT devlake_meta.require_compatible_schema(:'cell_id', :'db_resource_id', :'manifest_sha256');

CREATE TABLE IF NOT EXISTS devlake_meta.collection_generations (
    cell_id text PRIMARY KEY,
    generation bigint NOT NULL CHECK (generation > 0),
    tenant_id text NOT NULL CHECK (tenant_id <> ''),
    project_id text NOT NULL CHECK (project_id <> ''),
    operation_id text NOT NULL CHECK (operation_id <> ''),
    attempt_id text NOT NULL CHECK (attempt_id <> ''),
    state text NOT NULL CHECK (state IN ('collecting', 'sealed', 'cancelled')),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS devlake_meta.native_connections (
    native_connection_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cell_id text NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    tool text NOT NULL,
    source_connection_id text NOT NULL,
    UNIQUE (cell_id, tenant_id, project_id, tool, source_connection_id),
    UNIQUE (cell_id, native_connection_id)
);
CREATE OR REPLACE FUNCTION devlake_meta.native_connection(
    requested_cell text, requested_tenant text, requested_project text,
    requested_tool text, requested_connection text, requested_generation bigint
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, devlake_meta AS $function$
DECLARE assigned bigint;
BEGIN
    PERFORM 1 FROM devlake_meta.collection_generations
    WHERE cell_id = requested_cell AND tenant_id = requested_tenant AND
        project_id = requested_project AND generation = requested_generation AND
        state = 'collecting' FOR UPDATE;
    IF NOT FOUND OR requested_tool NOT IN
        ('sonarqube', 'jenkins', 'argocd', 'gitlab', 'pagerduty', 'linear', 'slack') OR
        coalesce(requested_connection, '') = '' THEN
        RAISE EXCEPTION 'native connection scope unavailable' USING ERRCODE = '42501';
    END IF;
    INSERT INTO devlake_meta.native_connections
        (cell_id, tenant_id, project_id, tool, source_connection_id)
    VALUES (requested_cell, requested_tenant, requested_project, requested_tool,
        jsonb_build_array(requested_connection, requested_generation)::text)
    ON CONFLICT (cell_id, tenant_id, project_id, tool, source_connection_id)
    DO UPDATE SET source_connection_id = EXCLUDED.source_connection_id
    RETURNING native_connection_id INTO assigned;
    RETURN assigned;
END;
$function$;
CREATE TABLE IF NOT EXISTS devlake_meta.collection_batch_manifests (
    batch_id uuid PRIMARY KEY,
    cell_id text NOT NULL,
    generation bigint NOT NULL,
    item_count bigint NOT NULL CHECK (item_count BETWEEN 0 AND 100000),
    manifest_payload bytea NOT NULL,
    content_digest bytea NOT NULL CHECK (octet_length(content_digest) = 32),
    checkpoint_updates jsonb NOT NULL,
    sealed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (cell_id, generation)
);
ALTER TABLE devlake_meta.collection_batch_manifests DROP CONSTRAINT IF EXISTS collection_batch_manifests_item_count_check;
ALTER TABLE devlake_meta.collection_batch_manifests ADD CONSTRAINT collection_batch_manifests_item_count_check CHECK (item_count BETWEEN 0 AND 100000);
CREATE TABLE IF NOT EXISTS devlake_meta.collection_batch_items (
    batch_id uuid NOT NULL REFERENCES devlake_meta.collection_batch_manifests(batch_id),
    item_key text NOT NULL,
    item_payload bytea NOT NULL,
    native_payload bytea NOT NULL,
    normalized_payload bytea NOT NULL,
    payload_digest bytea NOT NULL CHECK (octet_length(payload_digest) = 32),
    PRIMARY KEY (batch_id, item_key)
);
CREATE TABLE IF NOT EXISTS devlake_meta.collection_checkpoints (
    cell_id text NOT NULL,
    source_key text NOT NULL,
    generation bigint NOT NULL,
    cursor_value text NOT NULL,
    batch_id uuid NOT NULL REFERENCES devlake_meta.collection_batch_manifests(batch_id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cell_id, source_key)
);

-- Writer APIs never accept SQL, table names, role names or a destination.
CREATE OR REPLACE FUNCTION devlake_meta.begin_collection(
    requested_cell text, expected_generation bigint, requested_tenant text,
    requested_project text, requested_operation text, requested_attempt text
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, devlake_meta AS $function$
DECLARE current_row devlake_meta.collection_generations;
BEGIN
    PERFORM 1 FROM devlake_meta.schema_receipts WHERE cell_id = requested_cell AND status = 'applied' FOR UPDATE;
    IF NOT FOUND OR expected_generation IS NULL OR expected_generation < 0 OR
       coalesce(requested_tenant, '') = '' OR coalesce(requested_project, '') = '' OR
       coalesce(requested_operation, '') = '' OR coalesce(requested_attempt, '') = '' THEN
        RAISE EXCEPTION 'collection admission invalid' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO current_row FROM devlake_meta.collection_generations WHERE cell_id = requested_cell FOR UPDATE;
    IF FOUND THEN
        IF (current_row.tenant_id, current_row.project_id) <> (requested_tenant, requested_project) THEN
            RAISE EXCEPTION 'collection scope mismatch' USING ERRCODE = '42501';
        END IF;
        IF (current_row.operation_id, current_row.attempt_id) = (requested_operation, requested_attempt) THEN
            IF current_row.state <> 'collecting' OR current_row.generation <> expected_generation + 1 THEN
                RAISE EXCEPTION 'collection attempt is retired' USING ERRCODE = '42501';
            END IF;
            RETURN current_row.generation;
        END IF;
        IF current_row.generation <> expected_generation THEN
            RAISE EXCEPTION 'collection generation changed' USING ERRCODE = '40001';
        END IF;
    ELSIF expected_generation <> 0 THEN
        RAISE EXCEPTION 'collection generation missing' USING ERRCODE = '42501';
    END IF;
    INSERT INTO devlake_meta.collection_generations VALUES (
        requested_cell, expected_generation + 1, requested_tenant, requested_project,
        requested_operation, requested_attempt, 'collecting', now()
    ) ON CONFLICT (cell_id) DO UPDATE SET generation = EXCLUDED.generation,
        operation_id = EXCLUDED.operation_id, attempt_id = EXCLUDED.attempt_id,
        state = 'collecting', updated_at = now();
    RETURN expected_generation + 1;
END;
$function$;

CREATE OR REPLACE FUNCTION devlake_meta.lock_generation(requested_cell text, requested_generation bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, devlake_meta AS $function$
BEGIN
    PERFORM 1 FROM devlake_meta.collection_generations
    WHERE cell_id = requested_cell AND generation = requested_generation AND state = 'collecting' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'collection generation is inactive' USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('devlake.cell_id', requested_cell, true);
    PERFORM set_config('devlake.generation', requested_generation::text, true);
END;
$function$;

CREATE OR REPLACE FUNCTION devlake_meta.require_current_generation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, devlake_meta AS $function$
DECLARE
    requested_cell text := current_setting('devlake.cell_id', true);
    requested_generation text := current_setting('devlake.generation', true);
BEGIN
    IF coalesce(requested_cell, '') = '' OR coalesce(requested_generation, '') !~ '^[1-9][0-9]*$' THEN
        RAISE EXCEPTION 'collection generation is missing' USING ERRCODE = '42501';
    END IF;
    PERFORM devlake_meta.lock_generation(requested_cell, requested_generation::bigint);
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION devlake_meta.cancel_collection(requested_cell text, requested_generation bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, devlake_meta AS $function$
BEGIN
    PERFORM devlake_meta.lock_generation(requested_cell, requested_generation);
    UPDATE devlake_meta.collection_generations SET state = 'cancelled', updated_at = now() WHERE cell_id = requested_cell;
END;
$function$;

-- Payloads and manifest are exact bytes of S03's Go BatchItem/manifest JSON.
-- Parse for validation, but never re-encode the bytes which the exporter reads.
CREATE OR REPLACE FUNCTION devlake_meta.seal_batch(
    requested_cell text, requested_generation bigint, requested_batch uuid,
    item_payloads bytea[], supplied_manifest bytea, checkpoints jsonb
) RETURNS bytea LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, devlake_meta AS $function$
DECLARE
    active devlake_meta.collection_generations;
    prior devlake_meta.collection_batch_manifests;
    manifest jsonb;
    doc jsonb;
    item bytea;
    key jsonb;
    link jsonb;
    checkpoint jsonb;
    supplied_source_key text;
    source_identity jsonb;
    previous_cursor text;
    total_bytes bigint := 0;
    position integer := 0;
    digest bytea := sha256(supplied_manifest);
BEGIN
    SELECT * INTO active FROM devlake_meta.collection_generations WHERE cell_id = requested_cell FOR UPDATE;
    IF NOT FOUND OR active.generation <> requested_generation OR active.state = 'cancelled' THEN
        RAISE EXCEPTION 'collection generation is inactive' USING ERRCODE = '42501';
    END IF;
    IF requested_batch IS NULL OR item_payloads IS NULL OR
       (cardinality(item_payloads) > 0 AND (array_ndims(item_payloads) <> 1 OR array_lower(item_payloads, 1) <> 1)) OR
       cardinality(item_payloads) NOT BETWEEN 0 AND 100000 OR
       supplied_manifest IS NULL OR octet_length(supplied_manifest) > 104857600 OR
       checkpoints IS NULL OR jsonb_typeof(checkpoints) <> 'array' OR jsonb_array_length(checkpoints) > 100 THEN
        RAISE EXCEPTION 'invalid bounded batch';
    END IF;
    manifest := convert_from(supplied_manifest, 'UTF8')::jsonb;
    IF (manifest->>'BatchID', manifest->>'CellID', manifest->>'Generation') IS DISTINCT FROM
       (requested_batch::text, requested_cell, requested_generation::text) OR
       jsonb_typeof(manifest->'ItemDigests') IS DISTINCT FROM 'array' OR
       jsonb_array_length(manifest->'ItemDigests') <> cardinality(item_payloads) THEN
        RAISE EXCEPTION 'invalid batch manifest';
    END IF;
    IF manifest ? 'SourceKeys' THEN
        IF jsonb_typeof(manifest->'SourceKeys') IS DISTINCT FROM 'array' OR
           jsonb_array_length(manifest->'SourceKeys') NOT BETWEEN 1 AND 100 OR
           (SELECT count(DISTINCT value) FROM jsonb_array_elements(manifest->'SourceKeys')) <> jsonb_array_length(manifest->'SourceKeys') OR
           jsonb_array_length(checkpoints) <> jsonb_array_length(manifest->'SourceKeys') THEN
            RAISE EXCEPTION 'invalid batch source coverage';
        END IF;
        FOR supplied_source_key IN SELECT jsonb_array_elements_text(manifest->'SourceKeys') LOOP
            source_identity := supplied_source_key::jsonb;
            IF (source_identity->>'CellID', source_identity->>'TenantID', source_identity->>'ProjectID') IS DISTINCT FROM
               (requested_cell, active.tenant_id, active.project_id) OR
               coalesce(source_identity->>'Tool', '') NOT IN ('github', 'jira', 'sonarqube', 'jenkins', 'argocd', 'gitlab', 'pagerduty', 'linear', 'slack') OR
               coalesce(source_identity->>'ConnectionID', '') = '' OR coalesce(source_identity->>'Scope', '') = '' OR
               NOT EXISTS (SELECT 1 FROM jsonb_array_elements(checkpoints) WHERE value->>'source_key' = supplied_source_key) THEN
                RAISE EXCEPTION 'invalid batch source coverage';
            END IF;
        END LOOP;
    ELSIF cardinality(item_payloads) = 0 THEN
        RAISE EXCEPTION 'empty batch requires source coverage';
    END IF;
    SELECT * INTO prior FROM devlake_meta.collection_batch_manifests WHERE batch_id = requested_batch;
    IF FOUND THEN
        IF prior.cell_id <> requested_cell OR prior.generation <> requested_generation OR
           prior.manifest_payload <> supplied_manifest OR prior.checkpoint_updates <> checkpoints THEN
            RAISE EXCEPTION 'sealed batch conflict';
        END IF;
    ELSIF active.state <> 'collecting' THEN
        RAISE EXCEPTION 'collection generation is inactive' USING ERRCODE = '42501';
    END IF;
    FOREACH item IN ARRAY item_payloads LOOP
        total_bytes := total_bytes + coalesce(octet_length(item), 0);
        IF item IS NULL OR total_bytes > 104857600 THEN RAISE EXCEPTION 'batch size limit'; END IF;
        doc := convert_from(item, 'UTF8')::jsonb;
        key := doc->'Key';
        IF jsonb_typeof(key) IS DISTINCT FROM 'object' OR
           (key->>'CellID', key->>'TenantID', key->>'ProjectID') IS DISTINCT FROM
           (requested_cell, active.tenant_id, active.project_id) OR
           coalesce(key->>'Tool', '') NOT IN ('github', 'jira', 'sonarqube', 'jenkins', 'argocd', 'gitlab', 'pagerduty', 'linear', 'slack') OR
           coalesce(key->>'ConnectionID', '') = '' OR coalesce(key->>'Scope', '') = '' OR
           coalesce(key->>'Kind', '') = '' OR coalesce(key->>'NativeID', '') = '' OR
           coalesce(doc->>'PolicyVersion', '') = '' OR
           coalesce(doc->>'FieldMapVersion', '') = '' OR coalesce(doc->>'SchemaVersion', '') = '' OR
           manifest->'ItemDigests'->position->'Key' IS DISTINCT FROM key OR
           manifest->'ItemDigests'->position->>'SHA256' IS DISTINCT FROM encode(sha256(item), 'hex') THEN
            RAISE EXCEPTION 'batch item identity or digest mismatch';
        END IF;
        IF manifest ? 'SourceKeys' AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(manifest->'SourceKeys') AS source(value)
            WHERE source.value::jsonb = key - 'Kind' - 'NativeID'
        ) THEN RAISE EXCEPTION 'item outside batch source coverage'; END IF;
        IF coalesce(key->>'Tool', '') IN ('sonarqube', 'jenkins', 'argocd', 'gitlab', 'pagerduty', 'linear', 'slack') AND
           coalesce(doc->>'RemovalReason', '') = '' THEN
            IF NOT (manifest ? 'SourceKeys' AND manifest ? 'SourceBindings') OR
               jsonb_typeof(doc->'RawDataOrigin') IS DISTINCT FROM 'object' OR
               coalesce(doc->>'NativeConnectionID', '') = '' OR
               left(coalesce(doc->'RawDataOrigin'->>'_raw_data_table', ''), length('_raw_' || (key->>'Tool') || '_')) <> '_raw_' || (key->>'Tool') || '_' OR
               coalesce((doc->'RawDataOrigin'->>'_raw_data_id')::bigint, 0) <= 0 OR
               coalesce(doc->'RawDataOrigin'->>'_raw_data_params', '') = '' OR
               NOT EXISTS (
                   SELECT 1 FROM devlake_meta.native_connections AS mapping
                   WHERE mapping.cell_id = requested_cell AND mapping.tenant_id = active.tenant_id AND
                         mapping.project_id = active.project_id AND mapping.tool = key->>'Tool' AND
                         mapping.source_connection_id = jsonb_build_array(key->>'ConnectionID', requested_generation)::text AND
                         mapping.native_connection_id::text = doc->>'NativeConnectionID' AND
                         mapping.native_connection_id::text = coalesce(
                             (doc->'RawDataOrigin'->>'_raw_data_params')::jsonb->>'connectionId',
                             (doc->'RawDataOrigin'->>'_raw_data_params')::jsonb->>'ConnectionId')
               ) OR
               (doc->'Links' IS NOT NULL AND doc->'Links' NOT IN ('null'::jsonb, '[]'::jsonb)) THEN
                RAISE EXCEPTION 'expanded native record requires scoped raw origin without inferred links';
            END IF;
        END IF;
        IF coalesce(doc->>'RemovalReason', '') <> '' THEN
            IF doc->>'RemovalReason' NOT IN ('policy_excluded', 'absent_in_complete_source') OR
               doc->'RawDataOrigin' IS NOT NULL OR
               coalesce(doc->>'Revision', '') <> '' OR
               coalesce(octet_length(decode(doc->>'NativeBytes', 'base64')), 0) <> 0 OR
               coalesce(octet_length(decode(doc->>'NormalizedBytes', 'base64')), 0) <> 0 OR
               (doc->'Links' IS NOT NULL AND doc->'Links' NOT IN ('null'::jsonb, '[]'::jsonb)) THEN
                RAISE EXCEPTION 'removal cannot contain native payload or links';
            END IF;
        ELSIF coalesce(doc->>'Revision', '') = '' OR
              coalesce(octet_length(decode(doc->>'NativeBytes', 'base64')), 0) = 0 OR
              coalesce(octet_length(decode(doc->>'NormalizedBytes', 'base64')), 0) = 0 THEN
            RAISE EXCEPTION 'native record requires payload and revision';
        END IF;
        IF doc->'Links' IS NOT NULL AND doc->'Links' <> 'null'::jsonb THEN
            IF jsonb_typeof(doc->'Links') <> 'array' THEN RAISE EXCEPTION 'invalid links'; END IF;
            IF (SELECT count(DISTINCT value) FROM jsonb_array_elements(doc->'Links')) <> jsonb_array_length(doc->'Links') THEN
                RAISE EXCEPTION 'duplicate evidence link';
            END IF;
            FOR link IN SELECT value FROM jsonb_array_elements(doc->'Links') LOOP
                IF link->'From' IS DISTINCT FROM key OR
                   (link->'To'->>'CellID', link->'To'->>'TenantID', link->'To'->>'ProjectID') IS DISTINCT FROM
                   (requested_cell, active.tenant_id, active.project_id) OR
                   coalesce(link->'To'->>'Tool', '') NOT IN ('github', 'jira') OR
                   coalesce(link->'To'->>'ConnectionID', '') = '' OR coalesce(link->'To'->>'Scope', '') = '' OR
                   coalesce(link->'To'->>'Kind', '') = '' OR coalesce(link->'To'->>'NativeID', '') = '' OR
                   coalesce(link->>'Relation', '') <> 'source-reference' OR
                   coalesce(link->>'NativeEvidenceID', '') = '' THEN
                    RAISE EXCEPTION 'invalid cross-scope evidence link';
                END IF;
            END LOOP;
        END IF;
        position := position + 1;
    END LOOP;
    -- Replay also validates every supplied payload and detects missing/duplicate keys.
    IF (SELECT count(DISTINCT (convert_from(p, 'UTF8')::jsonb->'Key')::text) FROM unnest(item_payloads) AS p) <> cardinality(item_payloads) THEN
        RAISE EXCEPTION 'duplicate batch identity';
    END IF;
    IF prior.batch_id IS NOT NULL THEN RETURN prior.content_digest; END IF;
    INSERT INTO devlake_meta.collection_batch_manifests VALUES (
        requested_batch, requested_cell, requested_generation, cardinality(item_payloads), supplied_manifest, digest, checkpoints, now()
    );
    FOREACH item IN ARRAY item_payloads LOOP
        doc := convert_from(item, 'UTF8')::jsonb;
        INSERT INTO devlake_meta.collection_batch_items VALUES (
            requested_batch, (doc->'Key')::text, item, coalesce(decode(doc->>'NativeBytes', 'base64'), ''::bytea),
            coalesce(decode(doc->>'NormalizedBytes', 'base64'), ''::bytea), sha256(item)
        );
    END LOOP;
    IF (SELECT count(DISTINCT value->>'source_key') FROM jsonb_array_elements(checkpoints)) <> jsonb_array_length(checkpoints) THEN
        RAISE EXCEPTION 'duplicate checkpoint';
    END IF;
    FOR checkpoint IN SELECT value FROM jsonb_array_elements(checkpoints) LOOP
        IF coalesce(checkpoint->>'source_key', '') = '' OR coalesce(checkpoint->>'next_cursor', '') = '' OR
           NOT checkpoint ? 'expected_cursor' THEN RAISE EXCEPTION 'invalid checkpoint'; END IF;
        SELECT cursor_value INTO previous_cursor FROM devlake_meta.collection_checkpoints
        WHERE cell_id = requested_cell AND source_key = checkpoint->>'source_key';
        IF previous_cursor IS DISTINCT FROM checkpoint->>'expected_cursor' THEN
            RAISE EXCEPTION 'checkpoint changed' USING ERRCODE = '40001';
        END IF;
        INSERT INTO devlake_meta.collection_checkpoints VALUES (
            requested_cell, checkpoint->>'source_key', requested_generation, checkpoint->>'next_cursor', requested_batch, now()
        ) ON CONFLICT (cell_id, source_key) DO UPDATE SET generation = EXCLUDED.generation,
            cursor_value = EXCLUDED.cursor_value, batch_id = EXCLUDED.batch_id, updated_at = now();
    END LOOP;
    UPDATE devlake_meta.collection_generations SET state = 'sealed', updated_at = now() WHERE cell_id = requested_cell;
    RETURN digest;
END;
$function$;
REVOKE ALL ON ALL TABLES IN SCHEMA devlake_meta FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA devlake_meta FROM PUBLIC;

-- BEGIN PINNED DEVLAKE MODELS (generated by schema-models/main.go)
CREATE TABLE IF NOT EXISTS public._raw_github_scopes (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_jira_scopes (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_github_api_issues (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_github_api_pull_requests (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_jira_api_boards (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_jira_api_issues (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_slack_channel (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_slack_channel_message (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_sonarqube_api_projects (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_sonarqube_api_issues (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_jenkins_api_jobs (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_jenkins_api_builds (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_argocd_api_applications (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_argocd_api_sync_operations (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_gitlab_api_pipeline_details (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_pagerduty_services (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_pagerduty_incidents (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_linear_teams (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._raw_linear_issues (
    "id" bigserial,
    "params" varchar(255),
    "data" bytea,
    "url" text,
    "input" json,
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public._tool_github_repos (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigserial,
    "scope_config_id" bigint,
    "github_id" bigint,
    "name" varchar(255),
    "full_name" varchar(255),
    "html_url" varchar(255),
    "description" text,
    "owner_id" bigint,
    "language" varchar(255),
    "parent_github_id" bigint,
    "parent_html_url" text,
    "clone_url" varchar(255),
    "created_date" timestamptz,
    "updated_date" timestamptz,
    PRIMARY KEY ("connection_id", "github_id")
);

CREATE TABLE IF NOT EXISTS public._tool_jira_boards (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigserial,
    "scope_config_id" bigint,
    "board_id" bigint,
    "project_id" bigint,
    "name" varchar(255),
    "self" varchar(255),
    "type" varchar(100),
    "jql" text,
    "sub_query" text,
    PRIMARY KEY ("connection_id", "board_id")
);

CREATE TABLE IF NOT EXISTS public._tool_github_issues (
    "connection_id" bigint,
    "github_id" bigint,
    "repo_id" bigint,
    "number" bigint,
    "state" varchar(255),
    "title" text,
    "body" text,
    "priority" varchar(255),
    "type" varchar(500),
    "std_type" varchar(100),
    "author_id" bigint,
    "author_name" varchar(255),
    "assignee_id" bigint,
    "assignee_name" varchar(255),
    "milestone_id" bigint,
    "lead_time_minutes" bigint,
    "url" varchar(255),
    "closed_at" timestamptz,
    "github_created_at" timestamptz,
    "github_updated_at" timestamptz,
    "severity" varchar(255),
    "component" text,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "github_id")
);

CREATE TABLE IF NOT EXISTS public._tool_github_pull_requests (
    "connection_id" bigint,
    "github_id" bigint,
    "repo_id" bigint,
    "head_repo_id" bigint,
    "number" bigint,
    "state" varchar(255),
    "title" text,
    "github_created_at" timestamptz,
    "github_updated_at" timestamptz,
    "closed_at" timestamptz,
    "additions" bigint,
    "deletions" bigint,
    "comments" bigint,
    "commits" bigint,
    "review_comments" bigint,
    "is_draft" boolean,
    "merged" boolean,
    "merged_at" timestamptz,
    "body" text,
    "type" varchar(255),
    "component" varchar(255),
    "merge_commit_sha" varchar(40),
    "head_ref" text,
    "base_ref" text,
    "base_commit_sha" varchar(255),
    "head_commit_sha" varchar(255),
    "url" varchar(255),
    "author_name" text,
    "author_id" bigint,
    "merged_by_name" text,
    "merged_by_id" bigint,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "github_id")
);

CREATE TABLE IF NOT EXISTS public._tool_jira_issues (
    "connection_id" bigint,
    "issue_id" bigint,
    "project_id" bigint,
    "project_name" varchar(255),
    "self" varchar(255),
    "icon_url" varchar(255),
    "issue_key" varchar(255),
    "summary" text,
    "description" text,
    "type" varchar(255),
    "epic_key" varchar(255),
    "status_name" varchar(255),
    "status_key" varchar(255),
    "story_point" decimal,
    "original_estimate_minutes" bigint,
    "aggregate_estimate_minutes" bigint,
    "remaining_estimate_minutes" bigint,
    "creator_account_id" varchar(255),
    "creator_account_type" varchar(255),
    "creator_display_name" varchar(255),
    "assignee_account_id" varchar(255),
    "assignee_account_type" varchar(255),
    "assignee_display_name" varchar(255),
    "priority_id" bigint,
    "priority_name" varchar(255),
    "parent_id" bigint,
    "parent_key" varchar(255),
    "sprint_id" bigint,
    "sprint_name" varchar(255),
    "resolution_date" timestamptz,
    "created" timestamptz,
    "updated" timestamptz,
    "spent_minutes" bigint,
    "comment_total" bigint,
    "lead_time_minutes" bigint,
    "std_type" varchar(255),
    "std_status" varchar(255),
    "components" text,
    "subtask" boolean,
    "changelog_total" bigint,
    "worklog_total" bigint,
    "due_date" timestamptz,
    "fix_versions" text,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "issue_id")
);

CREATE TABLE IF NOT EXISTS public._tool_slack_channels (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigserial,
    "scope_config_id" bigint,
    "id" text,
    "name" text,
    "is_channel" boolean,
    "is_group" boolean,
    "is_im" boolean,
    "is_mpim" boolean,
    "is_private" boolean,
    "created" bigint,
    "is_archived" boolean,
    "is_general" boolean,
    "unlinked" bigint,
    "name_normalized" text,
    "is_shared" boolean,
    "is_org_shared" boolean,
    "is_pending_ext_shared" boolean,
    "context_team_id" text,
    "updated" bigint,
    "creator" text,
    "is_ext_shared" boolean,
    "is_member" boolean,
    "num_members" bigint,
    PRIMARY KEY ("connection_id", "id")
);

CREATE TABLE IF NOT EXISTS public._tool_slack_channel_messages (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigint,
    "channel_id" text,
    "ts" text,
    "client_msg_id" text,
    "type" text,
    "subtype" text,
    "thread_ts" text,
    "user" text,
    "text" text,
    "team" text,
    "reply_count" bigint,
    "reply_users_count" bigint,
    "latest_reply" text,
    "is_locked" boolean,
    "subscribed" boolean,
    "parent_user_id" text,
    PRIMARY KEY ("connection_id", "channel_id", "ts")
);

CREATE TABLE IF NOT EXISTS public._tool_sonarqube_projects (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigserial,
    "scope_config_id" bigint,
    "project_key" varchar(500),
    "name" varchar(500),
    "qualifier" varchar(255),
    "visibility" varchar(64),
    "last_analysis_date" timestamptz,
    "revision" varchar(128),
    PRIMARY KEY ("connection_id", "project_key")
);

CREATE TABLE IF NOT EXISTS public._tool_sonarqube_issues (
    "connection_id" bigint,
    "issue_key" varchar(100),
    "rule" varchar(255),
    "severity" varchar(100),
    "component" text,
    "project_key" varchar(255),
    "line" bigint,
    "status" varchar(20),
    "message" text,
    "debt" bigint,
    "effort" bigint,
    "author" varchar(100),
    "hash" varchar(100),
    "tags" text,
    "type" varchar(100),
    "scope" varchar(255),
    "start_line" bigint,
    "end_line" bigint,
    "start_offset" bigint,
    "end_offset" bigint,
    "creation_date" timestamptz,
    "update_date" timestamptz,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "issue_key")
);

CREATE TABLE IF NOT EXISTS public._tool_sonarqube_issue_code_blocks (
    "connection_id" bigint,
    "id" text,
    "issue_key" text,
    "component" text,
    "start_line" bigint,
    "end_line" bigint,
    "start_offset" bigint,
    "end_offset" bigint,
    "msg" text,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "id")
);

CREATE TABLE IF NOT EXISTS public._tool_sonarqube_issue_impacts (
    "connection_id" bigint,
    "issue_key" varchar(100),
    "software_quality" varchar(255),
    "severity" varchar(100),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "issue_key", "software_quality")
);

CREATE TABLE IF NOT EXISTS public._tool_jenkins_builds (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigint,
    "job_name" varchar(255),
    "job_path" varchar(255),
    "duration" decimal,
    "full_name" varchar(255),
    "estimated_duration" decimal,
    "number" bigint,
    "result" text,
    "timestamp" bigint,
    "start_time" timestamptz,
    "type" varchar(255),
    "class" varchar(255),
    "triggered_by" varchar(255),
    "building" boolean,
    "has_stages" boolean,
    PRIMARY KEY ("connection_id", "full_name")
);

CREATE TABLE IF NOT EXISTS public._tool_jenkins_build_commits (
    "connection_id" bigint,
    "build_name" varchar(255),
    "commit_sha" varchar(255),
    "branch" varchar(255),
    "repo_url" varchar(255),
    "number" bigint,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "build_name", "commit_sha")
);

CREATE TABLE IF NOT EXISTS public._tool_argocd_applications (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigserial,
    "scope_config_id" bigint,
    "name" varchar(255),
    "namespace" varchar(255),
    "project" varchar(255),
    "repo_url" varchar(500),
    "path" varchar(255),
    "target_revision" varchar(255),
    "dest_server" varchar(255),
    "dest_namespace" varchar(255),
    "sync_status" varchar(100),
    "health_status" varchar(100),
    "created_date" timestamptz,
    "summary_images" json,
    PRIMARY KEY ("connection_id", "name")
);

CREATE TABLE IF NOT EXISTS public._tool_argocd_sync_operations (
    "connection_id" bigint,
    "application_name" varchar(255),
    "deployment_id" bigint,
    "revision" varchar(255),
    "repo_url" varchar(500),
    "kind" varchar(100),
    "started_at" timestamptz,
    "finished_at" timestamptz,
    "phase" varchar(100),
    "message" text,
    "initiated_by" varchar(255),
    "sync_status" varchar(100),
    "health_status" varchar(100),
    "resources_count" bigint,
    "container_images" json,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "application_name", "deployment_id")
);

CREATE TABLE IF NOT EXISTS public._tool_argocd_revision_images (
    "connection_id" bigint,
    "application_name" varchar(255),
    "revision" varchar(255),
    "images" json,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "application_name", "revision")
);

CREATE TABLE IF NOT EXISTS public._tool_gitlab_pipelines (
    "connection_id" bigint,
    "gitlab_id" bigint,
    "project_id" bigint,
    "status" varchar(100),
    "ref" varchar(255),
    "sha" varchar(255),
    "web_url" varchar(255),
    "duration" bigint,
    "queued_duration" decimal,
    "gitlab_created_at" timestamptz,
    "gitlab_updated_at" timestamptz,
    "started_at" timestamptz,
    "finished_at" timestamptz,
    "coverage" text,
    "type" varchar(255),
    "environment" varchar(255),
    "is_detail_required" boolean,
    "source" text,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "gitlab_id")
);

CREATE TABLE IF NOT EXISTS public._devlake_subtask_states (
    "plugin" varchar(255),
    "subtask" varchar(255),
    "params" varchar(255),
    "prev_config" text,
    "time_after" timestamptz,
    "prev_started_at" timestamptz,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    PRIMARY KEY ("plugin", "subtask", "params")
);

CREATE TABLE IF NOT EXISTS public._tool_pagerduty_services (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigserial,
    "scope_config_id" bigint,
    "id" text,
    "url" text,
    "name" text,
    PRIMARY KEY ("connection_id", "id")
);

CREATE TABLE IF NOT EXISTS public._tool_pagerduty_incidents (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigint,
    "number" bigint,
    "url" text,
    "service_id" text,
    "summary" text,
    "status" text,
    "urgency" text,
    "priority" text,
    "created_date" timestamptz,
    "updated_date" timestamptz,
    PRIMARY KEY ("connection_id", "number")
);

CREATE TABLE IF NOT EXISTS public._tool_pagerduty_assignments (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigint,
    "user_id" text,
    "incident_number" bigint,
    "assigned_at" timestamptz,
    PRIMARY KEY ("user_id", "incident_number")
);

CREATE TABLE IF NOT EXISTS public._tool_pagerduty_users (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigint,
    "id" text,
    "url" text,
    "name" text,
    PRIMARY KEY ("connection_id", "id")
);

CREATE TABLE IF NOT EXISTS public._tool_linear_teams (
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "connection_id" bigserial,
    "scope_config_id" bigint,
    "team_id" varchar(255),
    "name" varchar(255),
    "key" varchar(255),
    "description" text,
    PRIMARY KEY ("connection_id", "team_id")
);

CREATE TABLE IF NOT EXISTS public._tool_linear_issues (
    "connection_id" bigint,
    "id" varchar(255),
    "team_id" varchar(255),
    "identifier" varchar(255),
    "number" bigint,
    "title" text,
    "description" text,
    "url" text,
    "priority" bigint,
    "priority_label" varchar(100),
    "estimate" decimal,
    "state_id" varchar(255),
    "state_name" varchar(255),
    "state_type" varchar(100),
    "creator_id" varchar(255),
    "assignee_id" varchar(255),
    "cycle_id" varchar(255),
    "parent_id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "started_at" timestamptz,
    "completed_at" timestamptz,
    "canceled_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "id")
);

CREATE TABLE IF NOT EXISTS public._tool_linear_issue_labels (
    "connection_id" bigint,
    "issue_id" varchar(255),
    "label_name" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "issue_id", "label_name")
);

CREATE TABLE IF NOT EXISTS public._tool_linear_accounts (
    "connection_id" bigint,
    "id" varchar(255),
    "name" varchar(255),
    "display_name" varchar(255),
    "email" varchar(255),
    "avatar_url" varchar(255),
    "active" boolean,
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("connection_id", "id")
);

CREATE TABLE IF NOT EXISTS public.repos (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "name" text,
    "url" text,
    "description" text,
    "owner_id" varchar(255),
    "language" varchar(255),
    "forked_from" text,
    "created_date" timestamptz,
    "updated_date" timestamptz,
    "deleted" boolean,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.boards (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "name" varchar(255),
    "description" text,
    "url" varchar(255),
    "created_date" timestamptz,
    "type" varchar(255),
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.issues (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "url" varchar(255),
    "icon_url" varchar(255),
    "issue_key" varchar(255),
    "title" text,
    "description" text,
    "epic_key" varchar(255),
    "type" varchar(100),
    "original_type" varchar(500),
    "status" varchar(100),
    "original_status" varchar(100),
    "story_point" decimal,
    "resolution_date" timestamptz,
    "created_date" timestamptz,
    "updated_date" timestamptz,
    "lead_time_minutes" bigint,
    "original_estimate_minutes" bigint,
    "time_spent_minutes" bigint,
    "time_remaining_minutes" bigint,
    "creator_id" varchar(255),
    "creator_name" varchar(255),
    "assignee_id" varchar(255),
    "assignee_name" varchar(255),
    "parent_issue_id" varchar(255),
    "priority" varchar(255),
    "severity" varchar(255),
    "urgency" varchar(255),
    "component" text,
    "original_project" varchar(255),
    "is_subtask" boolean,
    "due_date" timestamptz,
    "fix_versions" text,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.pull_requests (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "base_repo_id" text,
    "head_repo_id" text,
    "status" varchar(100),
    "original_status" varchar(100),
    "title" text,
    "description" text,
    "url" varchar(255),
    "author_name" varchar(100),
    "author_id" varchar(100),
    "merged_by_name" varchar(100),
    "merged_by_id" varchar(100),
    "parent_pr_id" varchar(100),
    "pull_request_key" bigint,
    "created_date" timestamptz,
    "merged_date" timestamptz,
    "closed_date" timestamptz,
    "type" varchar(100),
    "component" varchar(100),
    "merge_commit_sha" varchar(40),
    "head_ref" varchar(255),
    "base_ref" varchar(255),
    "base_commit_sha" varchar(40),
    "head_commit_sha" varchar(40),
    "additions" bigint,
    "deletions" bigint,
    "is_draft" boolean,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.cq_projects (
    "id" varchar(500),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "name" varchar(2000),
    "qualifier" varchar(255),
    "visibility" varchar(64),
    "last_analysis_date" timestamptz,
    "commit_sha" varchar(128),
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.cq_issues (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "rule" varchar(255),
    "severity" varchar(100),
    "component" text,
    "project_key" varchar(500),
    "line" bigint,
    "status" varchar(20),
    "message" text,
    "debt" bigint,
    "effort" bigint,
    "commit_author_email" varchar(255),
    "assignee" varchar(255),
    "hash" varchar(100),
    "tags" text,
    "type" varchar(100),
    "scope" varchar(255),
    "start_line" bigint,
    "end_line" bigint,
    "start_offset" bigint,
    "end_offset" bigint,
    "vulnerability_probability" varchar(100),
    "security_category" varchar(100),
    "created_date" timestamptz,
    "updated_date" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.cicd_pipelines (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "name" text,
    "display_title" text,
    "url" text,
    "result" varchar(100),
    "status" varchar(100),
    "original_status" varchar(100),
    "original_result" varchar(100),
    "type" varchar(100),
    "duration_sec" decimal,
    "queued_duration_sec" decimal,
    "environment" varchar(255),
    "created_date" timestamptz,
    "queued_date" timestamptz,
    "started_date" timestamptz,
    "finished_date" timestamptz,
    "cicd_scope_id" varchar(255),
    "is_child" boolean,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.cicd_tasks (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "name" text,
    "pipeline_id" varchar(255),
    "result" varchar(100),
    "status" varchar(100),
    "original_status" varchar(100),
    "original_result" varchar(100),
    "type" varchar(100),
    "environment" varchar(255),
    "duration_sec" decimal,
    "queued_duration_sec" decimal,
    "created_date" timestamptz,
    "queued_date" timestamptz,
    "started_date" timestamptz,
    "finished_date" timestamptz,
    "cicd_scope_id" varchar(255),
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.cicd_scopes (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "name" text,
    "description" text,
    "url" varchar(255),
    "created_date" timestamptz,
    "updated_date" timestamptz,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.cicd_deployments (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "cicd_scope_id" varchar(255),
    "name" text,
    "display_title" text,
    "url" text,
    "result" varchar(100),
    "status" varchar(100),
    "original_status" varchar(100),
    "original_result" varchar(100),
    "environment" varchar(255),
    "original_environment" varchar(255),
    "created_date" timestamptz,
    "queued_date" timestamptz,
    "started_date" timestamptz,
    "finished_date" timestamptz,
    "duration_sec" decimal,
    "queued_duration_sec" decimal,
    "subtask_name" varchar(255),
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS public.cicd_deployment_commits (
    "id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    "cicd_scope_id" varchar(255),
    "cicd_deployment_id" varchar(255),
    "name" text,
    "display_title" text,
    "url" text,
    "result" varchar(100),
    "status" varchar(100),
    "original_status" varchar(100),
    "original_result" varchar(100),
    "environment" varchar(255),
    "original_environment" varchar(255),
    "created_date" timestamptz,
    "queued_date" timestamptz,
    "started_date" timestamptz,
    "finished_date" timestamptz,
    "duration_sec" decimal,
    "queued_duration_sec" decimal,
    "commit_sha" varchar(255),
    "commit_msg" text,
    "ref_name" text,
    "repo_id" varchar(255),
    "repo_url" text NOT NULL,
    "prev_success_deployment_commit_id" varchar(255),
    "subtask_name" text,
    PRIMARY KEY ("id", "commit_sha")
);

CREATE TABLE IF NOT EXISTS public.board_issues (
    "board_id" varchar(255),
    "issue_id" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("board_id", "issue_id")
);

CREATE TABLE IF NOT EXISTS public.issue_assignees (
    "issue_id" varchar(255),
    "assignee_id" varchar(255),
    "assignee_name" varchar(255),
    "created_at" timestamptz,
    "updated_at" timestamptz,
    "_raw_data_params" varchar(255),
    "_raw_data_table" varchar(255),
    "_raw_data_id" bigint,
    "_raw_data_remark" text,
    PRIMARY KEY ("issue_id", "assignee_id")
);

-- END PINNED DEVLAKE MODELS

DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_github_scopes;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_github_scopes
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_jira_scopes;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_jira_scopes
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_github_api_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_github_api_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_github_api_pull_requests;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_github_api_pull_requests
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_jira_api_boards;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_jira_api_boards
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_jira_api_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_jira_api_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_slack_channel;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_slack_channel
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_slack_channel_message;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_slack_channel_message
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_sonarqube_api_projects;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_sonarqube_api_projects
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_sonarqube_api_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_sonarqube_api_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_github_repos;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_github_repos
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_jira_boards;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_jira_boards
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_github_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_github_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_github_pull_requests;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_github_pull_requests
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_jira_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_jira_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_slack_channels;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_slack_channels
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_slack_channel_messages;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_slack_channel_messages
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_sonarqube_projects;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_sonarqube_projects
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_sonarqube_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_sonarqube_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_sonarqube_issue_code_blocks;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_sonarqube_issue_code_blocks
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_sonarqube_issue_impacts;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_sonarqube_issue_impacts
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_jenkins_api_builds;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_jenkins_api_builds
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_jenkins_api_jobs;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_jenkins_api_jobs
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_jenkins_builds;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_jenkins_builds
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_jenkins_build_commits;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_jenkins_build_commits
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.cicd_pipelines;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cicd_pipelines
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.cicd_tasks;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cicd_tasks
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_argocd_api_applications;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_argocd_api_applications
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_argocd_api_sync_operations;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_argocd_api_sync_operations
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_argocd_applications;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_argocd_applications
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_argocd_sync_operations;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_argocd_sync_operations
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_argocd_revision_images;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_argocd_revision_images
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.cicd_scopes;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cicd_scopes
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.cicd_deployments;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cicd_deployments
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.cicd_deployment_commits;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cicd_deployment_commits
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_gitlab_api_pipeline_details;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_gitlab_api_pipeline_details
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_gitlab_pipelines;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_gitlab_pipelines
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._devlake_subtask_states;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._devlake_subtask_states
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_pagerduty_services;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_pagerduty_services
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_pagerduty_incidents;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_pagerduty_incidents
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_linear_teams;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_linear_teams
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._raw_linear_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._raw_linear_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_pagerduty_services;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_pagerduty_services
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_pagerduty_incidents;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_pagerduty_incidents
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_pagerduty_assignments;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_pagerduty_assignments
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_pagerduty_users;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_pagerduty_users
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_linear_teams;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_linear_teams
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_linear_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_linear_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_linear_issue_labels;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_linear_issue_labels
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public._tool_linear_accounts;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public._tool_linear_accounts
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.board_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.board_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.issue_assignees;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.issue_assignees
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.repos;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.repos
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.boards;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.boards
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.pull_requests;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.pull_requests
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.cq_projects;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cq_projects
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
DROP TRIGGER IF EXISTS collection_generation_guard ON public.cq_issues;
CREATE TRIGGER collection_generation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cq_issues
FOR EACH ROW EXECUTE FUNCTION devlake_meta.require_current_generation();
INSERT INTO devlake_meta.schema_receipts (
    cell_id, db_resource_id, schema_version, manifest_sha256, status
) VALUES (:'cell_id', :'db_resource_id', 2, :'manifest_sha256', 'applied')
ON CONFLICT (cell_id, db_resource_id, schema_version) DO UPDATE
SET manifest_sha256 = EXCLUDED.manifest_sha256, applied_at = now();
