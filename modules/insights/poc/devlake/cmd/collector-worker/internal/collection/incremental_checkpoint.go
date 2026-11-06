package collection

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"time"
)

func PlanPersistedIncremental(ctx context.Context, writer, sealedReader *sql.DB, sources []Source,
	source Source, generation int64, authority Authority, generationReader GenerationReader,
	current Reconciled, nextCursor string, fullEnumeration bool, start, end time.Time,
) (DeltaPlan, CheckpointUpdate, error) {
	if len(sources) < 2 || !validEvidenceSources(sources) || writer == nil || sealedReader == nil {
		return DeltaPlan{}, CheckpointUpdate{}, ErrIncomplete
	}
	selected := false
	for _, candidate := range sources {
		selected = selected || source == candidate
	}
	if !selected {
		return DeltaPlan{}, CheckpointUpdate{}, ErrIncomplete
	}
	if err := checkFence(ctx, source, generation, authority, generationReader); err != nil {
		return DeltaPlan{}, CheckpointUpdate{}, err
	}
	previousCursor, err := ReadSourceCheckpoint(ctx, writer, source)
	if err != nil {
		return DeltaPlan{}, CheckpointUpdate{}, err
	}
	stored, err := ReadCollectedDomainHistory(ctx, sealedReader, sources, authority, start, end)
	if err != nil {
		return DeltaPlan{}, CheckpointUpdate{}, err
	}
	previous := make(map[NativeKey]RecordState)
	for _, item := range stored.Items {
		if item.Key.CellID != source.CellID || item.Key.TenantID != source.TenantID ||
			item.Key.ProjectID != source.ProjectID || item.Key.Tool != source.Tool ||
			item.Key.ConnectionID != source.ConnectionID || item.Key.Scope != source.Scope {
			continue
		}
		digest := sha256.Sum256(item.NativeBytes)
		previous[item.Key] = RecordState{Revision: item.Revision, SHA256: hex.EncodeToString(digest[:])}
	}
	prior := ""
	if previousCursor != nil {
		prior = *previousCursor
	}
	plan, err := PlanIncremental(source, generation, prior, nextCursor, previous, current, fullEnumeration)
	if err != nil {
		return DeltaPlan{}, CheckpointUpdate{}, err
	}
	if err := checkFence(ctx, source, generation, authority, generationReader); err != nil {
		return DeltaPlan{}, CheckpointUpdate{}, err
	}
	return plan, CheckpointUpdate{SourceKey: SourceCheckpointKey(source),
		ExpectedCursor: previousCursor, NextCursor: plan.ProposedCursor}, nil
}

func PrepareIncrementalItems(plan DeltaPlan, upserts []BatchItem, policyVersion, fieldMapVersion, schemaVersion string) ([]BatchItem, error) {
	byKey := make(map[NativeKey]BatchItem, len(upserts))
	for _, item := range upserts {
		if _, exists := byKey[item.Key]; exists || item.RemovalReason != "" || !validItemPayload(item) {
			return nil, ErrBatch
		}
		byKey[item.Key] = item
	}
	items := make([]BatchItem, 0, len(plan.Changes))
	seen := make(map[NativeKey]bool, len(plan.Changes))
	for _, change := range plan.Changes {
		if seen[change.Key] || !validBatchKey(change.Key) {
			return nil, ErrBatch
		}
		seen[change.Key] = true
		switch change.Action {
		case "delete":
			if change.Revision != "" || len(change.NativeBytes) != 0 || change.SHA256 != "" {
				return nil, ErrBatch
			}
			item := BatchItem{Key: change.Key, RemovalReason: change.DeleteReason, PolicyVersion: policyVersion,
				FieldMapVersion: fieldMapVersion, SchemaVersion: schemaVersion}
			if item.RemovalReason == "" || !validItemPayload(item) || policyVersion == "" || fieldMapVersion == "" || schemaVersion == "" {
				return nil, ErrBatch
			}
			items = append(items, item)
		case "upsert":
			item, exists := byKey[change.Key]
			digest := sha256.Sum256(item.NativeBytes)
			if !exists || item.Revision != change.Revision || !bytes.Equal(item.NativeBytes, change.NativeBytes) ||
				hex.EncodeToString(digest[:]) != change.SHA256 {
				return nil, ErrBatch
			}
			items = append(items, item)
			delete(byKey, change.Key)
		default:
			return nil, ErrBatch
		}
	}
	if len(byKey) != 0 {
		return nil, ErrBatch
	}
	return items, nil
}
