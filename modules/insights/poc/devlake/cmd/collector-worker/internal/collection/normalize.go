package collection

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"time"
)

var ErrMapping = errors.New("source_mapping_unavailable")
var nativeNumber = regexp.MustCompile(`^(0|[1-9][0-9]*)$`)

type IssueMapping struct {
	IDField      string
	TitleField   string
	StatusField  string
	CreatedField string
	UpdatedField string
	Statuses     map[string]string
}

type CommonIssue struct {
	Key            NativeKey
	Revision       string
	FieldMap       string
	Native         json.RawMessage
	Title          string
	OriginalStatus string
	CommonStatus   string
	CreatedAt      *time.Time
	UpdatedAt      *time.Time
}

func NormalizeIssues(records []NativeRecord, mappings map[string]IssueMapping, fieldMapVersion string) ([]CommonIssue, error) {
	if fieldMapVersion == "" {
		return nil, ErrMapping
	}
	issues := make([]CommonIssue, 0, len(records))
	seen := make(map[NativeKey]bool, len(records))
	for _, record := range records {
		mapping, ok := mappings[record.Key.Tool]
		if !ok || record.Key.Kind != "issue" || record.Key.CellID == "" || record.Key.TenantID == "" || record.Key.ProjectID == "" || record.Key.ConnectionID == "" ||
			record.Key.Scope == "" || record.Key.NativeID == "" ||
			record.Revision == "" || seen[record.Key] ||
			mapping.IDField == "" || mapping.TitleField == "" || mapping.StatusField == "" ||
			mapping.CreatedField == "" || mapping.UpdatedField == "" {
			return nil, ErrMapping
		}
		seen[record.Key] = true
		var native map[string]json.RawMessage
		decoder := json.NewDecoder(bytes.NewReader(record.Payload))
		if err := decoder.Decode(&native); err != nil || native == nil || decoder.Decode(new(any)) != io.EOF {
			return nil, ErrMapping
		}
		var nativeID json.Number
		if err := json.Unmarshal(native[mapping.IDField], &nativeID); err != nil ||
			!nativeNumber.MatchString(nativeID.String()) || nativeID.String() != record.Key.NativeID {
			return nil, ErrMapping
		}
		issue := CommonIssue{Key: record.Key, Revision: record.Revision,
			FieldMap: fieldMapVersion, Native: bytes.Clone(record.Payload), CommonStatus: "unavailable"}
		for _, field := range []struct {
			name   string
			output *string
		}{{mapping.TitleField, &issue.Title}, {mapping.StatusField, &issue.OriginalStatus}} {
			if value := native[field.name]; len(value) != 0 && string(value) != "null" {
				if err := json.Unmarshal(value, field.output); err != nil {
					return nil, ErrMapping
				}
			}
		}
		if status, exists := mapping.Statuses[issue.OriginalStatus]; exists && status != "" {
			issue.CommonStatus = status
		}
		for _, field := range []struct {
			name   string
			output **time.Time
		}{{mapping.CreatedField, &issue.CreatedAt}, {mapping.UpdatedField, &issue.UpdatedAt}} {
			if value := native[field.name]; len(value) != 0 && string(value) != "null" {
				var text string
				if err := json.Unmarshal(value, &text); err != nil {
					return nil, ErrMapping
				}
				instant, err := time.Parse(time.RFC3339Nano, text)
				if err != nil {
					return nil, ErrMapping
				}
				utc := instant.UTC()
				*field.output = &utc
			}
		}
		issues = append(issues, issue)
	}
	return issues, nil
}

type PullRequest struct {
	Key       NativeKey
	CreatedAt *time.Time
	MergedAt  *time.Time
	Merged    bool
}

type MetricGap struct {
	Key    NativeKey
	Reason string
}

type PRMetrics struct {
	MergedCount    int
	CycleDurations []time.Duration
	Gaps           []MetricGap
}

func MeasurePRs(records []PullRequest, start, end time.Time) (PRMetrics, error) {
	if !start.Before(end) || start.Location() != time.UTC || end.Location() != time.UTC {
		return PRMetrics{}, ErrMapping
	}
	seen := make(map[NativeKey]bool, len(records))
	result := PRMetrics{}
	for _, record := range records {
		if record.Key.Kind != "pull_request" || record.Key.Tool != "github" ||
			record.Key.CellID == "" || record.Key.TenantID == "" || record.Key.ProjectID == "" || record.Key.ConnectionID == "" || record.Key.Scope == "" || record.Key.NativeID == "" ||
			seen[record.Key] {
			return PRMetrics{}, ErrMapping
		}
		seen[record.Key] = true
		if !record.Merged {
			if record.MergedAt != nil {
				result.Gaps = append(result.Gaps, MetricGap{record.Key, "inconsistent_merge"})
			}
			continue
		}
		if record.MergedAt == nil || record.MergedAt.IsZero() {
			result.Gaps = append(result.Gaps, MetricGap{record.Key, "missing_merge_time"})
			continue
		}
		mergedAt := record.MergedAt.UTC()
		if mergedAt.Before(start) || !mergedAt.Before(end) {
			continue
		}
		result.MergedCount++
		if record.CreatedAt == nil || record.CreatedAt.IsZero() || record.CreatedAt.After(mergedAt) {
			result.Gaps = append(result.Gaps, MetricGap{record.Key, "invalid_creation_time"})
			continue
		}
		result.CycleDurations = append(result.CycleDurations, mergedAt.Sub(*record.CreatedAt))
	}
	return result, nil
}
