package main

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

func main() {
	endpoint := os.Getenv("S03_SKYPILOT_ENDPOINT")
	serviceToken := os.Getenv("S03_SKYPILOT_SERVICE_TOKEN")
	if endpoint == "" || serviceToken == "" {
		panic("S03_SKYPILOT_ENDPOINT and S03_SKYPILOT_SERVICE_TOKEN are required")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client := skypilot.NewClient(
		endpoint,
		skypilot.WithServiceToken(serviceToken),
		skypilot.WithTimeout(30*time.Second),
	)
	health, err := client.Health(ctx)
	if err != nil {
		panic(fmt.Errorf("controller Health call failed: %w", err))
	}
	if health.Status != "healthy" || health.Version != "0.12.3" {
		panic(fmt.Errorf("unexpected health response: %#v", health))
	}
	statuses, err := client.Status(ctx)
	if err != nil {
		panic(fmt.Errorf("controller Status call failed: %w", err))
	}
	if len(statuses) != 0 {
		panic(fmt.Errorf("unexpected status records: %#v", statuses))
	}
	fmt.Printf(
		"controller_health=%s version=%s status_records=%d\n",
		health.Status,
		health.Version,
		len(statuses),
	)
}
