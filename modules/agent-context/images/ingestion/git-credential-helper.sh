#!/bin/sh
# Git credential helper for GitHub App authentication.
# Invoked via GIT_ASKPASS — git calls this script with a prompt string as $1.
# For "Username..." prompts, respond with x-access-token (GitHub convention).
# For "Password..." prompts, respond with the GitHub App installation token.
# Token is written by github-app-token.py with 0600 permissions.
#
# Security: This script only prints to stdout (consumed by git internally).
# It does NOT log, does NOT echo to stderr, does NOT embed in URLs.
TOKEN_FILE="/tmp/github-token"
case "$1" in
    Username*|username*)
        echo "x-access-token"
        ;;
    *)
        if [ -f "$TOKEN_FILE" ]; then
            cat "$TOKEN_FILE"
        else
            exit 1
        fi
        ;;
esac
