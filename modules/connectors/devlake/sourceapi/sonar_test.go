package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestSonarPagePreservesScopedNativeIssue(t *testing.T) {
	native := []byte(`{ "key":"ABC-1", "project":"ABC", "message":"native title", "status":"OPEN", "creationDate":"2026-10-01T02:00:00+0200", "updateDate":"2026-10-02T02:00:00+0200" }`)
	page := append(append([]byte(`{"issues":[`), native...), []byte(`]}`)...)
	records, err := DecodePage("sonarqube", "issue", "ABC", page, 1, true)
	if err != nil || len(records) != 1 || records[0].ID != "ABC-1" || records[0].Revision != "2026-10-02T00:00:00Z" || !records[0].Private || !bytes.Equal(records[0].Raw, native) {
		t.Fatalf("native issue changed or denied: %v, %v", records, err)
	}
	project, err := DecodePage("sonarqube", "project", "ABC", []byte(`{"component":{"key":"ABC","name":"selected"}}`), 1, false)
	if err != nil || len(project) != 1 || project[0].ID != "ABC" || project[0].Revision == "" {
		t.Fatalf("selected project unavailable: %v, %v", project, err)
	}
}

func TestSonarPageRejectsUnboundedOrForeignRecords(t *testing.T) {
	issue := `{"key":"ABC-1","project":"ABC","message":"title","status":"OPEN","creationDate":"2026-10-01T00:00:00Z","updateDate":"2026-10-02T00:00:00Z"}`
	for _, page := range []string{
		`{"component":{"key":"OTHER","name":"wrong"}}`,
		`{"issues":null}`,
		`{"issues":[` + issue + `,` + issue + `]}`,
		`{"issues":[` + strings.Replace(issue, `"project":"ABC"`, `"project":"OTHER"`, 1) + `]}`,
		`{"issues":[` + strings.Replace(issue, `"updateDate":"2026-10-02T00:00:00Z"`, `"updateDate":null`, 1) + `]}`,
	} {
		kind := "issue"
		if strings.Contains(page, "component") {
			kind = "project"
		}
		if _, err := DecodePage("sonarqube", kind, "ABC", []byte(page), 1, false); err == nil {
			t.Fatal("unbounded or foreign native response accepted", page)
		}
	}
	if _, err := DecodePage("sonarqube", "unknown", "ABC", []byte(`{}`), 1, false); err == nil {
		t.Fatal("unknown kind accepted")
	}
}
