// The mixed-tenant index is private to the Door. Never expose the raw webserver.
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

const (
	maxSearchBody   = 65536
	maxSearchTime   = 10 * time.Second
	maxDocuments    = 500
	maxShardMatches = 100000
	maxTotalMatches = 1000000
)

// Only the pinned JSON API's consumed fields are accepted. In particular,
// top-level num is not a provider limit. No caller may enable estimate/debug
// modes or remove the private runtime's ceilings.
type boundedSearchOptions struct {
	MaxWallTime        time.Duration
	MaxDocDisplayCount int
	ShardMaxMatchCount int
	TotalMaxMatchCount int
	Whole              bool
}

type boundedSearchRequest struct {
	Q    string               `json:"q"`
	Opts boundedSearchOptions `json:"opts"`
}

func limit(value, ceiling int) (int, error) {
	if value < 0 {
		return 0, errors.New("negative search limit")
	}
	if value == 0 || value > ceiling {
		return ceiling, nil
	}
	return value, nil
}

func boundedSearch(body io.Reader) ([]byte, time.Duration, error) {
	var request boundedSearchRequest
	decoder := json.NewDecoder(body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		return nil, 0, err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF || strings.TrimSpace(request.Q) == "" {
		return nil, 0, errors.New("invalid search document")
	}
	opts := &request.Opts
	if opts.MaxWallTime < 0 {
		return nil, 0, errors.New("negative search deadline")
	}
	if opts.MaxWallTime == 0 || opts.MaxWallTime > maxSearchTime {
		opts.MaxWallTime = maxSearchTime
	}
	for _, field := range []struct {
		value   *int
		ceiling int
	}{
		{&opts.MaxDocDisplayCount, maxDocuments},
		{&opts.ShardMaxMatchCount, maxShardMatches},
		{&opts.TotalMaxMatchCount, maxTotalMatches},
	} {
		value, err := limit(*field.value, field.ceiling)
		if err != nil {
			return nil, 0, err
		}
		*field.value = value
	}
	encoded, err := json.Marshal(request)
	if len(encoded) > maxSearchBody {
		return nil, 0, errors.New("oversized search document")
	}
	return encoded, opts.MaxWallTime, err
}

func handler(key string, upstream *url.URL) (http.Handler, error) {
	if len(key) < 32 || strings.HasPrefix(strings.ToUpper(key), "PLACEHOLDER") {
		return nil, errors.New("Zoekt authentication key is not configured")
	}
	proxy := httputil.NewSingleHostReverseProxy(upstream)
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "search unavailable", http.StatusServiceUnavailable)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" && r.Method == http.MethodGet {
			client := &http.Client{Timeout: 2 * time.Second}
			response, err := client.Get(upstream.String() + "/")
			if err != nil {
				http.Error(w, "not ready", http.StatusServiceUnavailable)
				return
			}
			response.Body.Close()
			if response.StatusCode != http.StatusOK {
				http.Error(w, "not ready", http.StatusServiceUnavailable)
				return
			}
			w.WriteHeader(http.StatusOK)
			return
		}
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+key)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		// Expose only the API the Door uses, never debug, file, or RPC endpoints.
		if r.URL.Path != "/api/search" || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		if r.URL.RawQuery != "" {
			http.Error(w, "invalid search request", http.StatusBadRequest)
			return
		}
		body := http.MaxBytesReader(w, r.Body, maxSearchBody)
		encoded, deadline, err := boundedSearch(body)
		body.Close()
		if err != nil {
			http.Error(w, "invalid search request", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), deadline)
		defer cancel()
		r = r.WithContext(ctx)
		r.Body = io.NopCloser(bytes.NewReader(encoded))
		r.ContentLength = int64(len(encoded))
		r.TransferEncoding = nil
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Content-Length", fmt.Sprint(len(encoded)))
		r.Header.Del("Authorization")
		proxy.ServeHTTP(w, r)
	}), nil
}

func run() error {
	upstream, _ := url.Parse("http://127.0.0.1:6071")
	h, err := handler(os.Getenv("ZOEKT_API_KEY"), upstream)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	child := exec.CommandContext(ctx, "zoekt-webserver", "-index", "/data/index", "-listen", "127.0.0.1:6071", "-rpc")
	child.Stdout, child.Stderr = os.Stdout, os.Stderr
	if err := child.Start(); err != nil {
		return err
	}
	childDone := make(chan error, 1)
	go func() { childDone <- child.Wait() }()
	server := &http.Server{Addr: ":6070", Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second}
	serverDone := make(chan error, 1)
	go func() { serverDone <- server.ListenAndServe() }()
	select {
	case <-ctx.Done():
	case err := <-childDone:
		server.Close()
		return fmt.Errorf("search process stopped: %v", err)
	case err := <-serverDone:
		stop()
		<-childDone
		return err
	}
	server.Close()
	<-childDone
	return nil
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
