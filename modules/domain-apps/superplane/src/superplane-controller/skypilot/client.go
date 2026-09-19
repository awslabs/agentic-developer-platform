package skypilot

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// maxResponseBodySize limits the size of API responses to prevent OOM from
// malicious or buggy servers.
const maxResponseBodySize = 10 * 1024 * 1024 // 10 MB

// Client is an HTTP client for the SkyPilot REST API server.
type Client struct {
	baseURL    string
	httpClient *http.Client
}

// ClientOption configures the Client.
type ClientOption func(*Client)

// WithHTTPClient sets a custom *http.Client.
func WithHTTPClient(hc *http.Client) ClientOption {
	return func(c *Client) {
		c.httpClient = hc
	}
}

// WithTimeout sets the default request timeout for non-streaming calls.
func WithTimeout(d time.Duration) ClientOption {
	return func(c *Client) {
		c.httpClient.Timeout = d
	}
}

// NewClient creates a new SkyPilot API client.
// baseURL should be e.g. "http://skypilot-api.skypilot.svc.cluster.local:46580".
func NewClient(baseURL string, opts ...ClientOption) *Client {
	c := &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
	for _, opt := range opts {
		opt(c)
	}
	return c
}

// Health checks whether the SkyPilot API server is healthy.
// GET /api/health
func (c *Client) Health(ctx context.Context) (*HealthResponse, error) {
	var resp HealthResponse
	if err := c.doJSON(ctx, http.MethodGet, "/api/health", nil, &resp); err != nil {
		return nil, fmt.Errorf("health check: %w", err)
	}
	return &resp, nil
}

// Launch launches a cluster.
// POST /launch — returns a request ID to track progress via StreamProgress.
func (c *Client) Launch(ctx context.Context, req LaunchRequest) (string, error) {
	var resp RequestResponse
	if err := c.doJSON(ctx, http.MethodPost, "/launch", req, &resp); err != nil {
		return "", fmt.Errorf("launch: %w", err)
	}
	return resp.RequestID, nil
}

// Status returns cluster statuses.
// POST /status
func (c *Client) Status(ctx context.Context, clusterNames ...string) ([]ClusterInfo, error) {
	var body interface{}
	if len(clusterNames) > 0 {
		body = StatusRequest{ClusterNames: clusterNames}
	}
	var resp StatusResponse
	if err := c.doJSON(ctx, http.MethodPost, "/status", body, &resp); err != nil {
		return nil, fmt.Errorf("status: %w", err)
	}
	return resp, nil
}

// Down terminates one or more clusters.
// POST /down — returns a request ID.
func (c *Client) Down(ctx context.Context, clusterNames []string, purge bool) (string, error) {
	var resp RequestResponse
	if err := c.doJSON(ctx, http.MethodPost, "/down", DownRequest{
		ClusterNames: clusterNames,
		Purge:        purge,
	}, &resp); err != nil {
		return "", fmt.Errorf("down: %w", err)
	}
	return resp.RequestID, nil
}

// EnabledClouds lists enabled cloud providers.
// GET /enabled_clouds
func (c *Client) EnabledClouds(ctx context.Context) ([]CloudInfo, error) {
	var resp EnabledCloudsResponse
	if err := c.doJSON(ctx, http.MethodGet, "/enabled_clouds", nil, &resp); err != nil {
		return nil, fmt.Errorf("enabled clouds: %w", err)
	}
	return resp.EnabledClouds, nil
}

// StreamProgress opens an SSE connection to stream request progress.
// GET /api/stream?request_id=X
// The returned channel is closed when the stream ends or context is cancelled.
// Callers must consume the channel to avoid goroutine leaks.
func (c *Client) StreamProgress(ctx context.Context, requestID string) (<-chan StreamEvent, <-chan error) {
	eventCh := make(chan StreamEvent, 16)
	errCh := make(chan error, 1)

	go func() {
		defer close(eventCh)
		defer close(errCh)

		url := fmt.Sprintf("%s/api/stream?request_id=%s", c.baseURL, url.QueryEscape(requestID))
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			errCh <- fmt.Errorf("stream progress: create request: %w", err)
			return
		}
		req.Header.Set("Accept", "text/event-stream")
		req.Header.Set("Cache-Control", "no-cache")

		// Use a separate client without timeout for streaming.
		streamClient := &http.Client{}
		resp, err := streamClient.Do(req)
		if err != nil {
			errCh <- fmt.Errorf("stream progress: %w", err)
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
			errCh <- fmt.Errorf("stream progress: unexpected status %d: %s", resp.StatusCode, string(body))
			return
		}

		c.parseSSE(ctx, resp.Body, eventCh, errCh)
	}()

	return eventCh, errCh
}

// parseSSE reads SSE events from r and sends them on eventCh.
func (c *Client) parseSSE(ctx context.Context, r io.Reader, eventCh chan<- StreamEvent, errCh chan<- error) {
	scanner := bufio.NewScanner(r)
	var event StreamEvent

	for scanner.Scan() {
		select {
		case <-ctx.Done():
			errCh <- ctx.Err()
			return
		default:
		}

		line := scanner.Text()

		// Empty line = dispatch event
		if line == "" {
			if event.Data != "" || event.Event != "" {
				event.ReceivedAt = time.Now()
				event.IsTerminal = event.Event == StreamEventTypeComplete || event.Event == StreamEventTypeError
				select {
				case eventCh <- event:
				case <-ctx.Done():
					errCh <- ctx.Err()
					return
				}
				if event.IsTerminal {
					return
				}
			}
			event = StreamEvent{}
			continue
		}

		// Parse SSE field
		if strings.HasPrefix(line, "id:") {
			event.ID = strings.TrimSpace(strings.TrimPrefix(line, "id:"))
		} else if strings.HasPrefix(line, "event:") {
			event.Event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		} else if strings.HasPrefix(line, "data:") {
			data := strings.TrimPrefix(line, "data:")
			if len(data) > 0 && data[0] == ' ' {
				data = data[1:]
			}
			if event.Data != "" {
				event.Data += "\n"
			}
			event.Data += data
		}
		// Lines starting with ":" are comments — ignore.
	}

	// If there's a partial event remaining, send it.
	if event.Data != "" || event.Event != "" {
		event.ReceivedAt = time.Now()
		event.IsTerminal = event.Event == StreamEventTypeComplete || event.Event == StreamEventTypeError
		select {
		case eventCh <- event:
		case <-ctx.Done():
		}
	}

	if err := scanner.Err(); err != nil {
		errCh <- fmt.Errorf("stream progress: read: %w", err)
	}
}

// doJSON performs an HTTP request, marshaling reqBody as JSON and unmarshaling
// the response into respBody.
func (c *Client) doJSON(ctx context.Context, method, path string, reqBody, respBody interface{}) error {
	var bodyReader io.Reader
	if reqBody != nil {
		data, err := json.Marshal(reqBody)
		if err != nil {
			return fmt.Errorf("marshal request: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	}

	url := c.baseURL + path
	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}

	if reqBody != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("do request: %w", err)
	}
	defer resp.Body.Close()

	respData, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBodySize))
	if err != nil {
		return fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &APIError{
			StatusCode: resp.StatusCode,
			Body:       string(respData),
		}
	}

	if respBody != nil && len(respData) > 0 {
		if err := json.Unmarshal(respData, respBody); err != nil {
			return fmt.Errorf("unmarshal response: %w", err)
		}
	}

	return nil
}

// APIError represents a non-2xx response from the SkyPilot API.
type APIError struct {
	StatusCode int
	Body       string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("skypilot api error (status %d): %s", e.StatusCode, e.Body)
}
