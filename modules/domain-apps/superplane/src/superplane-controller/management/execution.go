package management

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"path/filepath"
	"regexp"
	"time"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/execution"
)

// Assignment is metadata published from a trusted, admitted operation. Its token
// is delivered independently by the trusted execution service's read-only mount;
// possession of the observation/registry credential cannot obtain that token.
type Assignment struct {
	execution.Binding
	Action         string   `json:"action"`
	JobID          string   `json:"job_id"`
	AllocationID   string   `json:"allocation_id"`
	PlanDigest     string   `json:"plan_digest"`
	CredentialName string   `json:"credential_name"`
	StepIDs        []string `json:"step_ids"`
}

var credentialNamePattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$`)
var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type executionWorker struct {
	stamp           string
	cancel          context.CancelFunc
	workspaceID     string
	authorizedUntil time.Time
	state           string
}

func assignmentStamp(target Target, assignment Assignment) string {
	// Only immutable assignment and target identity participate. A registry lease
	// refresh must not restart an in-flight provider request.
	payload, _ := json.Marshal([]any{target.WorkspaceID, target.ClusterID, target.Namespace, target.ClusterARN, target.Endpoint, assignment})
	digest := sha256.Sum256(payload)
	return hex.EncodeToString(digest[:])
}

func (m *Manager) validateAssignments(targets []Target) bool {
	seen := map[string]bool{}
	for _, target := range targets {
		if len(target.Assignments) > 16 || (target.Provisional && (len(target.Assignments) > 0 || target.BootstrapOperationID == "" || target.RegistrationClaim == "")) {
			return false
		}
		for _, assignment := range target.Assignments {
			if (assignment.Action != "provision" && assignment.Action != "teardown") || assignment.OrgID != target.ExecutionOrgID || assignment.WorkspaceID != target.WorkspaceID || assignment.OperationID == "" || assignment.JobID == "" || assignment.AllocationID == "" || assignment.AttemptID == "" || assignment.Holder == "" || assignment.FenceToken < 1 || !digestPattern.MatchString(assignment.PlanDigest) || !credentialNamePattern.MatchString(assignment.CredentialName) || len(assignment.StepIDs) < 1 || len(assignment.StepIDs) > 16 || seen[assignment.OperationID] {
				return false
			}
			seen[assignment.OperationID] = true
			steps := map[string]bool{}
			for _, step := range assignment.StepIDs {
				if step == "" || len(step) > 2048 || steps[step] {
					return false
				}
				steps[step] = true
			}
		}
	}
	// This bounds local worker concurrency; provider attempts and spend remain
	// bounded separately by the shared service's durable admission/lease policy.
	return len(seen) <= 32
}

func (m *Manager) stopExecutions() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for key, worker := range m.workers {
		worker.cancel()
		delete(m.workers, key)
	}
	m.snapshot.GovernedProvisioning = false
}

func (m *Manager) updateExecutions(ctx context.Context, targets []Target, snapshot Snapshot) {
	m.mu.Lock()
	defer m.mu.Unlock()
	keep := map[string]bool{}
	if snapshot.GovernedProvisioning {
		for _, target := range targets {
			if snapshot.Targets[target.WorkspaceID] != "workspace_verified" {
				continue
			}
			for _, assignment := range target.Assignments {
				key, stamp := assignment.OperationID, assignmentStamp(target, assignment)
				if worker := m.workers[key]; worker != nil && worker.stamp == stamp {
					worker.authorizedUntil = snapshot.LeaseExpiresAt.Add(-5 * time.Second)
					keep[key] = true
					continue
				}
				if worker := m.workers[key]; worker != nil {
					worker.cancel()
				}
				var client stepExecutor
				var err error
				if m.executionClient != nil {
					client, err = m.executionClient(assignment)
				} else {
					client, err = execution.New(m.config.ExecutionSocket, filepath.Join(m.config.ExecutionCredentialsDir, assignment.CredentialName), assignment.Binding)
				}
				if err != nil {
					continue
				}
				workerCtx, cancel := context.WithCancel(ctx)
				worker := &executionWorker{stamp: stamp, cancel: cancel, workspaceID: target.WorkspaceID, authorizedUntil: snapshot.LeaseExpiresAt.Add(-5 * time.Second), state: "verifying_authority"}
				m.workers[key] = worker
				keep[key] = true
				go m.runExecution(workerCtx, worker, key, client, assignment)
			}
		}
	}
	for key, worker := range m.workers {
		if !keep[key] {
			worker.cancel()
			delete(m.workers, key)
		}
	}
}

type stepExecutor interface {
	ExecuteStep(context.Context, string) (execution.Result, error)
}

func (m *Manager) runExecution(ctx context.Context, worker *executionWorker, key string, client stepExecutor, assignment Assignment) {
	// Expiry is enforced even if the registry hangs between refreshes. Cancelling
	// local I/O never claims that a provider call was undone or permits a retry.
	done := make(chan struct{})
	defer close(done)
	go func() {
		ticker := time.NewTicker(250 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				m.mu.RLock()
				valid := m.workers[key] == worker && time.Now().Before(worker.authorizedUntil)
				m.mu.RUnlock()
				if !valid {
					worker.cancel()
					return
				}
			}
		}
	}()
	state := "steps_observed"
	for _, step := range assignment.StepIDs {
		m.mu.Lock()
		valid := m.workers[key] == worker && time.Now().Before(worker.authorizedUntil) && ctx.Err() == nil
		if valid {
			worker.state = "executing"
		}
		m.mu.Unlock()
		if !valid {
			state = "authority_lost"
			break
		}
		result, err := client.ExecuteStep(ctx, step)
		if err != nil || result.Call.Outcome == nil || *result.Call.Outcome != "succeeded" || result.Disposition != "settle" || result.Call.JobID != assignment.JobID {
			state = "recovery_required"
			break
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.workers[key] == worker {
		worker.state = state
	}
	// Leave the completed/refused entry until the trusted assignment changes.
	// A poll tick is not authority to retry an uncertain provider effect.
}
