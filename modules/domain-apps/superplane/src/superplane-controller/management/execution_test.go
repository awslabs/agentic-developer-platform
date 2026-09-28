package management

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/execution"
)

type stepFunc func(context.Context, string) (execution.Result, error)

func (f stepFunc) ExecuteStep(ctx context.Context, step string) (execution.Result, error) {
	return f(ctx, step)
}

func testAssignment() Assignment {
	return Assignment{Binding: execution.Binding{OperationID: "operation", OrgID: "adp-org", WorkspaceID: workspaceID, Holder: "invocation#1", AttemptID: "attempt", FenceToken: 3},
		Action: "provision", JobID: "real-job", AllocationID: "allocation", PlanDigest: strings.Repeat("a", 64), CredentialName: "scoped-token", StepIDs: []string{"launch", "schedule"}}
}

func TestAssignmentTenantStepAndCredentialBoundaries(t *testing.T) {
	for _, change := range []string{"valid", "tenant", "workspace", "token", "digest", "duplicate-step", "duplicate-operation", "fence"} {
		t.Run(change, func(t *testing.T) {
			assignment := testAssignment()
			switch change {
			case "tenant":
				assignment.OrgID = "other"
			case "workspace":
				assignment.WorkspaceID = orgID
			case "token":
				assignment.CredentialName = "../registry-credential"
			case "digest":
				assignment.PlanDigest = "unapproved"
			case "duplicate-step":
				assignment.StepIDs = []string{"launch", "launch"}
			case "fence":
				assignment.FenceToken = 0
			}
			target := Target{WorkspaceID: workspaceID, ExecutionOrgID: "adp-org", Assignments: []Assignment{assignment}}
			if change == "duplicate-operation" {
				target.Assignments = append(target.Assignments, assignment)
			}
			m := &Manager{config: Config{OrgID: orgID}}
			if got := m.validateAssignments([]Target{target}); got != (change == "valid") {
				t.Fatalf("accepted=%v", got)
			}
		})
	}
}

func TestWorkerStopsAtUnknownOutcomeWithoutRetryOrCleanup(t *testing.T) {
	m := &Manager{workers: map[string]*executionWorker{}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	worker := &executionWorker{cancel: cancel, authorizedUntil: time.Now().Add(time.Minute)}
	m.workers["operation"] = worker
	calls := 0
	m.runExecution(ctx, worker, "operation", stepFunc(func(context.Context, string) (execution.Result, error) {
		calls++
		return execution.Result{Disposition: "retain"}, errors.New("uncertain provider")
	}), testAssignment())
	if calls != 1 || worker.state != "recovery_required" {
		t.Fatalf("calls=%d state=%s", calls, worker.state)
	}
	if len(m.workers) != 1 {
		t.Fatal("uncertain assignment dropped, allowing polling to retry")
	}
}

func TestOwnershipExpiryCancelsStalledWorker(t *testing.T) {
	m := &Manager{workers: map[string]*executionWorker{}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	worker := &executionWorker{cancel: cancel, authorizedUntil: time.Now().Add(20 * time.Millisecond)}
	m.workers["operation"] = worker
	done := make(chan struct{})
	go func() {
		m.runExecution(ctx, worker, "operation", stepFunc(func(ctx context.Context, _ string) (execution.Result, error) {
			<-ctx.Done()
			return execution.Result{}, ctx.Err()
		}), testAssignment())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("expired registry authority did not cancel I/O")
	}
}

func TestRevocationAndWorkspaceLossCancelOnlyAffectedWorker(t *testing.T) {
	m := &Manager{workers: map[string]*executionWorker{}}
	for _, key := range []string{"one", "two"} {
		_, cancel := context.WithCancel(context.Background())
		m.workers[key] = &executionWorker{stamp: key, cancel: cancel, authorizedUntil: time.Now().Add(time.Minute)}
	}
	// A failed workspace probe or loss of execution capability may not retain workers.
	m.updateExecutions(context.Background(), nil, Snapshot{GovernedProvisioning: false})
	if len(m.workers) != 0 {
		t.Fatal("revoked workers retained")
	}
}

func TestZeroTargetsAddAndRemoveWithoutRestart(t *testing.T) {
	m := &Manager{workers: map[string]*executionWorker{}}
	started := make(chan string, 2)
	stopped := make(chan string, 2)
	m.executionClient = func(a Assignment) (stepExecutor, error) {
		return stepFunc(func(ctx context.Context, _ string) (execution.Result, error) {
			started <- a.WorkspaceID
			<-ctx.Done()
			stopped <- a.WorkspaceID
			return execution.Result{}, ctx.Err()
		}), nil
	}
	snapshot := Snapshot{RegistryReady: true, GovernedProvisioning: true, LeaseExpiresAt: time.Now().Add(time.Minute), Targets: map[string]string{}}
	m.updateExecutions(context.Background(), nil, snapshot)
	if len(m.workers) != 0 {
		t.Fatal("idle manager created worker")
	}
	a := testAssignment()
	b := testAssignment()
	b.OperationID = "second"
	b.WorkspaceID = orgID
	targets := []Target{{WorkspaceID: workspaceID, Assignments: []Assignment{a}}, {WorkspaceID: orgID, Assignments: []Assignment{b}}}
	snapshot.Targets[workspaceID] = "workspace_verified"
	snapshot.Targets[orgID] = "credential_unavailable"
	m.updateExecutions(context.Background(), targets, snapshot)
	select {
	case id := <-started:
		if id != workspaceID {
			t.Fatal("wrong workspace started")
		}
	case <-time.After(time.Second):
		t.Fatal("new target never started")
	}
	m.updateExecutions(context.Background(), targets, snapshot)
	select {
	case <-started:
		t.Fatal("poll restarted existing operation")
	default:
	}
	snapshot.Targets[orgID] = "workspace_verified"
	m.updateExecutions(context.Background(), targets, snapshot)
	select {
	case id := <-started:
		if id != orgID {
			t.Fatal("wrong workspace started")
		}
	case <-time.After(time.Second):
		t.Fatal("second target never started")
	}
	snapshot.Targets[workspaceID] = "credential_unavailable"
	m.updateExecutions(context.Background(), targets, snapshot)
	select {
	case id := <-stopped:
		if id != workspaceID {
			t.Fatal("other workspace stopped")
		}
	case <-time.After(time.Second):
		t.Fatal("credential loss did not stop worker")
	}
	m.mu.RLock()
	retained := m.workers["second"] != nil
	m.mu.RUnlock()
	if !retained {
		t.Fatal("credential loss affected other workspace")
	}
	m.stopExecutions()
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("shutdown did not stop remaining worker")
	}
}
