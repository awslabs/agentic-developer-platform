"""Offline upstream DeepWiki API/UI and current/legacy cache acceptance."""
import asyncio
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
import urllib.error
import urllib.request


def main():
    if not __debug__:
        raise RuntimeError('Acceptance requires assertions')
    assert os.getuid() == os.getgid() == 10001
    assert Path.home() == Path('/home/appuser')
    assert shutil.which('node')
    assert shutil.which('npm') and shutil.which('npx')
    npm = subprocess.run(['npm', '--version'], capture_output=True, text=True, check=True)
    assert npm.stdout.strip() == '11.20.0'
    node = subprocess.run(['node', '-e',
                           "const assert=require('node:assert'); const p=require('/app/node_modules/next/package.json'); assert.equal(p.version,'15.5.24'); const b=Buffer.from('fixture'); assert.equal(b.toString(),'fixture'); console.log(p.version)"],
                          capture_output=True, text=True, check=True)
    assert node.stdout.strip() == '15.5.24'
    from git import Repo
    from api.services.wiki.structure import detect_default_branch
    repo = Repo.init('/tmp/deepwiki-git-fixture', initial_branch='fixture')
    Path('/tmp/deepwiki-git-fixture/README.md').write_text('fixture')
    repo.index.add(['README.md'])
    from git import Actor
    actor = Actor('Fixture', 'fixture@example.invalid')
    repo.index.commit('fixture', author=actor, committer=actor)
    assert detect_default_branch(repo.working_tree_dir) == 'fixture'
    assert repo.head.commit.message == 'fixture'
    repo.close()
    for path in ('/app/api/main.py', '/app/start.sh', '/app/api/config/generator.json'):
        try:
            with open(path, 'ab'):
                pass
        except (PermissionError, OSError) as exc:
            assert exc.errno in (1, 13, 30)
        else:
            raise AssertionError('protected path writable: ' + path)
    from api.schemas import WikiCacheData, WikiStructureModel, RepoInfo
    from api.services.wiki.io import save_wiki_cache, delete_wiki_cache
    from api.utils import deepwiki_root
    assert Path(deepwiki_root()) == Path('/home/appuser/.adalflow')
    structure = WikiStructureModel(id='fixture', title='Synthetic wiki', description='offline', pages=[])
    current = WikiCacheData(wiki_structure=structure, generated_pages={},
                            repo=RepoInfo(owner='fixture', repo='current', type='github'))
    legacy = WikiCacheData(wiki_structure=structure, generated_pages={},
                           repo_url='https://example.invalid/fixture/legacy')
    assert asyncio.run(save_wiki_cache('fixture', 'current', 'github', 'en', current))
    assert asyncio.run(save_wiki_cache('fixture', 'legacy', 'github', 'en', legacy))
    log = open('/tmp/deepwiki-startup.log', 'w')
    process = subprocess.Popen(['/app/start.sh'], stdout=log, stderr=log, start_new_session=True)
    try:
        for port, path in ((8001, '/health'), (3000, '/')):
            ready = False
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError(Path('/tmp/deepwiki-startup.log').read_text())
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=2) as response:
                        ready = response.status == 200
                    if ready:
                        break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.5)
            assert ready, Path('/tmp/deepwiki-startup.log').read_text()
        for name in ('current', 'legacy'):
            url = f'http://127.0.0.1:8001/api/wiki_cache?owner=fixture&repo={name}&repo_type=github&language=en'
            with urllib.request.urlopen(url, timeout=5) as response:
                cache = json.load(response)
            assert cache['wiki_structure']['title'] == 'Synthetic wiki'
        assert asyncio.run(delete_wiki_cache('fixture', 'current', 'github', 'en'))
        with urllib.request.urlopen('http://127.0.0.1:8001/api/wiki_cache?owner=fixture&repo=current&repo_type=github&language=en', timeout=5) as response:
            assert json.load(response) is None
        print(json.dumps({'uid': os.getuid(), 'upstream_startup': 'API+UI passed',
                          'current_and_legacy_cache': 'save/read/delete passed',
                          'application_and_config_writes': 'denied'}))
    except Exception:
        log.flush()
        print(Path('/tmp/deepwiki-startup.log').read_text())
        raise
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        log.close()


if __name__ == '__main__':
    main()
