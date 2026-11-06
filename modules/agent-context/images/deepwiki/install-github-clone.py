"""Replace the reviewed upstream GitHub clone adapter at image build time."""

import ast
import hashlib
from pathlib import Path


def install(path: Path) -> None:
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != (
        "167dee61e783cc750a4e73b0944966848c81210eab7bdb47cb0a132cdc1e2b8b"
    ):
        raise SystemExit("DeepWiki repository adapter changed; review GitHub authentication")
    text = source.decode()
    function = next(
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == "_clone_from_github"
    )
    lines = text.splitlines(keepends=True)
    lines[function.body[0].lineno - 1 : function.end_lineno] = [
        "    from api.github_clone import clone_from_github\n\n",
        "    return clone_from_github(\n",
        "        remote_url, local_path, clone=GitRepo.clone_from,\n",
        "        access_token=access_token, **kwargs,\n",
        "    )\n",
    ]
    patched = "".join(lines)
    compile(patched, str(path), "exec")
    path.write_text(patched)


if __name__ == "__main__":
    install(Path("/app/api/repository.py"))
