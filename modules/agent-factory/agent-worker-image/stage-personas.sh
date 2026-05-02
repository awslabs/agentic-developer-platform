#!/usr/bin/env bash
# stage-personas.sh — Assembles personas and skills from source trees into
# flat staging directories for the agent worker image.
#
# Usage: stage-personas.sh <source_root> <stage_root>
#
# Source layout expected:
#   <source_root>/agent-factory/personas/*.md
#   <source_root>/domain-apps/<domain>/agent/personas/*.md
#   <source_root>/domain-apps/<domain>/agent/skills/<skill>/SKILL.md
#
# Output:
#   <stage_root>/personas/*.md        — all persona files (flat)
#   <stage_root>/skills/<name>/...    — all skill directories

set -euo pipefail

SOURCE_ROOT="${1:?Usage: stage-personas.sh <source_root> <stage_root>}"
STAGE_ROOT="${2:?Usage: stage-personas.sh <source_root> <stage_root>}"

mkdir -p "${STAGE_ROOT}/personas" "${STAGE_ROOT}/skills"

# --- Stage core agent-factory personas ---
CORE_PERSONAS="${SOURCE_ROOT}/agent-factory/personas"
if [ -d "${CORE_PERSONAS}" ]; then
    cp -f "${CORE_PERSONAS}"/*.md "${STAGE_ROOT}/personas/" 2>/dev/null || true
    echo "[stage] Copied core personas from ${CORE_PERSONAS}"
fi

# --- Stage domain-app personas and skills ---
if [ -d "${SOURCE_ROOT}/domain-apps" ]; then
    for domain_dir in "${SOURCE_ROOT}/domain-apps"/*/; do
        domain=$(basename "${domain_dir}")

        # Personas
        domain_personas="${domain_dir}agent/personas"
        if [ -d "${domain_personas}" ]; then
            cp -f "${domain_personas}"/*.md "${STAGE_ROOT}/personas/" 2>/dev/null || true
            echo "[stage] Copied personas from domain: ${domain}"
        fi

        # Skills
        domain_skills="${domain_dir}agent/skills"
        if [ -d "${domain_skills}" ]; then
            for skill_dir in "${domain_skills}"/*/; do
                [ -d "${skill_dir}" ] || continue
                skill_name=$(basename "${skill_dir}")
                cp -rf "${skill_dir}" "${STAGE_ROOT}/skills/${skill_name}"
                echo "[stage] Copied skill: ${domain}/${skill_name}"
            done
        fi
    done
fi

# --- Summary ---
persona_count=$(find "${STAGE_ROOT}/personas" -name "*.md" | wc -l)
skill_count=$(find "${STAGE_ROOT}/skills" -mindepth 1 -maxdepth 1 -type d | wc -l)
echo "[stage] Done: ${persona_count} personas, ${skill_count} skills staged"
