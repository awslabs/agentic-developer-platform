package collection

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"testing"

	"github.com/apache/devlake/core/models/common"
	"github.com/apache/devlake/core/models/domainlayer"
	"github.com/apache/devlake/core/models/domainlayer/devops"
	slackmodels "github.com/apache/devlake/plugins/slack/models"
)

func expandedSource(tool, connection string) Source {
	return Source{Tool: tool, TenantID: "tenant", ProjectID: "project", CellID: "cell",
		ConnectionID: connection, Scope: "selected", BindingID: tool + "-binding",
		BindingGeneration: 3, BindingRevision: 4}
}

func pipelineItem(t *testing.T, source Source, id, originalStatus, originalResult string) BatchItem {
	t.Helper()
	row := &devops.CICDPipeline{DomainEntity: domainlayer.DomainEntity{Id: id},
		OriginalStatus: originalStatus, OriginalResult: originalResult,
		Status: "DONE", Result: "FAILURE"}
	row.RawDataOrigin = common.RawDataOrigin{
		RawDataTable:  "_raw_" + source.Tool + "_api_builds",
		RawDataParams: `{"connectionId":1,"scopeId":"selected"}`, RawDataId: 21,
	}
	item, err := NativeDomainBatchItem(source, 1, "record-r2", row,
		json.RawMessage(`{"status":"`+originalStatus+`","result":"`+originalResult+`"}`),
		"policy-7", "devlake-pinned", "schema-v2")
	if err != nil {
		t.Fatal(err)
	}
	return item
}

func TestNativeRowsSealAndExportExactOriginalValuesAndRawOrigin(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	sources := []Source{expandedSource("jenkins", "opaque-jenkins"), expandedSource("gitlab", "opaque-gitlab"), expandedSource("slack", "opaque-slack")}
	jenkins := pipelineItem(t, sources[0], "jenkins:JenkinsBuild:1:api#7", "false", "UNSTABLE")
	gitlab := pipelineItem(t, sources[1], "gitlab:GitlabPipeline:1:7", "failed", "")
	sources[2].Scope = "C123"
	message := &slackmodels.SlackChannelMessage{ConnectionId: 1, ChannelId: "C123", Ts: "1700000000.0001", Text: "hello"}
	message.RawDataOrigin = common.RawDataOrigin{RawDataTable: "_raw_slack_channel_message",
		RawDataParams: `{"connectionId":1,"scopeId":"C123"}`, RawDataId: 21}
	slack, err := NativeSlackMessageBatchItem(sources[2], 1, "message-r2", message,
		json.RawMessage(`{"ts":"1700000000.0001","text":"hello"}`), "policy-7", "devlake-pinned", "schema-v2")
	if err != nil {
		t.Fatal(err)
	}
	batch, err := PrepareSourceBatch("123e4567-e89b-42d3-a456-426614174001", "cell", 2, []BatchItem{jenkins, gitlab, slack}, sources)
	if err != nil {
		t.Fatal(err)
	}
	unbound, err := PrepareBatch(batch.BatchID, batch.CellID, batch.Generation, batch.Items)
	if err != nil {
		t.Fatal(err)
	}
	database := sql.OpenDB(sealConnector{&sealState{stored: map[string]storedSeal{}}})
	if _, err := SealPreparedBatch(context.Background(), database, sources, &fixtureAuthority{}, unbound,
		[]CheckpointUpdate{{SourceKey: SourceCheckpointKey(sources[0]), NextCursor: "a"},
			{SourceKey: SourceCheckpointKey(sources[1]), NextCursor: "b"},
			{SourceKey: SourceCheckpointKey(sources[2]), NextCursor: "c"}}); !errors.Is(err, ErrBatch) {
		t.Fatalf("unbound native batch sealed: %v", err)
	}
	database.Close()
	checkpoints := make([]CheckpointUpdate, 0, len(sources))
	for _, source := range sources {
		checkpoints = append(checkpoints, CheckpointUpdate{SourceKey: SourceCheckpointKey(source), NextCursor: "complete"})
	}
	for _, scenario := range []struct {
		revokeAt, commits int
		want              error
	}{
		{0, 1, nil}, {6, 0, ErrUnauthorized},
	} {
		state := &sealState{stored: map[string]storedSeal{}}
		database := sql.OpenDB(sealConnector{state})
		digest, err := SealPreparedBatch(context.Background(), database, sources,
			&expiringAuthority{revokeAt: scenario.revokeAt}, batch, checkpoints)
		database.Close()
		if !errors.Is(err, scenario.want) || state.commits != scenario.commits ||
			scenario.want == nil && digest != batch.ContentSHA256 {
			t.Fatalf("scoped native seal lost its authority check: digest=%s commits=%d err=%v", digest, state.commits, err)
		}
	}
	state := &sealState{stored: map[string]storedSeal{}}
	storeExportFixture(batch, state, len(batch.Items))
	reader := sql.OpenDB(sealConnector{state})
	defer reader.Close()
	page, err := loadSealedPage(context.Background(), reader, "cell", batch.BatchID, sources, "", 10, 65536)
	if err != nil || !page.Complete || len(page.Items) != 3 {
		t.Fatalf("sealed export unavailable: %+v, %v", page, err)
	}
	authorizedSources := append([]Source(nil), sources...)
	for index := range authorizedSources {
		authorizedSources[index].GrantID, authorizedSources[index].Consumer = "grant", "insights"
	}
	authorized, err := ExportAuthorizedPage(context.Background(), reader, "cell", batch.BatchID, authorizedSources, "", 10, 65536, &policyFixture{})
	if err != nil || !authorized.Page.Complete || len(authorized.Page.Items) != 3 || len(authorized.Policies) != 3 {
		t.Fatalf("authorized native export lost a provider: %+v, %v", authorized, err)
	}
	if _, err := ExportAuthorizedPage(context.Background(), reader, "cell", batch.BatchID, authorizedSources, "", 10, 65536, &policyFixture{revokedAt: 6}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("revoked export returned native rows: %v", err)
	}
	foreign := append([]Source(nil), authorizedSources...)
	foreign[1].TenantID = "foreign"
	if _, err := ExportAuthorizedPage(context.Background(), reader, "cell", batch.BatchID, foreign, "", 10, 65536, &policyFixture{}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("foreign export returned native rows: %v", err)
	}
	byTool := map[string]Observation{}
	for _, entry := range page.Items {
		observation := *entry.Observation
		byTool[observation.SourceNativeKey.Tool] = observation
		if observation.SubjectRef.TenantID != "tenant" || observation.SubjectRef.ProjectID != "project" ||
			observation.SubjectRef.SourceConnectionID != "opaque-"+observation.SourceNativeKey.Tool || observation.SubjectRef.RecordID == "" ||
			observation.SubjectRef.RecordRevision == "" || observation.SubjectRef.RecordType == "" ||
			observation.RawDataOrigin.RawDataId != 21 || observation.RawDataOrigin.RawDataTable == "" ||
			len(observation.Relationships) != 0 || observation.ObservationStatus != "observation_only" {
			t.Fatalf("incomplete or invented provenance: %+v", observation)
		}
	}
	for _, pair := range []struct{ tool, status, result string }{
		{"jenkins", "false", "UNSTABLE"}, {"gitlab", "failed", ""},
	} {
		observation := byTool[pair.tool]
		var native devops.CICDPipeline
		if json.Unmarshal(observation.NormalizedBytes, &native) != nil ||
			native.OriginalStatus != pair.status || native.OriginalResult != pair.result ||
			native.Status != "DONE" || native.Result != "FAILURE" ||
			!bytes.Contains(observation.NativeBytes, []byte(pair.status)) {
			t.Fatalf("upstream values changed during export: %+v", observation)
		}
	}
	if byTool["slack"].SubjectRef.RecordType != "channel_message" ||
		bytes.Contains(byTool["slack"].NormalizedBytes, []byte("deployment")) {
		t.Fatal("Slack was interpreted as a domain deployment")
	}
	jenkins.RawDataOrigin.RawDataId = 99
	if err := VerifyPreparedBatch(batch); err != nil {
		t.Fatalf("caller mutation changed sealed native origin: %v", err)
	}
	batch.Items[0].RawDataOrigin.RawDataId = 99
	if !errors.Is(VerifyPreparedBatch(batch), ErrBatch) {
		t.Fatal("modified sealed raw origin was not detected")
	}
}

func TestExpandedNativeEvidenceRejectsMissingOriginForeignSourceAndInventedLinks(t *testing.T) {
	source := expandedSource("jenkins", "opaque-jenkins")
	item := pipelineItem(t, source, "jenkins:JenkinsBuild:1:api#7", "true", "SUCCESS")
	batchID := "123e4567-e89b-42d3-a456-426614174001"
	for _, change := range []func(*BatchItem){
		func(value *BatchItem) { value.RawDataOrigin = nil },
		func(value *BatchItem) { value.RawDataOrigin.RawDataTable = "_raw_gitlab_api_builds" },
		func(value *BatchItem) { value.RawDataOrigin.RawDataParams = `{"connectionId":2}` },
		func(value *BatchItem) { value.NativeConnectionID = 2 },
		func(value *BatchItem) {
			value.Links = []EvidenceLink{{From: value.Key, To: value.Key, Relation: "source-reference", NativeEvidenceID: "status"}}
		},
	} {
		wrong := item
		origin := *item.RawDataOrigin
		wrong.RawDataOrigin = &origin
		change(&wrong)
		if _, err := PrepareSourceBatch(batchID, "cell", 1, []BatchItem{wrong}, []Source{source}); !errors.Is(err, ErrBatch) {
			t.Fatalf("unproved expanded evidence sealed: %+v, %v", wrong, err)
		}
	}
	foreign := source
	foreign.ProjectID = "foreign"
	if _, err := PrepareSourceBatch(batchID, "cell", 1, []BatchItem{item}, []Source{foreign}); !errors.Is(err, ErrBatch) {
		t.Fatalf("foreign source sealed: %v", err)
	}
	row := &devops.CICDPipeline{DomainEntity: domainlayer.DomainEntity{Id: "gitlab:GitlabPipeline:1:7"}}
	row.RawDataOrigin = *item.RawDataOrigin
	if _, err := NativeDomainBatchItem(source, 1, "r1", row, []byte(`{"id":7}`), "policy", "map", "schema"); !errors.Is(err, ErrBatch) {
		t.Fatalf("foreign provider native ID accepted: %v", err)
	}
	row.Id = "jenkins:JenkinsBuild:2:api#7"
	if _, err := NativeDomainBatchItem(source, 1, "r1", row, []byte(`{"id":7}`), "policy", "map", "schema"); !errors.Is(err, ErrBatch) {
		t.Fatalf("foreign connection native ID accepted: %v", err)
	}
}

func TestSlackNativeToolEvidenceRejectsForeignChannelOrInventedTimestamp(t *testing.T) {
	source := expandedSource("slack", "opaque-slack")
	source.Scope = "C123"
	row := &slackmodels.SlackChannelMessage{ConnectionId: 1, ChannelId: "C123", Ts: "1700000000.000001", ThreadTs: "1700000000.000000", Text: "hello"}
	row.RawDataOrigin = common.RawDataOrigin{RawDataTable: "_raw_slack_channel_message",
		RawDataParams: `{"connectionId":1,"scopeId":"C123"}`, RawDataId: 7}
	native := json.RawMessage(`{"ts":"1700000000.000001","thread_ts":"1700000000.000000","text":"hello"}`)
	item, err := NativeSlackMessageBatchItem(source, 1, "message-revision", row, native, "policy", "devlake", "schema-v2")
	if err != nil || item.Key.Kind != "channel_message" || item.Key.NativeID != "C123:1700000000.000001" ||
		!bytes.Equal(item.NativeBytes, native) || len(item.Links) != 0 {
		t.Fatalf("native Slack message lost identity or text: %+v, %v", item, err)
	}
	var normalized slackmodels.SlackChannelMessage
	if json.Unmarshal(item.NormalizedBytes, &normalized) != nil || normalized.Ts != row.Ts ||
		normalized.ThreadTs != row.ThreadTs || normalized.Text != row.Text {
		t.Fatalf("native Slack tool values changed: %+v", normalized)
	}
	for _, scenario := range []struct {
		name string
		edit func(*Source, *slackmodels.SlackChannelMessage, *json.RawMessage)
	}{
		{"wrong channel", func(source *Source, _ *slackmodels.SlackChannelMessage, _ *json.RawMessage) { source.Scope = "C999" }},
		{"wrong connection", func(_ *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) {
			row.ConnectionId = 2
		}},
		{"wrong tool", func(source *Source, _ *slackmodels.SlackChannelMessage, _ *json.RawMessage) { source.Tool = "jenkins" }},
		{"ambiguous channel id", func(source *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) {
			source.Scope, row.ChannelId = "C123:shadow", "C123:shadow"
		}},
		{"missing timestamp", func(_ *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) { row.Ts = "" }},
		{"invalid timestamp", func(_ *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) {
			row.Ts = "1700000000/000001"
		}},
		{"wrong raw table", func(_ *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) {
			row.RawDataTable = "_raw_slack_other"
		}},
		{"wrong raw scope", func(_ *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) {
			row.RawDataParams = `{"connectionId":1,"scopeId":"C999"}`
		}},
		{"wrong raw connection", func(_ *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) {
			row.RawDataParams = `{"connectionId":2,"scopeId":"C123"}`
		}},
		{"missing raw origin", func(_ *Source, row *slackmodels.SlackChannelMessage, _ *json.RawMessage) { row.RawDataId = 0 }},
		{"invalid native body", func(_ *Source, _ *slackmodels.SlackChannelMessage, native *json.RawMessage) {
			*native = json.RawMessage(`not json`)
		}},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			selected, record, original := source, *row, append(json.RawMessage(nil), native...)
			scenario.edit(&selected, &record, &original)
			if _, err := NativeSlackMessageBatchItem(selected, 1, "message-revision", &record, original,
				"policy", "devlake", "schema-v2"); !errors.Is(err, ErrBatch) {
				t.Fatalf("unproved Slack message accepted: %v", err)
			}
		})
	}
	if _, err := NativeDomainBatchItem(source, 1, "message-revision", row, native,
		"policy", "devlake", "schema-v2"); !errors.Is(err, ErrBatch) {
		t.Fatalf("Slack incorrectly promoted to domain evidence: %v", err)
	}
}
