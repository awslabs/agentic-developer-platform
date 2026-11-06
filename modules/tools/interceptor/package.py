import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def build(destination):
    package = Path(__file__).parent / "tools_interceptor"
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for source in sorted(package.glob("*.py")):
            entry = ZipInfo(f"tools_interceptor/{source.name}", date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, source.read_bytes())


if __name__ == "__main__":
    build(sys.argv[1])
