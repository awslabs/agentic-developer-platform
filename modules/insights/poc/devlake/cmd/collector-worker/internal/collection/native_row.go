package collection

import (
	"bytes"
	"encoding/json"
	"reflect"
	"regexp"
	"strconv"
	"strings"

	"github.com/apache/devlake/core/models/common"
	slackmodels "github.com/apache/devlake/plugins/slack/models"
)

func NativeDomainBatchItem(source Source, nativeConnectionID uint64, revision string,
	row interface {
		GetRawDataOrigin() *common.RawDataOrigin
		TableName() string
	}, native json.RawMessage,
	policyVersion, fieldMapVersion, schemaVersion string) (BatchItem, error) {
	if !expandedEvidenceTool(source.Tool) || source.Tool == "slack" ||
		source.TenantID == "" || source.ProjectID == "" || source.CellID == "" ||
		source.ConnectionID == "" || nativeConnectionID == 0 || source.Scope == "" || revision == "" ||
		row == nil || reflect.ValueOf(row).Kind() == reflect.Ptr && reflect.ValueOf(row).IsNil() ||
		row.GetRawDataOrigin() == nil || !json.Valid(native) ||
		policyVersion == "" || fieldMapVersion == "" || schemaVersion == "" {
		return BatchItem{}, ErrBatch
	}
	encoded, err := json.Marshal(row)
	if err != nil {
		return BatchItem{}, ErrBatch
	}
	var identity struct {
		ID string `json:"id"`
	}
	if json.Unmarshal(encoded, &identity) != nil {
		return BatchItem{}, ErrBatch
	}
	parts := strings.SplitN(identity.ID, ":", 4)
	if len(parts) != 4 || parts[0] != source.Tool ||
		parts[1] == "" || parts[2] != strconv.FormatUint(nativeConnectionID, 10) || parts[3] == "" {
		return BatchItem{}, ErrBatch
	}
	kind := row.TableName()
	if kind == "" || strings.HasPrefix(kind, "_") {
		return BatchItem{}, ErrBatch
	}
	origin := *row.GetRawDataOrigin()
	item := BatchItem{
		Key: NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
			Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope,
			Kind: kind, NativeID: identity.ID},
		Revision: revision, NativeConnectionID: nativeConnectionID, RawDataOrigin: &origin, NativeBytes: bytes.Clone(native),
		NormalizedBytes: encoded, PolicyVersion: policyVersion,
		FieldMapVersion: fieldMapVersion, SchemaVersion: schemaVersion,
	}
	if !validItemPayload(item) {
		return BatchItem{}, ErrBatch
	}
	return item, nil
}

var slackMessageTimestamp = regexp.MustCompile(`^[0-9]{1,16}\.[0-9]{1,16}$`)

func NativeSlackMessageBatchItem(source Source, nativeConnectionID uint64, revision string, row *slackmodels.SlackChannelMessage,
	native json.RawMessage, policyVersion, fieldMapVersion, schemaVersion string) (BatchItem, error) {
	if source.Tool != "slack" || source.TenantID == "" || source.ProjectID == "" || source.CellID == "" ||
		source.ConnectionID == "" || source.Scope == "" || revision == "" || row == nil || nativeConnectionID == 0 ||
		row.ConnectionId != nativeConnectionID || row.ChannelId != source.Scope ||
		len(row.ChannelId) < 2 || len(row.ChannelId) > 128 ||
		row.ChannelId[0] != 'C' && row.ChannelId[0] != 'G' || strings.Contains(row.ChannelId, ":") ||
		!slackMessageTimestamp.MatchString(row.Ts) || !json.Valid(native) ||
		policyVersion == "" || fieldMapVersion == "" || schemaVersion == "" {
		return BatchItem{}, ErrBatch
	}
	origin := row.GetRawDataOrigin()
	if origin == nil || origin.RawDataTable != "_raw_slack_channel_message" {
		return BatchItem{}, ErrBatch
	}
	var params struct {
		ConnectionId uint64 `json:"connectionId"`
		ScopeId      string `json:"scopeId"`
	}
	if json.Unmarshal([]byte(origin.RawDataParams), &params) != nil ||
		params.ConnectionId != row.ConnectionId || params.ScopeId != row.ChannelId {
		return BatchItem{}, ErrBatch
	}
	var rawMessage struct {
		Ts       string `json:"ts"`
		ThreadTs string `json:"thread_ts"`
		Text     string `json:"text"`
	}
	if json.Unmarshal(native, &rawMessage) != nil || rawMessage.Ts != row.Ts ||
		rawMessage.ThreadTs != row.ThreadTs || rawMessage.Text != row.Text {
		return BatchItem{}, ErrBatch
	}
	encoded, err := json.Marshal(row)
	if err != nil {
		return BatchItem{}, ErrBatch
	}
	copyOrigin := *origin
	item := BatchItem{
		Key: NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
			Tool: "slack", ConnectionID: source.ConnectionID, Scope: source.Scope,
			Kind: "channel_message", NativeID: row.ChannelId + ":" + row.Ts},
		Revision: revision, NativeConnectionID: nativeConnectionID, RawDataOrigin: &copyOrigin, NativeBytes: bytes.Clone(native),
		NormalizedBytes: encoded, PolicyVersion: policyVersion,
		FieldMapVersion: fieldMapVersion, SchemaVersion: schemaVersion,
	}
	if !validItemPayload(item) {
		return BatchItem{}, ErrBatch
	}
	return item, nil
}
