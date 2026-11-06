#!/usr/bin/env python3
"""Run as root on GitLab after key provisioning. Installs the ADP login provider."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import boto3

BEGIN = "# BEGIN ADP PORTAL SSO"
END = "# END ADP PORTAL SSO"


def configure(environment, region, portal_url, guard_file, cli_guard_file=None):
    parsed = urlsplit(portal_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("portal-url must be the HTTPS ADP origin")
    portal_url = portal_url.rstrip("/")
    ssm = boto3.client("ssm", region_name=region)
    public = ssm.get_parameter(Name=f"/adp/{environment}/gitlab/jwt-public-key")[
        "Parameter"
    ]["Value"]
    Path("/etc/gitlab/adp-jwt-public-key.pem").write_text(public)
    gitlab_url = ssm.get_parameter(Name=f"/adp/{environment}/gitlab/url")["Parameter"][
        "Value"
    ]
    config = {
        "issuer": f"urn:adp:gateway:{environment}",
        "audience": f"adp-gitlab-{environment}",
        "relative_root": urlsplit(gitlab_url).path.rstrip("/"),
        "cli_audience": f"adp-gitlab-cli-{environment}",
    }
    runtime_config = Path(
        "/opt/gitlab/embedded/service/gitlab-rails/config/adp-sso.json"
    )
    if runtime_config.exists():
        previous = json.loads(runtime_config.read_text())
        if previous.get("cli_application_id"):
            config["cli_application_id"] = previous["cli_application_id"]
    runtime_config.write_text(json.dumps(config))
    runtime_config.chmod(0o644)
    for file in ("adp-jwt-public-key.pem",):
        os.chmod("/etc/gitlab/" + file, 0o644)
    destination = Path(
        "/opt/gitlab/embedded/service/gitlab-rails/config/initializers/adp_jwt_validation.rb"
    )
    destination.write_bytes(Path(guard_file).read_bytes())
    os.chmod(destination, 0o644)
    cli_guard = (
        Path(cli_guard_file)
        if cli_guard_file
        else Path(guard_file).with_name("adp_cli_oauth.rb")
    )
    if not cli_guard.is_file():
        raise ValueError(
            "The GitLab CLI OAuth initializer must accompany the browser guard"
        )
    cli_destination = destination.with_name("adp_cli_oauth.rb")
    cli_destination.write_bytes(cli_guard.read_bytes())
    cli_destination.chmod(0o644)
    path = Path("/etc/gitlab/gitlab.rb")
    source = path.read_text()
    if BEGIN in source:
        start = source.index(BEGIN)
        finish = source.index(END, start) + len(END)
        source = source[:start] + source[finish:]
    block = f"""{BEGIN}
gitlab_rails['omniauth_enabled'] = true
# Accounts and identities are provisioned by ADP before sign-in. No email linking.
gitlab_rails['omniauth_allow_single_sign_on'] = []
gitlab_rails['omniauth_auto_link_user'] = []
gitlab_rails['omniauth_block_auto_created_users'] = true
gitlab_rails['omniauth_auto_sign_in_with_provider'] = 'jwt'
# Exclude query strings containing login credentials from nginx access logs.
nginx['log_format'] = '$remote_addr - $remote_user [$time_local] "$request_method $uri $server_protocol" $status $body_bytes_sent'
gitlab_rails['omniauth_providers'] = Array(gitlab_rails['omniauth_providers']).reject {{ |p| p[:name] == 'jwt' }}
gitlab_rails['omniauth_providers'] << {{
  name: 'jwt', label: 'ADP', args: {{
    algorithm: 'RS256', secret: File.read('/etc/gitlab/adp-jwt-public-key.pem'),
    uid_claim: 'sub', required_claims: %w[iss aud sub iat exp jti email name return_path], valid_within: 65,
    info_map: {{ name: 'name', email: 'email', nickname: 'username' }},
    auth_url: {json.dumps(portal_url + "/gitlab-launch")}
  }}
}}
{END}
"""
    path.write_text(source.rstrip() + "\n\n" + block)
    os.chmod(path, 0o600)
    subprocess.run(["gitlab-ctl", "reconfigure"], check=True)
    seeded = subprocess.run(
        [
            "gitlab-rails",
            "runner",
            """
settings = ApplicationSetting.current
settings.update!(signup_enabled: false, default_project_visibility: 0, default_group_visibility: 0,
  restricted_visibility_levels: [10, 20], user_default_external: true)
path = Rails.root.join('config/adp-sso.json')
config = JSON.parse(File.read(path))
app = if config['cli_application_id']
  Doorkeeper.config.application_model.find(config['cli_application_id'])
else
  # Recover a completed create if a previous root-side config write failed.
  model = Doorkeeper.config.application_model
  matches = model.where(name: 'ADP CLI', owner_id: nil, owner_type: nil,
    organization_id: Organizations::Organization.default_organization.id)
  raise 'Ambiguous ADP CLI application' if matches.count > 1
  matches.first || model.create!(name: 'ADP CLI',
    redirect_uri: 'http://localhost:7171/auth/redirect', confidential: false,
    scopes: AdpCliOauth::SCOPES, organization_id: Organizations::Organization.default_organization.id)
end
raise 'Unexpected ADP CLI application' unless app.name == 'ADP CLI' && !app.confidential? &&
  app.owner_id.nil? && app.scopes.to_s == AdpCliOauth::SCOPES && app.redirect_uri == 'http://localhost:7171/auth/redirect'
puts "ADP_CLI_APPLICATION_ID=#{app.id}"
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # gitlab-rails drops to the git service account. Keep the issuer/public-key
    # configuration root-owned; only the root bootstrap process may update it.
    matches = re.findall(
        r"^ADP_CLI_APPLICATION_ID=([1-9][0-9]*)$", seeded.stdout, re.MULTILINE
    )
    if len(matches) != 1:
        raise RuntimeError("GitLab did not return one CLI application identifier")
    config["cli_application_id"] = int(matches[0])
    runtime_config.write_text(json.dumps(config))
    runtime_config.chmod(0o644)
    # runit's default wait is shorter than Puma's graceful shutdown under load.
    subprocess.run(
        ["gitlab-ctl", "restart", "puma"],
        check=True,
        env={**os.environ, "SVWAIT": "120"},
    )
    print(
        "ADP portal sign-in configured. Verify login before enabling the frontend feature."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["dev", "test", "prod"])
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--guard-file", required=True)
    parser.add_argument(
        "--cli-guard-file", help="Defaults to adp_cli_oauth.rb beside the browser guard"
    )
    args = parser.parse_args()
    configure(
        args.environment,
        args.region,
        args.portal_url,
        args.guard_file,
        args.cli_guard_file,
    )
