package execution

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"time"
)

var taskCredential = regexp.MustCompile(`^[a-f0-9]{64}$`)

type Task struct {
	Binding
	JobID          string   `json:"job_id"`
	PlanDigest     string   `json:"plan_digest"`
	CredentialName string   `json:"credential_name"`
	StepIDs        []string `json:"step_ids"`
}

// RunTask consumes one trusted pod-local assignment. It never starts the org
// registration manager or receives its credentials, IAM identity or databases.
func RunTask(ctx context.Context, assignmentFile, socket, tokenDirectory string) error {
	if !filepath.IsAbs(assignmentFile) || !filepath.IsAbs(tokenDirectory) {
		return ErrRefused
	}
	var file *os.File
	for {
		var err error
		file, err = os.Open(assignmentFile)
		if err == nil {
			break
		}
		if !errors.Is(err, os.ErrNotExist) {
			return ErrUnavailable
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, maxMessageBytes+1))
	if err != nil || len(raw) > maxMessageBytes {
		return ErrProtocol
	}
	var task Task
	if json.Unmarshal(raw, &task) != nil || !task.Binding.valid() || task.JobID == "" ||
		!taskCredential.MatchString(task.CredentialName) || len(task.StepIDs) == 0 || len(task.StepIDs) > 128 {
		return ErrProtocol
	}
	client, err := New(socket, filepath.Join(tokenDirectory, task.CredentialName), task.Binding)
	if err != nil {
		return err
	}
	defer func() {
		// Notification only. The trusted worker must read the durable shared
		// operation before acknowledgement; this file cannot report success.
		_ = os.WriteFile(assignmentFile[:len(assignmentFile)-len(filepath.Ext(assignmentFile))]+".finished", []byte("finished"), 0640)
	}()
	seen := map[string]bool{}
	for _, step := range task.StepIDs {
		if step == "" || seen[step] {
			return ErrProtocol
		}
		seen[step] = true
		result, err := client.ExecuteStep(ctx, step)
		if err != nil {
			return err
		}
		if result.Call.JobID != task.JobID || result.Call.Outcome == nil || *result.Call.Outcome != "succeeded" || result.Disposition != "settle" {
			return ErrRefused
		}
	}
	return nil
}
