"""Build exact CPython 3.14.7 XML adapters with statically linked Expat 2.8.5.

Only small ordinary valid/invalid XML fixtures are used for acceptance checks.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import sysconfig
import tarfile

archive, expat, out = map(Path, sys.argv[1:4])
assert sys.version_info[:3] == (3, 14, 7)
assert hashlib.sha256(archive.read_bytes()).hexdigest() == '3b48dac8fb59f62eaa67ac83c1eb12bda1b7a08406dd286e252c11a66be27f81'
with tarfile.open(archive) as tar:
    tar.extractall(out / 'source', filter='data')
source = out / 'source/Python-3.14.7'
assert '#define PY_VERSION              "3.14.7"' in (source / 'Include/patchlevel.h').read_text()
include = Path(sysconfig.get_path('include'))
modules = out / 'modules'
modules.mkdir(parents=True, exist_ok=True)
library = expat / 'usr/lib/x86_64-linux-gnu/libexpat.a'
manifest = {'python_version': '3.14.7', 'expat_version': '2.8.5', 'linkage': 'static',
            'adp_source_commit': os.environ['ADP_SOURCE_COMMIT'],
            'expat_source_sha256': 'fd022c541a189bd5bee042a22188351b31e39b9389c2525fa98c28bd05c9ef21',
            'python_source_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'static_library_sha256': hashlib.sha256(library.read_bytes()).hexdigest(), 'modules': []}
for name in ['pyexpat', '_elementtree']:
    file = source / 'Modules' / (name + '.c')
    target = modules / (name + sysconfig.get_config_var('EXT_SUFFIX'))
    command = shlex.split(sysconfig.get_config_var('LDSHARED')) + shlex.split(sysconfig.get_config_var('CFLAGS'))
    command += ['-fPIC', '-I' + str(expat / 'usr/include'), '-I' + str(include),
                '-I' + str(include / 'internal'), '-I' + str(source / 'Modules'), '-I' + str(source / 'Modules/expat'), str(file), '-o', str(target)]
    if name == 'pyexpat':
        command.append(str(library))
    subprocess.run(command, check=True)
    manifest['modules'].append({'file': target.name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    subprocess.run(['ldd', str(target)], check=True)
sys.path.insert(0, str(modules))
import pyexpat
import _elementtree
import xml.etree.ElementTree as ET
assert Path(pyexpat.__file__).parent == modules
assert Path(_elementtree.__file__).parent == modules
assert pyexpat.EXPAT_VERSION == 'expat_2.8.5'
fixture = '<root name="fixture"><value>normal XML ☃</value></root>'
assert ET.fromstring(fixture).find('value').text == 'normal XML ☃'
parser = pyexpat.ParserCreate()
parser.Parse(fixture[:20], False)
parser.Parse(fixture[20:], True)
for parse in [lambda: pyexpat.ParserCreate().Parse('<root></other>', True),
              lambda: ET.fromstring('<root></other>')]:
    try:
        parse()
    except (pyexpat.ExpatError, ET.ParseError):
        pass
    else:
        raise AssertionError('Malformed ordinary XML was accepted')
manifest['bounded_xml_acceptance'] = 'passed'
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest))
