package sourceapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

func decodeArgoPage(kind, scope string, payload []byte, limit int, private bool) ([]Record, error) {
	if kind != "application" && kind != "sync_operation" || scope == "" {
		return nil, ErrSource
	}
	var application struct {
		Metadata struct {
			Name string `json:"name"`
		} `json:"metadata"`
		Spec struct {
			Project string `json:"project"`
		} `json:"spec"`
		Status struct {
			History        json.RawMessage `json:"history"`
			OperationState json.RawMessage `json:"operationState"`
		} `json:"status"`
	}
	if decode(payload, &application) != nil || application.Metadata.Name != scope || application.Spec.Project == "" {
		return nil, ErrSource
	}
	if kind == "sync_operation" {
		var history []json.RawMessage
		if decode(application.Status.History, &history) != nil || history == nil {
			return nil, ErrSource
		}
		current := application.Status.OperationState
		if len(history) > limit || len(current) > 0 && string(current) != "null" && len(history) >= limit {
			return nil, ErrSource
		}
		for _, operation := range history {
			var entry map[string]json.RawMessage
			if decode(operation, &entry) != nil || entry == nil {
				return nil, ErrSource
			}
		}
		if len(current) > 0 && string(current) != "null" {
			var entry map[string]json.RawMessage
			if decode(current, &entry) != nil || entry == nil {
				return nil, ErrSource
			}
		}
	}
	digest := sha256.Sum256(payload)
	return []Record{{Kind: kind, ID: scope, Title: scope, Revision: hex.EncodeToString(digest[:]), Raw: bytes.Clone(payload), Private: private}}, nil
}
