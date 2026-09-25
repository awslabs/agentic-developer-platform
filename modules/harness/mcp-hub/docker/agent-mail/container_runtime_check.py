"""Run inside the built image with disposable /data and no production credentials."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

from git import Actor, Repo
from fastmcp import Client

assert os.getuid() == 10001
assert sys.version_info[:2] == (3, 14)
for module in ('mcp_agent_mail.cli', 'mcp_agent_mail.storage', 'mcp_agent_mail.http',
               'mcp_agent_mail.app', 'cryptography', 'PIL.Image', 'tiktoken', 'orjson', 'psutil'):
    importlib.import_module(module)
for executable in ('gcc', 'g++', 'cargo', 'rustc'):
    assert shutil.which(executable) is None, executable
repo = Repo.init('/data/git-check')
Path('/data/git-check/message.txt').write_text('mailbox test')
repo.index.add(['message.txt'])
actor = Actor('Test', 'test@example.invalid')
repo.index.commit('verify mailbox writes', author=actor, committer=actor)
assert repo.head.commit.message == 'verify mailbox writes'
conn = sqlite3.connect('/data/fts-check.sqlite')
conn.execute('create virtual table messages using fts5(body)')
conn.execute("insert into messages values ('mailbox search')")
assert conn.execute("select count(*) from messages where messages match 'mailbox'").fetchone()[0] == 1
conn.close()

async def workflow():
    async with Client('http://127.0.0.1:8765/api/') as client:
        async def call(name, **args):
            result = await client.call_tool(name, args)
            assert not result.is_error, name
            if result.data is not None:
                return result.data
            if result.structured_content is not None:
                return result.structured_content
            return [item.model_dump() for item in result.content]
        project = '/data/runtime-project'
        await call('ensure_project', human_key=project)
        sender = await call('register_agent', project_key=project, program='runtime-test', model='test')
        receiver = await call('register_agent', project_key=project, program='runtime-test', model='test')
        await call('request_contact', project_key=project, from_agent=sender['name'], to_agent=receiver['name'], registration_token=sender['registration_token'])
        await call('respond_contact', project_key=project, from_agent=sender['name'], to_agent=receiver['name'], accept=True, registration_token=receiver['registration_token'])
        await call('send_message', project_key=project, sender_name=sender['name'], to=[receiver['name']],
                   subject='alpineverification', body_md='Disposable compatibility test',
                   auto_contact_if_blocked=True, sender_token=sender['registration_token'])
        inbox = await call('fetch_inbox', project_key=project, agent_name=receiver['name'], include_bodies=True, registration_token=receiver['registration_token'])
        assert 'alpineverification' in json.dumps(inbox), 'inbox missing message'
        search = await call('search_messages', project_key=project, query='alpineverification', agent_name=receiver['name'], registration_token=receiver['registration_token'])
        assert 'alpineverification' in json.dumps(search), f'search missing message: {search!r}'

process = subprocess.Popen([sys.executable, '-c', "import sys; sys.argv=['mcp-agent-mail','serve-http']; from mcp_agent_mail.cli import app; app()"])
try:
    for _ in range(90):
        assert process.poll() is None, 'server exited'
        try:
            with urllib.request.urlopen('http://127.0.0.1:8765/health/liveness', timeout=1) as response:
                assert response.status == 200
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('health timeout')
    asyncio.run(workflow())
    print('PASS: UID, native imports, no compilers, Git commit, SQLite FTS5, HTTP health, MCP registration/send/inbox/search')
finally:
    process.terminate()
    process.wait(timeout=20)
