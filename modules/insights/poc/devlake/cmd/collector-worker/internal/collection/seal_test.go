package collection

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
	"time"
)

type sealState struct {
	items       [][]byte
	manifest    []byte
	checkpoints []byte
	commits     int
	rollbacks   int
	wrongDigest bool
	stored      map[string]storedSeal
	pending     *storedSeal
}

type storedSeal struct {
	batchID     string
	items       [][]byte
	manifest    []byte
	checkpoints []byte
	digest      []byte
}

type sealConnector struct{ state *sealState }
type sealConnection struct{ state *sealState }
type sealTransaction struct{ state *sealState }
type sealDigestRows struct {
	value []byte
	read  bool
}

func (connector sealConnector) Connect(context.Context) (driver.Conn, error) {
	return sealConnection{connector.state}, nil
}
func (connector sealConnector) Driver() driver.Driver { return connector }
func (connector sealConnector) Open(string) (driver.Conn, error) {
	return sealConnection{connector.state}, nil
}
func (connection sealConnection) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("unavailable")
}
func (connection sealConnection) Close() error { return nil }
func (connection sealConnection) Begin() (driver.Tx, error) {
	return sealTransaction{connection.state}, nil
}
func (connection sealConnection) BeginTx(context.Context, driver.TxOptions) (driver.Tx, error) {
	return sealTransaction{connection.state}, nil
}
func (connection sealConnection) CheckNamedValue(*driver.NamedValue) error { return nil }
func (connection sealConnection) QueryContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Rows, error) {
	if query == "SELECT manifest_payload, content_digest FROM devlake_meta.collection_batch_manifests WHERE cell_id = $1 AND batch_id = $2" {
		if len(arguments) != 2 || arguments[0].Value != "cell" {
			return nil, errors.New("wrong readback cell")
		}
		stored, exists := connection.state.stored[arguments[1].Value.(string)]
		if !exists {
			return &sealedBytesRows{}, nil
		}
		return &sealedBytesRows{entries: [][]byte{stored.manifest, stored.digest}}, nil
	}
	if strings.Contains(query, "FROM devlake_meta.collection_batch_items AS item") {
		if len(arguments) != 2 || arguments[0].Value != "cell" {
			return nil, errors.New("wrong item readback cell")
		}
		stored, exists := connection.state.stored[arguments[1].Value.(string)]
		if !exists {
			return &sealedBytesRows{}, nil
		}
		entries := make([][]byte, 0, len(stored.items)*2)
		for _, item := range stored.items {
			digest := sha256.Sum256(item)
			entries = append(entries, item, digest[:])
		}
		return &sealedBytesRows{entries: entries}, nil
	}
	if query != "SELECT devlake_meta.seal_batch($1, $2, $3, $4::bytea[], $5::bytea, $6::jsonb)" ||
		len(arguments) != 6 || arguments[0].Value != "cell" {
		return nil, errors.New("unexpected seal request")
	}
	items, ok := arguments[3].Value.([][]byte)
	if !ok || items == nil || len(items) > 100000 {
		return nil, errors.New("batch item array missing")
	}
	manifest, ok := arguments[4].Value.([]byte)
	if !ok || len(manifest) == 0 {
		return nil, errors.New("manifest bytes missing")
	}
	checkpoints, ok := arguments[5].Value.([]byte)
	if !ok || !json.Valid(checkpoints) {
		return nil, errors.New("checkpoint bytes missing")
	}
	batchID, valid := arguments[2].Value.(string)
	if !valid || (arguments[1].Value != int64(1) && arguments[1].Value != int64(2)) {
		return nil, errors.New("invalid batch identity")
	}
	connection.state.items = items
	connection.state.manifest = manifest
	connection.state.checkpoints = checkpoints
	digest := sha256.Sum256(manifest)
	if prior, exists := connection.state.stored[batchID]; exists {
		if !bytes.Equal(prior.manifest, manifest) || !bytes.Equal(prior.checkpoints, checkpoints) || len(prior.items) != len(items) {
			return nil, errors.New("sealed batch conflict")
		}
		for index, item := range items {
			if !bytes.Equal(prior.items[index], item) {
				return nil, errors.New("sealed item conflict")
			}
		}
	} else {
		copied := storedSeal{batchID: batchID, manifest: bytes.Clone(manifest), checkpoints: bytes.Clone(checkpoints), digest: bytes.Clone(digest[:])}
		for _, item := range items {
			copied.items = append(copied.items, bytes.Clone(item))
		}
		connection.state.pending = &copied
	}
	if connection.state.wrongDigest {
		digest[0] ^= 0xff
	}
	return &sealDigestRows{value: digest[:]}, nil
}
func (transaction sealTransaction) Commit() error {
	if transaction.state.pending != nil {
		if transaction.state.stored == nil {
			transaction.state.stored = make(map[string]storedSeal)
		}
		transaction.state.stored[transaction.state.pending.batchID] = *transaction.state.pending
		transaction.state.pending = nil
	}
	transaction.state.commits++
	return nil
}
func (transaction sealTransaction) Rollback() error {
	transaction.state.pending = nil
	transaction.state.rollbacks++
	return nil
}

type sealedBytesRows struct {
	entries  [][]byte
	position int
}

func (rows *sealedBytesRows) Columns() []string { return []string{"payload", "digest"} }
func (rows *sealedBytesRows) Close() error      { return nil }
func (rows *sealedBytesRows) Next(values []driver.Value) error {
	if rows.position >= len(rows.entries) {
		return io.EOF
	}
	values[0] = rows.entries[rows.position]
	values[1] = rows.entries[rows.position+1]
	rows.position += 2
	return nil
}

func (rows *sealDigestRows) Columns() []string { return []string{"digest"} }
func (rows *sealDigestRows) Close() error      { return nil }
func (rows *sealDigestRows) Next(values []driver.Value) error {
	if rows.read {
		return io.EOF
	}
	rows.read = true
	values[0] = rows.value
	return nil
}

func TestSealPreparedBatchUsesExactBytesAndTwoSourceAuthority(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []struct {
		name        string
		wrongDigest bool
		revokeAt    int
		wrongSource bool
		wrongCursor bool
		tamperBytes bool
		want        error
		commits     int
	}{
		{"sealed", false, 0, false, false, false, nil, 1},
		{"wrong digest", true, 0, false, false, false, ErrBatch, 0},
		{"second source revoked", false, 4, false, false, false, ErrUnauthorized, 0},
		{"foreign source", false, 0, true, false, false, ErrBatch, 0},
		{"wrong checkpoint", false, 0, false, true, false, ErrBatch, 0},
		{"mutated prepared bytes", false, 0, false, false, true, ErrBatch, 0},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			batchID, cellID, items := batchFixture()
			batch, err := PrepareBatch(batchID, cellID, 1, items)
			if err != nil {
				t.Fatal(err)
			}
			sources := fixtureJob(time.Now()).Sources
			sources[0].ConnectionID, sources[0].Scope = "github-connection", "repo"
			sources[1].ConnectionID, sources[1].Scope = "jira-connection", "board"
			updates := []CheckpointUpdate{
				{SourceKey: SourceCheckpointKey(sources[0]), NextCursor: "github-complete"},
				{SourceKey: SourceCheckpointKey(sources[1]), NextCursor: "jira-complete"},
			}
			if scenario.wrongSource {
				sources[1].CellID = "foreign"
			}
			if scenario.wrongCursor {
				updates[1].SourceKey = "fake"
			}
			if scenario.tamperBytes {
				batch.ItemPayloads[0][0] ^= 0xff
			}
			state := &sealState{wrongDigest: scenario.wrongDigest}
			pool := sql.OpenDB(sealConnector{state})
			defer pool.Close()
			result, err := SealPreparedBatch(context.Background(), pool, sources,
				&expiringAuthority{revokeAt: scenario.revokeAt}, batch, updates)
			if !errors.Is(err, scenario.want) && (scenario.want != nil || err != nil) {
				t.Fatalf("seal error %v, expected %v", err, scenario.want)
			}
			if state.commits != scenario.commits {
				t.Fatalf("commits %d", state.commits)
			}
			if scenario.want == nil {
				if result != batch.ContentSHA256 || !bytes.Equal(state.manifest, batch.ManifestPayload) ||
					!bytes.Equal(state.items[0], batch.ItemPayloads[0]) || !bytes.Equal(state.items[1], batch.ItemPayloads[1]) {
					t.Fatal("sealed bytes differ from prepared bytes")
				}
			} else if result != "" {
				t.Fatalf("failed seal exported digest %s", result)
			}
			if scenario.revokeAt != 0 && state.rollbacks != 1 {
				t.Fatal("revoked seal did not roll back")
			}
		})
	}
}

func TestSealPreparedBatchAuthenticatesBindingSnapshot(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, database := exportFixture(t)
	for index := range sources {
		sources[index].GrantID, sources[index].Consumer = "grant", "worker"
	}
	updates := []CheckpointUpdate{{SourceKey: SourceCheckpointKey(sources[0]), NextCursor: "first"},
		{SourceKey: SourceCheckpointKey(sources[1]), NextCursor: "first"}}
	for _, edit := range []func(*Source){
		func(source *Source) { source.BindingID = "replacement-binding" },
		func(source *Source) { source.BindingGeneration++ },
		func(source *Source) { source.BindingRevision++ },
	} {
		changed := append([]Source(nil), sources...)
		edit(&changed[0])
		if _, err := SealPreparedBatch(context.Background(), database, changed, &fixtureAuthority{}, batch, updates); !errors.Is(err, ErrBatch) || state.commits != 0 {
			t.Fatalf("binding snapshot sealed under a different current binding: %v", err)
		}
	}
	if _, err := SealPreparedBatch(context.Background(), database, sources, &fixtureAuthority{}, batch, updates); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadSealedBatch(context.Background(), database, batch.CellID, batch.BatchID)
	if err != nil || !bytes.Equal(loaded.ManifestPayload, batch.ManifestPayload) ||
		loaded.SourceBindings[SourceCheckpointKey(sources[0])] != (ObservationBinding{"github-binding", 3, 2}) {
		t.Fatalf("sealed binding snapshot lost on reload: %v", err)
	}
	stored := state.stored[batch.BatchID]
	stored.manifest = bytes.Replace(stored.manifest, []byte("github-binding"), []byte("forged-binding"), 1)
	state.stored[batch.BatchID] = stored
	if _, err := LoadSealedBatch(context.Background(), database, batch.CellID, batch.BatchID); !errors.Is(err, ErrBatch) {
		t.Fatalf("tampered stored binding snapshot loaded: %v", err)
	}
}

func TestSealReadbackReplayAndEdits(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batchID, cellID, items := batchFixture()
	first, err := PrepareBatch(batchID, cellID, 1, items)
	if err != nil {
		t.Fatal(err)
	}
	sources := fixtureJob(time.Now()).Sources
	sources[0].ConnectionID, sources[0].Scope = "github-connection", "repo"
	sources[1].ConnectionID, sources[1].Scope = "jira-connection", "board"
	updates := []CheckpointUpdate{{SourceKey: SourceCheckpointKey(sources[0]), NextCursor: "github-complete"},
		{SourceKey: SourceCheckpointKey(sources[1]), NextCursor: "jira-complete"}}
	state := &sealState{}
	pool := sql.OpenDB(sealConnector{state})
	defer pool.Close()
	seal := func(batch PreparedBatch, checkpoints []CheckpointUpdate) error {
		_, sealErr := SealPreparedBatch(context.Background(), pool, sources, &fixtureAuthority{}, batch, checkpoints)
		return sealErr
	}
	if err := ReadSealedBatch(context.Background(), pool, first); !errors.Is(err, ErrBatch) {
		t.Fatalf("unsealed B1 was readable: %v", err)
	}
	state.wrongDigest = true
	if err := seal(first, updates); !errors.Is(err, ErrBatch) || len(state.stored) != 0 {
		t.Fatalf("failed seal persisted B1: %v", err)
	}
	state.wrongDigest = false
	if err := seal(first, updates); err != nil {
		t.Fatalf("B1: %v", err)
	}
	if err := seal(first, updates); err != nil {
		t.Fatalf("identical replay: %v", err)
	}
	if len(state.stored) != 1 {
		t.Fatal("replay created another batch")
	}
	items[0].NativeBytes = []byte(`{"body":"edited"}`)
	items[0].Links[0].NativeEvidenceID = "new-native-evidence"
	items[0].PolicyVersion = "policy-v2"
	conflict, err := PrepareBatch(batchID, cellID, 1, items)
	if err != nil {
		t.Fatal(err)
	}
	if err := seal(conflict, updates); !errors.Is(err, ErrBatch) {
		t.Fatalf("conflicting payload replay: %v", err)
	}
	changed := append([]CheckpointUpdate(nil), updates...)
	changed[0].NextCursor = "conflicting-cursor"
	if err := seal(first, changed); !errors.Is(err, ErrBatch) {
		t.Fatalf("conflicting cursor replay: %v", err)
	}
	if len(state.stored) != 1 || ReadSealedBatch(context.Background(), pool, first) != nil {
		t.Fatal("rejected replay changed B1")
	}
	second, err := PrepareBatch("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff", cellID, 2, items)
	if err != nil {
		t.Fatal(err)
	}
	if err := seal(second, updates); err != nil {
		t.Fatalf("B2: %v", err)
	}
	if len(state.stored) != 2 || first.ContentSHA256 == second.ContentSHA256 {
		t.Fatal("B2 replaced B1")
	}
	for _, batch := range []PreparedBatch{first, second} {
		if err := ReadSealedBatch(context.Background(), pool, batch); err != nil {
			t.Fatalf("readback %s: %v", batch.BatchID, err)
		}
	}
	stored := state.stored[batchID]
	stored.items = stored.items[:1]
	state.stored[batchID] = stored
	if err := ReadSealedBatch(context.Background(), pool, first); !errors.Is(err, ErrBatch) {
		t.Fatalf("missing item: %v", err)
	}
	stored.items = append(stored.items, bytes.Clone(first.ItemPayloads[1]))
	stored.items[0][0] ^= 0xff
	state.stored[batchID] = stored
	if err := ReadSealedBatch(context.Background(), pool, first); !errors.Is(err, ErrBatch) {
		t.Fatalf("tampered item: %v", err)
	}
	stored.items[0] = bytes.Clone(first.ItemPayloads[0])
	stored.manifest[0] ^= 0xff
	state.stored[batchID] = stored
	if err := ReadSealedBatch(context.Background(), pool, first); !errors.Is(err, ErrBatch) {
		t.Fatalf("tampered manifest: %v", err)
	}
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
	if err := ReadSealedBatch(context.Background(), pool, second); err == nil {
		t.Fatal("disabled collection permitted readback")
	}
}
