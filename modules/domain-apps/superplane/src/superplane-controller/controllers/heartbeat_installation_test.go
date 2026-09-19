package controllers

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestInstallationHeartbeatSignsExactWorkspaceObservation(t *testing.T) {
	var received bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received = true
		body, _ := io.ReadAll(r.Body)
		if r.URL.Path != "/internal/observations" {
			t.Errorf("wrong route %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "controller-secret" {
			t.Error("missing service authentication")
		}
		mac := hmac.New(sha256.New, []byte("signing-secret"))
		mac.Write(body)
		if r.Header.Get("x-superplane-signature") != fmt.Sprintf("sha256=%x", mac.Sum(nil)) {
			t.Error("signature is not over transmitted bytes")
		}
		var payload map[string]any
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatal(err)
		}
		if payload["contract_version"] != "v1" || payload["kind"] != "fleet_health" {
			t.Error("wrong contract")
		}
		if payload["subject"].(map[string]any)["workspace"] != "workspace-id" {
			t.Error("wrong workspace")
		}
		if strings.Contains(string(body), "controller-secret") {
			t.Error("credential in body")
		}
		w.WriteHeader(200)
	}))
	defer server.Close()
	sender := HeartbeatSender{APIURL: server.URL, ClusterID: "cluster-id", WorkspaceID: "workspace-id", Credential: "controller-secret", SigningKey: "signing-secret", RequireAuthentication: true, HTTPClient: server.Client()}
	if err := sender.Send(context.Background(), ClusterHeartbeat{Status: "healthy", Timestamp: time.Now()}); err != nil {
		t.Fatal(err)
	}
	if !received {
		t.Fatal("observation not sent")
	}
}

func TestInstallationHeartbeatNeverFallsBackToLegacy(t *testing.T) {
	sender := HeartbeatSender{RequireAuthentication: true}
	if err := sender.Send(context.Background(), ClusterHeartbeat{Timestamp: time.Now()}); err == nil {
		t.Fatal("missing authentication accepted")
	}
}
