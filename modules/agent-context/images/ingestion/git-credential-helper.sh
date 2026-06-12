#!/bin/sh
# Git credential helper for GitHub App authentication.
# Invoked via GIT_ASKPASS — git calls this to get the password.
# Token is written by github-app-token.py with 0600 permissions.
#
# Security: This script only prints the token content to stdout.
# It does NOT log, does NOT echo to stderr, does NOT embed in URLs.
TOKEN_FILE="/tmp/github-token"
if [ -f "$TOKEN_FILE" ]; then
    cat "$TOKEN_FILE"
else
    exit 1
fi
