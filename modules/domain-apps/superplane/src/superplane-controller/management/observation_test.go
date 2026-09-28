package management

import (
	"context"
	"testing"
	"time"
)

func TestProviderObservationMustBeRecentAndHealthy(t *testing.T) {
	for _, test := range []struct {
		name  string
		value ProviderObservation
		want  bool
	}{
		{"current", ProviderObservation{true, time.Now().Add(-time.Second)}, true},
		{"stale", ProviderObservation{true, time.Now().Add(-time.Minute)}, false},
		{"future", ProviderObservation{true, time.Now().Add(time.Minute)}, false},
		{"unhealthy", ProviderObservation{false, time.Now()}, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := (observedSkyHealth{test.value}).HealthCheck(context.Background())
			if err != nil || got != test.want {
				t.Fatalf("health = %v, %v; want %v", got, err, test.want)
			}
		})
	}
}
