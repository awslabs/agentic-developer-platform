"""Independently reject links and writes outside the sealed Transform scope."""

from l5_services.adapters.transform.provenance import compare_source_trees


def check_write_scope(base_files: dict[str, bytes], candidate_files: dict[str, bytes],
                      *, base_modes: dict[str, str], candidate_modes: dict[str, str],
                      write_paths: list[str], excluded_paths: list[str]):
    if (not isinstance(write_paths, list) or not write_paths
            or not isinstance(excluded_paths, list)
            or not isinstance(base_modes, dict) or not isinstance(candidate_modes, dict)):
        raise ValueError("sealed write scope is unavailable")
    for path in write_paths:
        compare_source_trees({path: b""}, {})
    for path in excluded_paths:
        if path != ".git":
            compare_source_trees({path: b""}, {})
    if (len({path.casefold() for path in write_paths}) != len(write_paths)
            or len({path.casefold() for path in excluded_paths}) != len(excluded_paths)):
        raise ValueError("sealed write scope has colliding paths")
    comparison = compare_source_trees(base_files, candidate_files,
                                      base_modes=base_modes, candidate_modes=candidate_modes)
    if not comparison["changed_files"]:
        raise ValueError("provider did not change the requested source")
    if any(contents.startswith(b"version https://git-lfs.github.com/spec/v1\n")
           for contents in (*base_files.values(), *candidate_files.values())):
        raise ValueError("source snapshot contains an unqualified LFS pointer")
    for path in comparison["changed_files"]:
        if (not any(path == allowed or path.startswith(allowed + "/") for allowed in write_paths)
                or any(path == excluded or path.startswith(excluded + "/")
                       for excluded in excluded_paths)):
            raise ValueError("provider changed a path outside the sealed write scope")
    return {**comparison, "scope_status": "passed"}
