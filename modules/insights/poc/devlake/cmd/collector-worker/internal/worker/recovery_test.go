package worker

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestReceiptRecoveryValidation(test *testing.T) {
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	test.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []string{"success", "empty", "pending", "running", "failed", "cancelled", "expired", "wrong-job", "wrong-tenant", "wrong-batch", "missing-items", "negative-items", "too-many-items", "missing-attempt", "missing-fence", "bad-digest", "missing-receipt"} {
		test.Run(scenario, func(test *testing.T) {
			receipt := map[string]any{"batch_id": batchIdentity("job", "attempt"), "content_digest": strings.Repeat("a", 64), "items": 5}
			stored := map[string]any{"job_id": "job", "tenant_id": "tenant", "state": "succeeded", "attempt_id": "attempt", "lease_fence": 1, "receipt": receipt}
			switch scenario {
			case "empty":
				receipt["items"] = 0
			case "pending", "running", "failed", "cancelled", "expired":
				stored["state"], stored["receipt"] = scenario, nil
			case "wrong-job":
				stored["job_id"] = "other-job"
			case "wrong-tenant":
				stored["tenant_id"] = "other-tenant"
			case "wrong-batch":
				receipt["batch_id"] = batchIdentity("job", "other-attempt")
			case "missing-items":
				delete(receipt, "items")
			case "negative-items":
				receipt["items"] = -1
			case "too-many-items":
				receipt["items"] = 100001
			case "missing-attempt":
				delete(stored, "attempt_id")
			case "missing-fence":
				delete(stored, "lease_fence")
			case "bad-digest":
				receipt["content_digest"] = strings.Repeat("A", 64)
			case "missing-receipt":
				delete(stored, "receipt")
			}
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				if request.Method != "POST" || request.URL.Path != "/internal/v1/collection-jobs/tenant/job/receipt" {
					test.Error("unexpected recovery request")
				}
				_ = json.NewEncoder(response).Encode(stored)
			}))
			defer server.Close()
			gateway := &Gateway{URL: server.URL, Client: server.Client(), Sign: func(*http.Request, []byte) error { return nil }}
			result, err := gateway.Recover(test.Context(), "tenant", "job", "original_claim_key")
			switch scenario {
			case "success", "empty":
				if err != nil || result == nil || result.BatchID != receipt["batch_id"] || result.Items != receipt["items"] || result.ContentDigest != receipt["content_digest"] {
					test.Fatalf("receipt changed: %+v %v", result, err)
				}
			case "pending", "running":
				if err != nil || result != nil {
					test.Fatalf("active job prevented ordinary claim: %+v %v", result, err)
				}
			default:
				if err == nil || result != nil {
					test.Fatal("invalid terminal receipt accepted")
				}
			}
		})
	}
}

func TestGatewayCommittedReceiptRecovery(test *testing.T) {
	fixture := os.Getenv("DEVLAKE_S03_RECOVERY_FIXTURE")
	if fixture == "" {
		test.Skip("requires gateway test bridge with real Product work lifecycle")
	}
	var config struct {
		URL      string `json:"url"`
		ClaimKey string `json:"claim_key"`
		Claim    Claim  `json:"claim"`
	}
	if err := json.Unmarshal([]byte(fixture), &config); err != nil {
		test.Fatal(err)
	}
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	test.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	gateway := &Gateway{URL: config.URL, Client: http.DefaultClient, Sign: func(*http.Request, []byte) error { return nil }, Claim: config.Claim,
		JobPath: "/internal/v1/collection-jobs/" + config.Claim.TenantID + "/" + config.Claim.JobID}
	want := Result{BatchID: batchIdentity(config.Claim.JobID, config.Claim.AttemptID), ContentDigest: strings.Repeat("a", 64), Items: 5}
	if err := gateway.Finish(test.Context(), "succeeded", want); err == nil {
		test.Fatal("bridge did not lose the committed finish response")
	}
	restarted := &Gateway{URL: config.URL, Client: http.DefaultClient, Sign: func(*http.Request, []byte) error { return nil }}
	for retry := 0; retry < 2; retry++ {
		got, err := Run(test.Context(), Config{TenantID: config.Claim.TenantID, JobID: config.Claim.JobID, ClaimKey: config.ClaimKey}, restarted, nil)
		if err != nil || got != want {
			test.Fatalf("restart failed to recover original receipt without cell access: %+v %v", got, err)
		}
		if restarted.Claim.AttemptID != "" || len(restarted.Contexts) != 0 {
			test.Fatal("receipt recovery reopened source authority")
		}
	}
}
