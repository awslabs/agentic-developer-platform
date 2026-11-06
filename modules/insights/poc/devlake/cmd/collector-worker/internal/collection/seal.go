package collection

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
)

type CheckpointUpdate struct {
	SourceKey      string  `json:"source_key"`
	ExpectedCursor *string `json:"expected_cursor"`
	NextCursor     string  `json:"next_cursor"`
}

func SourceCheckpointKey(source Source) string {
	encoded, _ := json.Marshal(struct {
		CellID       string
		TenantID     string
		ProjectID    string
		Tool         string
		ConnectionID string
		Scope        string
	}{source.CellID, source.TenantID, source.ProjectID, source.Tool, source.ConnectionID, source.Scope})
	return string(encoded)
}

func SealPreparedBatch(ctx context.Context, db *sql.DB, sources []Source, authority Authority,
	batch PreparedBatch, checkpoints []CheckpointUpdate) (string, error) {
	return PersistAndSealPreparedBatch(ctx, db, sources, authority, batch, checkpoints, nil)
}

func PersistAndSealPreparedBatch(ctx context.Context, db *sql.DB, sources []Source, authority Authority,
	batch PreparedBatch, checkpoints []CheckpointUpdate, persist func(*sql.Tx) error) (string, error) {
	if err := CheckFlags(); err != nil {
		return "", err
	}
	if db == nil || authority == nil || !validEvidenceSources(sources) ||
		len(checkpoints) != len(sources) ||
		VerifyPreparedBatch(batch) != nil {
		return "", ErrBatch
	}
	for _, source := range sources {
		if expandedEvidenceTool(source.Tool) && (len(batch.SourceKeys) != len(sources) || len(batch.SourceBindings) != len(sources)) {
			return "", ErrBatch
		}
	}
	allowed := make(map[NativeKey]bool, len(sources))
	checkpointKeys := make(map[string]bool, len(sources))
	tools := make(map[string]bool, len(sources))
	for index, source := range sources {
		if source.CellID != batch.CellID || source.TenantID == "" || source.ProjectID == "" ||
			(!supportedEvidenceTool(source.Tool) || tools[source.Tool]) || source.ConnectionID == "" || source.Scope == "" ||
			source.TenantID != sources[0].TenantID || source.ProjectID != sources[0].ProjectID ||
			checkpoints[index].SourceKey != SourceCheckpointKey(source) ||
			checkpoints[index].NextCursor == "" || checkpointKeys[checkpoints[index].SourceKey] {
			return "", ErrBatch
		}
		checkpointKeys[checkpoints[index].SourceKey] = true
		tools[source.Tool] = true
		if len(batch.SourceBindings) > 0 && batch.SourceBindings[checkpoints[index].SourceKey] !=
			(ObservationBinding{source.BindingID, source.BindingGeneration, source.BindingRevision}) {
			return "", ErrBatch
		}
		allowed[NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
			Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope}] = true
		if err := currentSource(ctx, source, authority); err != nil {
			return "", err
		}
	}
	for _, item := range batch.Items {
		key := item.Key
		key.Kind, key.NativeID = "", ""
		if !allowed[key] {
			return "", ErrBatch
		}
	}
	checkpointPayload, err := json.Marshal(checkpoints)
	for _, key := range batch.SourceKeys {
		if !checkpointKeys[key] {
			return "", ErrBatch
		}
	}
	if err != nil {
		return "", ErrBatch
	}
	items := make([][]byte, len(batch.ItemPayloads))
	for index, payload := range batch.ItemPayloads {
		items[index] = bytes.Clone(payload)
	}
	manifest := bytes.Clone(batch.ManifestPayload)
	transaction, err := db.BeginTx(ctx, nil)
	if err != nil {
		return "", ErrBatch
	}
	defer transaction.Rollback()
	if persist != nil {
		if _, err := transaction.ExecContext(ctx, "SELECT devlake_meta.lock_generation($1, $2)", batch.CellID, batch.Generation); err != nil {
			return "", ErrBatch
		}
		if err := persist(transaction); err != nil {
			return "", ErrBatch
		}
	}
	var returned []byte
	if err := transaction.QueryRowContext(ctx,
		"SELECT devlake_meta.seal_batch($1, $2, $3, $4::bytea[], $5::bytea, $6::jsonb)",
		batch.CellID, batch.Generation, batch.BatchID, items, manifest, checkpointPayload).Scan(&returned); err != nil {
		return "", ErrBatch
	}
	if hex.EncodeToString(returned) != batch.ContentSHA256 {
		return "", ErrBatch
	}
	if err := CheckFlags(); err != nil {
		return "", err
	}
	for _, source := range sources {
		if err := currentSource(ctx, source, authority); err != nil {
			return "", err
		}
	}
	if err := transaction.Commit(); err != nil {
		return "", ErrBatch
	}
	return batch.ContentSHA256, nil
}
