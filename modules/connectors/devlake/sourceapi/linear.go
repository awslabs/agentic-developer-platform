package sourceapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"
)

func decodeLinearPage(kind, scope string, payload []byte, limit int, private bool) ([]Record, error) {
	if kind != "team" && kind != "issue" || scope == "" {
		return nil, ErrSource
	}
	var envelope struct {
		Errors json.RawMessage `json:"errors"`
		Data   struct {
			Team struct {
				ID     string          `json:"id"`
				Name   string          `json:"name"`
				Issues json.RawMessage `json:"issues"`
			} `json:"team"`
		} `json:"data"`
	}
	if decode(payload, &envelope) != nil || envelope.Data.Team.ID != scope || envelope.Errors != nil && string(envelope.Errors) != "[]" {
		return nil, ErrSource
	}
	if kind == "team" {
		var data struct {
			Data map[string]json.RawMessage `json:"data"`
		}
		if decode(payload, &data) != nil || len(data.Data["team"]) == 0 {
			return nil, ErrSource
		}
		selected := data.Data["team"]
		digest := sha256.Sum256(selected)
		return []Record{{Kind: kind, ID: scope, Title: envelope.Data.Team.Name, Revision: hex.EncodeToString(digest[:]), Raw: bytes.Clone(selected), Private: private}}, nil
	}
	var issues struct {
		Nodes []json.RawMessage `json:"nodes"`
	}
	if decode(envelope.Data.Team.Issues, &issues) != nil || issues.Nodes == nil || len(issues.Nodes) > limit {
		return nil, ErrSource
	}
	seen := make(map[string]bool, len(issues.Nodes))
	records := make([]Record, 0, len(issues.Nodes))
	for _, raw := range issues.Nodes {
		var issue struct {
			ID        string          `json:"id"`
			Title     string          `json:"title"`
			CreatedAt json.RawMessage `json:"createdAt"`
			UpdatedAt json.RawMessage `json:"updatedAt"`
			State     struct {
				Type string `json:"type"`
			} `json:"state"`
		}
		if decode(raw, &issue) != nil || issue.ID == "" || seen[issue.ID] {
			return nil, ErrSource
		}
		seen[issue.ID] = true
		created, err := instant(issue.CreatedAt)
		if err != nil || created == nil {
			return nil, ErrSource
		}
		updated, err := instant(issue.UpdatedAt)
		if err != nil || updated == nil || updated.Before(*created) {
			return nil, ErrSource
		}
		records = append(records, Record{Kind: kind, ID: issue.ID, Title: issue.Title, Status: issue.State.Type,
			Created: created, Updated: updated, Revision: updated.Format(time.RFC3339Nano), Raw: bytes.Clone(raw), Private: private})
	}
	return records, nil
}
