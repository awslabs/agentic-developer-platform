package skypilot

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// newTestServer creates an httptest.Server and returns both the server
// and a Client pointing at it.
func newTestServer(t *testing.T, handler http.Handler) (*httptest.Server, *Client) {
	t.Helper()
	ts := httptest.NewServer(handler)
	t.Cleanup(ts.Close)
	client := NewClient(ts.URL)
	return ts, client
}

// --- Health ---

func TestHealth_Success(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(HealthResponse{
			Status:  "healthy",
			Version: "0.12.0",
		})
	})
	_, client := newTestServer(t, mux)

	resp, err := client.Health(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.Status != "healthy" {
		t.Errorf("expected status=healthy, got %s", resp.Status)
	}
	if resp.Version != "0.12.0" {
		t.Errorf("expected version=0.12.0, got %s", resp.Version)
	}
}

func TestHealth_ServerError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("internal error"))
	})
	_, client := newTestServer(t, mux)

	_, err := client.Health(context.Background())
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("expected *APIError, got %T: %v", err, err)
	}
	if apiErr.StatusCode != 500 {
		t.Errorf("expected status 500, got %d", apiErr.StatusCode)
	}
}

// --- Launch ---

func TestLaunch_Success(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/launch", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("expected Content-Type application/json, got %s", ct)
		}

		var req LaunchRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if req.ClusterName != "test-cluster" {
			t.Errorf("expected cluster_name=test-cluster, got %s", req.ClusterName)
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RequestResponse{RequestID: "req-abc-123"})
	})
	_, client := newTestServer(t, mux)

	idle := 120
	reqID, err := client.Launch(context.Background(), LaunchRequest{
		ClusterName:           "test-cluster",
		IdleMinutesToAutostop: &idle,
		Envs:                  map[string]string{"FOO": "bar"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reqID != "req-abc-123" {
		t.Errorf("expected request_id=req-abc-123, got %s", reqID)
	}
}

// --- Status ---

func TestStatus_Success(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}

		body, _ := io.ReadAll(r.Body)
		if len(body) > 0 {
			var req StatusRequest
			_ = json.Unmarshal(body, &req)
			if len(req.ClusterNames) > 0 && req.ClusterNames[0] != "my-cluster" {
				t.Errorf("expected cluster name my-cluster, got %s", req.ClusterNames[0])
			}
		}

		w.Header().Set("Content-Type", "application/json")
		clusters := []ClusterInfo{
			{
				Name:   "my-cluster",
				Status: ClusterStatusUp,
				Handle: ClusterHandle{
					ClusterName: "my-cluster",
					HeadIP:      "10.0.0.1",
				},
			},
		}
		_ = json.NewEncoder(w).Encode(clusters)
	})
	_, client := newTestServer(t, mux)

	clusters, err := client.Status(context.Background(), "my-cluster")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(clusters) != 1 {
		t.Fatalf("expected 1 cluster, got %d", len(clusters))
	}
	if clusters[0].Name != "my-cluster" {
		t.Errorf("expected name=my-cluster, got %s", clusters[0].Name)
	}
	if clusters[0].Status != ClusterStatusUp {
		t.Errorf("expected status=UP, got %s", clusters[0].Status)
	}
	if clusters[0].Handle.HeadIP != "10.0.0.1" {
		t.Errorf("expected head_ip=10.0.0.1, got %s", clusters[0].Handle.HeadIP)
	}
}

func TestStatus_NoFilter(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if len(body) > 0 {
			t.Errorf("expected empty body for no-filter status, got %s", string(body))
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]ClusterInfo{})
	})
	_, client := newTestServer(t, mux)

	clusters, err := client.Status(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(clusters) != 0 {
		t.Errorf("expected 0 clusters, got %d", len(clusters))
	}
}

// --- Down ---

func TestDown_Success(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/down", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}

		var req DownRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		if len(req.ClusterNames) != 1 || req.ClusterNames[0] != "doomed-cluster" {
			t.Errorf("unexpected cluster names: %v", req.ClusterNames)
		}
		if !req.Purge {
			t.Error("expected purge=true")
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RequestResponse{RequestID: "req-down-456"})
	})
	_, client := newTestServer(t, mux)

	reqID, err := client.Down(context.Background(), []string{"doomed-cluster"}, true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reqID != "req-down-456" {
		t.Errorf("expected request_id=req-down-456, got %s", reqID)
	}
}

// --- EnabledClouds ---

func TestEnabledClouds_Success(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/enabled_clouds", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET, got %s", r.Method)
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(EnabledCloudsResponse{
			EnabledClouds: []CloudInfo{
				{Name: "aws", Enabled: true},
				{Name: "gcp", Enabled: false},
				{Name: "nebius", Enabled: true},
			},
		})
	})
	_, client := newTestServer(t, mux)

	clouds, err := client.EnabledClouds(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(clouds) != 3 {
		t.Fatalf("expected 3 clouds, got %d", len(clouds))
	}
	if clouds[0].Name != "aws" || !clouds[0].Enabled {
		t.Errorf("expected aws enabled, got %+v", clouds[0])
	}
}

// --- StreamProgress ---

func TestStreamProgress_Success(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET, got %s", r.Method)
		}
		reqID := r.URL.Query().Get("request_id")
		if reqID != "req-abc-123" {
			t.Errorf("expected request_id=req-abc-123, got %s", reqID)
		}

		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		flusher, ok := w.(http.Flusher)
		if !ok {
			t.Fatal("expected Flusher")
		}

		events := []string{
			"id: 1\nevent: message\ndata: Launching cluster...\n\n",
			"id: 2\nevent: message\ndata: Provisioning resources...\n\n",
			"id: 3\nevent: complete\ndata: Cluster is ready\n\n",
		}
		for _, e := range events {
			fmt.Fprint(w, e)
			flusher.Flush()
		}
	})
	_, client := newTestServer(t, mux)

	eventCh, errCh := client.StreamProgress(context.Background(), "req-abc-123")

	var events []StreamEvent
	for ev := range eventCh {
		events = append(events, ev)
	}

	// Check for errors
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	default:
	}

	if len(events) != 3 {
		t.Fatalf("expected 3 events, got %d", len(events))
	}
	if events[0].Data != "Launching cluster..." {
		t.Errorf("event 0 data = %q, want %q", events[0].Data, "Launching cluster...")
	}
	if events[0].Event != "message" {
		t.Errorf("event 0 type = %q, want %q", events[0].Event, "message")
	}
	if events[2].Event != "complete" {
		t.Errorf("event 2 type = %q, want %q", events[2].Event, "complete")
	}
	if !events[2].IsTerminal {
		t.Error("expected last event to be terminal")
	}
	if events[0].IsTerminal {
		t.Error("expected first event to NOT be terminal")
	}
}

func TestStreamProgress_ContextCancellation(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/stream", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		flusher, ok := w.(http.Flusher)
		if !ok {
			return
		}
		// Send one event, then wait forever (simulating a slow stream)
		fmt.Fprint(w, "event: message\ndata: Starting...\n\n")
		flusher.Flush()

		// Block until client disconnects
		<-r.Context().Done()
	})
	_, client := newTestServer(t, mux)

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	eventCh, errCh := client.StreamProgress(ctx, "req-timeout")

	var events []StreamEvent
	for ev := range eventCh {
		events = append(events, ev)
	}

	// Should have received at least the first event
	if len(events) < 1 {
		t.Error("expected at least 1 event before cancellation")
	}

	// Error channel should have context error
	select {
	case err := <-errCh:
		if err != nil && !strings.Contains(err.Error(), "context") {
			t.Errorf("expected context error, got: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Error("timed out waiting for error")
	}
}

func TestStreamProgress_ServerError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/stream", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte("request not found"))
	})
	_, client := newTestServer(t, mux)

	eventCh, errCh := client.StreamProgress(context.Background(), "bad-id")

	// Drain event channel (should be empty)
	for range eventCh {
		t.Error("unexpected event")
	}

	err := <-errCh
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "404") {
		t.Errorf("expected 404 in error, got: %v", err)
	}
}

func TestStreamProgress_MultilineData(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/stream", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		flusher, _ := w.(http.Flusher)
		// SSE spec: multiple data lines get joined with newlines
		fmt.Fprint(w, "event: message\ndata: line1\ndata: line2\n\nevent: complete\ndata: done\n\n")
		flusher.Flush()
	})
	_, client := newTestServer(t, mux)

	eventCh, _ := client.StreamProgress(context.Background(), "multi")
	var events []StreamEvent
	for ev := range eventCh {
		events = append(events, ev)
	}

	if len(events) < 1 {
		t.Fatal("expected at least 1 event")
	}
	if events[0].Data != "line1\nline2" {
		t.Errorf("expected multiline data, got %q", events[0].Data)
	}
}

// --- Context Cancellation for non-streaming ---

func TestHealth_ContextCancelled(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		// Simulate slow response — block until client disconnects.
		<-r.Context().Done()
	})
	_, client := newTestServer(t, mux)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	_, err := client.Health(ctx)
	if err == nil {
		t.Fatal("expected error due to context cancellation")
	}
}

// --- APIError ---

func TestAPIError_Error(t *testing.T) {
	e := &APIError{StatusCode: 422, Body: `{"detail":"invalid"}`}
	got := e.Error()
	if !strings.Contains(got, "422") || !strings.Contains(got, "invalid") {
		t.Errorf("unexpected error message: %s", got)
	}
}

// --- NewClient options ---

func TestNewClient_Defaults(t *testing.T) {
	c := NewClient("http://localhost:1234/")
	// Trailing slash should be trimmed
	if c.baseURL != "http://localhost:1234" {
		t.Errorf("expected trimmed URL, got %s", c.baseURL)
	}
	if c.httpClient.Timeout != 30*time.Second {
		t.Errorf("expected 30s timeout, got %s", c.httpClient.Timeout)
	}
}

func TestNewClient_WithTimeout(t *testing.T) {
	c := NewClient("http://localhost:1234", WithTimeout(60*time.Second))
	if c.httpClient.Timeout != 60*time.Second {
		t.Errorf("expected 60s timeout, got %s", c.httpClient.Timeout)
	}
}
