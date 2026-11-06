package sourceapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"
)

func decodeJenkinsPage(kind, scope string, payload []byte, limit int, private bool) ([]Record, error) {
	if kind != "job" && kind != "build" || scope == "" || strings.HasSuffix(scope, "/") {
		return nil, ErrSource
	}
	var job struct {
		FullName string            `json:"fullName"`
		Name     string            `json:"name"`
		Class    string            `json:"_class"`
		Builds   []json.RawMessage `json:"allBuilds"`
	}
	if decode(payload, &job) != nil || job.FullName != scope || job.Name != scope[strings.LastIndex(scope, "/")+1:] ||
		job.Class != "hudson.model.FreeStyleProject" && job.Class != "org.jenkinsci.plugins.workflow.job.WorkflowJob" {
		return nil, ErrSource
	}
	if kind == "job" {
		digest := sha256.Sum256(payload)
		return []Record{{Kind: kind, ID: scope, Title: job.Name, Revision: hex.EncodeToString(digest[:]), Raw: bytes.Clone(payload), Private: private}}, nil
	}
	if job.Builds == nil || len(job.Builds) > limit {
		return nil, ErrSource
	}
	seen := make(map[string]bool, len(job.Builds))
	records := make([]Record, 0, len(job.Builds))
	for _, raw := range job.Builds {
		var build struct {
			Number    json.Number `json:"number"`
			Timestamp json.Number `json:"timestamp"`
		}
		if decode(raw, &build) != nil {
			return nil, ErrSource
		}
		number, numberErr := strconv.ParseUint(build.Number.String(), 10, 63)
		timestamp, timeErr := strconv.ParseInt(build.Timestamp.String(), 10, 64)
		if numberErr != nil || number == 0 || strconv.FormatUint(number, 10) != build.Number.String() ||
			timeErr != nil || timestamp < 0 || strconv.FormatInt(timestamp, 10) != build.Timestamp.String() || seen[build.Number.String()] {
			return nil, ErrSource
		}
		seen[build.Number.String()] = true
		digest := sha256.Sum256(raw)
		records = append(records, Record{Kind: kind, ID: scope + "#" + build.Number.String(), Title: job.Name,
			Revision: hex.EncodeToString(digest[:]), Raw: bytes.Clone(raw), Private: private})
	}
	return records, nil
}
