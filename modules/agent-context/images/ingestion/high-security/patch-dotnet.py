import base64
import hashlib
import json
import pathlib
import zipfile

root = pathlib.Path("/usr/share/dotnet/sdk/8.0.425/DotnetTools/dotnet-format")
archive = pathlib.Path("/tmp/xml.8.0.4.nupkg")
with zipfile.ZipFile(archive) as z:
    (root / "System.Security.Cryptography.Xml.dll").write_bytes(
        z.read("lib/net8.0/System.Security.Cryptography.Xml.dll")
    )
    licenses = pathlib.Path("/usr/share/licenses/dotnet-xml-security")
    licenses.mkdir(parents=True, exist_ok=True)
    for n in z.namelist():
        if n.upper().startswith(("LICENSE", "THIRD-PARTY")):
            (licenses / pathlib.Path(n).name).write_bytes(z.read(n))
p = root / "dotnet-format.deps.json"
s = json.loads(p.read_text())
old = "System.Security.Cryptography.Xml/8.0.3"
new = "System.Security.Cryptography.Xml/8.0.4"
assert old in s["libraries"]
s["libraries"][new] = s["libraries"].pop(old)
s["libraries"][new]["path"] = "system.security.cryptography.xml/8.0.4"
# Exact assembly substitution: update both the library key and dependency edges.
for target in s["targets"].values():
    if old in target:
        target[new] = target.pop(old)
        for path, metadata in target[new].get("runtime", {}).items():
            if path.endswith("/System.Security.Cryptography.Xml.dll"):
                metadata["assemblyVersion"] = "8.0.0.0"
                metadata["fileVersion"] = "8.0.2926.32403"
    for library in target.values():
        deps = library.get("dependencies", {})
        if "System.Security.Cryptography.Xml" in deps:
            deps["System.Security.Cryptography.Xml"] = "8.0.4"
# Bind the dependency metadata to the exact replacement NuGet package.
s["libraries"][new]["sha512"] = (
    "sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
)
s["libraries"][new]["hashPath"] = "system.security.cryptography.xml.8.0.4.nupkg.sha512"
p.write_text(json.dumps(s, indent=2) + "\n")
print(
    "Installed Xml8.0.4 DLL SHA256",
    hashlib.sha256((root / "System.Security.Cryptography.Xml.dll").read_bytes()).hexdigest(),
)
