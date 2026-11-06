package collection

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPinnedManifestDigestMatchesWorkerImage(t *testing.T) {
	const schema = `{"schema_version":2,"staging":{"status":"ready"}}`
	digest := sha256.Sum256([]byte(schema))
	encoded := hex.EncodeToString(digest[:])
	for _, scenario := range []struct {
		name      string
		schema    string
		build     string
		wantError bool
	}{
		{"pinned", schema, `{"component":"worker","schema_sha256":"` + encoded + `"}`, false},
		{"tampered schema", schema + " ", `{"component":"worker","schema_sha256":"` + encoded + `"}`, true},
		{"wrong component", schema, `{"component":"api","schema_sha256":"` + encoded + `"}`, true},
		{"unmigrated schema", strings.Replace(schema, `"ready"`, `"pending"`, 1), `{"component":"worker","schema_sha256":"` + encoded + `"}`, true},
		{"missing build", schema, "", true},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			directory := t.TempDir()
			schemaPath := filepath.Join(directory, "schema-manifest.json")
			buildPath := filepath.Join(directory, "source-manifest.json")
			if err := os.WriteFile(schemaPath, []byte(scenario.schema), 0600); err != nil {
				t.Fatal(err)
			}
			if scenario.build != "" {
				if err := os.WriteFile(buildPath, []byte(scenario.build), 0600); err != nil {
					t.Fatal(err)
				}
			}
			actual, err := PinnedManifestDigest(schemaPath, buildPath)
			if scenario.wantError {
				if !errors.Is(err, ErrMigrationRequired) || actual != "" {
					t.Fatalf("digest %q, error %v", actual, err)
				}
			} else if err != nil || actual != encoded {
				t.Fatalf("digest %q, error %v", actual, err)
			}
		})
	}
}
