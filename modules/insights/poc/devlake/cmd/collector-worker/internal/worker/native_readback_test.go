package worker

import (
	"bytes"
	"errors"
	"testing"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
	"github.com/apache/devlake/core/models/domainlayer"
	"github.com/apache/devlake/core/models/domainlayer/devops"
)

func TestNativeReadbackMatchesIndependentRecordsToExactRawOrigin(t *testing.T) {
	for _, provider := range []string{"jenkins", "gitlab"} {
		scope, kind := "team/api", "build"
		if provider == "gitlab" {
			scope, kind = "901", "pipeline"
		}
		t.Run(provider, func(t *testing.T) {
			source := collection.Source{Tool: provider, TenantID: "tenant", ProjectID: "project", CellID: "cell", ConnectionID: "opaque", Scope: scope}
			origins := make(map[collection.NativeKey]common.RawDataOrigin)
			var records []collection.NativeRecord
			var rows []nativeDomainRow
			for index, id := range []string{"7", "8"} {
				key := collection.NativeKey{Tool: provider, TenantID: source.TenantID, ProjectID: source.ProjectID, CellID: source.CellID, ConnectionID: source.ConnectionID, Scope: scope, Kind: kind, NativeID: id}
				record := collection.NativeRecord{Key: key, Revision: "revision-" + id, Payload: []byte(`{"id":` + id + `,"result":"UNSTABLE"}`)}
				staged, err := stageSpec(source, 17, record)
				if err != nil {
					t.Fatal(err)
				}
				origin := common.RawDataOrigin{RawDataTable: staged.table, RawDataParams: staged.params, RawDataId: uint64(index + 11)}
				origins[key] = origin
				records = append(records, record)
				row := &devops.CICDPipeline{DomainEntity: domainlayer.DomainEntity{Id: provider + ":NativePipeline:17:" + id}, OriginalResult: "UNSTABLE"}
				row.RawDataOrigin = origin
				rows = append(rows, row)
			}
			items, err := nativeItemsForOrigins(source, 17, records, origins, rows, "policy-3")
			if err != nil || len(items) != 2 {
				t.Fatalf("independent rows unavailable: %+v, %v", items, err)
			}
			for index, item := range items {
				if item.Revision != records[index].Revision || !bytes.Equal(item.NativeBytes, records[index].Payload) || item.RawDataOrigin.RawDataId != uint64(index+11) || len(item.Links) != 0 {
					t.Fatalf("row inherited another record or invented a link: %+v", item)
				}
			}
			foreign := *rows[0].(*devops.CICDPipeline)
			foreign.RawDataOrigin.RawDataId = 99
			if _, err := nativeItemsForOrigins(source, 17, records, origins, []nativeDomainRow{&foreign}, "policy-3"); !errors.Is(err, collection.ErrIncomplete) {
				t.Fatalf("foreign raw origin accepted: %v", err)
			}
			foreign = *rows[0].(*devops.CICDPipeline)
			foreign.RawDataOrigin.RawDataParams = `{"connectionId":19}`
			if _, err := nativeItemsForOrigins(source, 17, records, origins, []nativeDomainRow{&foreign}, "policy-3"); !errors.Is(err, collection.ErrIncomplete) {
				t.Fatalf("foreign connection accepted: %v", err)
			}
			other := source
			other.CellID = "foreign"
			if _, err := nativeItemsForOrigins(other, 17, records, origins, rows, "policy-3"); !errors.Is(err, collection.ErrIncomplete) {
				t.Fatalf("foreign cell accepted: %v", err)
			}
			if _, err := nativeItemsForOrigins(source, 17, records, origins, append(rows, rows[0]), "policy-3"); !errors.Is(err, collection.ErrIncomplete) {
				t.Fatalf("duplicate domain row accepted: %v", err)
			}
		})
	}
}
