package collection

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

var ErrGenerationFenced = errors.New("collection_generation_fenced")
var ErrSourceUnavailable = errors.New("collection_source_unavailable")

type GenerationReader interface {
	CurrentGeneration(context.Context, string) (int64, string, error)
}

func currentSource(ctx context.Context, source Source, authority Authority) error {
	if authority == nil {
		return ErrUnauthorized
	}
	current, err := authority.CurrentSource(ctx, source)
	if err != nil || current.Source != source || !current.BindingActive || !current.CellActive ||
		!current.GrantActive || current.GrantPurpose != "collection" ||
		!current.GrantExpires.After(time.Now()) {
		return ErrUnauthorized
	}
	return nil
}

func checkFence(ctx context.Context, source Source, generation int64, authority Authority, reader GenerationReader) error {
	if err := CheckFlags(); err != nil {
		return err
	}
	if source.CellID == "" || generation < 1 || reader == nil {
		return ErrGenerationFenced
	}
	if err := currentSource(ctx, source, authority); err != nil {
		return err
	}
	current, state, err := reader.CurrentGeneration(ctx, source.CellID)
	if err != nil || current != generation || state != "collecting" {
		return ErrGenerationFenced
	}
	return nil
}

func FetchFencedPage(ctx context.Context, source Source, generation int64, authority Authority, reader GenerationReader,
	provider func(context.Context) (NativePage, error)) (NativePage, error) {
	if err := checkFence(ctx, source, generation, authority, reader); err != nil {
		return NativePage{}, err
	}
	if provider == nil {
		return NativePage{}, ErrSourceUnavailable
	}
	page, err := provider(ctx)
	if err != nil {
		return NativePage{}, ErrSourceUnavailable
	}
	if err := checkFence(ctx, source, generation, authority, reader); err != nil {
		return NativePage{}, err
	}
	return page, nil
}

func WriteFencedWorkspace(ctx context.Context, db *sql.DB, source Source, generation int64, authority Authority,
	write func(*sql.Tx) error) error {
	return WriteFencedWorkspaceSources(ctx, db, []Source{source}, generation, authority, write)
}

func WriteFencedWorkspaceSources(ctx context.Context, db *sql.DB, sources []Source, generation int64, authority Authority,
	write func(*sql.Tx) error) error {
	if err := CheckFlags(); err != nil {
		return err
	}
	if db == nil || write == nil || len(sources) == 0 || len(sources) > 9 || sources[0].CellID == "" || generation < 1 {
		return ErrGenerationFenced
	}
	tools := make(map[string]bool, len(sources))
	for _, source := range sources {
		if source.CellID != sources[0].CellID || source.TenantID != sources[0].TenantID ||
			source.ProjectID != sources[0].ProjectID || source.Consumer == "" ||
			source.Consumer != sources[0].Consumer || source.ConnectionID == "" || source.Scope == "" ||
			!supportedEvidenceTool(source.Tool) || tools[source.Tool] {
			return ErrGenerationFenced
		}
		tools[source.Tool] = true
		if err := currentSource(ctx, source, authority); err != nil {
			return err
		}
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return ErrGenerationFenced
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, "SELECT devlake_meta.lock_generation($1, $2)", sources[0].CellID, generation); err != nil {
		return ErrGenerationFenced
	}
	if err := write(tx); err != nil {
		return ErrGenerationFenced
	}
	if err := CheckFlags(); err != nil {
		return err
	}
	for _, source := range sources {
		if err := currentSource(ctx, source, authority); err != nil {
			return err
		}
	}
	if err := tx.Commit(); err != nil {
		return ErrGenerationFenced
	}
	return nil
}
