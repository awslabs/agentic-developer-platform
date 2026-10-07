# Ray bundled Jackson maintenance

This recipe replaces the actual Jackson core, databind and annotations 2.18.8
classes bundled in Ray 2.58.0's `ray_dist.jar` with upstream 2.18.11 classes. That
release contains the vendor fixes for core GHSA-p6pp-m3f8-5c89 and
GHSA-7hhh-6rmp-j9qf and databind GHSA-q4xh-88c3-wmh7,
GHSA-cxp5-3px4-pw24 and GHSA-wv8q-qhhj-9h54. It changes no release pin,
scanner policy or disposition. Other image findings require their own review.

`artifact-lock.json` records exact vendor binary, POM and source hashes and
published Maven Central checksums. `tool-lock.json` pins every Maven build
artifact. The build uses a digest-pinned Maven/JDK container with no network,
relocating classes to Ray's existing `io.ray.shaded.com.fasterxml.jackson`
namespace through Maven Shade. It also relocates service descriptors. The merger
corrects the Java 11/17/21 multi-release entry paths to match their relocated
bytecode; Maven Shade remaps these class bodies but leaves their ZIP entry paths
unchanged. Each vendor license and notice is retained under its own Maven metadata
directory. Unrelated Ray entries, including the existing manifest and JAXB module
descriptor, are preserved byte for byte.

From this directory, with the original JAR privately extracted from the exact
reviewed image:

```sh
python3 prepare.py
python3 build.py /private/original-ray_dist.jar
python3 verify.py --java 17
python3 verify.py --java 17 --base-classes
python3 verify.py --java 21
```

Preparation downloads the locked inputs; building and compatibility tests use no
network. Build refuses an original JAR with a different hash, changed dependency
cache, unexpected shaded content, signed or duplicate entries, or a replacement
that differs from `replacement-lock.json`. Compatibility exercises ordinary JSON,
annotations, services, every shaded class, Java-specific parser loading, Ray's
actual runtime-environment/protobuf serialization and its existing JSON schema
consumer. It contains no vulnerability demonstrations.

The overlay takes an explicit separately qualified base:

```sh
docker build --network=none --build-arg BASE_IMAGE=reviewed-base@sha256:REPLACE_WITH_VERIFIED_DIGEST \
  -t jackson-maintenance:candidate .
```

The installation has no network and checks the original Ray version/JAR and
replacement hash. Only `ray_dist.jar` and its hash/size row in Ray's wheel `RECORD`
change. The reviewed original RECORD row was already stale against the original
maintained JAR; the replacement records the bytes actually installed and preserves
all other rows. The image retains UID 1000:1000 and includes no build/test tools.

Qualification must independently compare complete image contents and runtime
configuration, extract and verify the installed replacement, run the maintained
SkyPilot ordinary runtime check, and retain a new raw SBOM and native/SARIF scan.
These source and ordinary compatibility checks do not approve the complete image,
transfer historical dispositions, verify cloud provisioning or authorize a release.
