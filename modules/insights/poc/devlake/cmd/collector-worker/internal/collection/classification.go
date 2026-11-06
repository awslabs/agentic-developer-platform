package collection

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

var ErrIncomplete = errors.New("collection_incomplete")
var ErrPolicy = errors.New("collection_policy_unavailable")

type NativeKey struct {
	CellID       string
	TenantID     string
	ProjectID    string
	Tool         string
	ConnectionID string
	Scope        string
	Kind         string
	NativeID     string
}

type NativeRecord struct {
	Key      NativeKey
	Revision string
	Payload  json.RawMessage
}

type NativePage struct {
	Records    []NativeRecord
	NextCursor string
	Complete   bool
}

type Decision struct {
	Disposition string
	Reason      string
	Fields      map[string]bool
}

type Classifier interface {
	Classify(context.Context, Source, NativeRecord) (Decision, error)
}

type Exclusion struct {
	Key    NativeKey
	Reason string
}

type Reconciled struct {
	Allowed    []NativeRecord
	Exclusions []Exclusion
}

func Reconcile(ctx context.Context, source Source, connectionID string, pages []NativePage, expected []NativeKey, classifier Classifier) (Reconciled, error) {
	if err := CheckFlags(); err != nil {
		return Reconciled{}, err
	}
	if classifier == nil || connectionID == "" || connectionID != source.ConnectionID || expected == nil || len(pages) < 1 || len(pages) > 5 {
		return Reconciled{}, ErrIncomplete
	}
	population := make(map[NativeKey]bool, len(expected))
	for _, key := range expected {
		if !validKey(key, source, connectionID) || population[key] {
			return Reconciled{}, ErrIncomplete
		}
		population[key] = true
	}
	seen := make(map[NativeKey]bool, len(expected))
	cursors := make(map[string]bool, len(pages))
	result := Reconciled{}
	totalBytes := 0
	for index, page := range pages {
		if len(page.Records) > 100 || page.Complete != (index == len(pages)-1) ||
			page.Complete == (page.NextCursor != "") ||
			(page.NextCursor != "" && cursors[page.NextCursor]) {
			return Reconciled{}, ErrIncomplete
		}
		cursors[page.NextCursor] = true
		for _, record := range page.Records {
			if !validKey(record.Key, source, connectionID) || !population[record.Key] || seen[record.Key] ||
				strings.TrimSpace(record.Revision) == "" || len(record.Payload) == 0 {
				return Reconciled{}, ErrIncomplete
			}
			totalBytes += len(record.Payload)
			if totalBytes > 104857600 {
				return Reconciled{}, ErrIncomplete
			}
			seen[record.Key] = true
			decision, err := classifier.Classify(ctx, source, record)
			if err != nil {
				return Reconciled{}, fmt.Errorf("%w: classification failed", ErrPolicy)
			}
			switch decision.Disposition {
			case "exclude":
				if decision.Reason != "private" && decision.Reason != "out_of_scope" {
					return Reconciled{}, ErrPolicy
				}
				result.Exclusions = append(result.Exclusions, Exclusion{Key: record.Key, Reason: decision.Reason})
			case "allow":
				if !approvedPayload(record.Payload, decision.Fields) {
					return Reconciled{}, ErrPolicy
				}
				record.Payload = bytes.Clone(record.Payload)
				result.Allowed = append(result.Allowed, record)
			default:
				return Reconciled{}, ErrPolicy
			}
		}
	}
	if len(seen) != len(population) {
		return Reconciled{}, ErrIncomplete
	}
	return result, nil
}

func validKey(key NativeKey, source Source, connectionID string) bool {
	return key.CellID == source.CellID && key.TenantID == source.TenantID && key.ProjectID == source.ProjectID &&
		key.Tool == source.Tool && key.ConnectionID == connectionID &&
		key.Scope == source.Scope && key.Kind != "" && key.NativeID != ""
}

func approvedPayload(payload []byte, fields map[string]bool) bool {
	if len(fields) == 0 || len(payload) > 1024*1024 {
		return false
	}
	decoder := json.NewDecoder(bytes.NewReader(payload))
	opening, err := decoder.Token()
	if err != nil || opening != json.Delim('{') {
		return false
	}
	seen := map[string]bool{}
	for decoder.More() {
		token, err := decoder.Token()
		key, ok := token.(string)
		if err != nil || !ok || !fields[key] || seen[key] {
			return false
		}
		seen[key] = true
		var value any
		if err := decoder.Decode(&value); err != nil {
			return false
		}
		switch value.(type) {
		case map[string]any, []any:
			return false
		}
	}
	closing, err := decoder.Token()
	if err != nil || closing != json.Delim('}') || len(seen) == 0 {
		return false
	}
	return decoder.Decode(new(any)) == io.EOF
}
