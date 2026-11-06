package collection

import "context"

type NativeProvider interface {
	FetchPage(context.Context, Source, string, int) (NativePage, error)
}

func CollectVerifiedPages(ctx context.Context, job Job, generation int64, authority Authority,
	reader GenerationReader, provider NativeProvider, classifier Classifier,
	expected map[Source][]NativeKey) ([]Reconciled, error) {
	if err := CheckFlags(); err != nil {
		return nil, err
	}
	if len(job.Sources) != 2 || job.PageSize < 1 || job.PageSize > 100 ||
		job.PageLimit < 1 || job.PageLimit > 5 || provider == nil || classifier == nil || len(expected) != 2 {
		return nil, ErrIncomplete
	}
	results := make([]Reconciled, 0, len(job.Sources))
	for _, source := range job.Sources {
		if expected[source] == nil {
			return nil, ErrIncomplete
		}
		cursor := ""
		pages := make([]NativePage, 0, job.PageLimit)
		for range job.PageLimit {
			page, err := FetchFencedPage(ctx, source, generation, authority, reader, func(ctx context.Context) (NativePage, error) {
				return provider.FetchPage(ctx, source, cursor, job.PageSize)
			})
			if err != nil {
				return nil, err
			}
			pages = append(pages, page)
			if page.Complete {
				break
			}
			cursor = page.NextCursor
		}
		result, err := Reconcile(ctx, source, source.ConnectionID, pages, expected[source], classifier)
		if err != nil {
			return nil, err
		}
		results = append(results, result)
	}
	for _, source := range job.Sources {
		if err := checkFence(ctx, source, generation, authority, reader); err != nil {
			return nil, err
		}
	}
	return results, nil
}
