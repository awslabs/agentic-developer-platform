package skypilot

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestServiceTokenIsAttachedAndNeverFollowsRedirect(t *testing.T) {
	leaked := false
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { leaked = true }))
	defer other.Close()
	first := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer private-service-token" {
			t.Error("service token absent")
		}
		http.Redirect(w, r, other.URL, http.StatusTemporaryRedirect)
	}))
	defer first.Close()
	client := NewClient(first.URL, WithServiceToken("private-service-token"))
	if _, err := client.Health(context.Background()); err == nil {
		t.Error("redirect treated as health")
	}
	if leaked {
		t.Fatal("service credential followed redirect")
	}
}
