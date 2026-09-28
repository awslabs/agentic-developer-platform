package controllers

// The other half of A19 (#5684).
//
// `tests/test_controller_rbac_scope.py` asserts the shipped ClusterRole does not grant
// more than this controller needs. That direction alone is not enough: it cannot tell
// whether the code still matches the grants. If someone later adds a call that needs a
// permission A19 removed, the manifest suite keeps passing — the role is still narrow,
// it is now narrow in the wrong shape — and the mismatch surfaces as a 403 against a
// live API server, in the reconcile loop that made the call.
//
// So these tests watch the SOURCE for the operations whose grants were removed or
// deliberately not taken. They are the reason the removals are safe to keep: each one
// fails in CI the moment its premise stops holding, which is what `RETIREMENT.md` and
// `test_controller_runtime_surface.py` already do for the no-subprocess premise behind
// the image shrink.
//
// They read the package's own .go files rather than using a fake client, because the
// property is "this call does not appear anywhere", and no test with a fake client can
// assert the absence of a call it does not make.

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// sourceFiles returns this package's non-test Go sources, comments stripped.
//
// Comments are stripped because the files DOCUMENT the removed operations at length —
// heartbeat.go explains the retired Secret list, and the manifest reasoning is echoed
// here. A raw substring scan would fail on the explanations it exists to protect, and
// the cheapest way to pass would be deleting them, leaving a tree that no longer records
// why the grants are absent.
func sourceFiles(t *testing.T) map[string]string {
	t.Helper()

	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read package dir: %v", err)
	}

	blockComment := regexp.MustCompile(`(?s)/\*.*?\*/`)
	lineComment := regexp.MustCompile(`(?m)//.*$`)

	sources := map[string]string{}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || filepath.Ext(name) != ".go" || strings.HasSuffix(name, "_test.go") {
			continue
		}
		raw, err := os.ReadFile(name)
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		text := blockComment.ReplaceAllString(string(raw), "")
		text = lineComment.ReplaceAllString(text, "")
		sources[name] = text
	}

	if len(sources) == 0 {
		t.Fatal("no non-test Go sources found — this suite would vacuously pass")
	}
	return sources
}

// TestNoSecretReadsRemainInTheControllers is the premise behind dropping the
// ClusterRole's cluster-wide `secrets: ["list"]` grant.
//
// A19 removed both the grant and checkVaultSyncStatus, the only caller. If a Secret read
// reappears, the role no longer authorises it and the call fails at runtime — for a
// heartbeat that would be a silent degradation, since the sender swallows collection
// errors into alerts rather than failing loudly.
//
// Reading Secrets is also not the right way to answer the question that function asked:
// real vault-sync state lives in ExternalSecret CR status conditions. If that check is
// rebuilt, grant `externalsecrets` on `external-secrets.io` and update this test — do not
// restore cluster-wide Secret access, which this controller has never needed.
func TestNoSecretReadsRemainInTheControllers(t *testing.T) {
	// corev1.SecretList / corev1.Secret are how a controller-runtime client reads them.
	patterns := []string{"corev1.SecretList", "corev1.Secret{", "&corev1.Secret"}

	for name, text := range sourceFiles(t) {
		for _, pattern := range patterns {
			if strings.Contains(text, pattern) {
				t.Errorf(
					"%s reads Kubernetes Secrets (%s), but A19 (#5684) removed the "+
						"ClusterRole's cluster-wide `secrets: [\"list\"]` grant, so this "+
						"call is not authorised and will fail at runtime.\n"+
						"The removed checkVaultSyncStatus returned the constant \"synced\" "+
						"on every branch including the failure branch, and the receiver "+
						"(cluster_health.go) could not interpret that value anyway.\n"+
						"A real credential-sync check reads ExternalSecret CR status "+
						"conditions: grant `externalsecrets` on `external-secrets.io` and "+
						"update this test and deploy/controller.yaml together.",
					name, pattern,
				)
			}
		}
	}
}

// TestVaultSyncStatusIsNotReintroduced pins the payload side of the same removal.
//
// Re-adding the field without a real check would reproduce the original defect exactly:
// a value the producer cannot substantiate and the consumer cannot interpret. The
// receiver's vocabulary is ok|pending|failed; "synced" was none of them, so
// cluster_health.go classified it Unknown. An ABSENT field is read as "not reported"
// (its `case ""` → NotChecked), which is the truthful state and, per the domain's health
// contract, never aggregates to healthy.
func TestVaultSyncStatusIsNotReintroduced(t *testing.T) {
	for name, text := range sourceFiles(t) {
		for _, pattern := range []string{"VaultSyncStatus", "vault_sync_status", "checkVaultSyncStatus"} {
			if strings.Contains(text, pattern) {
				t.Errorf(
					"%s reintroduces %s. A19 (#5684) removed vault-sync reporting together "+
						"with the cluster-wide Secret grant that fed it. Reporting a value "+
						"this controller cannot substantiate is the defect the domain's "+
						"health contract (contracts/superplane_contracts/health.py) exists "+
						"to make unrepresentable — it cites this function by name.\n"+
						"If a real check is added, send a value inside the receiver's "+
						"ok|pending|failed vocabulary and grant the ExternalSecret read it "+
						"needs, rather than restoring cluster-wide Secret access.",
					name, pattern,
				)
			}
		}
	}
}

// TestNoConfigMapAccessRemains is the premise behind dropping the ConfigMap grant.
//
// The manifest previously granted 8 verbs on configmaps "for leader election", but
// controller-runtime v0.20.1 defaults to resourcelock.LeasesResourceLock
// (pkg/leaderelection/leader_election.go) — the ConfigMap lock was the pre-v0.12 default
// and nothing in main.go selects it. The Deployment's own configMap mounts and
// configMapKeyRef env vars are resolved by the kubelet, not by this ServiceAccount, so
// they are unaffected by the removal.
//
// If a controller here ever reads a ConfigMap through the API, the grant has to come
// back with it.
func TestNoConfigMapAccessRemains(t *testing.T) {
	for name, text := range sourceFiles(t) {
		for _, pattern := range []string{"corev1.ConfigMapList", "corev1.ConfigMap{", "&corev1.ConfigMap"} {
			if strings.Contains(text, pattern) {
				t.Errorf(
					"%s accesses ConfigMaps (%s), but A19 (#5684) removed that grant from "+
						"deploy/controller.yaml because nothing in this tree used it and "+
						"leader election defaults to Leases, not the ConfigMap lock. "+
						"Re-add the grant in the same commit as the call.",
					name, pattern,
				)
			}
		}
	}
}

// TestNoSuperplaneNodeDeletionRemains is the premise behind dropping `delete` on
// superplanenodes — and it protects a safety property, not just a permission.
//
// RETIREMENT.md is explicit: a node whose teardown failed or could not be confirmed goes
// to ReleaseFailed and RETAINS status.skypilotCluster, because that cluster name is the
// only handle a later reconciliation or a human has on a possibly-live GPU cluster.
// Deleting the record would strand a billing resource with nothing pointing at it. So
// the absence of a delete call is a deliberate design decision, and the missing grant is
// a second lock on it.
//
// Deleting the Kubernetes *Node* object is different and still granted —
// consolidator.go does that after a CONFIRMED teardown.
func TestNoSuperplaneNodeDeletionRemains(t *testing.T) {
	// Match a Delete call whose argument mentions a SuperplaneNode value. Narrow on
	// purpose: `c.client.Delete(ctx, node)` in consolidator.go deletes a corev1.Node and
	// must keep passing.
	deleteCall := regexp.MustCompile(`Delete\(ctx,\s*&?(spNode|superplaneNode|current)\b`)

	for name, text := range sourceFiles(t) {
		if match := deleteCall.FindString(text); match != "" {
			t.Errorf(
				"%s appears to delete a SuperplaneNode (%q). A19 (#5684) removed `delete` "+
					"on superplanenodes, and RETIREMENT.md requires a failed or unconfirmed "+
					"teardown to RETAIN the record and its status.skypilotCluster — that "+
					"name is the only handle on a GPU cluster that may still be running and "+
					"billing. If deletion is genuinely intended, that is a scope change to "+
					"the retirement contract, not just an RBAC edit.",
				name, match,
			)
		}
	}
}

// TestEvictionUsesTheSubresourceClient guards the fix to the eviction apiGroup from the
// other side.
//
// The manifest suite asserts the grant sits under the core group, which is what RBAC
// matches for `pods/eviction` (the request path is /api/v1/.../pods/{name}/eviction, not
// /apis/policy/...). That grant is only the right one while drain actually goes through
// the eviction subresource. If someone "simplifies" drain into a plain pod Delete, the
// eviction grant becomes dead, a `pods: ["delete"]` grant becomes necessary, and — far
// worse — PodDisruptionBudgets stop being enforced, because only the Eviction API
// consults them. RETIREMENT.md promises drain respects PDBs.
//
// consolidator_test.go already pins the cordon-then-evict ordering; this asserts the
// mechanism the RBAC rule is written against.
func TestEvictionUsesTheSubresourceClient(t *testing.T) {
	sources := sourceFiles(t)

	found := false
	for _, text := range sources {
		if strings.Contains(text, `SubResource("eviction").Create`) {
			found = true
			break
		}
	}
	if !found {
		t.Error(
			"no SubResource(\"eviction\").Create call found in this package. Drain must go " +
				"through the Eviction API: it is what makes the API server enforce " +
				"PodDisruptionBudgets (RETIREMENT.md), and it is the operation " +
				"deploy/controller.yaml grants as `pods/eviction` under the core apiGroup. " +
				"A plain pod Delete would bypass PDBs and need a different grant.",
		)
	}

	// A plain pod delete would silently bypass PDBs, so it must not appear either.
	podDelete := regexp.MustCompile(`Delete\(ctx,\s*&?pod\b`)
	for name, text := range sources {
		if match := podDelete.FindString(text); match != "" {
			t.Errorf(
				"%s deletes a Pod directly (%q). That bypasses PodDisruptionBudgets, which "+
					"only the Eviction API consults, and is not authorised — the ClusterRole "+
					"grants pods/eviction:create, not pods:delete.",
				name, match,
			)
		}
	}
}
