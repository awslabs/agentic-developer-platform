package providers

import "testing"

func TestReviewSlackCursorAndPermissionFailuresCannotComplete(t *testing.T) {
	for _, payload := range []string{
		`{"ok":true,"channels":[],"response_metadata":{"next_cursor":null}}`,
		`{"ok":false,"error":"missing_scope","needed":"channels:read","provided":"groups:read"}`,
	} {
		client := fixture(t, payload, 200)
		result, err := Discover(client, req("slack", "", 10))
		if err == nil || len(result.Items) != 0 || result.Coverage == "complete" || client.calls != 1 {
			t.Fatalf("invalid Slack response yielded successful or retried discovery: %+v, %v, calls=%d", result, err, client.calls)
		}
	}
}
