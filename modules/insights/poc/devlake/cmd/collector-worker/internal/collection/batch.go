package collection

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/apache/devlake/core/models/common"
)

var ErrBatch = errors.New("collection_batch_invalid")
var batchIDPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

type EvidenceLink struct {
	From             NativeKey
	To               NativeKey
	Relation         string
	NativeEvidenceID string
}

type BatchItem struct {
	Key                NativeKey
	Revision           string
	NativeConnectionID uint64                `json:",omitempty"`
	RawDataOrigin      *common.RawDataOrigin `json:",omitempty"`
	NativeBytes        []byte
	NormalizedBytes    []byte
	Links              []EvidenceLink
	PolicyVersion      string
	FieldMapVersion    string
	SchemaVersion      string
	RemovalReason      string `json:",omitempty"`
}

type BatchDigest struct {
	Key    NativeKey
	SHA256 string
}

type PreparedBatch struct {
	BatchID         string
	CellID          string
	Generation      int64
	Items           []BatchItem
	ItemDigests     []BatchDigest
	ItemPayloads    [][]byte
	ManifestPayload []byte
	ContentSHA256   string
	SourceKeys      []string
	SourceBindings  map[string]ObservationBinding
}

func PrepareBatch(batchID, cellID string, generation int64, items []BatchItem) (PreparedBatch, error) {
	return prepareSourceBatch(batchID, cellID, generation, items, nil, nil)
}

func PrepareSourceBatch(batchID, cellID string, generation int64, items []BatchItem, sources []Source) (PreparedBatch, error) {
	if !validEvidenceSources(sources) {
		return PreparedBatch{}, ErrBatch
	}
	tools := make(map[string]bool, len(sources))
	keys := make([]string, 0, len(sources))
	bindings := make(map[string]ObservationBinding, len(sources))
	for _, source := range sources {
		if source.CellID != cellID || source.TenantID != sources[0].TenantID || source.ProjectID != sources[0].ProjectID ||
			tools[source.Tool] || !supportedEvidenceTool(source.Tool) {
			return PreparedBatch{}, ErrBatch
		}
		tools[source.Tool] = true
		keys = append(keys, SourceCheckpointKey(source))
		bindings[SourceCheckpointKey(source)] = ObservationBinding{source.BindingID, source.BindingGeneration, source.BindingRevision}
	}
	return prepareSourceBatch(batchID, cellID, generation, items, keys, bindings)
}

func prepareSourceBatch(batchID, cellID string, generation int64, items []BatchItem, sourceKeys []string, bindings map[string]ObservationBinding) (PreparedBatch, error) {
	if !batchIDPattern.MatchString(batchID) || cellID == "" || generation < 1 ||
		len(items) == 0 && len(sourceKeys) == 0 || len(items) > 100000 {
		return PreparedBatch{}, ErrBatch
	}
	prepared := PreparedBatch{BatchID: batchID, CellID: cellID, Generation: generation}
	prepared.SourceKeys = append([]string(nil), sourceKeys...)
	sort.Strings(prepared.SourceKeys)
	for index, sourceKey := range prepared.SourceKeys {
		if !validSourceKey(sourceKey, cellID) || index > 0 && sourceKey == prepared.SourceKeys[index-1] {
			return PreparedBatch{}, ErrBatch
		}
	}
	if len(bindings) > 0 {
		if len(bindings) != len(prepared.SourceKeys) {
			return PreparedBatch{}, ErrBatch
		}
		prepared.SourceBindings = make(map[string]ObservationBinding, len(bindings))
		for key, binding := range bindings {
			if !containsSourceKey(prepared.SourceKeys, key) || binding.ID == "" || binding.Generation < 1 || binding.Revision < 1 {
				return PreparedBatch{}, ErrBatch
			}
			prepared.SourceBindings[key] = binding
		}
	}
	prepared.ItemDigests = make([]BatchDigest, 0, len(items))
	prepared.Items = make([]BatchItem, 0, len(items))
	seen := make(map[NativeKey]bool, len(items))
	totalBytes := 0
	for _, item := range items {
		if !validBatchKey(item.Key) || !validItemPayload(item) ||
			item.Key.CellID != cellID || item.PolicyVersion == "" || item.FieldMapVersion == "" || item.SchemaVersion == "" ||
			seen[item.Key] {
			return PreparedBatch{}, ErrBatch
		}
		if len(sourceKeys) > 0 {
			key := SourceCheckpointKey(Source{CellID: item.Key.CellID, TenantID: item.Key.TenantID,
				ProjectID: item.Key.ProjectID, Tool: item.Key.Tool, ConnectionID: item.Key.ConnectionID, Scope: item.Key.Scope})
			if !containsSourceKey(prepared.SourceKeys, key) {
				return PreparedBatch{}, ErrBatch
			}
		}
		seen[item.Key] = true
		if expandedEvidenceTool(item.Key.Tool) && len(item.Links) != 0 {
			return PreparedBatch{}, ErrBatch
		}
		links := make(map[EvidenceLink]bool, len(item.Links))
		for _, link := range item.Links {
			if !validBatchKey(link.From) || !validBatchKey(link.To) ||
				link.From != item.Key || link.From.TenantID != link.To.TenantID ||
				link.From.ProjectID != link.To.ProjectID || link.From.CellID != link.To.CellID ||
				link.Relation != "source-reference" || link.NativeEvidenceID == "" || links[link] {
				return PreparedBatch{}, ErrBatch
			}
			links[link] = true
		}
		copyItem := item
		if item.RawDataOrigin != nil {
			origin := *item.RawDataOrigin
			copyItem.RawDataOrigin = &origin
		}
		copyItem.NativeBytes = bytes.Clone(item.NativeBytes)
		copyItem.NormalizedBytes = bytes.Clone(item.NormalizedBytes)
		copyItem.Links = append([]EvidenceLink(nil), item.Links...)
		encoded, err := json.Marshal(copyItem)
		if err != nil || len(encoded) > 104857600-totalBytes {
			return PreparedBatch{}, ErrBatch
		}
		totalBytes += len(encoded)
		prepared.Items = append(prepared.Items, copyItem)
	}
	sort.Slice(prepared.Items, func(left, right int) bool {
		leftKey, _ := json.Marshal(prepared.Items[left].Key)
		rightKey, _ := json.Marshal(prepared.Items[right].Key)
		return bytes.Compare(leftKey, rightKey) < 0
	})
	for _, item := range prepared.Items {
		encoded, _ := json.Marshal(item)
		prepared.ItemPayloads = append(prepared.ItemPayloads, bytes.Clone(encoded))
		digest := sha256.Sum256(encoded)
		prepared.ItemDigests = append(prepared.ItemDigests, BatchDigest{item.Key, hex.EncodeToString(digest[:])})
	}
	manifest, _ := json.Marshal(struct {
		BatchID        string
		CellID         string
		Generation     int64
		ItemDigests    []BatchDigest
		SourceKeys     []string                      `json:",omitempty"`
		SourceBindings map[string]ObservationBinding `json:",omitempty"`
	}{batchID, cellID, generation, prepared.ItemDigests, prepared.SourceKeys, prepared.SourceBindings})
	prepared.ManifestPayload = bytes.Clone(manifest)
	digest := sha256.Sum256(manifest)
	prepared.ContentSHA256 = hex.EncodeToString(digest[:])
	return prepared, nil
}

func VerifyPreparedBatch(batch PreparedBatch) error {
	actual, err := prepareSourceBatch(batch.BatchID, batch.CellID, batch.Generation, batch.Items, batch.SourceKeys, batch.SourceBindings)
	if err != nil || len(batch.ItemDigests) != len(actual.ItemDigests) ||
		len(batch.ItemPayloads) != len(actual.ItemPayloads) ||
		!bytes.Equal(batch.ManifestPayload, actual.ManifestPayload) || batch.ContentSHA256 != actual.ContentSHA256 {
		return ErrBatch
	}
	for index, digest := range actual.ItemDigests {
		if batch.ItemDigests[index] != digest || !bytes.Equal(batch.ItemPayloads[index], actual.ItemPayloads[index]) {
			return fmt.Errorf("%w: item digest mismatch", ErrBatch)
		}
	}
	return nil
}

func validItemPayload(item BatchItem) bool {
	if item.RemovalReason != "" {
		return item.NativeConnectionID == 0 && (item.RemovalReason == "policy_excluded" || item.RemovalReason == "absent_in_complete_source") &&
			item.Revision == "" && len(item.NativeBytes) == 0 && len(item.NormalizedBytes) == 0 && len(item.Links) == 0 && item.RawDataOrigin == nil
	}
	if expandedEvidenceTool(item.Key.Tool) {
		origin := item.RawDataOrigin
		if origin == nil || origin.RawDataId == 0 || origin.RawDataParams == "" ||
			!strings.HasPrefix(origin.RawDataTable, "_raw_"+item.Key.Tool+"_") {
			return false
		}
		var params struct {
			ConnectionID uint64 `json:"connectionId"`
		}
		if json.Unmarshal([]byte(origin.RawDataParams), &params) != nil || params.ConnectionID == 0 ||
			params.ConnectionID != item.NativeConnectionID {
			return false
		}
	}
	return item.Revision != "" && len(item.NativeBytes) > 0 && len(item.NormalizedBytes) > 0
}

func validSourceKey(encoded, cellID string) bool {
	var source Source
	if json.Unmarshal([]byte(encoded), &source) != nil || SourceCheckpointKey(source) != encoded || source.CellID != cellID {
		return false
	}
	return source.TenantID != "" && source.ProjectID != "" && supportedEvidenceTool(source.Tool) &&
		source.ConnectionID != "" && source.Scope != ""
}

func containsSourceKey(keys []string, key string) bool {
	index := sort.SearchStrings(keys, key)
	return index < len(keys) && keys[index] == key
}

func expandedEvidenceTool(tool string) bool {
	switch tool {
	case "sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack":
		return true
	}
	return false
}

func validEvidenceSources(sources []Source) bool {
	if len(sources) == 0 || len(sources) > 9 {
		return false
	}
	for _, source := range sources {
		if expandedEvidenceTool(source.Tool) {
			return true
		}
	}
	return len(sources) == 2
}

func supportedEvidenceTool(tool string) bool {
	return tool == "github" || tool == "jira" || expandedEvidenceTool(tool)
}

func validBatchKey(key NativeKey) bool {
	return key.CellID != "" && key.TenantID != "" && key.ProjectID != "" && supportedEvidenceTool(key.Tool) &&
		key.ConnectionID != "" && key.Scope != "" && key.Kind != "" && key.NativeID != ""
}
