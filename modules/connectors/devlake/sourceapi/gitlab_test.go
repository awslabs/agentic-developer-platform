package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestGitLabDetailPagePreservesNativePipelineAndRevision(t *testing.T) {
	detail := []byte(`{ "id":12, "project_id":901, "sha":"abc", "status":"success", "ref":"main", "created_at":"2026-10-01T00:00:00Z", "updated_at":"2026-10-02T00:00:00Z" }`)
	payload := append(append([]byte("["), detail...), ']')
	records, err := DecodePage("gitlab", "pipeline_detail", "901", payload, 1, true)
	if err != nil || len(records) != 1 || records[0].Kind != "pipeline" || records[0].ID != "12" ||
		records[0].Revision != "2026-10-02T00:00:00Z" || !bytes.Equal(records[0].Raw, detail) || !records[0].Private {
		t.Fatalf("scoped native detail changed or missing: %v, %v", records, err)
	}
}

func TestGitLabDetailPageRejectsAmbiguousAndForeignPipelines(t *testing.T) {
	detail := `{"id":12,"project_id":901,"sha":"abc","status":"success"}`
	for _, payload := range []string{
		`null`,
		`[` + detail + `,` + detail + `]`,
		`[` + strings.Replace(detail, `"project_id":901`, `"project_id":902`, 1) + `]`,
		`[` + strings.Replace(detail, `"id":12`, `"id":0`, 1) + `]`,
		`[` + strings.Replace(detail, `"status":"success"`, `"status":null`, 1) + `]`,
	} {
		if _, err := DecodePage("gitlab", "pipeline_detail", "901", []byte(payload), 1, false); err == nil {
			t.Fatal("ambiguous pipeline accepted", payload)
		}
	}
	if _, err := DecodePage("gitlab", "pipeline_detail", "901", []byte("["+detail+"]"), 11, false); err == nil {
		t.Fatal("unbounded detail page accepted")
	}
}
