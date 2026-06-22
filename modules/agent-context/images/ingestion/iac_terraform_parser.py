"""Terraform HCL parser — builds an IaC dependency graph.

Parses .tf files using python-hcl2 into an IaCGraph dataclass containing:
  - :InfraResource nodes (Terraform resources)
  - :InfraModule nodes (module calls)
  - :InfraProvider nodes (required provider declarations)
  - DEPENDS_ON edges (explicit + implicit interpolation references)
  - DECLARED_IN edges (resource → enclosing module)
  - USES_MODULE edges (module → child module)
  - USES_PROVIDER edges (resource → provider)

Mirrors the SCIP pipeline shape: this is the infra counterpart to scip_ingester.py.

Design authority: docs/agent-context/design-notes/1647-iac-dependency-graph-design.md (IAC-0)
Parent EPIC: #1647
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("iac_terraform_parser")


# ---------------------------------------------------------------------------
# Data structures (per design doc §4.3)
# ---------------------------------------------------------------------------


@dataclass
class IaCNode:
    """Single infrastructure resource/module/provider."""

    node_id: str  # Terraform address (e.g., "aws_iam_role.agent_runner")
    label: str  # "InfraResource" | "InfraModule" | "InfraProvider"
    resource_type: str  # e.g., "aws_iam_role" (empty for modules/providers)
    name: str  # Local name (e.g., "agent_runner")
    provider: str  # Provider name (e.g., "aws")
    file: str  # Relative file path
    line: int  # 1-indexed line number
    repo: str  # org/repo
    module_path: str  # Module path context (empty for root module)
    # Module-specific
    source: str  # Module source (for InfraModule nodes)
    # Provider-specific
    version_constraint: str  # For InfraProvider nodes


@dataclass
class IaCEdge:
    """Directed dependency edge."""

    from_id: str  # Source node address
    to_id: str  # Target node address
    edge_label: str  # "DEPENDS_ON" | "DECLARED_IN" | "USES_MODULE" | "USES_PROVIDER"
    file: str  # File where reference occurs
    line: int  # Line of the reference


@dataclass
class IaCGraph:
    """Complete IaC dependency graph for one repository."""

    nodes: dict[str, IaCNode] = field(default_factory=dict)  # address -> node
    edges: list[IaCEdge] = field(default_factory=list)
    repo: str = ""

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def depends_on_count(self) -> int:
        return sum(1 for e in self.edges if e.edge_label == "DEPENDS_ON")

    @property
    def declared_in_count(self) -> int:
        return sum(1 for e in self.edges if e.edge_label == "DECLARED_IN")


# ---------------------------------------------------------------------------
# Reference resolution patterns
# ---------------------------------------------------------------------------

# Matches resource references in interpolations and attribute access:
#   ${aws_iam_role.agent_runner.arn}
#   module.networking.vpc_id → module.networking
#   aws_iam_role.agent_runner.name
#   data.aws_caller_identity.current.account_id → data.aws_caller_identity.current
#
# Pattern: <resource_type>.<resource_name> (with type containing at least one
# underscore to distinguish from local/var references)
RESOURCE_REF_PATTERN = re.compile(
    r"(?<![.\w])"  # Not preceded by dot or word char
    r"([a-z][a-z0-9]*(?:_[a-z0-9]+)+)"  # resource_type (must have underscore)
    r"\."
    r"([a-z][a-z0-9_]*)"  # resource_name
    r"(?:\.[a-z_][a-z0-9_.]*)*"  # optional attribute chain (.arn, .id, etc.)
)

# Matches module references: module.<name>.<output>
MODULE_REF_PATTERN = re.compile(
    r"module\.([a-z][a-z0-9_]*)"  # module.<name>
    r"(?:\.[a-z_][a-z0-9_.]*)*"  # optional output chain
)

# Matches data source references: data.<type>.<name>.<attr>
DATA_REF_PATTERN = re.compile(
    r"data\.([a-z][a-z0-9]*(?:_[a-z0-9]+)+)"  # data.<type>
    r"\."
    r"([a-z][a-z0-9_]*)"  # <name>
    r"(?:\.[a-z_][a-z0-9_.]*)*"  # optional attribute chain
)

# Directories to skip during file discovery
SKIP_DIRS = {".terraform", ".terragrunt-cache", "node_modules", ".git"}


def _strip_quotes(s: str) -> str:
    """Strip literal surrounding quotes from python-hcl2 keys/values.

    python-hcl2 v8+ wraps block identifiers and string literals with quotes:
      "\"aws_iam_role\"" → "aws_iam_role"
      "\"./modules/vpc\"" → "./modules/vpc"
    """
    if isinstance(s, str) and len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def _strip_interpolation(s: str) -> str:
    """Strip ${...} wrapper from python-hcl2 expression values.

    python-hcl2 v8 wraps references as "${expr}":
      "${aws_iam_role.base.name}" → "aws_iam_role.base.name"
      "${module.networking.vpc_id}" → "module.networking.vpc_id"
    """
    if isinstance(s, str) and s.startswith("${") and s.endswith("}"):
        return s[2:-1]
    return s


# ---------------------------------------------------------------------------
# Parser implementation
# ---------------------------------------------------------------------------


def discover_tf_files(repo_path: str) -> list[str]:
    """Find all .tf files in a repository, excluding .terraform/ etc.

    Returns relative paths sorted for deterministic output.
    """
    tf_files = []
    repo_path_obj = Path(repo_path)

    for root, dirs, files in os.walk(repo_path):
        # Skip excluded directories (modifies in-place to prune walk)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in files:
            if fname.endswith(".tf"):
                abs_path = Path(root) / fname
                rel_path = str(abs_path.relative_to(repo_path_obj))
                tf_files.append(rel_path)

    return sorted(tf_files)


def _infer_provider_from_type(resource_type: str) -> str:
    """Infer provider name from resource type prefix.

    aws_iam_role → aws
    google_compute_instance → google
    azurerm_resource_group → azurerm
    kubernetes_deployment → kubernetes
    """
    parts = resource_type.split("_")
    if len(parts) >= 2:
        return parts[0]
    return "unknown"


def _extract_string_values(obj: object) -> list[str]:
    """Recursively extract all string values from a nested dict/list structure.

    Strips python-hcl2 wrappers (${...} and surrounding quotes) so that
    regex patterns can match resource references within the raw expressions.
    """
    strings = []
    if isinstance(obj, str):
        # Strip python-hcl2 wrappers to expose raw reference expressions
        unwrapped = _strip_interpolation(obj)
        unwrapped = _strip_quotes(unwrapped)
        strings.append(unwrapped)
        # Also add the original in case it has multiple refs in a template
        if unwrapped != obj:
            strings.append(obj)
    elif isinstance(obj, list):
        for item in obj:
            strings.extend(_extract_string_values(item))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key == "__is_block__":
                continue
            strings.extend(_extract_string_values(value))
    return strings


def _resolve_implicit_refs(
    block_body: dict,
    known_resources: set[str],
    known_modules: set[str],
    known_data_sources: set[str],
) -> list[tuple[str, str]]:
    """Scan a block body for implicit resource/module/data references.

    Returns list of (target_address, ref_type) tuples where ref_type is
    "resource", "module", or "data".
    """
    refs: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Collect all string values from the block
    all_strings = _extract_string_values(block_body)

    for s in all_strings:
        # Resource references (e.g., aws_iam_role.agent_runner.arn)
        for match in RESOURCE_REF_PATTERN.finditer(s):
            rtype = match.group(1)
            rname = match.group(2)
            address = f"{rtype}.{rname}"
            if address in known_resources and address not in seen:
                refs.append((address, "resource"))
                seen.add(address)

        # Module references (e.g., module.networking.vpc_id)
        for match in MODULE_REF_PATTERN.finditer(s):
            mod_name = match.group(1)
            address = f"module.{mod_name}"
            if address in known_modules and address not in seen:
                refs.append((address, "module"))
                seen.add(address)

        # Data source references (e.g., data.aws_caller_identity.current.id)
        for match in DATA_REF_PATTERN.finditer(s):
            dtype = match.group(1)
            dname = match.group(2)
            address = f"data.{dtype}.{dname}"
            if address in known_data_sources and address not in seen:
                refs.append((address, "data"))
                seen.add(address)

    return refs


def _parse_depends_on(block_body: dict) -> list[str]:
    """Extract explicit depends_on references from a block.

    depends_on can reference resources or modules:
      depends_on = [module.iam, aws_s3_bucket.data]

    python-hcl2 v8 wraps these as "${aws_iam_role.base}" strings.
    """
    depends_on = block_body.get("depends_on")
    if not depends_on:
        return []

    # python-hcl2 parses depends_on as a list of strings
    if isinstance(depends_on, list):
        refs = []
        for ref in depends_on:
            if isinstance(ref, str):
                # Strip ${} wrapper added by python-hcl2
                cleaned = _strip_interpolation(ref)
                # Also strip any remaining quotes
                cleaned = _strip_quotes(cleaned)
                if cleaned:
                    refs.append(cleaned)
            elif isinstance(ref, list):
                # Sometimes nested list from HCL2 parsing
                for item in ref:
                    if isinstance(item, str):
                        cleaned = _strip_interpolation(item)
                        cleaned = _strip_quotes(cleaned)
                        if cleaned:
                            refs.append(cleaned)
        return refs
    return []


def _get_line_number(block_body: dict) -> int:
    """Extract line number from python-hcl2 metadata.

    python-hcl2 v4+ provides __start_line__ when parsed with appropriate options.
    Falls back to 1 if not available.
    """
    line = block_body.get("__start_line__", 0)
    if isinstance(line, int) and line > 0:
        return line
    return 1


def parse_terraform(
    repo_path: str,
    repo: str,
    module_path: str = "",
) -> IaCGraph:
    """Parse Terraform HCL files into an IaC dependency graph.

    Args:
        repo_path: Absolute path to the repository root (or module dir for nested calls)
        repo: Repository identifier (e.g., "aws-e/adp")
        module_path: Terraform module path context (e.g., "module.gateway")

    Returns:
        IaCGraph with nodes and edges

    Raises:
        ValueError: If .tf files exist but parsing produces zero nodes (fail-loud rule)
    """
    # Import here to allow tests to run without python-hcl2 installed
    try:
        import hcl2  # type: ignore[import-untyped]
    except ImportError as e:
        raise ImportError(
            "python-hcl2 is required for Terraform parsing. Install with: pip install python-hcl2"
        ) from e

    graph = IaCGraph(repo=repo)

    # Discover .tf files
    tf_files = discover_tf_files(repo_path)
    if not tf_files:
        log.info("No .tf files found in %s — skipping", repo_path)
        return graph

    log.info("Discovered %d .tf files in %s", len(tf_files), repo_path)

    # --- Pass 1: Parse all files and collect nodes ---
    known_resources: set[str] = set()
    known_modules: set[str] = set()
    known_data_sources: set[str] = set()
    # Store raw block data for Pass 2 (reference resolution)
    resource_blocks: list[tuple[str, dict, str, int]] = []  # (address, body, file, line)
    module_blocks: list[tuple[str, dict, str, int]] = []
    data_blocks: list[tuple[str, dict, str, int]] = []

    for rel_path in tf_files:
        abs_path = os.path.join(repo_path, rel_path)
        try:
            with open(abs_path) as f:
                parsed = hcl2.load(f)
        except Exception as e:
            log.warning("Failed to parse %s: %s", rel_path, str(e)[:200])
            continue

        # Extract resources
        for resource_block in parsed.get("resource", []):
            if not isinstance(resource_block, dict):
                continue
            for raw_resource_type, instances in resource_block.items():
                resource_type = _strip_quotes(raw_resource_type)
                if resource_type == "__is_block__":
                    continue
                if not isinstance(instances, (list, dict)):
                    continue
                instance_list = instances if isinstance(instances, list) else [instances]
                for instance in instance_list:
                    if not isinstance(instance, dict):
                        # python-hcl2 may return the instance directly as a dict
                        # when there's a single instance
                        continue
                    for raw_resource_name, body in instance.items():
                        resource_name = _strip_quotes(raw_resource_name)
                        if resource_name == "__is_block__":
                            continue
                        if not isinstance(body, dict):
                            continue
                        address = f"{resource_type}.{resource_name}"
                        line = _get_line_number(body)
                        provider = _infer_provider_from_type(resource_type)

                        node = IaCNode(
                            node_id=address,
                            label="InfraResource",
                            resource_type=resource_type,
                            name=resource_name,
                            provider=provider,
                            file=rel_path,
                            line=line,
                            repo=repo,
                            module_path=module_path,
                            source="",
                            version_constraint="",
                        )
                        graph.nodes[address] = node
                        known_resources.add(address)
                        resource_blocks.append((address, body, rel_path, line))

        # Extract data sources (treated as resources for dependency purposes)
        for data_block in parsed.get("data", []):
            if not isinstance(data_block, dict):
                continue
            for raw_data_type, instances in data_block.items():
                data_type = _strip_quotes(raw_data_type)
                if data_type == "__is_block__":
                    continue
                if not isinstance(instances, (list, dict)):
                    continue
                instance_list = instances if isinstance(instances, list) else [instances]
                for instance in instance_list:
                    if not isinstance(instance, dict):
                        continue
                    for raw_data_name, body in instance.items():
                        data_name = _strip_quotes(raw_data_name)
                        if data_name == "__is_block__":
                            continue
                        if not isinstance(body, dict):
                            continue
                        address = f"data.{data_type}.{data_name}"
                        line = _get_line_number(body)
                        provider = _infer_provider_from_type(data_type)

                        node = IaCNode(
                            node_id=address,
                            label="InfraResource",
                            resource_type=f"data.{data_type}",
                            name=data_name,
                            provider=provider,
                            file=rel_path,
                            line=line,
                            repo=repo,
                            module_path=module_path,
                            source="",
                            version_constraint="",
                        )
                        graph.nodes[address] = node
                        known_data_sources.add(address)
                        data_blocks.append((address, body, rel_path, line))

        # Extract modules
        for module_block in parsed.get("module", []):
            if not isinstance(module_block, dict):
                continue
            for raw_mod_name, body_or_list in module_block.items():
                mod_name = _strip_quotes(raw_mod_name)
                if mod_name == "__is_block__":
                    continue
                if isinstance(body_or_list, list):
                    body = body_or_list[0] if body_or_list else {}
                elif isinstance(body_or_list, dict):
                    body = body_or_list
                else:
                    continue
                if not isinstance(body, dict):
                    continue

                address = f"module.{mod_name}"
                line = _get_line_number(body)
                mod_source = body.get("source", "")
                if isinstance(mod_source, list):
                    mod_source = mod_source[0] if mod_source else ""
                mod_source = _strip_quotes(str(mod_source))

                node = IaCNode(
                    node_id=address,
                    label="InfraModule",
                    resource_type="",
                    name=mod_name,
                    provider="",
                    file=rel_path,
                    line=line,
                    repo=repo,
                    module_path=module_path,
                    source=mod_source,
                    version_constraint="",
                )
                graph.nodes[address] = node
                known_modules.add(address)
                module_blocks.append((address, body, rel_path, line))

        # Extract required_providers (from terraform block)
        for terraform_block in parsed.get("terraform", []):
            if not isinstance(terraform_block, dict):
                continue
            required_providers = terraform_block.get("required_providers")
            if not required_providers:
                continue
            if isinstance(required_providers, list):
                required_providers = required_providers[0] if required_providers else {}
            if not isinstance(required_providers, dict):
                continue

            for prov_name, prov_config in required_providers.items():
                if prov_name == "__is_block__":
                    continue
                if not isinstance(prov_config, dict):
                    continue
                prov_source = prov_config.get("source", f"hashicorp/{prov_name}")
                if isinstance(prov_source, list):
                    prov_source = prov_source[0] if prov_source else ""
                prov_source = _strip_quotes(str(prov_source))
                version = prov_config.get("version", "")
                if isinstance(version, list):
                    version = version[0] if version else ""
                version = _strip_quotes(str(version))

                address = f"provider.{prov_name}"
                node = IaCNode(
                    node_id=address,
                    label="InfraProvider",
                    resource_type="",
                    name=prov_name,
                    provider=prov_name,
                    file=rel_path,
                    line=_get_line_number(prov_config) if isinstance(prov_config, dict) else 1,
                    repo=repo,
                    module_path=module_path,
                    source=prov_source,
                    version_constraint=version,
                )
                graph.nodes[address] = node

    # --- Pass 2: Resolve dependencies (edges) ---

    edge_set: set[tuple[str, str, str]] = set()  # Deduplicate (from, to, label)

    def _add_edge(from_id: str, to_id: str, label: str, file: str, line: int) -> None:
        """Add an edge if not already present (deduplication)."""
        key = (from_id, to_id, label)
        if key in edge_set:
            return
        if from_id == to_id:
            return  # No self-references
        edge_set.add(key)
        graph.edges.append(
            IaCEdge(
                from_id=from_id,
                to_id=to_id,
                edge_label=label,
                file=file,
                line=line,
            )
        )

    # Process resource blocks
    for address, body, file, line in resource_blocks:
        # Explicit depends_on
        for dep_ref in _parse_depends_on(body):
            if (
                dep_ref in known_resources
                or dep_ref in known_modules
                or dep_ref in known_data_sources
            ):
                _add_edge(address, dep_ref, "DEPENDS_ON", file, line)

        # Implicit interpolation references
        implicit_refs = _resolve_implicit_refs(
            body, known_resources, known_modules, known_data_sources
        )
        for target, _ref_type in implicit_refs:
            if target != address:  # No self-deps
                _add_edge(address, target, "DEPENDS_ON", file, line)

        # USES_PROVIDER edge
        provider_name = _infer_provider_from_type(graph.nodes[address].resource_type)
        provider_address = f"provider.{provider_name}"
        if provider_address in graph.nodes:
            _add_edge(address, provider_address, "USES_PROVIDER", file, line)

        # DECLARED_IN edge (resource → enclosing module, if module context exists)
        if module_path:
            if module_path in graph.nodes:
                _add_edge(address, module_path, "DECLARED_IN", file, line)

    # Process data source blocks (same reference resolution as resources)
    for address, body, file, line in data_blocks:
        # Explicit depends_on
        for dep_ref in _parse_depends_on(body):
            if (
                dep_ref in known_resources
                or dep_ref in known_modules
                or dep_ref in known_data_sources
            ):
                _add_edge(address, dep_ref, "DEPENDS_ON", file, line)

        # Implicit interpolation references
        implicit_refs = _resolve_implicit_refs(
            body, known_resources, known_modules, known_data_sources
        )
        for target, _ref_type in implicit_refs:
            if target != address:
                _add_edge(address, target, "DEPENDS_ON", file, line)

        # USES_PROVIDER edge
        provider_name = _infer_provider_from_type(
            graph.nodes[address].resource_type.removeprefix("data.")
        )
        provider_address = f"provider.{provider_name}"
        if provider_address in graph.nodes:
            _add_edge(address, provider_address, "USES_PROVIDER", file, line)

    # Process module blocks
    for address, body, file, line in module_blocks:
        # Explicit depends_on
        for dep_ref in _parse_depends_on(body):
            if (
                dep_ref in known_resources
                or dep_ref in known_modules
                or dep_ref in known_data_sources
            ):
                _add_edge(address, dep_ref, "DEPENDS_ON", file, line)

        # Implicit references in module arguments
        implicit_refs = _resolve_implicit_refs(
            body, known_resources, known_modules, known_data_sources
        )
        for target, _ref_type in implicit_refs:
            if target != address:
                _add_edge(address, target, "DEPENDS_ON", file, line)

        # USES_MODULE edge to parent if nested
        if module_path and module_path in graph.nodes:
            _add_edge(address, module_path, "USES_MODULE", file, line)

    # --- Fail-loud rule (design doc §4.3) ---
    if tf_files and graph.node_count == 0:
        raise ValueError(
            f"Terraform parse produced 0 nodes for {repo} "
            f"({len(tf_files)} .tf files found). "
            "This indicates a parse failure, not an empty repo."
        )

    log.info(
        "Terraform parse complete: %d nodes, %d edges "
        "(%d DEPENDS_ON, %d DECLARED_IN) from %d .tf files",
        graph.node_count,
        graph.edge_count,
        graph.depends_on_count,
        graph.declared_in_count,
        len(tf_files),
    )

    return graph
