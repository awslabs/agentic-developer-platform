package collection

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

type FieldFit struct {
	Tool            string
	FieldMapVersion string
	Field           string
	Present         int
	Missing         int
}

type CollectedDomain struct {
	Items   []BatchItem
	Issues  []CommonIssue
	PRs     []PullRequest
	Fit     []FieldFit
	Metrics *PRMetrics
}

type sealedSelection struct {
	batchID    string
	generation int64
	wanted     map[NativeKey]string
	seen       map[NativeKey]bool
	sourceKeys []string
}

func QueryCollectedDomain(ctx context.Context, reader *sql.DB, sources []Source, authority Authority,
	start, end time.Time) (CollectedDomain, error) {
	return queryCollectedDomain(ctx, reader, sources, authority, start, end, false)
}

func ReadCollectedDomainHistory(ctx context.Context, reader *sql.DB, sources []Source, authority Authority,
	start, end time.Time) (CollectedDomain, error) {
	return queryCollectedDomain(ctx, reader, sources, authority, start, end, true)
}

func queryCollectedDomain(ctx context.Context, reader *sql.DB, sources []Source, authority Authority,
	start, end time.Time, allowMissing bool) (CollectedDomain, error) {
	if err := CheckFlags(); err != nil {
		return CollectedDomain{}, err
	}
	if reader == nil || authority == nil || len(sources) < 2 || !validEvidenceSources(sources) || sources[0].CellID == "" ||
		!start.Before(end) || start.Location() != time.UTC || end.Location() != time.UTC {
		return CollectedDomain{}, ErrMapping
	}
	tools := make(map[string]bool, len(sources))
	for _, source := range sources {
		if tools[source.Tool] || source.CellID != sources[0].CellID ||
			source.TenantID != sources[0].TenantID || source.ProjectID != sources[0].ProjectID ||
			source.ConnectionID == "" || source.Scope == "" || source.BindingID == "" ||
			source.GrantID == "" || source.Consumer == "" || source.BindingGeneration < 1 ||
			source.BindingRevision < 1 {
			return CollectedDomain{}, ErrMapping
		}
		tools[source.Tool] = true
		if err := currentSource(ctx, source, authority); err != nil {
			return CollectedDomain{}, err
		}
	}
	transaction, err := reader.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return CollectedDomain{}, ErrBatch
	}
	defer transaction.Rollback()
	filters := make([]string, 0, len(sources))
	args := []any{sources[0].CellID, sources[0].TenantID, sources[0].ProjectID}
	for index, source := range sources {
		first := 4 + index*3
		filters = append(filters, fmt.Sprintf("(item.item_key::jsonb->>'Tool' = $%d AND item.item_key::jsonb->>'ConnectionID' = $%d AND item.item_key::jsonb->>'Scope' = $%d)", first, first+1, first+2))
		args = append(args, source.Tool, source.ConnectionID, source.Scope)
	}
	query := `SELECT manifest.batch_id::text, manifest.generation, manifest.item_count,
		manifest.manifest_payload, manifest.content_digest, coalesce(item.item_key, ''), item.item_payload,
		item.native_payload, item.normalized_payload, item.payload_digest
		FROM devlake_meta.collection_batch_manifests AS manifest
		LEFT JOIN devlake_meta.collection_batch_items AS item ON item.batch_id = manifest.batch_id
		WHERE manifest.cell_id = $1 AND (manifest.item_count = 0 OR (item.item_key::jsonb->>'TenantID' = $2
		AND item.item_key::jsonb->>'ProjectID' = $3 AND item.item_key::jsonb->>'CellID' = $1
		AND (` + strings.Join(filters, " OR ") + `)))
		ORDER BY manifest.generation DESC, item.item_key LIMIT 100001`
	rows, err := transaction.QueryContext(ctx, query, args...)
	if err != nil {
		return CollectedDomain{}, ErrBatch
	}
	latest := make(map[NativeKey]BatchItem)
	selections := make(map[string]*sealedSelection)
	newest := make(map[NativeKey]int64)
	covered := make(map[string]bool)
	count, totalBytes := 0, 0
	for rows.Next() {
		count++
		var batchID, itemKey string
		var generation, itemCount int64
		var manifestBytes, manifestDigest, itemBytes, nativeBytes, normalizedBytes, itemDigest []byte
		if err := rows.Scan(&batchID, &generation, &itemCount, &manifestBytes, &manifestDigest,
			&itemKey, &itemBytes, &nativeBytes, &normalizedBytes, &itemDigest); err != nil {
			rows.Close()
			return CollectedDomain{}, ErrBatch
		}
		totalBytes += len(itemBytes)
		if count > 100000 || totalBytes > 104857600 {
			rows.Close()
			return CollectedDomain{}, ErrIncomplete
		}
		selection := selections[batchID]
		if selection == nil {
			selection, err = selectManifest(batchID, sources, generation, itemCount, manifestBytes, manifestDigest)
			if err != nil {
				rows.Close()
				return CollectedDomain{}, err
			}
			selections[batchID] = selection
		} else if selection.generation != generation {
			rows.Close()
			return CollectedDomain{}, ErrBatch
		}
		for _, source := range sources {
			if containsSourceKey(selection.sourceKeys, SourceCheckpointKey(source)) {
				covered[source.Tool] = true
			}
		}
		if itemCount == 0 {
			if itemKey != "" || len(itemBytes) != 0 || len(nativeBytes) != 0 || len(normalizedBytes) != 0 || len(itemDigest) != 0 {
				rows.Close()
				return CollectedDomain{}, ErrBatch
			}
			continue
		}
		var key NativeKey
		var item BatchItem
		if json.Unmarshal([]byte(itemKey), &key) != nil || json.Unmarshal(itemBytes, &item) != nil ||
			key != item.Key || !validItemPayload(item) || !belongsToSources(key, sources) || selection.seen[key] ||
			!bytes.Equal(item.NativeBytes, nativeBytes) || !bytes.Equal(item.NormalizedBytes, normalizedBytes) {
			rows.Close()
			return CollectedDomain{}, ErrBatch
		}
		encoded, err := json.Marshal(item)
		digest := sha256.Sum256(itemBytes)
		if err != nil || !bytes.Equal(encoded, itemBytes) || !bytes.Equal(itemDigest, digest[:]) ||
			selection.wanted[key] != hex.EncodeToString(digest[:]) {
			rows.Close()
			return CollectedDomain{}, ErrBatch
		}
		selection.seen[key] = true
		if previous, exists := newest[key]; !exists || generation > previous {
			newest[key] = generation
			latest[key] = item
		} else if generation == previous {
			rows.Close()
			return CollectedDomain{}, ErrBatch
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return CollectedDomain{}, ErrBatch
	}
	rows.Close()
	if count == 0 && !allowMissing {
		return CollectedDomain{}, ErrIncomplete
	}
	for _, selection := range selections {
		if len(selection.wanted) != len(selection.seen) {
			return CollectedDomain{}, ErrIncomplete
		}
	}
	if allowMissing {
		for _, source := range sources {
			covered[source.Tool] = true
		}
	}
	result, err := collectedDomain(latest, sources, covered, start, end)
	if err != nil {
		return CollectedDomain{}, err
	}
	for _, source := range sources {
		if err := currentSource(ctx, source, authority); err != nil {
			return CollectedDomain{}, err
		}
	}
	if err := CheckFlags(); err != nil {
		return CollectedDomain{}, err
	}
	if err := transaction.Commit(); err != nil {
		return CollectedDomain{}, ErrBatch
	}
	return result, nil
}

func belongsToSources(key NativeKey, sources []Source) bool {
	for _, source := range sources {
		if key.CellID == source.CellID && key.TenantID == source.TenantID &&
			key.ProjectID == source.ProjectID && key.Tool == source.Tool &&
			key.ConnectionID == source.ConnectionID && key.Scope == source.Scope {
			return true
		}
	}
	return false
}

func selectManifest(batchID string, sources []Source, generation, itemCount int64,
	payload, digest []byte) (*sealedSelection, error) {
	hash := sha256.Sum256(payload)
	if !bytes.Equal(hash[:], digest) || len(payload) > 104857600 || !batchIDPattern.MatchString(batchID) {
		return nil, ErrBatch
	}
	var manifest struct {
		BatchID        string
		CellID         string
		Generation     int64
		ItemDigests    []BatchDigest
		SourceKeys     []string                      `json:",omitempty"`
		SourceBindings map[string]ObservationBinding `json:",omitempty"`
	}
	if json.Unmarshal(payload, &manifest) != nil || manifest.BatchID != batchID ||
		manifest.CellID != sources[0].CellID || manifest.Generation != generation ||
		itemCount < 0 || itemCount == 0 && len(manifest.SourceKeys) == 0 ||
		itemCount != int64(len(manifest.ItemDigests)) || itemCount > 100000 {
		return nil, ErrBatch
	}
	encoded, err := json.Marshal(manifest)
	if err != nil || !bytes.Equal(encoded, payload) {
		return nil, ErrBatch
	}
	selection := &sealedSelection{batchID: batchID, generation: generation,
		wanted: make(map[NativeKey]string), seen: make(map[NativeKey]bool), sourceKeys: manifest.SourceKeys}
	for index, key := range manifest.SourceKeys {
		if !validSourceKey(key, sources[0].CellID) || index > 0 && key <= manifest.SourceKeys[index-1] {
			return nil, ErrBatch
		}
	}
	all := make(map[NativeKey]bool, len(manifest.ItemDigests))
	for _, item := range manifest.ItemDigests {
		if !validBatchKey(item.Key) || item.Key.CellID != sources[0].CellID || all[item.Key] {
			return nil, ErrBatch
		}
		all[item.Key] = true
		if belongsToSources(item.Key, sources) {
			if len(item.SHA256) != 64 {
				return nil, ErrBatch
			}
			if _, err := hex.DecodeString(item.SHA256); err != nil {
				return nil, ErrBatch
			}
			selection.wanted[item.Key] = item.SHA256
		}
	}
	return selection, nil
}

func collectedDomain(latest map[NativeKey]BatchItem, sources []Source, covered map[string]bool, start, end time.Time) (CollectedDomain, error) {
	result := CollectedDomain{}
	fit := make(map[string]*FieldFit)
	issueCounts := make(map[string]int)
	for _, item := range latest {
		if item.RemovalReason != "" {
			covered[item.Key.Tool] = true
			continue
		}
		result.Items = append(result.Items, item)
	}
	sort.Slice(result.Items, func(left, right int) bool {
		leftKey, _ := json.Marshal(result.Items[left].Key)
		rightKey, _ := json.Marshal(result.Items[right].Key)
		return bytes.Compare(leftKey, rightKey) < 0
	})
	for _, item := range result.Items {
		switch item.Key.Kind {
		case "issue":
			var issue CommonIssue
			if json.Unmarshal(item.NormalizedBytes, &issue) != nil || issue.Key != item.Key ||
				issue.Revision != item.Revision || issue.FieldMap != item.FieldMapVersion {
				return CollectedDomain{}, ErrMapping
			}
			native, err := json.Marshal(json.RawMessage(item.NativeBytes))
			if err != nil || !bytes.Equal(issue.Native, native) {
				return CollectedDomain{}, ErrMapping
			}
			issue.Native = bytes.Clone(item.NativeBytes)
			result.Issues = append(result.Issues, issue)
			issueCounts[item.Key.Tool]++
			for _, field := range []struct {
				name    string
				present bool
			}{
				{"title", issue.Title != ""}, {"original_status", issue.OriginalStatus != ""},
				{"common_status", issue.CommonStatus != "" && issue.CommonStatus != "unavailable"},
				{"created_at", issue.CreatedAt != nil}, {"updated_at", issue.UpdatedAt != nil},
			} {
				fitKey := item.Key.Tool + "\x00" + item.FieldMapVersion + "\x00" + field.name
				if fit[fitKey] == nil {
					fit[fitKey] = &FieldFit{Tool: item.Key.Tool, FieldMapVersion: item.FieldMapVersion, Field: field.name}
				}
				if field.present {
					fit[fitKey].Present++
				} else {
					fit[fitKey].Missing++
				}
			}
		case "pull_request":
			if item.Key.Tool != "github" {
				return CollectedDomain{}, ErrMapping
			}
			var request PullRequest
			if json.Unmarshal(item.NormalizedBytes, &request) != nil || request.Key != item.Key {
				return CollectedDomain{}, ErrMapping
			}
			result.PRs = append(result.PRs, request)
		}
	}
	for _, source := range sources {
		if issueCounts[source.Tool] == 0 && !covered[source.Tool] {
			return CollectedDomain{}, ErrIncomplete
		}
	}
	for _, value := range fit {
		result.Fit = append(result.Fit, *value)
	}
	sort.Slice(result.Fit, func(left, right int) bool {
		first, second := result.Fit[left], result.Fit[right]
		if first.Tool != second.Tool {
			return first.Tool < second.Tool
		}
		if first.FieldMapVersion != second.FieldMapVersion {
			return first.FieldMapVersion < second.FieldMapVersion
		}
		return first.Field < second.Field
	})
	if len(result.PRs) != 0 {
		metrics, err := MeasurePRs(result.PRs, start, end)
		if err != nil {
			return CollectedDomain{}, err
		}
		result.Metrics = &metrics
	}
	return result, nil
}
