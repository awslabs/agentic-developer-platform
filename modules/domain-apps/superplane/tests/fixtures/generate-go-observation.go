package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
)

func main() {
	p := map[string]interface{}{"contract_version": "v1", "kind": "budget_usage", "subject": map[string]string{"cluster_id": "cluster-w1-a", "workspace": "ws-w1"}, "reported_at": "2026-09-16T12:00:00Z", "reporter": "moniteur-é<&>", "status": "not_checked", "budget": map[string]interface{}{"workspace": "ws-w1", "window_start": "2026-09-16T11:00:00Z", "window_end": "2026-09-16T12:00:00Z", "observed_spend_usd": 0.000001, "currency": "USD"}}
	b, _ := json.Marshal(p)
	key := []byte("test-signing-key-not-a-credential")
	h := hmac.New(sha256.New, key)
	h.Write(b)
	out, _ := json.MarshalIndent(map[string]string{"source": "Go encoding/json and crypto/hmac; synthetic credential", "body_base64": base64.StdEncoding.EncodeToString(b), "signature": "sha256=" + hex.EncodeToString(h.Sum(nil))}, "", "  ")
	fmt.Println(string(out))
}
