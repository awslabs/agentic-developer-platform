package collection

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"
)

var ErrDisabled = errors.New("feature_disabled")
var ErrUnauthorized = errors.New("collection_not_authorized")
var ErrMigrationRequired = errors.New("migration_required")

type Source struct {
	Tool              string
	TenantID          string
	ProjectID         string
	CellID            string
	ConnectionID      string
	BindingID         string
	BindingGeneration int64
	BindingRevision   int64
	GrantID           string
	Consumer          string
	Scope             string
}

type Job struct {
	Sources   []Source
	Cutoff    time.Time
	PageSize  int
	PageLimit int
}

type CurrentSource struct {
	Source
	BindingActive bool
	CellActive    bool
	GrantActive   bool
	GrantPurpose  string
	GrantExpires  time.Time
}

type Authority interface {
	CurrentSource(context.Context, Source) (CurrentSource, error)
}

type Migration interface {
	CheckReceipt(context.Context, string, string) error
}

func CheckFlags() error {
	if !strings.EqualFold(os.Getenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED"), "true") ||
		!strings.EqualFold(os.Getenv("DEVLAKE_COLLECTION_ENABLED"), "true") {
		return ErrDisabled
	}
	return nil
}

func Admit(ctx context.Context, job Job, authority Authority, migration Migration, manifestDigest string, now time.Time) error {
	if err := CheckFlags(); err != nil {
		return err
	}
	if authority == nil || migration == nil || len(manifestDigest) != 64 ||
		!validEvidenceSources(job.Sources) || job.PageSize < 1 || job.PageSize > 100 ||
		job.PageLimit < 1 || job.PageLimit > 5 || job.Cutoff.IsZero() ||
		job.Cutoff.After(now) || job.Cutoff.Before(now.Add(-30*24*time.Hour)) {
		return ErrUnauthorized
	}
	if _, err := hex.DecodeString(manifestDigest); err != nil {
		return ErrUnauthorized
	}
	tools := make(map[string]bool, len(job.Sources))
	cell := job.Sources[0].CellID
	for _, source := range job.Sources {
		if !supportedEvidenceTool(source.Tool) || tools[source.Tool] ||
			source.TenantID == "" || source.ProjectID == "" || source.CellID == "" ||
			source.CellID != cell || source.TenantID != job.Sources[0].TenantID ||
			source.ProjectID != job.Sources[0].ProjectID || source.Consumer != job.Sources[0].Consumer ||
			source.ConnectionID == "" || source.BindingID == "" || source.GrantID == "" ||
			source.Consumer == "" || source.Scope == "" ||
			source.BindingGeneration < 1 || source.BindingRevision < 1 {
			return ErrUnauthorized
		}
		tools[source.Tool] = true
	}
	for _, source := range job.Sources {
		current, err := authority.CurrentSource(ctx, source)
		if err != nil {
			return fmt.Errorf("%w: current authority unavailable", ErrUnauthorized)
		}
		if current.Source != source || !current.BindingActive || !current.CellActive ||
			!current.GrantActive || current.GrantPurpose != "collection" ||
			!current.GrantExpires.After(now) {
			return ErrUnauthorized
		}
	}
	if err := migration.CheckReceipt(ctx, cell, manifestDigest); err != nil {
		return fmt.Errorf("%w: cell schema receipt unavailable", ErrMigrationRequired)
	}
	return nil
}
