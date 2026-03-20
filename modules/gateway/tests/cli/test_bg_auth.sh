#!/usr/bin/env bash
#
# test_bg_auth.sh - Shell-based tests for bg-auth.sh
#
# These tests verify the basic functionality of bg-auth.sh without
# requiring a real gateway server. They test argument parsing,
# help output, and basic error handling.
#
# Usage:
#   ./test_bg_auth.sh
#
# Exit codes:
#   0 - All tests passed
#   1 - One or more tests failed
#

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/../../cli" && pwd)"
BG_AUTH="$CLI_DIR/bg-auth.sh"

# Test counters
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_TOTAL=0

# Colors for output
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    NC=''
fi

# Test functions
pass() {
    echo -e "${GREEN}[PASS]${NC} $1"
    ((TESTS_PASSED++)) || true
}

fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    ((TESTS_FAILED++)) || true
}

skip() {
    echo -e "${YELLOW}[SKIP]${NC} $1"
}

# Run a test
run_test() {
    local test_name="$1"
    ((TESTS_TOTAL++)) || true
    echo -n "Running: $test_name... "
}

# Test: Script exists and is executable
test_script_exists() {
    run_test "Script exists and is executable"

    if [[ -x "$BG_AUTH" ]]; then
        pass "bg-auth.sh is executable"
    else
        fail "bg-auth.sh not found or not executable at $BG_AUTH"
    fi
}

# Test: --help option
test_help_option() {
    run_test "--help displays usage"

    local output
    output=$("$BG_AUTH" --help 2>&1) || true

    if echo "$output" | grep -q "Usage:"; then
        pass "--help shows usage"
    else
        fail "--help output doesn't contain 'Usage:'"
    fi
}

# Test: --version option
test_version_option() {
    run_test "--version displays version"

    local output
    output=$("$BG_AUTH" --version 2>&1) || true

    if echo "$output" | grep -q "v[0-9]"; then
        pass "--version shows version"
    else
        fail "--version output doesn't show version number"
    fi
}

# Test: Missing gateway URL error
test_missing_gateway_url() {
    run_test "Missing gateway URL produces error"

    local output
    local exit_code=0

    # Unset BG_GATEWAY_URL and run
    output=$(BG_GATEWAY_URL="" "$BG_AUTH" 2>&1) || exit_code=$?

    if [[ $exit_code -eq 3 ]] && echo "$output" | grep -qi "gateway url"; then
        pass "Missing gateway URL produces exit code 3"
    else
        fail "Expected exit code 3 for missing gateway URL, got $exit_code"
    fi
}

# Test: Invalid option error
test_invalid_option() {
    run_test "Invalid option produces error"

    local output
    local exit_code=0

    output=$("$BG_AUTH" --invalid-option 2>&1) || exit_code=$?

    if [[ $exit_code -eq 3 ]]; then
        pass "Invalid option produces error"
    else
        fail "Expected exit code 3 for invalid option, got $exit_code"
    fi
}

# Test: --gateway-url requires argument
test_gateway_url_requires_arg() {
    run_test "--gateway-url requires argument"

    local output
    local exit_code=0

    output=$(BG_GATEWAY_URL="" "$BG_AUTH" --gateway-url 2>&1) || exit_code=$?

    if [[ $exit_code -eq 3 ]]; then
        pass "--gateway-url without arg produces error"
    else
        fail "Expected exit code 3, got $exit_code"
    fi
}

# Test: --profile requires argument
test_profile_requires_arg() {
    run_test "--profile requires argument"

    local output
    local exit_code=0

    output=$(BG_GATEWAY_URL="http://localhost" "$BG_AUTH" --profile 2>&1) || exit_code=$?

    if [[ $exit_code -eq 3 ]]; then
        pass "--profile without arg produces error"
    else
        fail "Expected exit code 3, got $exit_code"
    fi
}

# Test: Debug flag is accepted
test_debug_flag() {
    run_test "--debug flag is accepted"

    local output
    local exit_code=0

    # This should fail because no credentials, but --debug should be accepted
    output=$(BG_GATEWAY_URL="http://localhost:1" "$BG_AUTH" --debug 2>&1) || exit_code=$?

    # Should not exit with code 3 (config error for invalid flag)
    if [[ $exit_code -ne 3 ]] && echo "$output" | grep -q "\[DEBUG\]"; then
        pass "--debug flag produces debug output"
    else
        # Debug flag might be accepted but no debug output if it fails early
        if [[ $exit_code -ne 3 ]]; then
            pass "--debug flag accepted (exit code: $exit_code)"
        else
            fail "--debug flag not accepted properly"
        fi
    fi
}

# Test: install.sh exists
test_install_script_exists() {
    run_test "install.sh exists and is executable"

    local install_script="$CLI_DIR/install.sh"

    if [[ -x "$install_script" ]]; then
        pass "install.sh is executable"
    else
        fail "install.sh not found or not executable"
    fi
}

# Test: install.sh --help
test_install_help() {
    run_test "install.sh --help works"

    local install_script="$CLI_DIR/install.sh"
    local output

    output=$("$install_script" --help 2>&1) || true

    if echo "$output" | grep -qi "install"; then
        pass "install.sh --help shows install info"
    else
        fail "install.sh --help doesn't show expected output"
    fi
}

# Test: claude-settings.example.json exists
test_settings_file_exists() {
    run_test "claude-settings.example.json exists"

    local settings_file="$CLI_DIR/claude-settings.example.json"

    if [[ -f "$settings_file" ]]; then
        pass "claude-settings.example.json exists"
    else
        fail "claude-settings.example.json not found"
    fi
}

# Test: claude-settings.example.json is valid JSON
test_settings_valid_json() {
    run_test "claude-settings.example.json is valid JSON"

    local settings_file="$CLI_DIR/claude-settings.example.json"

    if command -v jq &>/dev/null; then
        if jq empty "$settings_file" 2>/dev/null; then
            pass "JSON is valid"
        else
            fail "JSON is invalid"
        fi
    else
        skip "jq not available for JSON validation"
    fi
}

# Test: Dockerfile.agent exists
test_dockerfile_exists() {
    run_test "Dockerfile.agent exists"

    local dockerfile="$CLI_DIR/examples/Dockerfile.agent"

    if [[ -f "$dockerfile" ]]; then
        pass "Dockerfile.agent exists"
    else
        fail "Dockerfile.agent not found"
    fi
}

# Test: k8s-agent.yaml exists
test_k8s_manifest_exists() {
    run_test "k8s-agent.yaml exists"

    local manifest="$CLI_DIR/examples/k8s-agent.yaml"

    if [[ -f "$manifest" ]]; then
        pass "k8s-agent.yaml exists"
    else
        fail "k8s-agent.yaml not found"
    fi
}

# Test: README.md exists
test_readme_exists() {
    run_test "README.md exists"

    local readme="$CLI_DIR/README.md"

    if [[ -f "$readme" ]]; then
        pass "README.md exists"
    else
        fail "README.md not found"
    fi
}

# Main
main() {
    echo ""
    echo "============================================"
    echo "  CLI Tools Test Suite"
    echo "============================================"
    echo ""
    echo "CLI Directory: $CLI_DIR"
    echo ""

    # Run tests
    test_script_exists
    test_help_option
    test_version_option
    test_missing_gateway_url
    test_invalid_option
    test_gateway_url_requires_arg
    test_profile_requires_arg
    test_debug_flag
    test_install_script_exists
    test_install_help
    test_settings_file_exists
    test_settings_valid_json
    test_dockerfile_exists
    test_k8s_manifest_exists
    test_readme_exists

    # Summary
    echo ""
    echo "============================================"
    echo "  Test Results"
    echo "============================================"
    echo ""
    echo -e "Passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Failed: ${RED}$TESTS_FAILED${NC}"
    echo "Total:  $TESTS_TOTAL"
    echo ""

    if [[ $TESTS_FAILED -eq 0 ]]; then
        echo -e "${GREEN}All tests passed!${NC}"
        exit 0
    else
        echo -e "${RED}Some tests failed.${NC}"
        exit 1
    fi
}

main "$@"
