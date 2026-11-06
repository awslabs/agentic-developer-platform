"""Behavioral regressions for Pillow's October 2026 TIFF/JPEG/EPS fixes.

The TIFF/JPEG cases adapt the upstream tests retained in pillow/patches.
Use --real-ghostscript in a test environment that supplies Ghostscript; the
default EPS check exercises the subprocess argument contract without installing
an additional interpreter in the production image.
"""

import argparse
import ctypes
import io
import json
import os
import random
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import EpsImagePlugin, Image, features


def tiff():
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "output.tiff"
        image = Image.new("L", (7, 8191))
        image.putdata([(i * 37) & 255 for i in range(7 * 8191)])
        padded = Image.frombytes("RGB", (300, 300), random.Random(0).randbytes(270000))
        cases = [
            (image, {"compression": "packbits", "description": "D" * 65535}),
            (
                padded,
                {
                    "compression": "tiff_lzw",
                    "description": "abcdef",
                    "software": "xyz0",
                    "tiffinfo": {315: "artist", 33432: "copyright!"},
                },
            ),
        ]
        for image, kwargs in cases:
            image.save(output, **kwargs)
            memory = io.BytesIO()
            image.save(memory, format="TIFF", **kwargs)
            assert memory.getvalue() == output.read_bytes(), (
                "TIFF BytesIO differs from file output"
            )


def jpeg():
    data = io.BytesIO()
    Image.new("RGB", (128, 128), (20, 40, 60)).save(data, format="JPEG")
    data.seek(0)
    with Image.open(data) as image:
        image.tile = [image.tile[0]._replace(extents=(0, 0, 64, 64))]
        try:
            image.load()
        except OSError as exc:
            assert "broken data stream" in str(exc)
        else:
            raise AssertionError("JPEG accepted a scanline wider than its tile")


def jpeg_components():
    data = io.BytesIO()
    Image.new("CMYK", (16, 16)).save(data, format="JPEG")
    try:
        Image.frombytes("RGBA", (16, 16), data.getvalue(), "jpeg", "LA", "")
    except (OSError, ValueError) as exc:
        # Image.frombytes reports the native BROKEN status as ValueError on
        # 12.x; ImageFile.load and newer APIs may raise OSError instead.
        assert str(exc) in {
            "cannot decode image data",
            "broken data stream when reading image file",
        }
    else:
        raise AssertionError(
            "JPEG accepted four components into a two-component buffer"
        )


def multiframe():
    data = io.BytesIO()
    Image.new("L", (32, 32), 128).save(
        data,
        format="MPO",
        save_all=True,
        append_images=[Image.new("RGB", (32, 32), (255, 0, 0))],
    )
    data.seek(0)
    with Image.open(data) as image:
        image.load()
        assert image.mode == "L"
        image.seek(1)
        assert image._im is None, (
            "MPO reused a narrow native buffer after a mode change"
        )
        image.load()
        assert image.mode == image.im.mode == "RGB"
        assert image.im.size == image.size


def eps(real_ghostscript=False):
    benign = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\n0 0 1 setrgbcolor\n0 0 100 100 rectfill\nshowpage\n"

    def render(command, **kwargs):
        infile = command[command.index("-f") + 1]
        assert os.path.isabs(infile), (
            "EPS filename can be parsed as a Ghostscript option"
        )
        assert Path(infile).read_bytes() == benign
        outfile = next(
            arg.split("=", 1)[1] for arg in command if arg.startswith("-sOutputFile=")
        )
        Path(outfile).write_bytes(b"P6\n100 100\n255\n" + b"\x00\x00\xff" * 10000)

    cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="pillow eps ") as tmp:
        try:
            os.chdir(tmp)
            for name in ("-h", "@options"):
                Path(name).write_bytes(benign)
                if real_ghostscript:
                    with Image.open(name) as image:
                        image.load()
                        assert image.size == (100, 100)
                else:
                    with (
                        patch.object(EpsImagePlugin, "gs_binary", "gs"),
                        patch.object(
                            EpsImagePlugin.subprocess, "check_call", side_effect=render
                        ),
                        Image.open(name) as image,
                    ):
                        image.load()
                        assert image.size == (100, 100)
        finally:
            os.chdir(cwd)


def codecs():
    libraries = list(
        (Path(Image.__file__).parent.parent / "pillow.libs").glob("libbrotlidec*.so*")
    )
    assert len(libraries) == 1
    decoder = ctypes.CDLL(str(libraries[0]))
    decoder.BrotliDecoderVersion.restype = ctypes.c_uint32
    assert decoder.BrotliDecoderVersion() == 0x01002001, (
        "Pillow must bundle Brotli 1.2.1"
    )
    for encoding in ("PNG", "JPEG", "TIFF", "WEBP", "AVIF", "JPEG2000"):
        data = io.BytesIO()
        Image.new("RGB", (32, 32), (20, 40, 60)).save(data, format=encoding)
        data.seek(0)
        with Image.open(data) as image:
            image.load()
            assert image.size == (32, 32)
    versions = {name: features.version(name) for name in features.get_supported()}
    for name, expected in {"libjpeg_turbo": "3.2.0", "libtiff": "4.7.2"}.items():
        assert versions[name] == expected, (name, versions[name])
    if "harfbuzz" in versions:
        assert versions["harfbuzz"] == "14.5.0"
    return versions


def main():
    if not __debug__:
        raise RuntimeError("Acceptance requires assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=(
            "tiff",
            "jpeg",
            "jpeg_components",
            "multiframe",
            "eps",
            "codecs",
            "all",
        ),
        default="all",
    )
    parser.add_argument("--real-ghostscript", action="store_true")
    args = parser.parse_args()
    results = {}
    for name, test in {
        "tiff": tiff,
        "jpeg": jpeg,
        "jpeg_components": jpeg_components,
        "multiframe": multiframe,
        "eps": lambda: eps(args.real_ghostscript),
        "codecs": codecs,
    }.items():
        if args.case in (name, "all"):
            results[name] = test() or "passed"
    print(json.dumps({"pillow": Image.__version__, "results": results}))


if __name__ == "__main__":
    main()
