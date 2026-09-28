"""Run real image startup against disposable pgvector; opt in with GBRAIN_RUNTIME_IMAGE."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import unittest
import uuid


@unittest.skipUnless(
    os.environ.get("GBRAIN_RUNTIME_IMAGE"), "requires a built Gbrain image and Docker"
)
class ContainerRuntimeTests(unittest.TestCase):
    def test_fresh_home_database_migrations_and_peer_health(self):
        image = os.environ["GBRAIN_RUNTIME_IMAGE"]
        prefix = "gbrain-runtime-" + uuid.uuid4().hex[:10]
        network, database, app = prefix + "-net", prefix + "-db", prefix + "-app"
        password = "disposable-only:p@ss/word"

        def docker(*args):
            result = subprocess.run(
                ["docker", *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return (
                result.stdout + result.stderr if args[0] == "logs" else result.stdout
            ).strip()

        try:
            docker("network", "create", "--internal", network)
            docker(
                "run",
                "-d",
                "--name",
                database,
                "--network",
                network,
                "--tmpfs",
                "/var/lib/postgresql/data",
                "-e",
                "POSTGRES_USER=gbrain",
                "-e",
                "POSTGRES_DB=gbrain",
                "-e",
                "POSTGRES_PASSWORD=" + password,
                os.environ.get("GBRAIN_TEST_POSTGRES_IMAGE", "pgvector/pgvector:pg15"),
            )
            for _ in range(90):
                result = subprocess.run(
                    ["docker", "exec", database, "pg_isready", "-U", "gbrain"],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                self.fail("Disposable PostgreSQL did not become ready")

            # A second fresh container proves initialization against an existing
            # database works after its ephemeral HOME/config has been discarded.
            for attempt in range(2):
                docker(
                    "run",
                    "-d",
                    "--name",
                    app,
                    "--network",
                    network,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "-e",
                    "GBRAIN_DB_HOST=" + database,
                    "-e",
                    "GBRAIN_DB_USER=gbrain",
                    "-e",
                    "GBRAIN_DB_NAME=gbrain",
                    "-e",
                    "GBRAIN_DB_PASSWORD=" + password,
                    "-e",
                    "GBRAIN_DB_SSLMODE=disable",
                    image,
                )
                ready = False
                for _ in range(120):
                    state = json.loads(
                        docker("inspect", "--format", "{{json .State}}", app)
                    )
                    if state["Status"] == "exited":
                        logs = docker("logs", app).replace(
                            password, "<fixture-password>"
                        )
                        self.fail(
                            "Gbrain exited during actual startup:\n" + logs[-6000:]
                        )
                    if state.get("Health", {}).get("Status") == "healthy":
                        ready = True
                        break
                    time.sleep(1)
                self.assertTrue(
                    ready, "Gbrain did not pass its actual container health check"
                )
                docker(
                    "exec",
                    app,
                    "node",
                    "-e",
                    "const fs=require('fs'),os=require('os');"
                    "if(process.getuid()!==1001||os.homedir()!=='/home/appuser')process.exit(1);"
                    "fs.writeFileSync(os.homedir()+'/runtime-test','ok');",
                )
                # Request from another container: localhost-only bind must fail.
                docker(
                    "run",
                    "--rm",
                    "--network",
                    network,
                    "--entrypoint",
                    "node",
                    image,
                    "-e",
                    f"fetch('http://{app}:3000/health').then(r=>{{if(r.status!==200)process.exit(1)}}).catch(()=>process.exit(1))",
                )
                legacy_token = "disposable-legacy-mcp-token"
                token_hash = hashlib.sha256(legacy_token.encode()).hexdigest()
                if attempt == 0:
                    docker(
                        "exec",
                        database,
                        "psql",
                        "-U",
                        "gbrain",
                        "-d",
                        "gbrain",
                        "-c",
                        "INSERT INTO access_tokens (name, token_hash, permissions, scopes) VALUES "
                        f"('existing-adp-client', '{token_hash}', '{{}}', ARRAY['read','write'])",
                    )
                probe = r"""
const base=process.env.PROBE_URL;
const request=async(token,payload={jsonrpc:'2.0',id:1,method:'tools/list',params:{}})=>{
 const headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'};
 if(token)headers.Authorization='Bearer '+token;
 return fetch(base+'/mcp',{method:'POST',headers,body:JSON.stringify(payload)});
};
(async()=>{
 for(const token of [null,'wrong-token']){
  const response=await request(token);
  if(response.status!==401)throw Error('Unauthenticated/invalid MCP credential was not rejected: '+response.status);
 }
 const response=await request(process.env.PROBE_TOKEN);
 const text=await response.text();
 if(response.status!==200)throw Error('Existing bearer client rejected: '+response.status);
 const data=text.split('\n').find(line=>line.startsWith('data:'));
 const result=JSON.parse(data?data.slice(5).trim():text);
 if(!result.result?.tools?.length)throw Error('Legacy tools/list did not return tools: '+JSON.stringify(result));
 const names=new Set(result.result.tools.map(tool=>tool.name));
 if(!names.has('put_page')||!names.has('search'))throw Error('Existing ADP client tools unavailable');
 const tool=async(name,args)=>{
  const r=await request(process.env.PROBE_TOKEN,{jsonrpc:'2.0',id:2,method:'tools/call',params:{name,arguments:args}});
  const text=await r.text();const event=text.split('\n').find(line=>line.startsWith('data:'));
  const result=JSON.parse(event?event.slice(5).trim():text);
  if(r.status!==200||result.error||result.result?.isError)throw Error('Legacy tool failed: '+JSON.stringify(result));
  return result;
 };
 if(process.env.PROBE_ATTEMPT==='0')await tool('put_page',{slug:'test/runtime-persistence',content:'---\ntitle: Runtime persistence\n---\nretained-runtime-fixture-1948'});
 const page=await tool('get_page',{slug:'test/runtime-persistence'});
 if(!JSON.stringify(page).includes('retained-runtime-fixture-1948'))throw Error('MCP write was not retained across fresh container startup');
 console.log('MCP rejects missing/invalid auth; legacy bearer supports tools/list and persisted put_page/get_page');
})().catch(e=>{console.error(e.message);process.exit(1)});
"""
                docker(
                    "run",
                    "--rm",
                    "--network",
                    network,
                    "--entrypoint",
                    "node",
                    "-e",
                    "PROBE_URL=http://" + app + ":3000",
                    "-e",
                    "PROBE_TOKEN=" + legacy_token,
                    "-e",
                    "PROBE_ATTEMPT=" + str(attempt),
                    image,
                    "-e",
                    probe,
                )
                tables = docker(
                    "exec",
                    database,
                    "psql",
                    "-U",
                    "gbrain",
                    "-d",
                    "gbrain",
                    "-Atc",
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
                )
                self.assertGreater(
                    int(tables),
                    5,
                    "Database migrations did not create the application schema",
                )
                vector = docker(
                    "exec",
                    database,
                    "psql",
                    "-U",
                    "gbrain",
                    "-d",
                    "gbrain",
                    "-Atc",
                    "SELECT count(*) FROM pg_extension WHERE extname='vector'",
                )
                self.assertEqual(vector, "1")
                print(
                    f"startup {attempt + 1}: UID 1001, writable HOME, migrated pgvector database, peer HTTP 200"
                )
                docker("rm", "-f", app)
        finally:
            subprocess.run(
                ["docker", "rm", "-f", app, database], capture_output=True, timeout=30
            )
            subprocess.run(
                ["docker", "network", "rm", network], capture_output=True, timeout=30
            )


if __name__ == "__main__":
    unittest.main()
