package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestPagerDutyPagePreservesScopedNativeIncident(t *testing.T) {
	native := []byte(`{ "id":"PINC-1", "service":{"id":"PSVC"}, "title":"native incident", "status":"acknowledged", "created_at":"2026-10-01T00:00:00Z", "last_status_change_at":"2026-10-02T00:00:00Z" }`)
	page := append(append([]byte(`{"incidents":[`), native...), []byte(`]}`)...)
	records, err := DecodePage("pagerduty", "incident", "PSVC", page, 1, false)
	if err != nil || len(records) != 1 || records[0].ID != "PINC-1" || records[0].Revision != "2026-10-02T00:00:00Z" || !bytes.Equal(records[0].Raw, native) {
		t.Fatalf("native incident changed or denied: %v, %v", records, err)
	}
	service, err := DecodePage("pagerduty", "service", "PSVC", []byte(`{"service":{"id":"PSVC","name":"selected"}}`), 1, true)
	if err != nil || len(service) != 1 || service[0].ID != "PSVC" || !service[0].Private || service[0].Revision == "" {
		t.Fatalf("selected service unavailable: %v, %v", service, err)
	}
}

func TestPagerDutyPageRejectsUnboundedOrForeignRecords(t *testing.T) {
	incident := `{"id":"PINC-1","service":{"id":"PSVC"},"title":"native","status":"triggered","created_at":"2026-10-01T00:00:00Z","last_status_change_at":"2026-10-02T00:00:00Z"}`
	for _, page := range []string{
		`{"service":{"id":"OTHER","name":"wrong"}}`,
		`{"incidents":null}`,
		`{"incidents":[` + incident + `,` + incident + `]}`,
		`{"incidents":[` + strings.Replace(incident, `"id":"PSVC"`, `"id":"OTHER"`, 1) + `]}`,
		`{"incidents":[` + strings.Replace(incident, `"last_status_change_at":"2026-10-02T00:00:00Z"`, `"last_status_change_at":null`, 1) + `]}`,
		`{"incidents":[` + strings.Replace(incident, `"last_status_change_at"`, `"updated_at"`, 1) + `]}`,
	} {
		kind := "incident"
		if strings.Contains(page, `"service":{`) && !strings.Contains(page, `"incidents"`) {
			kind = "service"
		}
		if _, err := DecodePage("pagerduty", kind, "PSVC", []byte(page), 1, false); err == nil {
			t.Fatal("unbounded or foreign native response accepted", page)
		}
	}
	if _, err := DecodePage("pagerduty", "unknown", "PSVC", []byte(`{}`), 1, false); err == nil {
		t.Fatal("unknown kind accepted")
	}
}
