"""Exercise actual Terraform-managed Helm rendering with offline AWS mocks."""

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERED_FIXTURE = "tests/rendered_config.tftest.hcl"


def add_registration_fixture(module):
    (module / "registration_fixture.tf").write_text('''
output "registration_fixture" {
  value = {
    for group_id, rendered in data.helm_template.manager : group_id => {
      values = nonsensitive(local.manager_values[group_id])
      manifest = nonsensitive(rendered.manifest)
      config = yamldecode(nonsensitive(rendered.manifests["templates/configmap.yaml"])).data
      env = yamldecode(nonsensitive(rendered.manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].env
    }
  }
}
''')


def render(module, fixture=RENDERED_FIXTURE, *, artifact_dir=None):
    if artifact_dir:
        add_registration_fixture(module)
    result = subprocess.run(
        ["terraform", "test", "-filter=" + fixture, "-json", "-no-color"]
        + (["-verbose"] if artifact_dir else []),
        cwd=module, capture_output=True, text=True, check=False,
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    runs = {
        event["test_run"]["run"]: event["test_run"]["status"]
        for event in events if event.get("type") == "test_run"
        and event["test_run"].get("progress") == "complete"
    }
    if artifact_dir and result.returncode == 0:
        plan = next(event["test_plan"] for event in events if event.get("type") == "test_plan")
        managers = plan["output_changes"]["registration_fixture"]["after"]
        (artifact_dir / "terraform-results.json").write_text(json.dumps(runs, indent=2) + "\n")
        for group_id, manager in managers.items():
            directory = artifact_dir / group_id
            directory.mkdir()
            (directory / "helm-values.yaml").write_text(manager["values"])
            (directory / "rendered.yaml").write_text(manager["manifest"])
            (directory / "config.template.toml").write_text(manager["config"]["config.template.toml"])
            (directory / "register-the-runner.sh").write_text(manager["config"]["register-the-runner"])
        print(f"runner_rendered_fixture_path={artifact_dir}")
    return result.returncode, runs


class RenderedRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="adp-rendered-runner-")
        self.addCleanup(temporary.cleanup)
        self.module = Path(temporary.name) / "runners"
        shutil.copytree(ROOT, self.module, ignore=shutil.ignore_patterns(".terraform", "__pycache__"))
        self.assertTrue((ROOT / ".terraform" / "providers").is_dir(),
                        "Run terraform init -backend=false -input=false before rendered-runner tests")
        (self.module / ".terraform").symlink_to(ROOT / ".terraform", target_is_directory=True)

    def test_chart_registration_with_pinned_runner_passes_manager_guard(self):
        binary = os.environ.get("GITLAB_RUNNER_BINARY") or shutil.which("gitlab-runner")
        self.assertTrue(binary, "Set GITLAB_RUNNER_BINARY to the release-locked Runner executable")
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
        lock = json.loads((ROOT / "versions.lock.json").read_text())
        self.assertIn("Version:      " + lock["chart"]["runner_version"], version.stdout)
        add_registration_fixture(self.module)
        rendered = subprocess.run(
            ["terraform", "test", "-filter=" + RENDERED_FIXTURE, "-json", "-verbose", "-no-color"],
            cwd=self.module, capture_output=True, text=True, check=False,
        )
        self.assertEqual(rendered.returncode, 0, "Offline registration render failed")
        events = [json.loads(line) for line in rendered.stdout.splitlines() if line.strip()]
        plan = next(event["test_plan"] for event in events if event.get("type") == "test_plan")
        managers = plan["output_changes"]["registration_fixture"]["after"]
        spec = importlib.util.spec_from_file_location("registration_guard", ROOT / "scripts/prepare_manager.py")
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        certificate = self.module / "fixture-ca.pem"
        private_key = self.module / "fixture-key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(private_key), "-out", str(certificate),
        ], capture_output=True, check=True)
        requests = []

        class VerifyHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                fields = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, fields))
                response = json.dumps({"id": 101, "token": fields["token"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), VerifyHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for group_id, rendered_manager in managers.items():
                with self.subTest(group_id=group_id):
                    directory = self.module / ("registration-" + group_id)
                    directory.mkdir()
                    config = rendered_manager["config"]
                    (directory / "config.toml").write_text(config["config.toml"])
                    (directory / "config.template.toml").write_text(config["config.template.toml"])
                    system_id = "r_" + f"{int(group_id):012x}"
                    token = "glrt-synthetic-canary-" + group_id
                    receipt = {"runner_id": 101, "system_id": system_id}
                    (directory / ".runner_system_id").write_text(system_id)
                    (directory / ".adp-verified-runner.json").write_text(json.dumps(receipt))
                    (directory / "runner-token").write_text(token)
                    environment = {entry["name"]: entry["value"] for entry in rendered_manager["env"]
                                   if entry.get("value") is not None}
                    self.assertEqual(environment["CI_SERVER_TLS_CA_FILE"], "/secrets/ca.pem")
                    environment.update({
                        "PATH": os.defpath, "HOME": str(directory),
                        "CONFIG_FILE": str(directory / "config.toml"),
                        "CI_SERVER_URL": f"https://localhost:{server.server_port}/gitlab",
                        "CI_SERVER_TOKEN": token, "SSL_CERT_FILE": str(certificate),
                    })
                    script = config["register-the-runner"].replace(
                        "/entrypoint", shlex.quote(str(Path(binary).resolve()))).replace(
                        "/configmaps/", str(directory) + "/").replace(
                        "MAX_REGISTER_ATTEMPTS=30", "MAX_REGISTER_ATTEMPTS=1")
                    result = subprocess.run(["sh", "-c", script], env=environment, cwd=directory,
                                            capture_output=True, text=True, timeout=30, check=False)
                    self.assertEqual(result.returncode, 0, "Pinned Runner registration failed")
                    self.assertNotIn(token, result.stdout + result.stderr)
                    self.assertEqual(requests[-1][0], "/gitlab/api/v4/runners/verify")
                    self.assertEqual(requests[-1][1]["token"], token)
                    self.assertEqual(requests[-1][1]["system_id"], system_id)
                    self.assertEqual(json.loads(guard.verified_registration(directory, directory, group_id)), receipt)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_checked_chart_renders_restricted_groups_with_host_admission(self):
        chart = self.module / "assets/gitlab-runner-0.94.2.tgz"
        lock = json.loads((self.module / "versions.lock.json").read_text())
        self.assertEqual(hashlib.sha256(chart.read_bytes()).hexdigest(), lock["chart"]["sha256"])
        artifacts = Path(tempfile.mkdtemp(prefix="adp-runner-render-artifacts-"))
        returncode, runs = render(self.module, artifact_dir=artifacts)
        self.assertEqual(returncode, 0, runs)
        for group_id in ("42", "43"):
            for filename in ("helm-values.yaml", "rendered.yaml", "config.template.toml", "register-the-runner.sh"):
                self.assertGreater((artifacts / group_id / filename).stat().st_size, 0)
        self.assertEqual(runs["render_production_manager"], "pass")
        self.assertEqual(runs["reject_unsafe_real_chart_executor_configuration"], "pass")
        self.assertEqual(runs["verify_rendered_job_admission_path"], "pass")
        self.assertEqual(runs["render_shared_gitlab_and_exact_cache_route"], "pass")
        self.assertEqual(runs["render_private_clone_and_transfer_routes"], "pass")

    def test_render_rejects_registration_name_or_untagged_overrides(self):
        source = self.module / "helm.tf"
        original = source.read_text()
        for expected, replacement in (
            ('        name        = "gitlab-group-${group_id}"\n', ""),
            ("runUntagged = false", "runUntagged = true"),
        ):
            with self.subTest(replacement=replacement):
                self.assertIn(expected, original)
                source.write_text(original.replace(expected, replacement, 1))
                returncode, runs = render(self.module)
                self.assertNotEqual(returncode, 0)
                self.assertEqual(runs["render_production_manager"], "fail")

    def test_render_rejects_public_or_missing_checkout_route(self):
        config = self.module / "templates/runner.toml.tftpl"
        original = config.read_text()
        expected = 'clone_url = "https://${gitlab_host}/gitlab"'
        self.assertIn(expected, original)
        for replacement in ('clone_url = "https://public.example.test/gitlab"',
                            'clone_url = "https://${gitlab_host}"', ''):
            with self.subTest(replacement=replacement):
                config.write_text(original.replace(expected, replacement, 1))
                try:
                    returncode, runs = render(self.module)
                    self.assertNotEqual(returncode, 0)
                    self.assertEqual(runs["render_private_clone_and_transfer_routes"], "fail")
                finally:
                    config.write_text(original)

    def test_render_rejects_public_manager_api_fallback(self):
        source = self.module / "helm.tf"
        original = source.read_text()
        expected = 'gitlabUrl          = data.aws_ssm_parameter.runner_url.value'
        self.assertIn(expected, original)
        source.write_text(original.replace(expected, 'gitlabUrl          = "https://public.example.test/gitlab"', 1))
        try:
            returncode, runs = render(self.module)
            self.assertNotEqual(returncode, 0)
            self.assertEqual(runs["render_private_clone_and_transfer_routes"], "fail")
        finally:
            source.write_text(original)

    def test_render_rejects_unverified_or_undiscoverable_connector(self):
        source = self.module / "helm.tf"
        original = source.read_text()
        for old, replacement in (
            ('"/opt/fleeting/fleeting-plugin-aws", "/runner-plugin"',
             '"/opt/fleeting/unreviewed", "/runner-plugin"'),
            ('PATH                  = "/runner-plugin:',
             'PATH                  = "/unreviewed:'),
            ('{ name = "manager-plugin", mountPath = "/runner-plugin", readOnly = true }',
             '{ name = "manager-plugin", mountPath = "/runner-plugin", readOnly = false }'),
        ):
            with self.subTest(replacement=replacement):
                self.assertIn(old, original)
                source.write_text(original.replace(old, replacement, 1))
                try:
                    returncode, runs = render(self.module)
                    self.assertNotEqual(returncode, 0)
                    self.assertEqual(runs["verified_plugin_before_registration"], "fail")
                finally:
                    source.write_text(original)

    def test_render_rejects_permissive_service_configuration(self):
        config = self.module / "templates/runner.toml.tftpl"
        original = config.read_text()
        self.assertIn("services_limit = 0", original)
        config.write_text(original.replace("services_limit = 0", "services_limit = 1"))
        returncode, runs = render(self.module)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["render_production_manager"], "pass")
        self.assertEqual(runs["reject_unsafe_real_chart_executor_configuration"], "fail")

    def test_render_rejects_unqualified_cache_hostname(self):
        config = self.module / "templates/runner.toml.tftpl"
        original = config.read_text()
        self.assertIn('ServerAddress = "s3.${aws_region}.amazonaws.com"', original)
        config.write_text(original.replace('ServerAddress = "s3.${aws_region}.amazonaws.com"',
                                           'ServerAddress = "public.example.test"'))
        returncode, runs = render(self.module)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["render_production_manager"], "pass")
        self.assertEqual(runs["render_shared_gitlab_and_exact_cache_route"], "fail")

    def test_render_refuses_runner_without_private_tls_ca(self):
        config = self.module / "templates/runner.toml.tftpl"
        original = config.read_text()
        self.assertIn('tls-ca-file = "/secrets/ca.pem"', original)
        config.write_text(original.replace('tls-ca-file = "/secrets/ca.pem"', 'tls-ca-file = ""'))
        returncode, runs = render(self.module)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["render_production_manager"], "pass")
        self.assertEqual(runs["runtime_tls_init_path"], "fail")

    def test_planned_manager_values_and_outputs_never_contain_runtime_canary(self):
        result = subprocess.run(
            ["terraform", "test", "-filter=" + RENDERED_FIXTURE, "-json", "-verbose", "-no-color"],
            cwd=self.module, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, "Offline rendered plan failed")
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        plans = [event["test_plan"] for event in events if event.get("type") == "test_plan"]
        self.assertTrue(plans)
        releases = [change["change"]["after"]["values"][0]
                    for plan in plans for change in plan["resource_changes"]
                    if change["address"].startswith('helm_release.manager[')]
        self.assertGreaterEqual(len(releases), 2)
        self.assertTrue(all("CI_SERVER_TLS_CA_FILE" in values for values in releases))
        self.assertTrue(any("adp/test/gitlab-runner/42" in values for values in releases))
        self.assertTrue(any("adp/test/gitlab-runner/43" in values for values in releases))
        for plan in plans:
            self.assertNotIn("glrt-synthetic-canary", json.dumps(plan))
            self.assertNotIn("glrt-untrusted-response-canary", json.dumps(plan))
            self.assertFalse(any(change["type"] in ("aws_secretsmanager_secret_version", "kubernetes_secret_v1")
                                 for change in plan["resource_changes"]))

    def test_render_refuses_insecure_manager_and_helper_tls_options(self):
        for path, old, replacement in (
            ("helm.tf", 'CI_SERVER_TLS_CA_FILE = "/secrets/ca.pem"',
             'CI_SERVER_TLS_CA_FILE = "/secrets/ca.pem"\n        GIT_SSL_NO_VERIFY = "true"'),
            ("helm.tf", 'CI_SERVER_TLS_CA_FILE = "/secrets/ca.pem"',
             'CI_SERVER_TLS_CA_FILE = "/secrets/ca.pem"\n        CI_SERVER_TLS_SKIP_VERIFY = "true"'),
            ("templates/runner.toml.tftpl", 'tls-ca-file = "/secrets/ca.pem"',
             'tls-ca-file = "/secrets/ca.pem"\n  tls-skip-verify = true'),
            ("templates/runner.toml.tftpl", '  executor = "docker-autoscaler"',
             '  executor = "docker-autoscaler"\n  environment = ["GIT_SSL_NO_VERIFY=true"]'),
        ):
            with self.subTest(replacement=replacement):
                source = self.module / path
                original = source.read_text()
                self.assertIn(old, original)
                source.write_text(original.replace(old, replacement, 1))
                try:
                    returncode, runs = render(self.module)
                    self.assertNotEqual(returncode, 0)
                    self.assertEqual(runs["runtime_tls_init_path"], "fail")
                finally:
                    source.write_text(original)

    def test_render_rejects_ssh_before_host_readiness(self):
        activation = self.module / "templates/job-network-activate.sh.tftpl"
        original = activation.read_text()
        self.assertIn("/usr/local/libexec/adp-job-host --ready\nsystemctl restart sshd.service", original)
        activation.write_text(original.replace(
            "/usr/local/libexec/adp-job-host --ready\nsystemctl restart sshd.service",
            "systemctl restart sshd.service\n/usr/local/libexec/adp-job-host --ready",
        ))
        returncode, runs = render(self.module)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["render_production_manager"], "pass")
        self.assertEqual(runs["verify_rendered_job_admission_path"], "fail")


    def test_render_rejects_reintroduced_registry_host_mapping(self):
        config = self.module / "templates/runner.toml.tftpl"
        original = config.read_text()
        hosts = '"${gitlab_host}:${gitlab_ip}", "s3.${aws_region}.amazonaws.com:${cache_ip}"'
        self.assertIn(hosts, original)
        config.write_text(original.replace(hosts, '"${gitlab_host}:${gitlab_ip}", "registry.example.test:10.0.0.11", "s3.${aws_region}.amazonaws.com:${cache_ip}"'))
        returncode, runs = render(self.module)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["verify_rendered_job_admission_path"], "fail")

    def test_render_rejects_registry_dns_mapping_in_host_boot(self):
        activation = self.module / "templates/job-network-activate.sh.tftpl"
        original = activation.read_text()
        hosts = "printf '%s %s\\n' \"${cache_ip}\" \"${cache_host}\" >> /etc/hosts"
        self.assertIn(hosts, original)
        activation.write_text(original.replace(hosts, hosts + "\nprintf '%s %s\\n' \"10.0.0.11\" \"registry.example.test\" >> /etc/hosts"))
        returncode, runs = render(self.module)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["render_shared_gitlab_and_exact_cache_route"], "fail")

    def test_global_cache_dns_is_rejected(self):
        network = self.module / "network.tf"
        original = network.read_text()
        self.assertIn("private_dns_enabled = false", original)
        network.write_text(original.replace("private_dns_enabled = false", "private_dns_enabled = true", 1))
        returncode, runs = render(self.module, "tests/security_contract.tftest.hcl")
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["qualified_network_only"], "fail")

    def test_manager_cache_alias_must_match_verified_endpoint(self):
        source = self.module / "helm.tf"
        original = source.read_text()
        alias = '''      hostAliases = [{
        ip        = try(data.aws_network_interface.endpoint["${group_id}/cache"].private_ip, "0.0.0.0")
        hostnames = ["s3.${var.aws_region}.amazonaws.com"]
      }]
'''
        self.assertIn(alias, original)
        for replacement in ('', alias.replace('"s3.${var.aws_region}.amazonaws.com"', '"s3.amazonaws.com"'),
                            alias.replace('try(data.aws_network_interface.endpoint["${group_id}/cache"].private_ip, "0.0.0.0")', '"10.0.0.11"')):
            with self.subTest(replacement=replacement):
                source.write_text(original.replace(alias, replacement, 1))
                try:
                    returncode, runs = render(self.module)
                    self.assertNotEqual(returncode, 0)
                    self.assertEqual(runs["render_shared_gitlab_and_exact_cache_route"], "fail")
                finally:
                    source.write_text(original)

    def test_job_cache_host_mapping_must_remain(self):
        activation = self.module / "templates/job-network-activate.sh.tftpl"
        original = activation.read_text()
        mapping = '''printf '%s %s\\n' "${cache_ip}" "${cache_host}" >> /etc/hosts
'''
        self.assertIn(mapping, original)
        activation.write_text(original.replace(mapping, "", 1))
        returncode, runs = render(self.module)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(runs["render_shared_gitlab_and_exact_cache_route"], "fail")

    def test_render_rejects_missing_and_over_quota_requests(self):
        source = self.module / "helm.tf"
        original = source.read_text()
        for old, replacement in (
            ('requests = { cpu = "500m", memory = "512Mi" }', 'requests = {}'),
            ('requests = { cpu = "500m", memory = "512Mi" }',
             'requests = { cpu = "2500m", memory = "512Mi" }'),
            ('requests = { cpu = "500m", memory = "512Mi" }',
             'requests = { cpu = "500m", memory = "4096Mi" }'),
            ('resources       = local.bootstrap_resources', 'resources       = {}'),
            ('resources = local.bootstrap_resources', 'resources = {}'),
        ):
            with self.subTest(replacement=replacement, old=old):
                self.assertIn(old, original)
                source.write_text(original.replace(old, replacement, 1))
                try:
                    returncode, runs = render(self.module)
                    self.assertNotEqual(returncode, 0)
                    self.assertEqual(runs["manager_pods_fit_quota"], "fail")
                finally:
                    source.write_text(original)

if __name__ == "__main__":
    unittest.main()
