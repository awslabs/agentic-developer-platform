// celcheck compiles every CEL expression of the ValidatingAdmissionPolicy
// objects found in the given manifests with the same compiler, environment and
// variable declarations the kube-apiserver uses (k8s.io/apiserver), then
// type-checks the validations against the OpenAPI schema of every matched kind
// exactly as the apiserver's policy status controller does (the warnings it
// would publish to status.typeChecking are failures here). It exits non-zero
// when any expression fails either step, printing the apiserver error.
package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	v1 "k8s.io/api/admissionregistration/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	plugincel "k8s.io/apiserver/pkg/admission/plugin/cel"
	"k8s.io/apiserver/pkg/admission/plugin/policy/validating"
	"k8s.io/apiserver/pkg/admission/plugin/webhook/matchconditions"
	"k8s.io/apiserver/pkg/cel/environment"
	"k8s.io/apiserver/pkg/cel/openapi/resolver"
	clientscheme "k8s.io/client-go/kubernetes/scheme"
	generatedopenapi "k8s.io/kubernetes/pkg/generated/openapi"
	sigsyaml "sigs.k8s.io/yaml"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: celcheck <manifest.yaml>...")
		os.Exit(2)
	}
	failed := 0
	policies := 0
	for _, path := range os.Args[1:] {
		found, errs := checkFile(path)
		policies += found
		for _, err := range errs {
			failed++
			fmt.Fprintf(os.Stderr, "FAIL %s: %v\n", path, err)
		}
	}
	if policies == 0 {
		fmt.Fprintln(os.Stderr, "FAIL: no ValidatingAdmissionPolicy found")
		os.Exit(1)
	}
	if failed > 0 {
		fmt.Fprintf(os.Stderr, "%d expression(s) failed to compile or type-check\n", failed)
		os.Exit(1)
	}
	fmt.Printf("OK: %d policies compiled and type-checked\n", policies)
}

func checkFile(path string) (int, []error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, []error{err}
	}
	decoder := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(raw), 4096)
	var errs []error
	found := 0
	for {
		var doc map[string]interface{}
		if err := decoder.Decode(&doc); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return found, append(errs, err)
		}
		if doc == nil || doc["kind"] != "ValidatingAdmissionPolicy" {
			continue
		}
		encoded, err := sigsyaml.Marshal(doc)
		if err != nil {
			return found, append(errs, err)
		}
		var policy v1.ValidatingAdmissionPolicy
		if err := sigsyaml.UnmarshalStrict(encoded, &policy); err != nil {
			return found, append(errs, fmt.Errorf("%v: %w", doc["metadata"], err))
		}
		found++
		errs = append(errs, compilePolicy(&policy)...)
		errs = append(errs, typeCheckPolicy(&policy)...)
	}
	return found, errs
}

// typeChecker mirrors the apiserver's ValidatingAdmissionPolicy status controller
// (k8s.io/apiserver/pkg/admission/plugin/policy/validating/typechecking.go): the
// built-in OpenAPI definitions the kube-apiserver serves become CEL declarations
// for every kind the policy's resourceRules match, and spec.validations[*] are
// checked against them. Fields the schema cannot type (for example
// resource.Quantity, published as oneOf string|number) are absent from those
// declarations and surface as "undefined field".
func typeChecker() *validating.TypeChecker {
	mapper := meta.NewDefaultRESTMapper(nil)
	for gvk := range clientscheme.Scheme.AllKnownTypes() {
		if gvk.Version == runtime.APIVersionInternal || strings.HasSuffix(gvk.Kind, "List") || strings.HasSuffix(gvk.Kind, "Options") {
			continue
		}
		mapper.Add(gvk, meta.RESTScopeNamespace)
	}
	return &validating.TypeChecker{
		SchemaResolver: resolver.NewDefinitionsSchemaResolver(generatedopenapi.GetOpenAPIDefinitions, clientscheme.Scheme),
		RestMapper:     mapper,
	}
}

// typeCheckPolicy fails when a resourceRule names no built-in kind (the apiserver
// would silently skip type checking) or when the apiserver would publish a
// status.typeChecking warning for any validation.
func typeCheckPolicy(policy *v1.ValidatingAdmissionPolicy) []error {
	checker := typeChecker()
	var kinds []schema.GroupVersionKind
	for _, rule := range policy.Spec.MatchConstraints.ResourceRules {
		for _, group := range rule.APIGroups {
			for _, version := range rule.APIVersions {
				for _, resource := range rule.Resources {
					resolved, err := checker.RestMapper.KindsFor(schema.GroupVersionResource{Group: group, Version: version, Resource: resource})
					if err != nil {
						return []error{fmt.Errorf("%s: no built-in kind for %s/%s %s: %w", policy.Name, group, version, resource, err)}
					}
					kinds = append(kinds, resolved...)
				}
			}
		}
	}
	if len(kinds) == 0 {
		return []error{fmt.Errorf("%s: matchConstraints.resourceRules match no built-in kind to type-check against", policy.Name)}
	}
	var errs []error
	for _, warning := range checker.Check(policy) {
		errs = append(errs, fmt.Errorf("%s %s (type check): %s", policy.Name, warning.FieldRef, strings.TrimSpace(warning.Warning)))
	}
	if len(errs) == 0 {
		for i := range policy.Spec.Validations {
			fmt.Printf("ok   %s spec.validations[%d].expression (type check against %v)\n", policy.Name, i, kinds)
		}
	}
	return errs
}

// compilePolicy mirrors k8s.io/apiserver/pkg/admission/plugin/policy/validating.compilePolicy
// and the apiserver's write-time validation (NewExpressions mode).
func compilePolicy(policy *v1.ValidatingAdmissionPolicy) []error {
	var errs []error
	opts := plugincel.OptionalVariableDeclarations{HasParams: policy.Spec.ParamKind != nil, HasAuthorizer: true}
	for _, mode := range []environment.Type{environment.NewExpressions, environment.StoredExpressions} {
		compiler, err := plugincel.NewCompositedCompiler(environment.MustBaseEnvSet(environment.DefaultCompatibilityVersion()))
		if err != nil {
			return []error{err}
		}
		variables := make([]plugincel.NamedExpressionAccessor, 0, len(policy.Spec.Variables))
		for i := range policy.Spec.Variables {
			variables = append(variables, &validating.Variable{Name: policy.Spec.Variables[i].Name, Expression: policy.Spec.Variables[i].Expression})
		}
		compiler.CompileAndStoreVariables(variables, opts, mode)
		report := func(field string, result plugincel.CompilationResult) {
			if result.Error != nil {
				errs = append(errs, fmt.Errorf("%s %s (%v): %v", policy.Name, field, mode, result.Error))
				return
			}
			fmt.Printf("ok   %s %s (%v)\n", policy.Name, field, mode)
		}
		for i := range policy.Spec.MatchConditions {
			accessor := (*matchconditions.MatchCondition)(&policy.Spec.MatchConditions[i])
			report(fmt.Sprintf("spec.matchConditions[%d].expression", i), compiler.Compiler.CompileCELExpression(accessor, opts, mode))
		}
		for i, validation := range policy.Spec.Validations {
			accessor := &validating.ValidationCondition{Expression: validation.Expression, Message: validation.Message, Reason: validation.Reason}
			report(fmt.Sprintf("spec.validations[%d].expression", i), compiler.Compiler.CompileCELExpression(accessor, opts, mode))
			if validation.MessageExpression != "" {
				msg := &validating.MessageExpressionCondition{MessageExpression: validation.MessageExpression}
				report(fmt.Sprintf("spec.validations[%d].messageExpression", i), compiler.Compiler.CompileCELExpression(msg, plugincel.OptionalVariableDeclarations{HasParams: opts.HasParams}, mode))
			}
		}
		for i, annotation := range policy.Spec.AuditAnnotations {
			accessor := &validating.AuditAnnotationCondition{Key: annotation.Key, ValueExpression: annotation.ValueExpression}
			report(fmt.Sprintf("spec.auditAnnotations[%d].valueExpression", i), compiler.Compiler.CompileCELExpression(accessor, opts, mode))
		}
	}
	return errs
}
