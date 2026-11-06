package collection

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"time"
)

const EvidencePolicyVersion = "evidence-policy/1.0"

type ExportPolicyRequirement struct {
	Ref                string `json:"ref"`
	Revision           int64  `json:"revision"`
	Visibility         string `json:"visibility"`
	AudienceExpression string `json:"audience_expression"`
}

type ExportDependencyVersion struct {
	Identity       QualifiedRef `json:"identity"`
	Revision       int64        `json:"revision"`
	MetadataDigest string       `json:"metadata_digest"`
}

type ExportPolicyEnvelope struct {
	EnvelopeVersion      int                       `json:"envelope_version"`
	Identity             QualifiedRef              `json:"identity"`
	RealmID              string                    `json:"realm_id"`
	RealmRevision        int64                     `json:"realm_revision"`
	DescriptorDigest     string                    `json:"descriptor_digest"`
	RequiredPolicies     []ExportPolicyRequirement `json:"required_policies"`
	DependencyIdentities []QualifiedRef            `json:"dependency_identities"`
	DependencyVersions   []ExportDependencyVersion `json:"dependency_versions"`
	Audience             string                    `json:"audience"`
}

type ExportAuthority interface {
	Authority
	DeriveEvidence(context.Context, Source, QualifiedRef) (ExportPolicyEnvelope, error)
}

type ExportPolicyDescriptor struct {
	SchemaVersion     string               `json:"schema_version"`
	ObservationSHA256 string               `json:"observation_sha256"`
	ItemSHA256        string               `json:"item_sha256"`
	Envelope          ExportPolicyEnvelope `json:"envelope"`
	DescriptorSHA256  string               `json:"descriptor_sha256"`
}

type AuthorizedExportPage struct {
	Page     ExportPage               `json:"page"`
	Policies []ExportPolicyDescriptor `json:"policies"`
}

func exportGrant(ctx context.Context, authority ExportAuthority, source Source) error {
	current, err := authority.CurrentSource(ctx, source)
	if err != nil || current.Source != source || !current.BindingActive || !current.CellActive ||
		!current.GrantActive || current.GrantPurpose != "export" || !current.GrantExpires.After(time.Now()) {
		return ErrUnauthorized
	}
	return nil
}

func validDigest(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func checkExportEnvelope(observation Observation, envelope ExportPolicyEnvelope) error {
	root := observation.SubjectRef
	if envelope.EnvelopeVersion != 1 || envelope.Identity != root || envelope.RealmID == "" ||
		envelope.RealmRevision < 1 || !validDigest(envelope.DescriptorDigest) ||
		envelope.Audience != "shared" && envelope.Audience != "private" ||
		len(envelope.RequiredPolicies) < 2 || len(envelope.DependencyIdentities) == 0 ||
		len(envelope.DependencyIdentities) != len(envelope.DependencyVersions) {
		return ErrPolicy
	}
	seen := make(map[QualifiedRef]bool, len(envelope.DependencyIdentities))
	for index, dependency := range envelope.DependencyIdentities {
		version := envelope.DependencyVersions[index]
		if dependency.TenantID != root.TenantID || dependency.ProjectID != root.ProjectID ||
			dependency.SourceConnectionID == "" || dependency.RecordType == "" || dependency.RecordID == "" || dependency.RecordRevision == "" ||
			seen[dependency] || version.Identity != dependency || version.Revision < 1 || !validDigest(version.MetadataDigest) {
			return ErrPolicy
		}
		seen[dependency] = true
	}
	if !seen[root] {
		return ErrPolicy
	}
	if len(observation.Relationships) != len(observation.ResolvedRelationships) {
		return ErrPolicy
	}
	for index, link := range observation.Relationships {
		if link.From != observation.SourceNativeKey || link.To.TenantID != root.TenantID || link.To.ProjectID != root.ProjectID ||
			link.To.CellID != observation.SourceNativeKey.CellID || link.Relation != "source-reference" || link.NativeEvidenceID == "" {
			return ErrPolicy
		}
		resolved := observation.ResolvedRelationships[index]
		if resolved.From != root || resolved.To.TenantID != link.To.TenantID ||
			resolved.To.ProjectID != link.To.ProjectID || resolved.To.SourceConnectionID != link.To.ConnectionID ||
			resolved.To.RecordType != link.To.Kind || resolved.To.RecordID != link.To.NativeID ||
			resolved.Relation != link.Relation || resolved.NativeEvidenceID != link.NativeEvidenceID || !seen[resolved.To] {
			return ErrPolicy
		}
	}
	sharedExpression := envelope.RequiredPolicies[0].AudienceExpression
	for _, policy := range envelope.RequiredPolicies {
		if policy.Ref == "" || policy.Revision < 1 || policy.AudienceExpression == "" ||
			policy.Visibility != "private" && policy.Visibility != "shared" ||
			envelope.Audience == "shared" && (policy.Visibility != "shared" || policy.AudienceExpression != sharedExpression) {
			return ErrPolicy
		}
	}
	return nil
}

func ExportAuthorizedPage(ctx context.Context, reader *sql.DB, cellID, batchID string, sources []Source,
	after string, maxItems, maxBytes int, authority ExportAuthority) (AuthorizedExportPage, error) {
	if authority == nil || !validEvidenceSources(sources) {
		return AuthorizedExportPage{}, ErrUnauthorized
	}
	bySource := make(map[string]Source, len(sources))
	tools := make(map[string]bool, len(sources))
	for _, source := range sources {
		if source.CellID != cellID || source.GrantID == "" || source.Consumer == "" ||
			!supportedEvidenceTool(source.Tool) || source.TenantID == "" || source.ProjectID == "" ||
			source.TenantID != sources[0].TenantID || source.ProjectID != sources[0].ProjectID ||
			source.Consumer != sources[0].Consumer || tools[source.Tool] ||
			exportGrant(ctx, authority, source) != nil {
			return AuthorizedExportPage{}, ErrUnauthorized
		}
		tools[source.Tool] = true
		bySource[SourceCheckpointKey(source)] = source
	}
	if len(bySource) != len(sources) {
		return AuthorizedExportPage{}, ErrUnauthorized
	}
	page, err := loadSealedPage(ctx, reader, cellID, batchID, sources, after, maxItems, maxBytes)
	if err != nil {
		return AuthorizedExportPage{}, err
	}
	result := AuthorizedExportPage{Page: page, Policies: make([]ExportPolicyDescriptor, 0, len(page.Items))}
	result.Page.Items = make([]ExportItem, 0, len(page.Items))
	for index, item := range page.Items {
		previousPage := result.Page
		previousPolicies := len(result.Policies)
		if item.Observation != nil {
			observation := item.Observation
			native := observation.SourceNativeKey
			source, exists := bySource[SourceCheckpointKey(Source{CellID: native.CellID, TenantID: native.TenantID,
				ProjectID: native.ProjectID, Tool: native.Tool, ConnectionID: native.ConnectionID, Scope: native.Scope})]
			if !exists || observation.SourceRef != qualify(native, observation.SourceRevision) || observation.SubjectRef != observation.SourceRef ||
				observation.SourceSnapshot.ManifestSHA256 != page.ManifestSHA256 {
				return AuthorizedExportPage{}, ErrUnauthorized
			}
			envelope, err := authority.DeriveEvidence(ctx, source, observation.SubjectRef)
			if err != nil || checkExportEnvelope(*observation, envelope) != nil {
				return AuthorizedExportPage{}, ErrPolicy
			}
			observationBytes, err := json.Marshal(observation)
			if err != nil {
				return AuthorizedExportPage{}, ErrPolicy
			}
			observationHash := sha256.Sum256(observationBytes)
			descriptor := ExportPolicyDescriptor{SchemaVersion: EvidencePolicyVersion,
				ObservationSHA256: hex.EncodeToString(observationHash[:]), ItemSHA256: item.ItemSHA256, Envelope: envelope}
			descriptorBytes, err := json.Marshal(descriptor)
			if err != nil {
				return AuthorizedExportPage{}, ErrPolicy
			}
			descriptorHash := sha256.Sum256(descriptorBytes)
			descriptor.DescriptorSHA256 = hex.EncodeToString(descriptorHash[:])
			result.Policies = append(result.Policies, descriptor)
		}
		result.Page.Items = append(result.Page.Items, item)
		result.Page.Complete = page.Complete && index == len(page.Items)-1
		result.Page.NextCursor = item.ItemSHA256
		if result.Page.Complete {
			result.Page.NextCursor = ""
		}
		encoded, err := json.Marshal(result)
		if err != nil || len(encoded) > maxBytes {
			if len(previousPage.Items) == 0 {
				return AuthorizedExportPage{}, ErrBatch
			}
			result.Page = previousPage
			result.Policies = result.Policies[:previousPolicies]
			break
		}
	}
	for _, source := range sources {
		if exportGrant(ctx, authority, source) != nil {
			return AuthorizedExportPage{}, ErrUnauthorized
		}
	}
	encoded, err := json.Marshal(result)
	if err != nil || len(encoded) > maxBytes {
		return AuthorizedExportPage{}, ErrBatch
	}
	return result, nil
}
