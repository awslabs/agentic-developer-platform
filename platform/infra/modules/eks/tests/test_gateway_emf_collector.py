"""Exercise the installed Fluent Bit CloudWatch serializer against a local TLS Logs API."""

import http.server
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import tempfile
import threading
import time
import unittest


CONFIG = Path(__file__).resolve().parents[1] / "gateway-emf.conf"
ROOT = os.environ.get("FLUENT_BIT_ROOT")


class LogsApi(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "PutLogEvents" in self.headers.get("X-Amz-Target", ""):
            self.server.events.extend(body["logEvents"])
            self.server.streams.extend([body["logStreamName"]] * len(body["logEvents"]))
        response = json.dumps({"nextSequenceToken": "1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("x-amzn-RequestId", "example")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args):
        pass


@unittest.skipUnless(ROOT, "Set FLUENT_BIT_ROOT to the extracted pinned aws-for-fluent-bit image root")
class GatewayEmfCollectorTest(unittest.TestCase):
    @staticmethod
    def tail_tag(pod, namespace, container, container_id="012345"):
        filename = f"{pod}_{namespace}_{container}-{container_id}.log"
        return "application." + str(Path("/var/log/containers") / filename).lstrip("/").replace("/", ".")

    def test_metric_is_delivered_once_as_root_emf(self):
        metric = {
            "_aws": {
                "Timestamp": 1730000000000,
                "CloudWatchMetrics": [{"Namespace": "BedrockGateway", "Dimensions": [[]],
                                       "Metrics": [{"Name": "ExporterEmitAttempt", "Unit": "Count"}]}],
            },
            "ExporterEmitAttempt": 1,
        }
        envelope = {"log": json.dumps(metric, separators=(",", ":")), "log_processed": metric,
                    "kubernetes": {"container_name": "bedrockgateway"}}
        gateway_tag = self.tail_tag("bedrockgateway-proof", "adp-gateway", "bedrockgateway")
        self.assertEqual(gateway_tag, "application.var.log.containers.bedrockgateway-proof_adp-gateway_bedrockgateway-012345.log")
        self.assertEqual(self.collect([(gateway_tag, envelope)]), [metric])
        self.assertIn(gateway_tag, self.last_streams[0])

    def test_each_gateway_pod_keeps_its_own_log_stream(self):
        metric = {"_aws": {"Timestamp": 1730000000000,
                           "CloudWatchMetrics": [{"Namespace": "BedrockGateway", "Dimensions": [[]],
                                                  "Metrics": [{"Name": "ExporterEmitAttempt", "Unit": "Count"}]}]},
                  "ExporterEmitAttempt": 1}
        envelope = {"log": json.dumps(metric, separators=(",", ":")), "log_processed": metric,
                    "kubernetes": {"container_name": "bedrockgateway"}}
        tags = [self.tail_tag("bedrockgateway-first", "adp-gateway", "bedrockgateway"),
                self.tail_tag("bedrockgateway-second", "adp-gateway", "bedrockgateway")]
        self.assertEqual(self.collect([(tag, envelope) for tag in tags]), [metric, metric])
        self.assertEqual(len(set(self.last_streams)), 2)
        for tag in tags:
            self.assertTrue(any(tag in stream for stream in self.last_streams))

    def test_other_logs_keep_their_envelopes_once(self):
        gateway_tag = self.tail_tag("bedrockgateway-proof", "adp-gateway", "bedrockgateway")
        other_tag = self.tail_tag("other-proof", "adp-gateway", "other")
        other_namespace_tag = self.tail_tag("bedrockgateway-proof", "another-namespace", "bedrockgateway")
        sidecar_tag = self.tail_tag("bedrockgateway-proof", "adp-gateway", "bedrockgateway-sidecar")
        lookalike_tag = self.tail_tag("bedrockgatewayish-proof", "adp-gateway", "bedrockgateway")
        ordinary = {"log": '{"level":"INFO","message":"BedrockGateway ready"}',
                    "log_processed": {"level": "INFO", "message": "BedrockGateway ready"},
                    "stream": "stdout", "unrelated": "keep",
                    "kubernetes": {"container_name": "bedrockgateway", "namespace_name": "adp-gateway"}}
        unrelated = {"log": '{"_aws":{"CloudWatchMetrics":[{"Namespace":"OtherNamespace"}]},"value":1}',
                     "log_processed": {"_aws": {"CloudWatchMetrics": [{"Namespace": "OtherNamespace"}]}, "value": 1},
                     "stream": "stdout", "unrelated": "keep",
                     "kubernetes": {"container_name": "bedrockgateway", "namespace_name": "adp-gateway"}}
        other_container = {"log": '{"_aws":{"CloudWatchMetrics":[{"Namespace":"BedrockGateway"}]},"value":1}',
                           "log_processed": {"_aws": {"CloudWatchMetrics": [{"Namespace": "BedrockGateway"}]}, "value": 1},
                           "stream": "stdout", "unrelated": "keep",
                           "kubernetes": {"container_name": "other", "namespace_name": "adp-gateway"}}
        unrelated_namespace = json.loads(json.dumps(other_container))
        unrelated_namespace["kubernetes"]["container_name"] = "bedrockgateway"
        unrelated_namespace["kubernetes"]["namespace_name"] = "another-namespace"
        sidecar = json.loads(json.dumps(other_container))
        sidecar["kubernetes"]["container_name"] = "bedrockgateway-sidecar"
        lookalike = json.loads(json.dumps(other_container))
        lookalike["kubernetes"]["container_name"] = "bedrockgateway"
        records = [(gateway_tag, ordinary), (gateway_tag, unrelated), (other_tag, other_container),
                   (other_namespace_tag, unrelated_namespace), (sidecar_tag, sidecar), (lookalike_tag, lookalike)]
        delivered = self.collect(records)
        self.assertCountEqual([json.dumps(item, sort_keys=True) for item in delivered],
                              [json.dumps(item, sort_keys=True) for _, item in records])

    def collect(self, records):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(work / "key.pem"), "-out", str(work / "cert.pem"),
                            "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server = http.server.HTTPServer(("127.0.0.1", 0), LogsApi)
            server.events = []
            server.streams = []
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(work / "cert.pem", work / "key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config = ["[SERVICE]", "    Flush 1", "    Log_Level info", "    storage.path " + str(work / "storage")]
                for tag, envelope in records:
                    config += ["[INPUT]", "    Name dummy", "    Tag " + tag,
                               "    Dummy " + json.dumps(envelope, separators=(",", ":")), "    Samples 1"]
                output = ["    endpoint localhost", "    port " + str(server.server_port), "    tls.ca_file " + str(work / "cert.pem")]
                config += ["[OUTPUT]", "    Name cloudwatch_logs", "    Match application.*",
                           "    region us-east-1", "    log_group_name /aws/containerinsights/example/application",
                           "    log_stream_prefix example-", "    auto_create_group true",
                           "    extra_user_agent container-insights", "    add_entity true",
                           "    endpoint localhost", "    port " + str(server.server_port),
                           "    tls.ca_file " + str(work / "cert.pem")]
                config += CONFIG.read_text().splitlines() + output
                (work / "fluent-bit.conf").write_text("\n".join(config) + "\n")
                root = Path(ROOT)
                loader = root / "lib64/ld-linux-x86-64.so.2"
                binary = root / "fluent-bit/bin/fluent-bit"
                self.assertEqual(subprocess.check_output(["sha256sum", str(binary)], text=True).split()[0],
                                 "4de1d009e42d84922f0b0531d642ee3639d50aaeff010f033b370a54f8a2b4c8")
                command = [str(loader), "--library-path", str(root / "usr/lib64") + ":" + str(root / "lib64"),
                           str(binary), "-c", str(work / "fluent-bit.conf")]
                env = dict(os.environ, AWS_ACCESS_KEY_ID="example", AWS_SECRET_ACCESS_KEY="example",
                           AWS_EC2_METADATA_DISABLED="true", AWS_REGION="us-east-1",
                           CLUSTER_NAME="example", HOST_NAME="example")
                process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 12
                    while len(server.events) < len(records) and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.1)
                finally:
                    process.send_signal(signal.SIGTERM) if process.poll() is None else None
                    stdout, stderr = process.communicate(timeout=5)
                self.assertTrue(server.events, f"collector did not send logs: {stderr.decode()[-3000:]}")
                self.last_streams = server.streams
                return [json.loads(event["message"]) for event in server.events]
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
