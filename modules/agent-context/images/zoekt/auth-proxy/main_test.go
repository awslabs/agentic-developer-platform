package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sourcegraph/zoekt"
	zjson "github.com/sourcegraph/zoekt/internal/json"
	"github.com/sourcegraph/zoekt/query"
)

// Exercise the actual pinned provider JSON decoder, not a mock that accepts
// Door's formerly ignored top-level num field.
type recordingSearcher struct {
	mu      sync.Mutex
	options []zoekt.SearchOptions
}

func (s *recordingSearcher) Search(ctx context.Context, q query.Q, opts *zoekt.SearchOptions) (*zoekt.SearchResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.options = append(s.options, *opts)
	return &zoekt.SearchResult{}, nil
}

func (s *recordingSearcher) recordedOptions() []zoekt.SearchOptions {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]zoekt.SearchOptions(nil), s.options...)
}
func (s *recordingSearcher) List(context.Context, query.Q, *zoekt.ListOptions) (*zoekt.RepoList, error) {
	panic("unexpected list")
}
func (s *recordingSearcher) Close()         {}
func (s *recordingSearcher) String() string { return "offline-recording-searcher" }

func TestPinnedProviderIgnoresLegacyNum(t *testing.T) {
	searcher := &recordingSearcher{}
	provider := zjson.JSONServer(searcher)
	w := httptest.NewRecorder()
	provider.ServeHTTP(w, httptest.NewRequest("POST", "/search", strings.NewReader(`{"q":"needle","num":5}`)))
	options := searcher.recordedOptions()
	if w.Code != 200 || len(options) != 1 || options[0].MaxDocDisplayCount != 0 {
		t.Fatal("pinned decoder behavior changed; re-review provider contract")
	}
}

func TestBoundsReachPinnedProviderDecoder(t *testing.T) {
	for _, tc := range []struct {
		name, input string
		want        zoekt.SearchOptions
	}{
		{
			"absent bounds", `{"q":"needle"}`,
			zoekt.SearchOptions{MaxWallTime: maxSearchTime, MaxDocDisplayCount: maxDocuments, ShardMaxMatchCount: maxShardMatches, TotalMaxMatchCount: maxTotalMatches},
		},
		{
			"excessive bounds", `{"q":"needle","opts":{"MaxWallTime":999999999999,"MaxDocDisplayCount":99999,"ShardMaxMatchCount":9999999,"TotalMaxMatchCount":9999999,"Whole":true}}`,
			zoekt.SearchOptions{MaxWallTime: maxSearchTime, MaxDocDisplayCount: maxDocuments, ShardMaxMatchCount: maxShardMatches, TotalMaxMatchCount: maxTotalMatches, Whole: true},
		},
		{
			"smaller bounds", `{"q":"needle","opts":{"MaxWallTime":1000000000,"MaxDocDisplayCount":5,"ShardMaxMatchCount":10,"TotalMaxMatchCount":20,"Whole":true}}`,
			zoekt.SearchOptions{MaxWallTime: time.Second, MaxDocDisplayCount: 5, ShardMaxMatchCount: 10, TotalMaxMatchCount: 20, Whole: true},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			searcher := &recordingSearcher{}
			provider := httptest.NewServer(http.StripPrefix("/api", zjson.JSONServer(searcher)))
			defer provider.Close()
			u, _ := url.Parse(provider.URL)
			key := strings.Repeat("k", 64)
			h, _ := handler(key, u)
			r := httptest.NewRequest("POST", "/api/search", strings.NewReader(tc.input))
			r.Header.Set("Authorization", "Bearer "+key)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			options := searcher.recordedOptions()
			if w.Code != 200 || len(options) != 1 {
				t.Fatalf("provider requests=%d, HTTP=%d", len(options), w.Code)
			}
			opts := options[0]
			if opts != tc.want {
				t.Fatalf("provider options=%+v, want=%+v", opts, tc.want)
			}
		})
	}
}

func TestInvalidSearchNeverReachesProvider(t *testing.T) {
	var calls atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(200) }))
	defer provider.Close()
	u, _ := url.Parse(provider.URL)
	key := strings.Repeat("k", 64)
	h, _ := handler(key, u)
	for _, input := range []string{
		`{"q":"needle","num":5}`, `{"q":"needle","opts":{"EstimateDocCount":true}}`,
		`{"q":"needle","opts":{"MaxWallTime":-1}}`, `{"q":"needle","opts":{"MaxDocDisplayCount":-1}}`,
		`{"q":"needle","opts":{"ShardMaxMatchCount":-1}}`, `{"q":"needle","opts":{"TotalMaxMatchCount":-1}}`,
		`{"q":"needle"} {"q":"second"}`, `{"q":"` + strings.Repeat("x", maxSearchBody) + `"}`,
	} {
		r := httptest.NewRequest("POST", "/api/search", strings.NewReader(input))
		r.Header.Set("Authorization", "Bearer "+key)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 400 {
			t.Fatalf("invalid request HTTP=%d", w.Code)
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid requests reached provider: %d", calls.Load())
	}
}

func TestProxyCancelsOriginalUpstreamContext(t *testing.T) {
	started, canceled := make(chan struct{}), make(chan struct{})
	release := make(chan struct{})
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		close(started)
		select {
		case <-r.Context().Done():
			close(canceled)
		case <-release:
		}
	}))
	defer provider.Close()
	defer close(release)
	u, _ := url.Parse(provider.URL)
	key := strings.Repeat("k", 64)
	h, _ := handler(key, u)
	r := httptest.NewRequest("POST", "/api/search", strings.NewReader(`{"q":"needle"}`))
	r.Header.Set("Authorization", "Bearer "+key)
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	r = r.WithContext(ctx)
	done := make(chan struct{})
	go func() { h.ServeHTTP(httptest.NewRecorder(), r); close(done) }()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("upstream did not start")
	}
	cancel()
	select {
	case <-canceled:
	case <-time.After(3 * time.Second):
		t.Fatal("upstream context not canceled")
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("proxy did not finish")
	}
}

func TestProxyDeadlineCancelsUpstream(t *testing.T) {
	started, canceled := make(chan struct{}), make(chan struct{})
	release := make(chan struct{})
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		close(started)
		select {
		case <-r.Context().Done():
			close(canceled)
		case <-release:
		}
	}))
	defer provider.Close()
	defer close(release)
	u, _ := url.Parse(provider.URL)
	key := strings.Repeat("k", 64)
	h, _ := handler(key, u)
	r := httptest.NewRequest("POST", "/api/search", strings.NewReader(`{"q":"needle","opts":{"MaxWallTime":100000000}}`))
	r.Header.Set("Authorization", "Bearer "+key)
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	done := make(chan struct{})
	w := httptest.NewRecorder()
	go func() { h.ServeHTTP(w, r.WithContext(ctx)); close(done) }()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("upstream did not start")
	}
	select {
	case <-canceled:
	case <-time.After(3 * time.Second):
		t.Fatal("bounded proxy deadline did not cancel upstream")
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("proxy did not finish")
	}
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("deadline failure HTTP=%d", w.Code)
	}
}

func TestAuthenticationBoundary(t *testing.T) {
	key := strings.Repeat("k", 64)
	var calls atomic.Int32
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "" {
			t.Error("credential forwarded to backend")
		}
		w.Write([]byte("private source"))
	}))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	h, err := handler(key, u)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/api/search", "/", "/debug/pprof", "/.api/search"} {
		for _, token := range []string{"", "Bearer wrong", "Bearer " + key + "x"} {
			r := httptest.NewRequest("POST", path, nil)
			r.Header.Set("Authorization", token)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != 401 {
				t.Fatalf("%s: got %d", path, w.Code)
			}
		}
	}
	if calls.Load() != 0 {
		t.Fatal("unauthenticated call reached backend")
	}
	r := httptest.NewRequest("POST", "/api/search", strings.NewReader(`{"q":"test"}`))
	r.Header.Set("Authorization", "Bearer "+key)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 200 || w.Body.String() != "private source" {
		t.Fatal("authenticated search failed")
	}
	w = httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("GET", "/healthz", nil))
	if w.Code != 200 || w.Body.Len() != 0 {
		t.Fatal("health endpoint must not expose source")
	}
}

func TestMissingKeyFailsClosed(t *testing.T) {
	u, _ := url.Parse("http://127.0.0.1")
	for _, key := range []string{"", "short", "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"} {
		if _, err := handler(key, u); err == nil {
			t.Fatal("accepted unconfigured key")
		}
	}
}
