package worker

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/celldb"
	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/cmd/adp-collector-worker/internal/sourceapi"
)

type Cell struct {
	DatabaseRef, RoleRef, ResourceID, Host, Database, Writer, Reader, Region, CAPath string
}

type Config struct {
	GatewayURL, Region, TenantID, JobID, ClaimKey string
	SchemaPath, BuildManifestPath                 string
	Cells                                         []Cell
}

type Result struct {
	BatchID       string `json:"batch_id"`
	ContentDigest string `json:"content_digest"`
	Items         int    `json:"items"`
}

type OpenPool func(celldb.Config) (*sql.DB, error)

func Run(ctx context.Context, config Config, gateway *Gateway, open OpenPool) (result Result, returned error) {
	if err := collection.CheckFlags(); err != nil {
		return result, err
	}
	if receipt, err := gateway.Recover(ctx, config.TenantID, config.JobID, config.ClaimKey); err != nil {
		return result, err
	} else if receipt != nil {
		return *receipt, nil
	}
	digest, err := collection.PinnedManifestDigest(config.SchemaPath, config.BuildManifestPath)
	if err != nil {
		return result, err
	}
	job, err := gateway.Admit(ctx, config.TenantID, config.JobID, config.ClaimKey)
	if err != nil {
		return result, err
	}
	sealedDurably := false
	defer func() {
		if returned != nil && !sealedDurably {
			_ = gateway.Finish(ctx, "failed", Result{})
		}
	}()
	if len(job.Sources) < 2 || len(job.Sources) > 9 || open == nil {
		return result, ErrWork
	}
	target, err := selectCell(job, gateway.Contexts, config.Cells)
	if err != nil {
		return result, err
	}
	first := gateway.Contexts[job.Sources[0]]
	poolConfig := celldb.Config{Host: target.Host, Database: target.Database, Username: target.Writer, Region: target.Region, CAPath: target.CAPath, MaxOpen: 2}
	writer, err := open(poolConfig)
	if err != nil {
		return result, err
	}
	defer writer.Close()
	poolConfig.Username = target.Reader
	reader, err := open(poolConfig)
	if err != nil {
		return result, err
	}
	defer reader.Close()
	migration := collection.SQLMigration{DB: writer, ResourceID: target.ResourceID}
	now := gateway.Claim.WindowEnd.UTC()
	if now.IsZero() || now.After(time.Now().UTC()) || time.Since(now) > 30*time.Minute {
		return result, ErrWork
	}
	if err := collection.Admit(ctx, job, gateway, migration, digest, now); err != nil {
		return result, err
	}
	var previous int64
	var state, operation, attempt string
	err = writer.QueryRowContext(ctx, "SELECT generation, state, operation_id, attempt_id FROM devlake_meta.collection_generations WHERE cell_id = $1", first.CellID).Scan(&previous, &state, &operation, &attempt)
	if err != nil && err != sql.ErrNoRows {
		return result, ErrWork
	}
	batchID := batchIdentity(config.JobID, gateway.Claim.AttemptID)
	if operation == config.JobID && attempt == gateway.Claim.AttemptID {
		if state == "sealed" {
			sealedDurably = true
			batch, err := collection.LoadSealedBatch(ctx, reader, first.CellID, batchID)
			if err != nil {
				return result, err
			}
			result = Result{BatchID: batchID, ContentDigest: batch.ContentSHA256, Items: len(batch.Items)}
			if err := gateway.Finish(ctx, "succeeded", result); err != nil {
				return result, err
			}
			return result, nil
		}
		previous--
	}
	generation, err := collection.StartGeneration(ctx, writer, job, gateway, migration, digest, now, previous, config.JobID, gateway.Claim.AttemptID)
	if err != nil {
		return result, err
	}
	defer func() {
		if returned != nil && !sealedDurably {
			_, _ = writer.ExecContext(ctx, "SELECT devlake_meta.cancel_collection($1, $2)", first.CellID, generation)
		}
	}()
	var items []collection.BatchItem
	var checkpoints []collection.CheckpointUpdate
	for _, source := range job.Sources {
		var current collection.Reconciled
		var normalized map[collection.NativeKey]collection.BatchItem
		var err error
		if source.Tool != "github" && source.Tool != "jira" {
			current, normalized, err = collectAndProcessNative(ctx, gateway, writer, source, job, generation)
		} else {
			current, normalized, err = collect(ctx, gateway, writer, source, job, generation)
		}
		if err != nil {
			return result, err
		}
		plan, checkpoint, err := collection.PlanPersistedIncremental(ctx, writer, reader, job.Sources, source, generation,
			gateway, collection.SQLGenerationReader{DB: writer}, current, gateway.Claim.AttemptID, true, job.Cutoff.UTC(), now)
		if err != nil {
			return result, err
		}
		var upserts []collection.BatchItem
		for _, change := range plan.Changes {
			if change.Action == "upsert" {
				upserts = append(upserts, normalized[change.Key])
			}
		}
		fieldMap := "provider-native-v1"
		if source.Tool != "github" && source.Tool != "jira" {
			fieldMap = "devlake-pinned"
		}
		prepared, err := collection.PrepareIncrementalItems(plan, upserts, fmt.Sprint(gateway.Contexts[source].PolicyRevision), fieldMap, "schema-v2")
		if err != nil {
			return result, err
		}
		items = append(items, prepared...)
		checkpoints = append(checkpoints, checkpoint)
	}
	batch, err := collection.PrepareSourceBatch(batchID, first.CellID, generation, items, job.Sources)
	if err != nil {
		return result, err
	}
	var legacyItems []collection.BatchItem
	for _, item := range batch.Items {
		if item.Key.Tool == "github" || item.Key.Tool == "jira" {
			legacyItems = append(legacyItems, item)
		}
	}
	sealed, err := collection.PersistAndSealPreparedBatch(ctx, writer, job.Sources, gateway, batch, checkpoints,
		func(transaction *sql.Tx) error { return persist(ctx, transaction, legacyItems) })
	if err != nil {
		return result, err
	}
	sealedDurably = true
	if err := collection.ReadSealedBatch(ctx, reader, batch); err != nil {
		return result, err
	}
	if _, err := collection.QueryCollectedDomain(ctx, reader, job.Sources, gateway, job.Cutoff.UTC(), now); err != nil {
		return result, err
	}
	result = Result{BatchID: batchID, ContentDigest: sealed, Items: len(items)}
	if err := gateway.Finish(ctx, "succeeded", result); err != nil {
		return result, err
	}
	return result, nil
}

func selectCell(job collection.Job, contexts map[collection.Source]SourceContext, cells []Cell) (*Cell, error) {
	if len(job.Sources) == 0 || len(job.Sources) > 9 || len(contexts) != len(job.Sources) || len(cells) == 0 {
		return nil, ErrWork
	}
	first, exists := contexts[job.Sources[0]]
	if !exists || first.CellID == "" || first.Destination.DatabaseRef == "" || first.Destination.RoleRef == "" {
		return nil, ErrWork
	}
	tools := make(map[string]bool, len(job.Sources))
	for _, source := range job.Sources {
		current, exists := contexts[source]
		if !exists || tools[source.Tool] || source.CellID != first.CellID || current.CellID != first.CellID ||
			current.TenantID != source.TenantID || source.TenantID != job.Sources[0].TenantID ||
			source.ProjectID != job.Sources[0].ProjectID ||
			source.Consumer != job.Sources[0].Consumer || current.Destination != first.Destination ||
			current.Provider != source.Tool || current.ProjectID != source.ProjectID ||
			current.Consumer != source.Consumer || current.BindingID != source.BindingID ||
			current.BindingRevision != source.BindingRevision || current.SourceGeneration != source.BindingGeneration ||
			current.GrantID != source.GrantID || current.Handle == "" {
			return nil, ErrWork
		}
		tools[source.Tool] = true
	}
	var target *Cell
	for index := range cells {
		candidate := &cells[index]
		if candidate.DatabaseRef == first.Destination.DatabaseRef && candidate.RoleRef == first.Destination.RoleRef {
			if target != nil {
				return nil, ErrWork
			}
			target = candidate
		}
	}
	if target == nil || target.Writer == "" || target.Reader == "" || target.Writer == target.Reader ||
		target.ResourceID == "" {
		return nil, ErrWork
	}
	return target, nil
}

func batchIdentity(job, attempt string) string {
	digest := sha256.Sum256([]byte(job + "\x00" + attempt))
	encoded := hex.EncodeToString(digest[:16])
	return encoded[:8] + "-" + encoded[8:12] + "-" + encoded[12:16] + "-" + encoded[16:20] + "-" + encoded[20:]
}

type collectionPageKind struct {
	kind  string
	limit int
}

func pageKinds(tool string, pageSize int) ([]collectionPageKind, error) {
	if pageSize < 1 || pageSize > 100 {
		return nil, ErrWork
	}
	var kinds []string
	switch tool {
	case "github":
		kinds = []string{"repository", "issue", "pull_request"}
	case "jira":
		kinds = []string{"board", "issue"}
	case "sonarqube":
		kinds = []string{"project", "issue"}
	case "argocd":
		kinds = []string{"application", "sync_operation"}
	case "jenkins":
		kinds = []string{"job", "build"}
	case "slack":
		kinds = []string{"channel", "channel_message"}
	case "linear":
		kinds = []string{"team", "issue"}
	case "pagerduty":
		kinds = []string{"service", "incident"}
	case "gitlab":
		kinds = []string{"pipeline_detail"}
	default:
		return nil, ErrWork
	}
	result := make([]collectionPageKind, 0, len(kinds))
	for _, kind := range kinds {
		limit := pageSize
		if kind == "pipeline_detail" && limit > 10 {
			limit = 10
		}
		result = append(result, collectionPageKind{kind: kind, limit: limit})
	}
	return result, nil
}

func collect(ctx context.Context, gateway *Gateway, writer *sql.DB, source collection.Source, job collection.Job, generation int64) (collection.Reconciled, map[collection.NativeKey]collection.BatchItem, error) {
	result := collection.Reconciled{}
	items := map[collection.NativeKey]collection.BatchItem{}
	seen := map[collection.NativeKey]bool{}
	nativePulls := map[string]collection.NativeKey{}
	nativeReferences := map[collection.NativeKey]string{}
	kinds, err := pageKinds(source.Tool, job.PageSize)
	if err != nil {
		return result, nil, err
	}
	used, total := 0, 0
	for _, spec := range kinds {
		kind := spec.kind
		for page := 1; ; page++ {
			used++
			if used > job.PageLimit {
				return result, nil, collection.ErrIncomplete
			}
			var records []sourceapi.Record
			var response Page
			_, err := collection.FetchFencedPage(ctx, source, generation, gateway, collection.SQLGenerationReader{DB: writer},
				func(ctx context.Context) (collection.NativePage, error) {
					var err error
					response, err = gateway.Page(ctx, source, kind, page, spec.limit)
					if err != nil {
						return collection.NativePage{}, err
					}
					records, err = sourceapi.DecodePage(source.Tool, kind, source.Scope, response.Payload, spec.limit, response.Private)
					return collection.NativePage{}, err
				})
			if err != nil {
				return result, nil, err
			}
			for _, record := range records {
				key := collection.NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
					Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: record.Kind, NativeID: record.ID}
				total += len(record.Raw)
				if seen[key] || total > 100*1024*1024 {
					return result, nil, collection.ErrIncomplete
				}
				seen[key] = true
				if record.Private {
					result.Exclusions = append(result.Exclusions, collection.Exclusion{Key: key, Reason: "private"})
					continue
				}
				if (kind == "issue" || kind == "pull_request") && record.Updated.Before(job.Cutoff) {
					result.Exclusions = append(result.Exclusions, collection.Exclusion{Key: key, Reason: "out_of_scope"})
					continue
				}
				result.Allowed = append(result.Allowed, collection.NativeRecord{Key: key, Revision: record.Revision, Payload: record.Raw})
				var normalized any
				switch kind {
				case "issue":
					status := map[string]string{"open": "todo", "closed": "done", "To Do": "todo", "In Progress": "in_progress", "Done": "done"}[record.Status]
					if status == "" {
						status = "unavailable"
					}
					normalized = collection.CommonIssue{Key: key, Revision: record.Revision, FieldMap: "provider-native-v1", Native: record.Raw,
						Title: record.Title, OriginalStatus: record.Status, CommonStatus: status, CreatedAt: record.Created, UpdatedAt: record.Updated}
				case "pull_request":
					normalized = collection.PullRequest{Key: key, CreatedAt: record.Created, MergedAt: record.Merged, Merged: record.Merged != nil}
				default:
					normalized = struct {
						Key  collection.NativeKey
						Name string
					}{key, record.Title}
				}
				encoded, err := json.Marshal(normalized)
				if err != nil {
					return result, nil, err
				}
				items[key] = collection.BatchItem{Key: key, Revision: record.Revision, NativeBytes: record.Raw, NormalizedBytes: encoded,
					PolicyVersion: fmt.Sprint(gateway.Contexts[source].PolicyRevision), FieldMapVersion: "provider-native-v1", SchemaVersion: "schema-v2"}
				if source.Tool == "github" && kind == "pull_request" && record.URL != "" {
					if _, duplicate := nativePulls[record.URL]; duplicate {
						return result, nil, collection.ErrIncomplete
					}
					nativePulls[record.URL] = key
				}
				if source.Tool == "github" && kind == "issue" && record.PullURL != "" {
					nativeReferences[key] = record.PullURL
				}
			}
			if response.Complete {
				break
			}
		}
	}
	for key, reference := range nativeReferences {
		if target, exists := nativePulls[reference]; exists {
			item := items[key]
			item.Links = []collection.EvidenceLink{{From: key, To: target, Relation: "source-reference", NativeEvidenceID: reference}}
			items[key] = item
		}
	}
	return result, items, nil
}

func persist(ctx context.Context, transaction *sql.Tx, items []collection.BatchItem) error {
	for _, item := range items {
		var rawTable, domainTable string
		switch item.Key.Tool + ":" + item.Key.Kind {
		case "github:repository":
			rawTable, domainTable = "_raw_github_scopes", "repos"
		case "github:issue":
			rawTable, domainTable = "_raw_github_api_issues", "issues"
		case "github:pull_request":
			rawTable, domainTable = "_raw_github_api_pull_requests", "pull_requests"
		case "jira:board":
			rawTable, domainTable = "_raw_jira_api_boards", "boards"
		case "jira:issue":
			rawTable, domainTable = "_raw_jira_api_issues", "issues"
		default:
			return collection.ErrMapping
		}
		key, _ := json.Marshal(item.Key)
		digest := sha256.Sum256(key)
		identity := hex.EncodeToString(digest[:])
		if _, err := transaction.ExecContext(ctx, "DELETE FROM public."+rawTable+" WHERE params = $1", identity); err != nil {
			return err
		}
		if _, err := transaction.ExecContext(ctx, "DELETE FROM public."+domainTable+" WHERE id = $1", identity); err != nil {
			return err
		}
		if item.RemovalReason != "" {
			continue
		}
		var rawID int64
		if err := transaction.QueryRowContext(ctx, "INSERT INTO public."+rawTable+" (params, data, input, created_at) VALUES ($1, $2, $3::json, now()) RETURNING id", identity, item.NativeBytes, key).Scan(&rawID); err != nil {
			return err
		}
		if domainTable == "issues" {
			var issue collection.CommonIssue
			if json.Unmarshal(item.NormalizedBytes, &issue) != nil {
				return collection.ErrMapping
			}
			if _, err := transaction.ExecContext(ctx, "INSERT INTO public.issues (id, title, original_status, status, _raw_data_table, _raw_data_id) VALUES ($1, $2, $3, $4, $5, $6)", identity, issue.Title, issue.OriginalStatus, issue.CommonStatus, rawTable, rawID); err != nil {
				return err
			}
		} else {
			if _, err := transaction.ExecContext(ctx, "INSERT INTO public."+domainTable+" (id, _raw_data_table, _raw_data_id) VALUES ($1, $2, $3)", identity, rawTable, rawID); err != nil {
				return err
			}
		}
	}
	return nil
}
