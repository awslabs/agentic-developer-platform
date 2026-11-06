package sourceapi

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"regexp"
)

var slackTimestamp = regexp.MustCompile(`^[0-9]{1,16}\.[0-9]{1,16}$`)

func decodeSlackPage(kind, scope string, payload []byte, limit int, private bool) ([]Record, error) {
	if kind != "channel" && kind != "channel_message" || len(scope) < 2 ||
		(scope[0] != 'C' && scope[0] != 'G') {
		return nil, ErrSource
	}
	var envelope map[string]json.RawMessage
	if decode(payload, &envelope) != nil || envelope == nil || string(envelope["ok"]) != "true" {
		return nil, ErrSource
	}
	if kind == "channel" {
		var channel struct {
			ID      string `json:"id"`
			Name    string `json:"name"`
			Channel bool   `json:"is_channel"`
			Group   bool   `json:"is_group"`
			IM      bool   `json:"is_im"`
			MPIM    bool   `json:"is_mpim"`
		}
		raw := envelope["channel"]
		if decode(raw, &channel) != nil || channel.ID != scope || !channel.Channel && !channel.Group || channel.IM || channel.MPIM {
			return nil, ErrSource
		}
		digest := sha256.Sum256(raw)
		return []Record{{Kind: kind, ID: scope, Title: channel.Name, Revision: hex.EncodeToString(digest[:]), Raw: bytes.Clone(raw), Private: private}}, nil
	}
	if string(envelope["has_more"]) != "false" {
		return nil, ErrSource
	}
	var metadata struct {
		NextCursor string `json:"next_cursor"`
	}
	if decode(envelope["response_metadata"], &metadata) != nil || metadata.NextCursor != "" {
		return nil, ErrSource
	}
	var messages []json.RawMessage
	if decode(envelope["messages"], &messages) != nil || messages == nil || len(messages) > limit {
		return nil, ErrSource
	}
	seen := make(map[string]bool, len(messages))
	records := make([]Record, 0, len(messages))
	for _, raw := range messages {
		var message struct {
			Ts   string `json:"ts"`
			Text string `json:"text"`
		}
		if decode(raw, &message) != nil || !slackTimestamp.MatchString(message.Ts) || seen[message.Ts] {
			return nil, ErrSource
		}
		seen[message.Ts] = true
		digest := sha256.Sum256(raw)
		records = append(records, Record{Kind: kind, ID: scope + ":" + message.Ts, Title: message.Text,
			Revision: hex.EncodeToString(digest[:]), Raw: bytes.Clone(raw), Private: private})
	}
	return records, nil
}
