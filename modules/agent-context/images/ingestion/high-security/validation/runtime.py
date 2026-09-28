import ctypes
import glob
import hashlib
import json
import os
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

assert os.getuid() == 10001
libpath = "/usr/lib/x86_64-linux-gnu/libxml2.so.2"
lib = ctypes.CDLL(libpath, mode=os.RTLD_NOW)
assert ctypes.c_char_p.in_dll(lib, "xmlParserVersion").value == b"21309"
llvm = glob.glob("/usr/lib/x86_64-linux-gnu/libLLVM*.so*")
assert llvm
ctypes.CDLL(llvm[0], mode=os.RTLD_NOW)
ruby = """require "zlib"; require "net/imap"; require "stringio"; require "json"
raise unless Zlib::VERSION == "3.2.3" && Net::IMAP::VERSION == "0.5.15"
io=StringIO.new; writer=Zlib::GzipWriter.new(io); writer.write("round-trip"); writer.close
reader=Zlib::GzipReader.new(StringIO.new(io.string)); raise unless reader.read == "round-trip"
puts JSON.generate({ruby:RUBY_VERSION,zlib:Zlib::VERSION,imap:Net::IMAP::VERSION})"""
versions = json.loads(subprocess.check_output(["ruby", "-e", ruby], text=True))
subprocess.run(["bundle", "--version"], check=True, stdout=subprocess.DEVNULL)
subprocess.run(
    ["srb", "tc", "--no-config", "--ignore", "/", "-e", "# typed: true\nx = 1 + 2"],
    check=True,
    capture_output=True,
)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.set_content('<h1>Ingestion fixture</h1><canvas id="c"></canvas>')
    assert page.locator("h1").inner_text() == "Ingestion fixture"
    assert page.evaluate('document.querySelector("canvas").getContext("2d") !== null')
    png = page.screenshot()
    assert png.startswith(b"\x89PNG")
    browser.close()
print(
    json.dumps(
        {
            "uid": os.getuid(),
            "ruby": versions,
            "bundler": True,
            "sorbet": True,
            "libxml2_version": "2.13.9",
            "libxml2_sha256": hashlib.sha256(Path(libpath).read_bytes()).hexdigest(),
            "llvm_load": True,
            "chromium_dom_canvas_screenshot": True,
            "network": "isolated loopback only",
        }
    )
)
