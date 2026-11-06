package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestNativeDecodingPreservesExactBytesAndPrivacy(test *testing.T) {
	native := []byte(`{ "id":"7", "fields":{"summary":"Issue","status":{"name":"Done"},"security":null,"created":"2026-10-01T00:00:00.000+0000","updated":"2026-10-02T00:00:00.000+0000"}}`)
	page := append(append([]byte(`{"issues":[`), native...), []byte(`]}`)...)
	records, err := DecodePage("jira", "issue", "91", page, 100, false)
	if err != nil || len(records) != 1 || !bytes.Equal(records[0].Raw, native) || records[0].Status != "Done" || records[0].Private {
		test.Fatal("native values or bytes changed", records, err)
	}
	private := bytes.Replace(page, []byte(`"security":null`), []byte(`"security":{"id":"restricted"}`), 1)
	records, err = DecodePage("jira", "issue", "91", private, 100, false)
	if err != nil || !records[0].Private {
		test.Fatal("private issue not classified", err)
	}
	for _, invalid := range [][]byte{
		bytes.Replace(page, []byte(`"security":null,`), nil, 1),
		bytes.Replace(page, []byte(`"id":"7"`), []byte(`"id":"007"`), 1),
		bytes.Replace(page, []byte(`"created":"2026-10-01T00:00:00.000+0000"`), []byte(`"created":null`), 1),
		append(page, []byte(`{}`)...),
	} {
		if _, err := DecodePage("jira", "issue", "91", invalid, 100, false); err == nil {
			test.Fatal("ambiguous source accepted", string(invalid))
		}
	}
}

func TestNativeDecoderRejectsDuplicateAndWrongScope(test *testing.T) {
	row := `{"id":7,"title":"Issue","state":"open","created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-02T00:00:00Z"}`
	for _, payload := range []string{"[" + row + "," + row + "]", "null", "[" + strings.Replace(row, `"id":7`, `"id":-1`, 1) + "]"} {
		if _, err := DecodePage("github", "issue", "42", []byte(payload), 100, false); err == nil {
			test.Fatal("invalid page accepted")
		}
	}
	if _, err := DecodePage("jira", "board", "91", []byte(`{"id":92,"name":"wrong board"}`), 100, false); err == nil {
		test.Fatal("foreign scope accepted")
	}
	if records, err := DecodePage("github", "issue", "42", []byte("["+row+"]"), 100, true); err != nil || !records[0].Private {
		test.Fatal("private repository classification lost")
	}
}
