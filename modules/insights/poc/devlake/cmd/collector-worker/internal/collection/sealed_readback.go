package collection

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
)

func ReadSealedBatch(ctx context.Context, reader *sql.DB, batch PreparedBatch) error {
	if err := CheckFlags(); err != nil {
		return err
	}
	if reader == nil || VerifyPreparedBatch(batch) != nil {
		return ErrBatch
	}
	transaction, err := reader.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return ErrBatch
	}
	defer transaction.Rollback()
	var storedManifest, storedDigest []byte
	if err := transaction.QueryRowContext(ctx,
		"SELECT manifest_payload, content_digest FROM devlake_meta.collection_batch_manifests WHERE cell_id = $1 AND batch_id = $2",
		batch.CellID, batch.BatchID).Scan(&storedManifest, &storedDigest); err != nil {
		return ErrBatch
	}
	if !bytes.Equal(storedManifest, batch.ManifestPayload) || hex.EncodeToString(storedDigest) != batch.ContentSHA256 {
		return ErrBatch
	}
	rows, err := transaction.QueryContext(ctx,
		`SELECT item_payload, payload_digest FROM devlake_meta.collection_batch_items AS item
		 JOIN devlake_meta.collection_batch_manifests AS manifest ON manifest.batch_id = item.batch_id
		 WHERE manifest.cell_id = $1 AND manifest.batch_id = $2`, batch.CellID, batch.BatchID)
	if err != nil {
		return ErrBatch
	}
	defer rows.Close()
	expected := make(map[string][]byte, len(batch.ItemPayloads))
	for index, digest := range batch.ItemDigests {
		expected[digest.SHA256] = batch.ItemPayloads[index]
	}
	seen := make(map[string]bool, len(expected))
	for rows.Next() {
		var item, storedItemDigest []byte
		if err := rows.Scan(&item, &storedItemDigest); err != nil {
			return ErrBatch
		}
		actual := sha256.Sum256(item)
		encoded := hex.EncodeToString(actual[:])
		if seen[encoded] || !bytes.Equal(storedItemDigest, actual[:]) || !bytes.Equal(item, expected[encoded]) {
			return ErrBatch
		}
		seen[encoded] = true
	}
	if rows.Err() != nil || len(seen) != len(expected) {
		return ErrBatch
	}
	if err := CheckFlags(); err != nil {
		return err
	}
	if err := transaction.Commit(); err != nil {
		return ErrBatch
	}
	return nil
}

func LoadSealedBatch(ctx context.Context, reader *sql.DB, cellID, batchID string) (PreparedBatch, error) {
	if err := CheckFlags(); err != nil {
		return PreparedBatch{}, err
	}
	if reader == nil || cellID == "" || !batchIDPattern.MatchString(batchID) {
		return PreparedBatch{}, ErrBatch
	}
	transaction, err := reader.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return PreparedBatch{}, ErrBatch
	}
	defer transaction.Rollback()
	var manifest, digest []byte
	if err := transaction.QueryRowContext(ctx, "SELECT manifest_payload, content_digest FROM devlake_meta.collection_batch_manifests WHERE cell_id = $1 AND batch_id = $2", cellID, batchID).Scan(&manifest, &digest); err != nil {
		return PreparedBatch{}, ErrBatch
	}
	var identity struct {
		BatchID, CellID string
		Generation      int64
		ItemDigests     []BatchDigest
		SourceKeys      []string
		SourceBindings  map[string]ObservationBinding
	}
	if len(manifest) > 104857600 || json.Unmarshal(manifest, &identity) != nil || identity.BatchID != batchID || identity.CellID != cellID {
		return PreparedBatch{}, ErrBatch
	}
	rows, err := transaction.QueryContext(ctx, `SELECT item_payload, payload_digest FROM devlake_meta.collection_batch_items AS item
		JOIN devlake_meta.collection_batch_manifests AS manifest ON manifest.batch_id = item.batch_id
		WHERE manifest.cell_id = $1 AND manifest.batch_id = $2`, cellID, batchID)
	if err != nil {
		return PreparedBatch{}, ErrBatch
	}
	defer rows.Close()
	var items []BatchItem
	total := 0
	for rows.Next() {
		var payload, storedDigest []byte
		var item BatchItem
		if rows.Scan(&payload, &storedDigest) != nil {
			return PreparedBatch{}, ErrBatch
		}
		total += len(payload)
		actual := sha256.Sum256(payload)
		if total > 104857600 || len(items) >= 100000 || !bytes.Equal(actual[:], storedDigest) || json.Unmarshal(payload, &item) != nil {
			return PreparedBatch{}, ErrBatch
		}
		encoded, err := json.Marshal(item)
		if err != nil || !bytes.Equal(encoded, payload) {
			return PreparedBatch{}, ErrBatch
		}
		items = append(items, item)
	}
	if rows.Err() != nil {
		return PreparedBatch{}, ErrBatch
	}
	batch, err := prepareSourceBatch(batchID, cellID, identity.Generation, items, identity.SourceKeys, identity.SourceBindings)
	if err != nil || !bytes.Equal(manifest, batch.ManifestPayload) || hex.EncodeToString(digest) != batch.ContentSHA256 {
		return PreparedBatch{}, ErrBatch
	}
	if err := transaction.Commit(); err != nil {
		return PreparedBatch{}, ErrBatch
	}
	return batch, nil
}
