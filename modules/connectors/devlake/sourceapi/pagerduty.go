package sourceapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"
)

func decodePagerDutyPage(kind, scope string, payload []byte, limit int, private bool) ([]Record, error) {
	var envelope map[string]json.RawMessage
	if decode(payload, &envelope) != nil || envelope == nil {
		return nil, ErrSource
	}
	var items []json.RawMessage
	switch kind {
	case "service":
		if len(envelope["service"]) == 0 {
			return nil, ErrSource
		}
		items = []json.RawMessage{envelope["service"]}
	case "incident":
		if decode(envelope["incidents"], &items) != nil || items == nil {
			return nil, ErrSource
		}
	default:
		return nil, ErrSource
	}
	if len(items) > limit {
		return nil, ErrSource
	}
	records := make([]Record, 0, len(items))
	seen := make(map[string]bool, len(items))
	for _, raw := range items {
		var fields map[string]json.RawMessage
		if decode(raw, &fields) != nil || fields == nil {
			return nil, ErrSource
		}
		var record Record
		if json.Unmarshal(fields["id"], &record.ID) != nil || record.ID == "" || seen[record.ID] {
			return nil, ErrSource
		}
		seen[record.ID] = true
		if kind == "service" {
			if record.ID != scope || json.Unmarshal(fields["name"], &record.Title) != nil {
				return nil, ErrSource
			}
		} else {
			var service struct {
				ID string `json:"id"`
			}
			if json.Unmarshal(fields["service"], &service) != nil || service.ID != scope ||
				json.Unmarshal(fields["title"], &record.Title) != nil ||
				json.Unmarshal(fields["status"], &record.Status) != nil {
				return nil, ErrSource
			}
		}
		var err error
		if record.Created, err = instant(fields["created_at"]); err != nil {
			return nil, err
		}
		updatedField := "updated_at"
		if kind == "incident" {
			// DevLake maps the native last status change to the incident update time.
			updatedField = "last_status_change_at"
		}
		if record.Updated, err = instant(fields[updatedField]); err != nil {
			return nil, err
		}
		if kind == "incident" && (record.Created == nil || record.Updated == nil) {
			return nil, ErrSource
		}
		record.Kind, record.Raw, record.Private = kind, bytes.Clone(raw), private
		if record.Updated != nil {
			record.Revision = record.Updated.Format(time.RFC3339Nano)
		} else {
			digest := sha256.Sum256(raw)
			record.Revision = hex.EncodeToString(digest[:])
		}
		records = append(records, record)
	}
	return records, nil
}
