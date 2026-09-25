"""Opt-in actual scheduled command against disposable PostgreSQL and a mock model."""

import os
from pathlib import Path
import subprocess
import time
import unittest
import uuid


@unittest.skipUnless(
    os.environ.get("GBRAIN_RUNTIME_IMAGE"), "requires built Gbrain image"
)
class DreamRuntimeTests(unittest.TestCase):
    def test_scheduled_command_initializes_and_embeds_with_mock_model(self):
        image = os.environ["GBRAIN_RUNTIME_IMAGE"]
        prefix = "gbrain-dream-" + uuid.uuid4().hex[:8]
        network, database, model = prefix + "-net", prefix + "-db", prefix + "-model"
        tasks = []
        script = (
            Path(__file__).resolve().parents[1]
            / "terraform/modules/fargate/dream-command.sh"
        ).read_text()
        password = "fixture-only:p@ss/word"

        def docker(*args, timeout=180):
            r = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=timeout
            )
            self.assertEqual(
                r.returncode, 0, (r.stdout + r.stderr).replace(password, "<fixture>")
            )
            return (r.stdout + r.stderr if args[0] == "logs" else r.stdout).strip()

        def query(sql):
            return docker(
                "exec", database, "psql", "-U", "gbrain", "-d", "gbrain", "-Atc", sql
            )

        mock = r"""
const http=require('http');
http.createServer(async(req,res)=>{
 let raw='';for await(const part of req)raw+=part;
 if(req.url!='/v1/embeddings'){res.writeHead(500);res.end('unexpected mock route');return;}
 const body=JSON.parse(raw),inputs=Array.isArray(body.input)?body.input:[body.input];
 console.log('mock_embedding_request');
 res.setHeader('Content-Type','application/json');
 res.end(JSON.stringify({object:'list',model:body.model,data:inputs.map((_,index)=>({object:'embedding',index,embedding:Array(1024).fill(0.01)})),usage:{prompt_tokens:1,total_tokens:1}}));
}).listen(3001,'0.0.0.0');
"""
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
                if (
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            database,
                            "pg_isready",
                            "-h",
                            "127.0.0.1",
                            "-U",
                            "gbrain",
                        ],
                        capture_output=True,
                    ).returncode
                    == 0
                ):
                    break
                time.sleep(1)
            else:
                self.fail("Postgres did not become ready")
            docker(
                "run",
                "-d",
                "--name",
                model,
                "--network",
                network,
                "--cap-drop",
                "ALL",
                "--entrypoint",
                "node",
                image,
                "-e",
                mock,
            )
            for attempt in range(3):
                if attempt == 1:
                    # Seed a sealed page projection, as the real page writer does.
                    # Upstream intentionally skips embedding unsealed projections.
                    query(
                        "INSERT INTO pages(slug,type,title,compiled_truth) VALUES ('test/dream','note','Dream fixture',repeat('A disposable scheduler embedding fixture with sufficient searchable content. ',40)); UPDATE pages SET text_projection_revision=knowledge_revision WHERE slug='test/dream'; INSERT INTO content_chunks(page_id,chunk_index,chunk_text) SELECT id,0,compiled_truth FROM pages WHERE slug='test/dream';"
                    )
                if attempt == 2:
                    # An existing vector with no recorded model must block init,
                    # even when its width matches the requested model.
                    query("DELETE FROM config WHERE key='embedding_model'")
                    original_vectors = query(
                        "SELECT md5(embedding::text) FROM content_chunks ORDER BY id"
                    )
                    original_requests = docker("logs", model).count(
                        "mock_embedding_request"
                    )
                task = prefix + "-task-" + str(attempt)
                tasks.append(task)
                docker(
                    "run",
                    "-d",
                    "--name",
                    task,
                    "--network",
                    network,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--no-healthcheck",
                    "--entrypoint",
                    "/bin/sh",
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
                    "-e",
                    "LITELLM_BASE_URL=http://" + model + ":3001/v1",
                    "-e",
                    "LITELLM_API_KEY=fixture-only",
                    image,
                    "-c",
                    script,
                )
                exit_code = docker("wait", task)
                logs = docker("logs", task)
                if attempt == 2:
                    self.assertNotEqual(exit_code, "0", logs)
                    self.assertIn("different or unrecorded embedding model", logs)
                    self.assertEqual(
                        query(
                            "SELECT md5(embedding::text) FROM content_chunks ORDER BY id"
                        ),
                        original_vectors,
                    )
                    self.assertEqual(
                        docker("logs", model).count("mock_embedding_request"),
                        original_requests,
                    )
                    self.assertEqual(
                        query(
                            "SELECT count(*) FROM config WHERE key='embedding_model'"
                        ),
                        "0",
                    )
                    continue
                self.assertEqual(exit_code, "0", logs.replace(password, "<fixture>"))
                self.assertTrue("phase(s)" in logs or "Dream cycle" in logs, logs)
                self.assertEqual(
                    query(
                        "SELECT count(*) FROM sources WHERE id='default' AND local_path IS NOT NULL"
                    ),
                    "0",
                )
            self.assertIn("mock_embedding_request", docker("logs", model), logs)
            column = (
                query("SELECT value FROM config WHERE key='search_embedding_column'")
                or "embedding"
            )
            self.assertRegex(column, r"^[a-z][a-z0-9_]*$")
            self.assertEqual(
                query(
                    f"SELECT vector_dims((to_jsonb(cc)->>'{column}')::vector) FROM content_chunks cc WHERE page_id=(SELECT id FROM pages WHERE slug='test/dream')"
                ),
                "1024",
                logs,
            )
        finally:
            for name in [*tasks, model, database]:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            subprocess.run(["docker", "network", "rm", network], capture_output=True)
