// The mixed-tenant index is private to the Door. Never expose the raw webserver.
package main

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
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
