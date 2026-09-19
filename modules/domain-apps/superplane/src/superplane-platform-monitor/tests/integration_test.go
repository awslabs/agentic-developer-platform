//go:build integration

// Package tests contains integration tests for the platform monitor.
//
// # These tests were rewritten by issue #5056 (U15), and why they had to be
//
// They used to open a `pgxpool` against a real PostgreSQL, seed the `clusters`
// table, run a monitor cycle, and assert by selecting `clusters.health_status`
// and counting `reconcile_locks` rows. Every one of those steps needs the direct
// table grant that U15 withdraws.
//
// So the old file could not be carried forward by fixing a signature. Its asserts
// *required* the permission whose removal is the story's acceptance: a suite that
// still passed by reading the tables directly would have been evidence that the
// direct path still worked. Keeping it would also have made the grant withdrawal
// break the tests, which invites re-granting the permission to get CI green — the
// exact outcome R11 acceptance 1 exists to prevent.
//
// What replaces it drives the same monitor cycles through the authenticated
// client and asserts through the receiver's own scoped read routes. It therefore
// needs a deployed receiver rather than a database:
//
//	INTEGRATION_OBSERVATION_API_URL="https://superplane-api.internal" \
//	INTEGRATION_OBSERVATION_CREDENTIAL="..." \
//	INTEGRATION_OBSERVATION_SIGNING_KEY="..." \
//	INTEGRATION_CLUSTER_ID="..." \
//	  go test -tags=integration -v ./tests/...
//
// # This is not the live acceptance
//
// These tests exercise the monitor against a receiver; they do not prove
// acceptance 1, which requires the grant to actually be withdrawn and observed.
// That is U15-L1, deferred with the story: it needs a deployed receiver and
// monitor, an authorized database grant change, named identities and a rollback
// owner. `docs/runbooks/superplane-monitor-grant-withdrawal.md` sequences it.
//
// Like the previous version, everything here skips unless its environment is set,
// and the CI lane does not pass `-tags=integration`, so none of it runs there.
// Credentials come from the environment and are never logged.
package tests

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/config"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/monitors"
)

// receiverTarget is the deployed receiver these tests run against, or a skip.
type receiverTarget struct {
	baseURL    string
	credential string
	signingKey []byte
	clusterID  string
}

func target(t *testing.T) receiverTarget {
	t.Helper()
	baseURL := os.Getenv("INTEGRATION_OBSERVATION_API_URL")
	if baseURL == "" {
		t.Skip("INTEGRATION_OBSERVATION_API_URL not set — skipping integration test")
	}
	credential := os.Getenv("INTEGRATION_OBSERVATION_CREDENTIAL")
	signingKey := os.Getenv("INTEGRATION_OBSERVATION_SIGNING_KEY")
	if credential == "" || signingKey == "" {
		// Skipped rather than run unauthenticated: a suite that silently dropped
		// authentication would report a passing monitor against a receiver that
		// refuses every call.
		t.Skip("observation credential/signing key not set — skipping integration test")
	}
	return receiverTarget{
		baseURL:    baseURL,
		credential: credential,
		signingKey: []byte(signingKey),
		clusterID:  os.Getenv("INTEGRATION_CLUSTER_ID"),
	}
}

func newIntegrationClient(t *testing.T, target receiverTarget, instanceID string) *db.Client {
	t.Helper()
	client, err := db.NewClient(db.ClientConfig{
		BaseURL:    target.baseURL,
		Credential: target.credential,
		SigningKey: target.signingKey,
		Reporter:   "superplane-platform-monitor",
		InstanceID: instanceID,
	})
	if err != nil {
		// Never %v the config: the error is safe, but building the message from
		// the target would risk putting the credential in the test log.
		t.Fatalf("build observation client: %v", err)
	}
	t.Cleanup(client.Close)
	return client
}

func integrationConfig(monitorID string) *config.Config {
	return &config.Config{
		PollInterval:                  30 * time.Second,
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     monitorID,
		MetricsAddr:                   ":0",
		LogLevel:                      "debug",
	}
}

// TestIntegration_MonitorRunsWithoutADatabaseGrant is the closest a test suite can
// get to acceptance 1 without performing the grant change itself.
//
// The process under test holds no database credential at all — `config.Config` no
// longer has a field for one — so a full cycle completing here is a cycle that
// completed without the grant.
func TestIntegration_MonitorRunsWithoutADatabaseGrant(t *testing.T) {
	target := target(t)
	ctx := context.Background()
	client := newIntegrationClient(t, target, "integration-no-grant")

	monitor := &monitors.ClusterHealthMonitor{
		DB:        client,
		Config:    integrationConfig("integration-no-grant"),
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.UnconfiguredEKSProber{},
	}

	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor cycle failed against the receiver: %v", err)
	}
}

// TestIntegration_ClusterListIsScopedToTheCredential verifies the monitor only
// sees what its credential is granted.
//
// Asserted as a property of the response rather than by comparing against a
// database query, because a query broad enough to check the negative case would
// itself need the withdrawn grant.
func TestIntegration_ClusterListIsScopedToTheCredential(t *testing.T) {
	target := target(t)
	ctx := context.Background()
	client := newIntegrationClient(t, target, "integration-scope")

	clusters, err := client.ListActiveClusters(ctx)
	if err != nil {
		t.Fatalf("list clusters: %v", err)
	}

	for i := range clusters {
		if clusters[i].WorkspaceID == nil || *clusters[i].WorkspaceID == "" {
			t.Errorf("cluster %s arrived without a resolved owning workspace",
				clusters[i].ID)
		}
	}
	if target.clusterID != "" {
		found := false
		for i := range clusters {
			if clusters[i].ID == target.clusterID {
				found = true
			}
		}
		if !found {
			t.Errorf("INTEGRATION_CLUSTER_ID %s is not in this credential's scope",
				target.clusterID)
		}
	}
}

// TestIntegration_ObservationsContinueThroughTheContract is the continuity check
// the runbook's cutover step depends on: an observation submitted through the
// authenticated path must be recorded, observably, through the scoped read.
//
// It asserts on `last_reconciled_at` — "a monitor cycle completed" — and asserts
// that `last_heartbeat` is *unchanged*. The earlier version of this test watched
// `last_heartbeat` advance, which a receiver can only satisfy by stamping the
// controller's liveness field from the monitor's own submission. That makes the
// staleness detection self-satisfying: the check would pass on every deploy while
// proving nothing about whether the controller is still reporting.
func TestIntegration_ObservationsContinueThroughTheContract(t *testing.T) {
	target := target(t)
	if target.clusterID == "" {
		t.Skip("INTEGRATION_CLUSTER_ID not set — skipping continuity check")
	}
	ctx := context.Background()
	client := newIntegrationClient(t, target, "integration-continuity")

	before, err := client.ListActiveClusters(ctx)
	if err != nil {
		t.Fatalf("list clusters: %v", err)
	}
	var previous *time.Time
	var heartbeatBefore *time.Time
	for i := range before {
		if before[i].ID == target.clusterID {
			previous = before[i].LastReconciledAt
			heartbeatBefore = before[i].LastHeartbeat
		}
	}

	details, err := json.Marshal(map[string]any{
		"overall_status": "Healthy",
		"checked_at":     time.Now().UTC().Format(time.RFC3339Nano),
		"dimensions": []map[string]string{
			{"name": "heartbeat_freshness", "status": "Healthy", "message": "observed"},
		},
	})
	if err != nil {
		t.Fatalf("marshal details: %v", err)
	}
	if err := client.UpdateClusterHealth(ctx, target.clusterID, "Healthy", details); err != nil {
		t.Fatalf("submit observation: %v", err)
	}

	after, err := client.ListActiveClusters(ctx)
	if err != nil {
		t.Fatalf("re-list clusters: %v", err)
	}
	for i := range after {
		if after[i].ID != target.clusterID {
			continue
		}
		if after[i].LastReconciledAt == nil {
			t.Fatal("no monitor cycle recorded after an accepted observation")
		}
		if previous != nil && !after[i].LastReconciledAt.After(*previous) {
			t.Errorf("recorded monitor cycle did not advance: was %s, now %s",
				previous, after[i].LastReconciledAt)
		}
		// The controller's liveness field must be exactly as the monitor found it.
		// A receiver that moved it would make the heartbeat dimension unable to
		// ever report a silent controller.
		if !sameInstant(heartbeatBefore, after[i].LastHeartbeat) {
			t.Errorf("submitting an observation moved last_heartbeat (%s -> %s); "+
				"the monitor cannot vouch for the controller's liveness",
				heartbeatBefore, after[i].LastHeartbeat)
		}
	}
}

// sameInstant reports whether two optional timestamps denote the same moment,
// treating both-unset as equal. Compared by instant rather than by ==, since a
// round trip through JSON can change the location without changing the time.
func sameInstant(a, b *time.Time) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return a.Equal(*b)
}

// TestIntegration_LeasingSerializesTwoInstances replaces the `reconcile_locks`
// test. Two clients with distinct instance IDs stand in for the two replicas the
// deployment runs.
func TestIntegration_LeasingSerializesTwoInstances(t *testing.T) {
	target := target(t)
	if target.clusterID == "" {
		t.Skip("INTEGRATION_CLUSTER_ID not set — skipping lease check")
	}
	ctx := context.Background()
	first := newIntegrationClient(t, target, "integration-lease-a")
	second := newIntegrationClient(t, target, "integration-lease-b")

	acquired, err := first.AcquireLock(ctx, "cluster_health", target.clusterID, "a", time.Minute)
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}
	if !acquired {
		t.Skip("scope already held by a running monitor — cannot test contention here")
	}
	defer func() {
		if err := first.ReleaseLock(ctx, "cluster_health", target.clusterID, "a"); err != nil {
			t.Errorf("release: %v", err)
		}
	}()

	contended, err := second.AcquireLock(ctx, "cluster_health", target.clusterID, "b", time.Minute)
	if err != nil {
		t.Fatalf("contended acquire returned an error rather than false: %v", err)
	}
	if contended {
		t.Error("two instances hold the same scope; the lease is not serializing")
	}
}

// TestIntegration_ForeignSubjectSubmissionIsRefused covers acceptance 3 against a
// real receiver.
//
// Skipped unless a cluster outside this credential's grant is named, because
// inventing one would test a 404 for a nonexistent cluster instead of a refusal
// for a foreign one — and those must be indistinguishable anyway.
func TestIntegration_ForeignSubjectSubmissionIsRefused(t *testing.T) {
	target := target(t)
	foreign := os.Getenv("INTEGRATION_FOREIGN_CLUSTER_ID")
	if foreign == "" {
		t.Skip("INTEGRATION_FOREIGN_CLUSTER_ID not set — skipping cross-workspace check")
	}
	ctx := context.Background()
	client := newIntegrationClient(t, target, "integration-foreign")

	_, err := client.SubmitObservation(
		ctx, foreign, "any-workspace",
		[]db.Check{{
			Name:       "heartbeat_freshness",
			Status:     "Healthy",
			ObservedAt: time.Now().UTC(),
			Detail:     "forged",
		}},
		time.Now().UTC(),
	)

	if err == nil {
		t.Error("a submission for a cluster outside this credential's scope was accepted")
	}
}
