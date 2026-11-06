package collection

import (
	"context"
	"database/sql"
	"encoding/json"
)

const (
	MaxExportPageItems = 100
	MaxExportPageBytes = 1 << 20
)

type ExportItem struct {
	ItemSHA256    string       `json:"item_sha256"`
	Observation   *Observation `json:"observation,omitempty"`
	RemovalReason string       `json:"removal_reason,omitempty"`
}

type ExportPage struct {
	BatchID        string       `json:"batch_id"`
	ManifestSHA256 string       `json:"manifest_sha256"`
	SchemaVersion  string       `json:"schema_version"`
	MappingVersion string       `json:"mapping_version"`
	Items          []ExportItem `json:"items"`
	NextCursor     string       `json:"next_cursor,omitempty"`
	Complete       bool         `json:"complete"`
}

func loadSealedPage(ctx context.Context, reader *sql.DB, cellID, batchID string,
	sources []Source, after string, maxItems, maxBytes int) (ExportPage, error) {
	if maxItems < 1 || maxItems > MaxExportPageItems || maxBytes < 512 || maxBytes > MaxExportPageBytes {
		return ExportPage{}, ErrBatch
	}
	batch, err := LoadSealedBatch(ctx, reader, cellID, batchID)
	if err != nil {
		return ExportPage{}, err
	}
	mapped, err := MapPreparedObservations(batch, sources)
	if err != nil {
		return ExportPage{}, err
	}
	byDigest := make(map[string]Observation, len(mapped))
	for _, observation := range mapped {
		byDigest[observation.SourceSnapshot.ItemSHA256] = observation
	}
	start := 0
	if after != "" {
		start = -1
		for index, digest := range batch.ItemDigests {
			if digest.SHA256 == after {
				start = index + 1
				break
			}
		}
		if start < 0 {
			return ExportPage{}, ErrBatch
		}
	}
	page := ExportPage{BatchID: batchID, ManifestSHA256: batch.ContentSHA256,
		SchemaVersion: ObservationSchemaVersion, MappingVersion: ObservationSchemaVersion,
		Items: make([]ExportItem, 0, maxItems)}
	if len(batch.Items) == 0 {
		page.Complete = true
		encoded, err := json.Marshal(page)
		if err != nil || len(encoded) > maxBytes {
			return ExportPage{}, ErrBatch
		}
		return page, nil
	}
	for index := start; index < len(batch.Items) && len(page.Items) < maxItems; index++ {
		item := ExportItem{ItemSHA256: batch.ItemDigests[index].SHA256}
		if batch.Items[index].RemovalReason != "" {
			item.RemovalReason = batch.Items[index].RemovalReason
		} else {
			observation, exists := byDigest[item.ItemSHA256]
			if !exists {
				return ExportPage{}, ErrBatch
			}
			item.Observation = &observation
		}
		page.Items = append(page.Items, item)
		page.NextCursor = item.ItemSHA256
		page.Complete = index == len(batch.Items)-1
		if page.Complete {
			page.NextCursor = ""
		}
		encoded, err := json.Marshal(page)
		if err != nil || len(encoded) > maxBytes {
			page.Items = page.Items[:len(page.Items)-1]
			if len(page.Items) == 0 {
				return ExportPage{}, ErrBatch
			}
			page.NextCursor = page.Items[len(page.Items)-1].ItemSHA256
			page.Complete = false
			break
		}
	}
	if len(page.Items) == 0 {
		return ExportPage{}, ErrBatch
	}
	return page, nil
}
