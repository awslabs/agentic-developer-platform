package sourceapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"time"
)

func decodeGitLabDetails(scope string, payload []byte, limit int, private bool) ([]Record, error) {
	var details []json.RawMessage
	if decode(payload, &details) != nil || details == nil || len(details) > limit || limit > 10 {
		return nil, ErrSource
	}
	projectID, err := strconv.ParseUint(scope, 10, 63)
	if err != nil || projectID == 0 || strconv.FormatUint(projectID, 10) != scope {
		return nil, ErrSource
	}
	seen := make(map[string]bool, len(details))
	records := make([]Record, 0, len(details))
	for _, raw := range details {
		var fields map[string]json.RawMessage
		if decode(raw, &fields) != nil || fields == nil {
			return nil, ErrSource
		}
		var pipelineID, owner json.Number
		if json.Unmarshal(fields["id"], &pipelineID) != nil || json.Unmarshal(fields["project_id"], &owner) != nil ||
			owner.String() != scope {
			return nil, ErrSource
		}
		identifier, err := strconv.ParseUint(pipelineID.String(), 10, 63)
		if err != nil || identifier == 0 || strconv.FormatUint(identifier, 10) != pipelineID.String() || seen[pipelineID.String()] {
			return nil, ErrSource
		}
		seen[pipelineID.String()] = true
		record := Record{Kind: "pipeline", ID: pipelineID.String(), Raw: bytes.Clone(raw), Private: private}
		var sha string
		if json.Unmarshal(fields["status"], &record.Status) != nil || record.Status == "" ||
			json.Unmarshal(fields["sha"], &sha) != nil {
			return nil, ErrSource
		}
		if value := fields["ref"]; len(value) > 0 && string(value) != "null" && json.Unmarshal(value, &record.Title) != nil {
			return nil, ErrSource
		}
		if record.Created, err = instant(fields["created_at"]); err != nil {
			return nil, err
		}
		if record.Updated, err = instant(fields["updated_at"]); err != nil {
			return nil, err
		}
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
