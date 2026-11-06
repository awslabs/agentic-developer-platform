package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestArgoPageRetainsSelectedNativeApplication(t *testing.T) {
	for _, kind := range []string{"application", "sync_operation"} {
		payload := []byte(`{"metadata":{"name":"api","namespace":"cluster"},"spec":{"project":"team"},"status":{"history":[{"revision":"native"}],"operationState":null}}`)
		records, err := DecodePage("argocd", kind, "api", payload, 2, true)
		if err != nil || len(records) != 1 || records[0].Kind != kind || records[0].ID != "api" ||
			len(records[0].Revision) != 64 || !records[0].Private || !bytes.Equal(records[0].Raw, payload) {
			t.Fatalf("native %s application changed: %v, %v", kind, records, err)
		}
	}
}

func TestArgoPageRejectsForeignOrUnboundedApplication(t *testing.T) {
	valid := `{"metadata":{"name":"api"},"spec":{"project":"team"},"status":{"history":[{"revision":"native"}],"operationState":null}}`
	for _, scenario := range []struct {
		kind, payload string
	}{
		{"application", strings.Replace(valid, `"name":"api"`, `"name":"other"`, 1)},
		{"application", strings.Replace(valid, `"project":"team"`, `"project":""`, 1)},
		{"sync_operation", strings.Replace(valid, `"history":[{"revision":"native"}]`, `"history":null`, 1)},
		{"sync_operation", strings.Replace(valid, `"history":[{"revision":"native"}]`, `"history":[null]`, 1)},
		{"sync_operation", strings.Replace(valid, `"history":[{"revision":"native"}]`, `"history":[{},{}]`, 1)},
		{"sync_operation", strings.Replace(valid, `"operationState":null`, `"operationState":[]`, 1)},
		{"unsupported", valid},
	} {
		if _, err := DecodePage("argocd", scenario.kind, "api", []byte(scenario.payload), 1, false); err == nil {
			t.Fatal("invalid Argo CD response accepted", scenario.kind, scenario.payload)
		}
	}
}
