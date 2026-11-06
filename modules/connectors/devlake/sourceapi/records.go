package sourceapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"time"
)

var ErrSource = errors.New("invalid_native_source_response")

type Record struct {
	Kind, ID, Revision, Title, Status, URL, PullURL string
	Raw                                             []byte
	Created, Updated, Merged                        *time.Time
	Private                                         bool
}

func decode(payload []byte, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	if err := decoder.Decode(value); err != nil || decoder.Decode(new(any)) != io.EOF {
		return ErrSource
	}
	return nil
}

func instant(value json.RawMessage) (*time.Time, error) {
	if len(value) == 0 || string(value) == "null" {
		return nil, nil
	}
	var text string
	if json.Unmarshal(value, &text) != nil {
		return nil, ErrSource
	}
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05-0700"} {
		if parsed, err := time.Parse(layout, text); err == nil {
			utc := parsed.UTC()
			return &utc, nil
		}
	}
	return nil, ErrSource
}

func DecodePage(tool, kind, scope string, payload []byte, limit int, private bool) ([]Record, error) {
	if len(payload) == 0 || len(payload) > 8*1024*1024 || limit < 1 || limit > 100 {
		return nil, ErrSource
	}
	if tool == "linear" {
		return decodeLinearPage(kind, scope, payload, limit, private)
	}
	if tool == "slack" {
		return decodeSlackPage(kind, scope, payload, limit, private)
	}
	if tool == "jenkins" {
		return decodeJenkinsPage(kind, scope, payload, limit, private)
	}
	if tool == "argocd" {
		return decodeArgoPage(kind, scope, payload, limit, private)
	}
	if tool == "sonarqube" {
		return decodeSonarPage(kind, scope, payload, limit, private)
	}
	if tool == "pagerduty" {
		return decodePagerDutyPage(kind, scope, payload, limit, private)
	}
	if tool == "gitlab" && kind == "pipeline_detail" {
		return decodeGitLabDetails(scope, payload, limit, private)
	}
	var items []json.RawMessage
	if kind == "repository" && tool == "github" || kind == "board" && tool == "jira" {
		items = []json.RawMessage{payload}
	} else if tool == "jira" && kind == "issue" {
		var page struct {
			Issues []json.RawMessage `json:"issues"`
		}
		if decode(payload, &page) != nil || page.Issues == nil {
			return nil, ErrSource
		}
		items = page.Issues
	} else if tool == "github" && (kind == "issue" || kind == "pull_request") {
		if decode(payload, &items) != nil || items == nil {
			return nil, ErrSource
		}
	} else {
		return nil, ErrSource
	}
	if len(items) > limit {
		return nil, ErrSource
	}
	records := make([]Record, 0, len(items))
	seen := map[string]bool{}
	for _, raw := range items {
		var fields map[string]json.RawMessage
		if decode(raw, &fields) != nil || fields == nil {
			return nil, ErrSource
		}
		var identifier json.Number
		if json.Unmarshal(fields["id"], &identifier) != nil {
			return nil, ErrSource
		}
		if parsed, err := strconv.ParseUint(identifier.String(), 10, 63); err != nil || parsed == 0 || strconv.FormatUint(parsed, 10) != identifier.String() {
			return nil, ErrSource
		}
		record := Record{Kind: kind, ID: identifier.String(), Raw: bytes.Clone(raw), Private: private}
		if seen[record.ID] {
			return nil, ErrSource
		}
		seen[record.ID] = true
		if (kind == "repository" || kind == "board") && record.ID != scope {
			return nil, ErrSource
		}
		data := fields
		if tool == "jira" && kind == "issue" {
			if json.Unmarshal(fields["fields"], &data) != nil || data == nil {
				return nil, ErrSource
			}
			if len(data["security"]) == 0 {
				return nil, ErrSource
			}
			if string(data["security"]) != "null" {
				record.Private = true
			}
			var status struct {
				Name string `json:"name"`
			}
			if json.Unmarshal(data["status"], &status) != nil || json.Unmarshal(data["summary"], &record.Title) != nil {
				return nil, ErrSource
			}
			record.Status = status.Name
		} else {
			titleField := "title"
			if kind == "repository" || kind == "board" {
				titleField = "name"
			}
			if json.Unmarshal(data[titleField], &record.Title) != nil {
				return nil, ErrSource
			}
			if value := data["state"]; len(value) > 0 && json.Unmarshal(value, &record.Status) != nil {
				return nil, ErrSource
			}
		}
		var err error
		created, updated := "created_at", "updated_at"
		if tool == "jira" {
			created, updated = "created", "updated"
		}
		if record.Created, err = instant(data[created]); err != nil {
			return nil, err
		}
		if record.Updated, err = instant(data[updated]); err != nil {
			return nil, err
		}
		if record.Merged, err = instant(data["merged_at"]); err != nil {
			return nil, err
		}
		if kind != "board" && record.Updated == nil {
			return nil, ErrSource
		}
		if (kind == "issue" || kind == "pull_request") && record.Created == nil {
			return nil, ErrSource
		}
		if record.Updated != nil {
			record.Revision = record.Updated.Format(time.RFC3339Nano)
		} else {
			digest := sha256.Sum256(raw)
			record.Revision = hex.EncodeToString(digest[:])
		}
		if value := fields["url"]; len(value) > 0 && json.Unmarshal(value, &record.URL) != nil {
			return nil, ErrSource
		}
		if tool == "github" && kind == "issue" && len(fields["pull_request"]) > 0 {
			var reference struct {
				URL string `json:"url"`
			}
			if json.Unmarshal(fields["pull_request"], &reference) != nil {
				return nil, ErrSource
			}
			record.PullURL = reference.URL
		}
		records = append(records, record)
	}
	return records, nil
}
