package execution

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

var testBinding = Binding{OperationID: "operation", OrgID: "org", WorkspaceID: "workspace", Holder: "worker", AttemptID: "attempt", FenceToken: 3}

func socketClient(t *testing.T, respond func(map[string]json.RawMessage) any) (*Client, string) {
	t.Helper()
	// macOS Unix socket paths are limited to 104 bytes; avoid long test names.
	dir, err := os.MkdirTemp("/tmp", "go-rpc-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	socket, token := filepath.Join(dir, "rpc.sock"), filepath.Join(dir, "token")
	if err := os.WriteFile(token, []byte("scoped-test-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			func() {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
				var request map[string]json.RawMessage
				if json.NewDecoder(conn).Decode(&request) != nil {
					return
				}
				response := respond(request)
				if response == nil {
					return
				}
				if raw, ok := response.(string); ok {
					_, _ = conn.Write([]byte(raw))
					return
				}
				_ = json.NewEncoder(conn).Encode(response)
			}()
		}
	}()
	client, err := New(socket, token, testBinding)
	if err != nil {
		t.Fatal(err)
	}
	return client, token
}

func successResponse(result any) any { return map[string]any{"ok": true, "result": result} }

func testLease() Lease {
	return Lease{Binding: testBinding, ExpiresAt: time.Now().Add(time.Minute), RuntimeDeadline: time.Now().Add(2 * time.Minute), Attempts: 1, MaxAttempts: 3}
}

func TestRequestContainsOnlyScopedTokenAndAdmittedStep(t *testing.T) {
	var effects atomic.Int32
	client, _ := socketClient(t, func(request map[string]json.RawMessage) any {
		if len(request) != 3 || string(request["token"]) != `"scoped-test-token"` {
			t.Error("unexpected envelope")
		}
		switch string(request["method"]) {
		case `"renew"`:
			return successResponse(testLease())
		case `"status"`:
			return successResponse(Status{OperationID: "operation", State: "running"})
		case `"execute_step"`:
			if string(request["arguments"]) != `{"step_id":"launch"}` {
				t.Error("worker supplied effect arguments")
			}
			effects.Add(1)
			return successResponse([]any{Call{Binding: testBinding, JobID: "job", IdempotencyKey: "durable", Provider: "skypilot", OperationKind: "create", Target: "registered-target", Stage: "intended"}, "retain"})
		default:
			t.Error("unexpected method")
			return nil
		}
	})
	result, err := client.ExecuteStep(context.Background(), "launch")
	if err != nil || result.Disposition != "retain" || result.Call.IdempotencyKey != "durable" || effects.Load() != 1 {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}

func TestBindingMismatchNeverRequestsEffect(t *testing.T) {
	for _, field := range []string{"operation", "org", "workspace", "holder", "attempt", "fence", "expired", "runtime", "ceiling"} {
		t.Run(field, func(t *testing.T) {
			client, _ := socketClient(t, func(request map[string]json.RawMessage) any {
				if string(request["method"]) != `"renew"` {
					t.Error("effect after refused binding")
				}
				lease := testLease()
				switch field {
				case "operation":
					lease.OperationID = "foreign"
				case "org":
					lease.OrgID = "foreign"
				case "workspace":
					lease.WorkspaceID = "foreign"
				case "holder":
					lease.Holder = "foreign"
				case "attempt":
					lease.AttemptID = "foreign"
				case "fence":
					lease.FenceToken++
				case "expired":
					lease.ExpiresAt = time.Now().Add(-time.Second)
				case "runtime":
					lease.RuntimeDeadline = time.Now().Add(-time.Second)
				case "ceiling":
					lease.Attempts = lease.MaxAttempts + 1
				}
				return successResponse(lease)
			})
			if _, err := client.ExecuteStep(context.Background(), "launch"); !errors.Is(err, ErrRefused) {
				t.Fatal(err)
			}
		})
	}
}

func TestTokenRemovalAndRotationApplyToNextRequest(t *testing.T) {
	var requests atomic.Int32
	client, token := socketClient(t, func(request map[string]json.RawMessage) any {
		requests.Add(1)
		if requests.Load() == 2 && string(request["token"]) != `"rotated-token"` {
			t.Error("cached credential")
		}
		return successResponse(Status{OperationID: "operation", State: "running"})
	})
	if _, err := client.Status(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(token, []byte("rotated-token"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Status(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(token); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Status(context.Background()); !errors.Is(err, ErrRefused) || requests.Load() != 2 {
		t.Fatal("credential removal ignored", err)
	}
}

func TestTokenCannotSwitchOperationBetweenVerificationAndExecution(t *testing.T) {
	var tokenFile atomic.Value
	client, token := socketClient(t, func(request map[string]json.RawMessage) any {
		switch string(request["method"]) {
		case `"renew"`:
			return successResponse(testLease())
		case `"status"`:
			if err := os.WriteFile(tokenFile.Load().(string), []byte("different-operation-token"), 0600); err != nil {
				t.Error(err)
			}
			return successResponse(Status{OperationID: "operation", State: "running"})
		default:
			t.Error("effect requested with replacement operation token")
			return nil
		}
	})
	tokenFile.Store(token)
	if _, err := client.ExecuteStep(context.Background(), "launch"); !errors.Is(err, ErrRefused) {
		t.Fatal(err)
	}
}

func TestMalformedLostAndOversizedResponsesAreNeverRetried(t *testing.T) {
	for _, reply := range []string{"", "{}\n", "not-json\n", `{"ok":true}` + "\n", `{"ok":true,"result":null}` + "\n", strings.Repeat("x", maxMessageBytes) + "\n"} {
		t.Run("reply", func(t *testing.T) {
			var requests atomic.Int32
			client, _ := socketClient(t, func(map[string]json.RawMessage) any { requests.Add(1); return reply })
			if _, err := client.Status(context.Background()); err == nil {
				t.Fatal("invalid reply accepted")
			}
			if requests.Load() != 1 {
				t.Fatal("uncertain request retried")
			}
		})
	}
}

func TestCancelledAndUncertainOperationsCannotRequestEffect(t *testing.T) {
	for _, status := range []Status{
		{OperationID: "operation", State: "running", CancelRequested: true},
		{OperationID: "operation", State: "running", CleanupRequired: true},
		{OperationID: "operation", State: "unknown"},
		{OperationID: "operation", State: "succeeded"},
		{OperationID: "foreign", State: "running"},
	} {
		client, _ := socketClient(t, func(request map[string]json.RawMessage) any {
			switch string(request["method"]) {
			case `"renew"`:
				return successResponse(testLease())
			case `"status"`:
				return successResponse(status)
			default:
				t.Error("effect requested after cancellation/unknown outcome")
				return nil
			}
		})
		if _, err := client.ExecuteStep(context.Background(), "launch"); err == nil {
			t.Fatal("unsafe operation accepted")
		}
	}
}

func TestInvalidExecutionEvidenceCannotReleaseBudget(t *testing.T) {
	for _, scenario := range []string{"empty-outcome", "unknown-release", "future-fence", "wrong-attempt", "foreign-workspace"} {
		t.Run(scenario, func(t *testing.T) {
			client, _ := socketClient(t, func(request map[string]json.RawMessage) any {
				switch string(request["method"]) {
				case `"renew"`:
					return successResponse(testLease())
				case `"status"`:
					return successResponse(Status{OperationID: "operation", State: "running"})
				}
				call := map[string]any{"operation_id": "operation", "org_id": "org", "workspace_id": "workspace", "attempt_id": "attempt", "fence_token": 3, "job_id": "job", "idempotency_key": "durable", "provider": "skypilot", "operation_kind": "create", "target": "target", "stage": "intended", "outcome": nil}
				disposition := "retain"
				switch scenario {
				case "empty-outcome":
					call["outcome"] = ""
				case "unknown-release":
					call["outcome"] = "unknown"
					call["stage"] = "unresolved"
					disposition = "release"
				case "future-fence":
					call["fence_token"] = 4
				case "wrong-attempt":
					call["attempt_id"] = "different"
				case "foreign-workspace":
					call["workspace_id"] = "foreign"
				}
				return successResponse([]any{call, disposition})
			})
			result, err := client.ExecuteStep(context.Background(), "launch")
			if !errors.Is(err, ErrProtocol) || result.Disposition != "" {
				t.Fatal("invalid evidence was usable", result, err)
			}
		})
	}
}

func TestContextCancellationClosesInFlightTransport(t *testing.T) {
	client, _ := socketClient(t, func(map[string]json.RawMessage) any { time.Sleep(100 * time.Millisecond); return nil })
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(10*time.Millisecond, cancel)
	start := time.Now()
	if _, err := client.Status(ctx); err == nil {
		t.Fatal("cancelled request succeeded")
	}
	if time.Since(start) > time.Second {
		t.Fatal("context cancellation did not stop transport")
	}
}

// This helper runs only in a separately compiled test binary launched by the real
// Python/PostgreSQL integration fixture. It is not a production CLI entry point.
func TestRPCWorkerProcess(t *testing.T) {
	if os.Getenv("SUPERPLANE_TEST_RPC_WORKER") != "1" {
		return
	}
	for _, forbidden := range []string{"DATABASE_URL", "HARNESS_JOBS_TEST_POSTGRES_URL", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "SKYPILOT_SERVICE_TOKEN"} {
		if _, present := os.LookupEnv(forbidden); present {
			t.Fatalf("worker received %s", forbidden)
		}
	}
	input := bufio.NewReader(os.Stdin)
	for {
		line, err := input.ReadBytes('\n')
		if err != nil {
			return
		}
		var req struct {
			Socket    string  `json:"socket"`
			TokenFile string  `json:"token_file"`
			Binding   Binding `json:"binding"`
			Step      string  `json:"step"`
			Method    string  `json:"method"`
		}
		if json.Unmarshal(line, &req) != nil {
			t.Fatal("invalid fixture request")
		}
		client, err := New(req.Socket, req.TokenFile, req.Binding)
		var result Result
		if err == nil {
			switch req.Method {
			case "cancel":
				err = client.Cancel(context.Background())
			case "status":
				_, err = client.Status(context.Background())
			default:
				result, err = client.ExecuteStep(context.Background(), req.Step)
			}
		}
		errorCode := ""
		if err != nil {
			errorCode = err.Error()
		}
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"error": errorCode, "result": result})
	}
}

func TestPaidTaskConsumesOnlyItsAdmittedStepsAndStopsAfterUncertainty(t *testing.T) {
	var effects atomic.Int32
	client, token := socketClient(t, func(request map[string]json.RawMessage) any {
		switch string(request["method"]) {
		case `"renew"`:
			return successResponse(testLease())
		case `"status"`:
			return successResponse(Status{OperationID: "operation", State: "running"})
		case `"execute_step"`:
			effects.Add(1)
			return nil // the provider may have committed before its reply was lost
		default:
			t.Error("unexpected RPC")
			return nil
		}
	})
	directory := filepath.Dir(token)
	name := strings.Repeat("a", 64)
	if err := os.Rename(token, filepath.Join(directory, name)); err != nil {
		t.Fatal(err)
	}
	assignment := filepath.Join(directory, "assignment.json")
	body, _ := json.Marshal(Task{Binding: testBinding, JobID: "original-job", CredentialName: name, StepIDs: []string{"launch", "deploy"}})
	if err := os.WriteFile(assignment, body, 0600); err != nil {
		t.Fatal(err)
	}
	if err := RunTask(context.Background(), assignment, client.socketPath, directory); err == nil {
		t.Fatal("uncertain task reported success")
	}
	if effects.Load() != 1 {
		t.Fatal("uncertain provider action repeated or later step executed")
	}
	if _, err := os.Stat(filepath.Join(directory, "assignment.finished")); err != nil {
		t.Fatal("trusted worker was not notified", err)
	}
}
