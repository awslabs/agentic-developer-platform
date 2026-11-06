package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestJenkinsPagePreservesExactJobAndBuild(t *testing.T) {
	job := []byte(`{"fullName":"team/api","name":"api","_class":"hudson.model.FreeStyleProject"}`)
	build := []byte(`{"number":7,"timestamp":1700000000000,"result":"SUCCESS"}`)
	for _, scenario := range []struct {
		kind, expectedID string
		payload, raw     []byte
	}{
		{"job", "team/api", job, job},
		{"build", "team/api#7", []byte(strings.TrimSuffix(string(job), "}") + `,"allBuilds":[` + string(build) + `]}`), build},
	} {
		records, err := DecodePage("jenkins", scenario.kind, "team/api", scenario.payload, 100, true)
		if err != nil || len(records) != 1 || records[0].ID != scenario.expectedID || records[0].Kind != scenario.kind ||
			!records[0].Private || len(records[0].Revision) != 64 || !bytes.Equal(records[0].Raw, scenario.raw) {
			t.Fatalf("native Jenkins %s lost: %v, %v", scenario.kind, records, err)
		}
	}
}

func TestJenkinsPageRejectsForeignDuplicateAndAmbiguousBuilds(t *testing.T) {
	base := `{"fullName":"team/api","name":"api","_class":"hudson.model.FreeStyleProject","allBuilds":[{"number":7,"timestamp":1}]}`
	for _, payload := range []string{
		strings.Replace(base, `"team/api"`, `"team/other"`, 1),
		strings.Replace(base, `"name":"api"`, `"name":"other"`, 1),
		strings.Replace(base, `"number":7`, `"number":-1`, 1),
		strings.Replace(base, `"timestamp":1`, `"timestamp":null`, 1),
		strings.Replace(base, `"number":7`, `"number":7.0`, 1),
		strings.Replace(base, `{"number":7,"timestamp":1}`, `{"number":7,"timestamp":1},{"number":7,"timestamp":1}`, 1),
		strings.Replace(base, `"allBuilds":[{"number":7,"timestamp":1}]`, `"allBuilds":null`, 1),
	} {
		if _, err := DecodePage("jenkins", "build", "team/api", []byte(payload), 100, false); err == nil {
			t.Fatal("unsafe Jenkins build accepted", payload)
		}
	}
	if _, err := DecodePage("jenkins", "build", "team/api", []byte(base), 0, false); err == nil {
		t.Fatal("unbounded Jenkins builds accepted")
	}
}
