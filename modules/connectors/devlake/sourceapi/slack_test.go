package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestSlackPagePreservesChannelAndExactMessages(t *testing.T) {
	channel := []byte(`{"id":"C123","name":"ops","is_channel":true}`)
	message := []byte(`{"ts":"123.456","text":"native message"}`)
	for _, scenario := range []struct {
		kind, expected string
		payload, raw   []byte
	}{
		{"channel", "C123", []byte(`{"ok":true,"channel":` + string(channel) + `}`), channel},
		{"channel_message", "C123:123.456", []byte(`{"ok":true,"messages":[` + string(message) + `],"has_more":false,"response_metadata":{"next_cursor":""}}`), message},
	} {
		records, err := DecodePage("slack", scenario.kind, "C123", scenario.payload, 100, false)
		if err != nil || len(records) != 1 || records[0].ID != scenario.expected || records[0].Kind != scenario.kind ||
			len(records[0].Revision) != 64 || !bytes.Equal(records[0].Raw, scenario.raw) {
			t.Fatalf("native Slack %s lost: %v, %v", scenario.kind, records, err)
		}
	}
}

func TestSlackPageRejectsForeignChannelAndIncompleteHistory(t *testing.T) {
	channel := `{"ok":true,"channel":{"id":"C123","is_channel":true}}`
	for _, payload := range []string{strings.Replace(channel, "C123", "C124", 1),
		strings.Replace(channel, `"is_channel":true`, `"is_im":true`, 1),
		strings.Replace(channel, `"ok":true`, `"ok":false`, 1)} {
		if _, err := DecodePage("slack", "channel", "C123", []byte(payload), 100, false); err == nil {
			t.Fatal("unsafe channel accepted", payload)
		}
	}
	history := `{"ok":true,"messages":[{"ts":"123.456","text":"native"}],"has_more":false,"response_metadata":{"next_cursor":""}}`
	for _, payload := range []string{
		strings.Replace(history, `"has_more":false`, `"has_more":true`, 1),
		strings.Replace(history, `"next_cursor":""`, `"next_cursor":"unread"`, 1),
		strings.Replace(history, `"123.456"`, `"invalid"`, 1),
		strings.Replace(history, `"messages":[{"ts":"123.456","text":"native"}]`, `"messages":null`, 1),
		strings.Replace(history, `{"ts":"123.456","text":"native"}`, `{"ts":"123.456"},{"ts":"123.456"}`, 1),
	} {
		if _, err := DecodePage("slack", "channel_message", "C123", []byte(payload), 100, false); err == nil {
			t.Fatal("incomplete history accepted", payload)
		}
	}
}
