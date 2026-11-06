package collection

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
)

func PinnedManifestDigest(schemaPath, buildPath string) (string, error) {
	schema, err := os.ReadFile(schemaPath)
	if err != nil || len(schema) == 0 || len(schema) > 1024*1024 {
		return "", ErrMigrationRequired
	}
	build, err := os.ReadFile(buildPath)
	if err != nil || len(build) == 0 || len(build) > 1024*1024 {
		return "", ErrMigrationRequired
	}
	var schemaInventory struct {
		SchemaVersion int `json:"schema_version"`
		Staging       struct {
			Status string `json:"status"`
		} `json:"staging"`
	}
	var sourceManifest struct {
		Component    string `json:"component"`
		SchemaSHA256 string `json:"schema_sha256"`
	}
	if json.Unmarshal(schema, &schemaInventory) != nil || schemaInventory.SchemaVersion != 2 ||
		schemaInventory.Staging.Status != "ready" || json.Unmarshal(build, &sourceManifest) != nil ||
		sourceManifest.Component != "worker" {
		return "", ErrMigrationRequired
	}
	digest := sha256.Sum256(schema)
	encoded := hex.EncodeToString(digest[:])
	if sourceManifest.SchemaSHA256 != encoded {
		return "", ErrMigrationRequired
	}
	return encoded, nil
}
