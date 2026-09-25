// Package management reconciles durable registrations without acquiring cloud
// spending authority. Workspace execution requires a separately authenticated
// shared executor and a current registration; idle health is not workspace readiness.
package management

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

type Target struct {
	SharedMembership     bool                  `json:"shared_membership,omitempty"`
	PlatformEligible     bool                  `json:"platform_eligible,omitempty"`
	MembershipCredential *MembershipCredential `json:"membership_credential,omitempty"`
	Provisional          bool                  `json:"provisional,omitempty"`
	BootstrapOperationID string                `json:"bootstrap_operation_id,omitempty"`
	RegistrationClaim    string                `json:"registration_claim,omitempty"`
	ExecutionOrgID       string                `json:"execution_org_id"`
	WorkspaceID          string                `json:"workspace_id"`
	ClusterID            string                `json:"cluster_id"`
	Namespace            string                `json:"namespace"`
	WorkspaceStatus      string                `json:"workspace_status"`
	ClusterStatus        string                `json:"cluster_status"`
	ClusterARN           string                `json:"cluster_arn"`
	Endpoint             string                `json:"endpoint"`
	Assignments          []Assignment          `json:"execution_assignments,omitempty"`
	Reports              []json.RawMessage     `json:"execution_reports,omitempty"`
	ProviderObservations []ProviderObservation `json:"provider_observations,omitempty"`
}

type registry struct {
	Version              int       `json:"version"`
	OrgID                string    `json:"org_id"`
	LeaseExpiresAt       time.Time `json:"lease_expires_at"`
	FenceToken           int64     `json:"fence_token"`
	Targets              []Target  `json:"targets"`
	GovernedProvisioning bool      `json:"governed_provisioning"`
}

type BootstrapObservation struct {
	MembershipCredential *MembershipCredential `json:"membership_credential,omitempty"`
	BootstrapOperationID string                `json:"bootstrap_operation_id"`
	RegistrationClaim    string                `json:"registration_claim"`
	ClusterARN           string                `json:"cluster_arn"`
	Namespace            string                `json:"namespace"`
}

type Snapshot struct {
	InstanceID            string                          `json:"instance_id"`
	FenceToken            int64                           `json:"fence_token"`
	BootstrapObservations map[string]BootstrapObservation `json:"bootstrap_observations"`
	Mode                  string                          `json:"mode"`
	RegistryReady         bool                            `json:"registry_ready"`
	LastReconciled        time.Time                       `json:"last_reconciled"`
	LeaseExpiresAt        time.Time                       `json:"lease_expires_at"`
	Targets               map[string]string               `json:"targets"`
	GovernedProvisioning  bool                            `json:"governed_provisioning"`
	ExecutionSupported    bool                            `json:"governed_execution_supported"`
	Executions            map[string]string               `json:"executions"`
	Reports               map[string][]json.RawMessage    `json:"execution_reports"`
}

type Config struct {
	APIURL                    string
	OrgID                     string
	CredentialFile            string
	WorkspaceCredentialsDir   string
	ManagementAPIServer       string
	EnableExecution           bool
	ExecutionSocket           string
	ExecutionCredentialsDir   string
	InstanceFile              string
	ObservationCredentialsDir string
}

type Manager struct {
	config          Config
	instanceID      string
	client          *http.Client
	mu              sync.RWMutex
	targets         map[string]Target
	snapshot        Snapshot
	inspect         func(context.Context, Target) string
	reconcileMu     sync.Mutex
	workers         map[string]*executionWorker
	executionClient func(Assignment) (stepExecutor, error)
}

func New(config Config) (*Manager, error) {
	if config.EnableExecution && (!filepath.IsAbs(config.ExecutionSocket) || !filepath.IsAbs(config.ExecutionCredentialsDir) || !filepath.IsAbs(config.InstanceFile)) {
		return nil, errors.New("governed execution requires explicit trusted socket and token mount paths")
	}
	u, err := url.Parse(config.APIURL)
	if err != nil || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || u.Host == "" {
		return nil, errors.New("an explicit control-plane API origin is required")
	}
	// In-cluster HTTP is permitted only for an explicit Kubernetes service DNS
	// name. It must be isolated by the installer NetworkPolicy. External origins
	// require TLS. Neither environment proxies nor redirects receive credentials.
	if u.Scheme != "https" && !(u.Scheme == "http" && (strings.HasSuffix(u.Hostname(), ".svc.cluster.local") || strings.HasSuffix(u.Hostname(), ".svc"))) {
		return nil, errors.New("registry transport must use HTTPS or in-cluster service DNS")
	}
	if !uuidPattern.MatchString(config.OrgID) || config.CredentialFile == "" {
		return nil, errors.New("organization and registry credential file are required")
	}
	if config.WorkspaceCredentialsDir != "" {
		managementURL, err := url.Parse(config.ManagementAPIServer)
		if err != nil || managementURL.Scheme != "https" || managementURL.Host == "" {
			return nil, errors.New("workspace inspection requires the explicit management API endpoint exclusion")
		}
	}
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return nil, err
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	h := hex.EncodeToString(b)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	m := &Manager{
		config:     config,
		workers:    map[string]*executionWorker{},
		instanceID: h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:],
		client:     &http.Client{Timeout: 20 * time.Second, Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		snapshot:   Snapshot{Mode: "management", ExecutionSupported: true, Targets: map[string]string{}},
	}
	if config.EnableExecution {
		// Pod-private volume: only this controller writes its random instance ID;
		// the trusted sidecar reads it and binds tokens to this registry owner.
		temporary, err := os.CreateTemp(filepath.Dir(config.InstanceFile), ".instance-*")
		if err != nil {
			return nil, errors.New("controller instance mount unavailable")
		}
		name := temporary.Name()
		defer os.Remove(name)
		if _, err = temporary.WriteString(m.instanceID); err == nil {
			err = temporary.Chmod(0640)
		}
		closeErr := temporary.Close()
		if err != nil || closeErr != nil {
			return nil, errors.New("controller instance publication failed")
		}
		if err := os.Rename(name, config.InstanceFile); err != nil {
			return nil, errors.New("controller instance publication failed")
		}
	}
	m.inspect = m.inspectTarget
	return m, nil
}

func (m *Manager) credential() ([]byte, error) {
	b, err := os.ReadFile(m.config.CredentialFile)
	if err != nil || len(b) < 32 || len(b) > 8192 {
		return nil, errors.New("registry credential unavailable")
	}
	return bytes.TrimSpace(b), nil
}

func (m *Manager) Reconcile(ctx context.Context) error {
	m.reconcileMu.Lock()
	defer m.reconcileMu.Unlock()
	succeeded := false
	defer func() {
		if !succeeded {
			m.stopExecutions()
		}
	}()
	// Clear readiness before the request. An expired/revoked credential or a
	// failed registry read cannot retain the previous successful ready state.
	m.mu.Lock()
	m.snapshot.RegistryReady = false
	m.snapshot.Targets = map[string]string{}
	m.snapshot.BootstrapObservations = map[string]BootstrapObservation{}
	m.mu.Unlock()
	credential, err := m.credential()
	if err != nil {
		return err
	}
	body, _ := json.Marshal(map[string]string{"org_id": m.config.OrgID, "instance_id": m.instanceID})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(m.config.APIURL, "/")+"/internal/controller/reconcile", bytes.NewReader(body))
	if err != nil {
		return errors.New("invalid registry request")
	}
	req.Header.Set("Authorization", string(credential))
	req.Header.Set("Content-Type", "application/json")
	response, err := m.client.Do(req)
	if err != nil {
		return errors.New("registry transport unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return errors.New("registry authentication, lease or database unavailable")
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, (4<<20)+1))
	if err != nil || len(data) > 4<<20 {
		return errors.New("invalid registry response size")
	}
	var result registry
	if json.Unmarshal(data, &result) != nil || result.Version != 1 || result.OrgID != m.config.OrgID || result.FenceToken < 1 || result.Targets == nil || !result.LeaseExpiresAt.After(time.Now()) || result.LeaseExpiresAt.After(time.Now().Add(time.Minute)) || !m.validateAssignments(result.Targets) {
		return errors.New("registry contract mismatch")
	}
	snapshot := Snapshot{InstanceID: m.instanceID, FenceToken: result.FenceToken, BootstrapObservations: map[string]BootstrapObservation{}, Mode: "management", ExecutionSupported: true, RegistryReady: true, LastReconciled: time.Now().UTC(), LeaseExpiresAt: result.LeaseExpiresAt, Targets: map[string]string{}}
	snapshot.Reports = map[string][]json.RawMessage{}
	if m.config.EnableExecution {
		snapshot.Mode = "governed"
		snapshot.ExecutionSupported = true
		snapshot.GovernedProvisioning = result.GovernedProvisioning
	}
	for _, target := range result.Targets {
		if !uuidPattern.MatchString(target.WorkspaceID) {
			return errors.New("invalid registry identity")
		}
		if _, duplicate := snapshot.Targets[target.WorkspaceID]; duplicate {
			return errors.New("duplicate registry identity")
		}
		snapshot.Targets[target.WorkspaceID] = "pending"
		snapshot.Reports[target.WorkspaceID] = target.Reports
	}
	// All probes are reads. Bound each probe and the entire reconcile to the
	// current lease. There is intentionally no provider client in this package.
	probeCtx, cancel := context.WithDeadline(ctx, result.LeaseExpiresAt.Add(-5*time.Second))
	defer cancel()
	for _, target := range result.Targets {
		snapshot.Targets[target.WorkspaceID] = m.inspect(probeCtx, target)
		if snapshot.Targets[target.WorkspaceID] == "observed_execution_unavailable" && target.Provisional {
			snapshot.BootstrapObservations[target.WorkspaceID] = BootstrapObservation{BootstrapOperationID: target.BootstrapOperationID, RegistrationClaim: target.RegistrationClaim, ClusterARN: target.ClusterARN, Namespace: target.Namespace, MembershipCredential: target.MembershipCredential}
		}
		if snapshot.Targets[target.WorkspaceID] == "observed_execution_unavailable" && !target.Provisional && snapshot.GovernedProvisioning {
			snapshot.Targets[target.WorkspaceID] = "workspace_verified"
		}
	}
	if probeCtx.Err() != nil {
		return errors.New("registry lease expired during inspection")
	}
	m.mu.Lock()
	m.snapshot = snapshot
	m.targets = make(map[string]Target, len(result.Targets))
	for _, target := range result.Targets {
		m.targets[target.WorkspaceID] = target
	}
	m.mu.Unlock()
	m.updateExecutions(ctx, result.Targets, snapshot)
	succeeded = true
	return nil
}

func (m *Manager) Snapshot() Snapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	s := m.snapshot
	s.Targets = make(map[string]string, len(m.snapshot.Targets))
	for id, state := range m.snapshot.Targets {
		s.Targets[id] = state
	}
	s.BootstrapObservations = make(map[string]BootstrapObservation, len(m.snapshot.BootstrapObservations))
	for id, observation := range m.snapshot.BootstrapObservations {
		s.BootstrapObservations[id] = observation
	}
	s.RegistryReady = s.RegistryReady && time.Now().Before(s.LeaseExpiresAt)
	s.GovernedProvisioning = s.GovernedProvisioning && s.RegistryReady
	s.Executions = make(map[string]string, len(m.workers))
	s.Reports = make(map[string][]json.RawMessage, len(m.snapshot.Reports))
	for workspace, reports := range m.snapshot.Reports {
		for _, report := range reports {
			s.Reports[workspace] = append(s.Reports[workspace], append(json.RawMessage(nil), report...))
		}
	}
	for id, worker := range m.workers {
		if s.RegistryReady && time.Now().Before(worker.authorizedUntil) {
			s.Executions[id] = worker.state
		} else {
			s.Executions[id] = "authority_lost"
		}
	}
	return s
}

func (m *Manager) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		if !m.Snapshot().RegistryReady {
			http.Error(w, "registry not ready", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /statusz", func(w http.ResponseWriter, r *http.Request) {
		credential, err := m.credential()
		if err != nil || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), credential) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(m.Snapshot())
	})
	mux.HandleFunc("GET /workload-observation", m.workloadObservation)
	return mux
}

func (m *Manager) Run(ctx context.Context, address string) error {
	defer m.stopExecutions()
	server := &http.Server{Addr: address, Handler: m.Handler(), ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second}
	errors := make(chan error, 1)
	go func() { errors <- server.ListenAndServe() }()
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	defer func() {
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	for {
		_ = m.Reconcile(ctx) // Readiness records failure; do not log transport secrets.
		select {
		case <-ctx.Done():
			return nil
		case err := <-errors:
			return err
		case <-ticker.C:
		}
	}
}
