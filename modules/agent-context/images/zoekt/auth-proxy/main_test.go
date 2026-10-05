package main

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestAuthenticationBoundary(t *testing.T) {
	key := strings.Repeat("k", 64)
	calls := 0
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
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
	if calls != 0 {
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
