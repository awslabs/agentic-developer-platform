import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { Hash } from "@smithy/hash-node";
import { SignatureV4 } from "@smithy/signature-v4";

interface BrokerResponse {
  token?: string;
  expires_at?: string;
  identity?: "default" | "review";
}

export interface BrokerCredential {
  token: string;
  expiresAt: number;
  identity: "default" | "review";
}

export class TokenBroker {
  private token = "";
  private expiresAt = 0;

  constructor(
    private readonly gatewayEndpoint: string,
    private readonly region: string,
    private readonly installationId: number,
    private readonly repository: string,
    private readonly invocationId: string,
    private readonly identity: "default" | "review" = "default",
  ) {}

  async getToken(): Promise<string> {
    return (await this.getCredential()).token;
  }

  async getCredential(): Promise<BrokerCredential> {
    if (this.token && this.expiresAt - Date.now() > 10 * 60 * 1000) {
      return { token: this.token, expiresAt: this.expiresAt, identity: this.grantedIdentity };
    }
    const [repoOwner, repoName] = this.repository.split("/");
    if (!repoOwner || !repoName) throw new Error("repository is invalid");
    const endpoint = `${this.gatewayEndpoint.replace(/\/+$/, "")}/internal/v1/github-installation-token`;
    const body = JSON.stringify({
      installation_id: this.installationId,
      repo_owner: repoOwner,
      repo_name: repoName,
      invocation_id: this.invocationId,
      purpose: "independent agent-codex-reviewer GitHub access",
      identity: this.identity,
    });
    const url = new URL(endpoint);
    const signer = new SignatureV4({
      credentials: defaultProvider(),
      region: this.region,
      service: "execute-api",
      sha256: Hash.bind(null, "sha256"),
    });
    const signed = await signer.sign({
      method: "POST",
      protocol: url.protocol,
      hostname: url.hostname,
      path: url.pathname,
      query: {},
      headers: { host: url.hostname, "content-type": "application/json" },
      body,
    });
    const response = await fetch(endpoint, {
      method: "POST",
      headers: signed.headers as Record<string, string>,
      body,
      signal: AbortSignal.timeout(20_000),
    });
    if (!response.ok) {
      throw new Error(
        `GitHub token broker returned ${response.status}: ${await response.text()}`,
      );
    }
    const result = (await response.json()) as BrokerResponse;
    const expiresAt = Date.parse(result.expires_at ?? "");
    if (
      !result.token ||
      !Number.isFinite(expiresAt) ||
      !["default", "review"].includes(result.identity ?? "")
    ) {
      throw new Error("GitHub token broker returned an incomplete response");
    }
    this.token = result.token;
    this.expiresAt = expiresAt;
    this.grantedIdentity = result.identity as "default" | "review";
    return { token: this.token, expiresAt, identity: this.grantedIdentity };
  }

  private grantedIdentity: "default" | "review" = "default";
}
