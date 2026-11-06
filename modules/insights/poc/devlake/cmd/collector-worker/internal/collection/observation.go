package collection

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	"github.com/apache/devlake/core/models/common"
)

const ObservationSchemaVersion = "devlake-observation/1.0"

type ObservationSnapshot struct {
	BatchID        string `json:"batch_id"`
	Generation     int64  `json:"generation"`
	ManifestSHA256 string `json:"manifest_sha256"`
	ItemSHA256     string `json:"item_sha256"`
}

type ObservationBinding struct {
	ID         string `json:"id"`
	Generation int64  `json:"generation"`
	Revision   int64  `json:"revision"`
}

type QualifiedRef struct {
	TenantID           string `json:"tenant_id"`
	ProjectID          string `json:"project_id"`
	SourceConnectionID string `json:"source_connection_id"`
	RecordType         string `json:"record_type"`
	RecordID           string `json:"record_id"`
	RecordRevision     string `json:"record_revision"`
}

func qualify(key NativeKey, revision string) QualifiedRef {
	return QualifiedRef{key.TenantID, key.ProjectID, key.ConnectionID, key.Kind, key.NativeID, revision}
}

type QualifiedRelationship struct {
	From             QualifiedRef `json:"from"`
	To               QualifiedRef `json:"to"`
	Relation         string       `json:"relation"`
	NativeEvidenceID string       `json:"native_evidence_id"`
}

type Observation struct {
	SchemaVersion         string                  `json:"schema_version"`
	MappingVersion        string                  `json:"mapping_version"`
	SubjectRef            QualifiedRef            `json:"subject_ref"`
	SourceRef             QualifiedRef            `json:"source_ref"`
	SourceNativeKey       NativeKey               `json:"source_native_key"`
	SourceRevision        string                  `json:"source_revision"`
	SourceSnapshot        ObservationSnapshot     `json:"source_snapshot"`
	Binding               ObservationBinding      `json:"binding"`
	RawDataOrigin         *common.RawDataOrigin   `json:"raw_data_origin,omitempty"`
	NativeBytes           []byte                  `json:"native_bytes"`
	NativeSHA256          string                  `json:"native_sha256"`
	NormalizedBytes       []byte                  `json:"normalized_bytes"`
	NormalizedSHA256      string                  `json:"normalized_sha256"`
	FieldMapVersion       string                  `json:"field_map_version"`
	SourceSchema          string                  `json:"source_schema_version"`
	SourcePolicy          string                  `json:"source_policy_version"`
	Relationships         []EvidenceLink          `json:"relationships"`
	ResolvedRelationships []QualifiedRelationship `json:"resolved_relationships"`
	ObservationStatus     string                  `json:"observation_status"`
}

func MapPreparedObservations(batch PreparedBatch, sources []Source) ([]Observation, error) {
	if VerifyPreparedBatch(batch) != nil || len(batch.SourceKeys) != len(sources) || len(sources) == 0 ||
		len(batch.SourceBindings) != len(sources) {
		return nil, ErrBatch
	}
	bySource := make(map[string]Source, len(sources))
	for _, source := range sources {
		key := SourceCheckpointKey(source)
		if source.CellID != batch.CellID || source.TenantID == "" || source.ProjectID == "" ||
			source.BindingID == "" || source.BindingGeneration < 1 || source.BindingRevision < 1 ||
			!containsSourceKey(batch.SourceKeys, key) {
			return nil, ErrBatch
		}
		bySource[key] = source
	}
	if len(bySource) != len(sources) {
		return nil, ErrBatch
	}
	observations := make([]Observation, 0, len(batch.Items))
	revisions := make(map[NativeKey]string, len(batch.Items))
	for _, item := range batch.Items {
		if item.RemovalReason == "" {
			revisions[item.Key] = item.Revision
		}
	}
	for index, item := range batch.Items {
		if item.RemovalReason != "" {
			continue
		}
		key := SourceCheckpointKey(Source{CellID: item.Key.CellID, TenantID: item.Key.TenantID,
			ProjectID: item.Key.ProjectID, Tool: item.Key.Tool, ConnectionID: item.Key.ConnectionID, Scope: item.Key.Scope})
		_, exists := bySource[key]
		if !exists {
			return nil, ErrBatch
		}
		actualItem, err := json.Marshal(item)
		if err != nil || !bytes.Equal(actualItem, batch.ItemPayloads[index]) {
			return nil, ErrBatch
		}
		resolved := make([]QualifiedRelationship, 0, len(item.Links))
		for _, link := range item.Links {
			revision, exists := revisions[link.To]
			if !exists {
				return nil, ErrBatch
			}
			resolved = append(resolved, QualifiedRelationship{qualify(item.Key, item.Revision), qualify(link.To, revision), link.Relation, link.NativeEvidenceID})
		}
		var rawOrigin *common.RawDataOrigin
		if item.RawDataOrigin != nil {
			origin := *item.RawDataOrigin
			rawOrigin = &origin
		}
		nativeDigest := sha256.Sum256(item.NativeBytes)
		normalizedDigest := sha256.Sum256(item.NormalizedBytes)
		observations = append(observations, Observation{
			SchemaVersion: ObservationSchemaVersion, MappingVersion: ObservationSchemaVersion,
			SubjectRef: qualify(item.Key, item.Revision), SourceRef: qualify(item.Key, item.Revision),
			SourceNativeKey: item.Key, SourceRevision: item.Revision,
			SourceSnapshot: ObservationSnapshot{batch.BatchID, batch.Generation, batch.ContentSHA256, batch.ItemDigests[index].SHA256},
			Binding:        batch.SourceBindings[key], RawDataOrigin: rawOrigin,
			NativeBytes: bytes.Clone(item.NativeBytes), NativeSHA256: hex.EncodeToString(nativeDigest[:]),
			NormalizedBytes: bytes.Clone(item.NormalizedBytes), NormalizedSHA256: hex.EncodeToString(normalizedDigest[:]),
			FieldMapVersion: item.FieldMapVersion, SourceSchema: item.SchemaVersion, SourcePolicy: item.PolicyVersion,
			Relationships: append([]EvidenceLink{}, item.Links...), ResolvedRelationships: resolved, ObservationStatus: "observation_only",
		})
	}
	return observations, nil
}
