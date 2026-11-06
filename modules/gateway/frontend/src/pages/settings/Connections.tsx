import JiraSystemConnection from './JiraSystemConnection';
import { deploymentSetting } from '@/config/runtime';
/**
 * Connections page — link external services (GitHub, Slack, etc.) to ADP.
 *
 * Issue #465: GitHub App install + auto-provision flow.
 * Issue #2596: GitHub App registration + lifecycle states (platform_admin gated).
 *
 * URL: /settings/connections
 *
 * On arrival with ?success=1 the page shows a success toast and refreshes
 * the connection list. On ?error=<code>&message=<msg> it shows an error toast.
 * On arrival with ?github_app=registered it shows a success toast and refetches
 * the app status.
 */

import { switchWorkspace } from "@/services/workspaces";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useToast } from "@/contexts/ToastContext";
import { FreeTierBanner } from "@/components/FreeTierBanner";
import { PostInstallPanel } from "@/components/PostInstallPanel";
import { useAuth } from "@/hooks/useAuth";
import { AdminRole } from "@/types";
import {
  deleteGitHubConnection,
  disconnectGitHubApp,
  getGitHubAppStatus,
  listConnections,
  registerManualGitHubApp,
  revalidateGitHubAppConfig,
  rotateGitHubAppKey,
  startGitHubAppRegistration,
  startGitHubInstall,
  type AppStatusResponse,
  type GitHubConnectionItem,
  type PlatformVerification,
  type RegisterManualResponse,
} from "@/services/connections";
import { GitHubTile } from "./components/GitHubTile";
import { OAuthAppSetup } from "./components/OAuthAppSetup";
import { listCredentials, setCredentialAgentConsent, type CredentialItem } from "@/services/credentials";
import { extractCredentialError } from "@/services/credentials";
import { startOAuthConnection, completeOAuthConnection, disconnectOAuthConnection, getOAuthProviders, getAtlassianConnectionStatus, prepareAtlassianConnection, startAtlassianConnection, completeAtlassianConnection, resumeAtlassianConnection, selectAtlassianSite, disconnectAtlassianConnection, type AtlassianConnectionStatus, type OAuthProviderStatus } from "@/services/oauthConnections";
import { rememberProductConnection, takeProductConnectionReturn, type ProductConnectionContext } from '@/services/productConnectionSetup';

/** Well-known org ID for the adp-default free-tier workspace. */
const ADP_DEFAULT_ORG_ID = "00000000-0000-4000-a000-000000000001";
const OAUTH_PROVIDERS = [
  { service: "github", provider: "github", name: "GitHub" },
  { service: "slack", provider: "slack", name: "Slack" },
];

type ConnectionsProps = { service?: 'github' | 'slack'; productContext?: ProductConnectionContext; onReadyChange?: (ready: boolean) => void };

export default function Connections({ service: focusedService, productContext, onReadyChange }: ConnectionsProps = {}) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialOAuthReturn = useRef(searchParams.has("oauth_consent")).current;
  const handledOAuthReturn = useRef(false);
  const toast = useToast();
  const { user, hasRole } = useAuth();

  const isPlatformAdmin = hasRole(AdminRole.PLATFORM_ADMIN);
  const showGithub = !focusedService || focusedService === 'github';
  const showAtlassian = !focusedService;
  const showSlack = !focusedService || focusedService === 'slack';
  const loadPersonalAccounts = focusedService !== 'github';
  const returnToProduct = useCallback((service?: string) => {
    const path = takeProductConnectionReturn(user, service);
    if (path) navigate(path, { replace: true });
  }, [user, navigate]);

  const [connections, setConnections] = useState<GitHubConnectionItem[]>([]);
  const [oauthCredentials, setOauthCredentials] = useState<CredentialItem[]>([]);
  const [oauthLoading, setOauthLoading] = useState(true);
  const [oauthError, setOauthError] = useState(false);
  const [oauthBusy, setOauthBusy] = useState<string | null>(null);
  const [oauthProviders, setOauthProviders] = useState<OAuthProviderStatus[]>([]);
  const [atlassianStatus, setAtlassianStatus] = useState<AtlassianConnectionStatus | null>(null);
  const [atlassianLoading, setAtlassianLoading] = useState(true);
  const [providersLoading, setProvidersLoading] = useState(true);
  const [setupProvider, setSetupProvider] = useState<string | null>(null);
  // Issue #4016: deployment-wide onboarding checks. The API omits this for
  // callers who cannot manage connections, so null means "not shown to me"
  // rather than "everything is fine".
  const [platformVerification, setPlatformVerification] =
    useState<PlatformVerification | null>(null);
  const [appStatus, setAppStatus] = useState<AppStatusResponse | null>(null);
  const [appStatusError, setAppStatusError] = useState(false);
  const [appStatusLoading, setAppStatusLoading] = useState(true);
  const [isLoading, setIsLoading] = useState(true);
  const [isInstalling, setIsInstalling] = useState(false);
  // Issue #2984: Track post-install success state to show what's-next panel
  const [showPostInstall, setShowPostInstall] = useState(false);
  // Issue #3072: Auto-switch banner state (org name + previous tenant ID)
  const [switchBanner, setSwitchBanner] = useState<{
    installed: string;
    switchedFrom: string;
  } | null>(null);
  // Issue #3360: Lift manual registration warnings to page level so they
  // survive the status refetch that unmounts the form.
  const [manualRegWarnings, setManualRegWarnings] = useState<string[]>([]);

  // -------------------------------------------------------------------------
  // Load connections + app status
  // -------------------------------------------------------------------------

  const loadConnections = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await listConnections();
      setConnections(data?.connections ?? []);
      setPlatformVerification(data?.platform_verification ?? null);
    } catch (err: unknown) {
      setConnections([]);
      const message =
        err instanceof Error ? err.message : "Failed to load connections";
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }, [toast]);

  const loadAppStatus = useCallback(async () => {
    if (!isPlatformAdmin) return;
    setAppStatusLoading(true);
    try {
      const status = await getGitHubAppStatus();
      setAppStatus(status);
      setAppStatusError(false);
    } catch {
      // Mark that we failed to fetch status so the tile can distinguish
      // "not registered" from "status unavailable" and avoid showing the
      // dangerous "Set up GitHub App" CTA that could overwrite a live App.
      setAppStatusError(true);
    } finally {
      setAppStatusLoading(false);
    }
  }, [isPlatformAdmin]);

  useEffect(() => {
    if (showGithub) { loadConnections(); loadAppStatus(); }
  }, [loadConnections, loadAppStatus, showGithub]);

  const loadOauthCredentials = useCallback(async () => {
    setOauthLoading(true);
    try {
      const credentials = await listCredentials();
      const pointers = credentials.filter((credential) =>
        credential.scope === "user" &&
        credential.credential_type === "oauth_token" &&
        credential.provider === "agentcore-identity",
      );
      setOauthCredentials(pointers);
      setOauthError(false);
      return pointers;
    } catch {
      setOauthError(true);
      throw new Error("Could not load third-party connections");
    } finally {
      setOauthLoading(false);
    }
  }, []);

  useEffect(() => {
    if (loadPersonalAccounts && !initialOAuthReturn) loadOauthCredentials().catch(() => {});
  }, [initialOAuthReturn, loadOauthCredentials, loadPersonalAccounts]);

  const loadOAuthProviders = useCallback(async () => {
    setProvidersLoading(true);
    try {
      const result = await getOAuthProviders();
      setOauthProviders(result.providers);
    } catch {
      setOauthProviders([]);
    } finally {
      setProvidersLoading(false);
    }
  }, []);

  useEffect(() => { if (loadPersonalAccounts) loadOAuthProviders(); }, [loadOAuthProviders, loadPersonalAccounts]);

  const loadAtlassianStatus = useCallback(async () => {
    setAtlassianLoading(true);
    try {
      const status = await getAtlassianConnectionStatus();
      setAtlassianStatus(status);
      return status;
    } catch {
      setAtlassianStatus(null);
      return null;
    } finally {
      setAtlassianLoading(false);
    }
  }, []);

  useEffect(() => { if (showAtlassian) loadAtlassianStatus(); }, [loadAtlassianStatus, showAtlassian]);

  const githubReady = !isLoading && connections.some(connection =>
    !connection.revocation_pending && connection.is_active_tenant !== false
    && (!connection.tenant_id || connection.tenant_id === user?.orgId)
    && connection.repository_count > 0 && connection.verification?.repositories_live === true);
  const slackReady = !oauthLoading && !oauthError && !providersLoading
    && oauthCredentials.some(credential => credential.service === 'slack' && credential.agent_consent)
    && oauthProviders.some(provider => provider.service === 'slack' && provider.connection_status === 'verified' && !provider.tool_setup_required);
  useEffect(() => { onReadyChange?.(focusedService === 'github' ? githubReady : focusedService === 'slack' && slackReady); }, [focusedService, githubReady, slackReady, onReadyChange]);

  useEffect(() => {
    const consent = searchParams.get("oauth_consent");
    if (!consent || handledOAuthReturn.current) return;
    handledOAuthReturn.current = true;
    const pending = window.sessionStorage.getItem("oauth_connection_pending");
    const returnedState = searchParams.get("atlassian_state");
    const returnedSessionSha256 = searchParams.get("atlassian_session_sha256");
    window.sessionStorage.removeItem("oauth_connection_pending");
    setSearchParams({}, { replace: true });
    if (consent !== "returned" || (!pending && !returnedState)) {
      loadOauthCredentials().catch(() => {});
      loadAtlassianStatus();
      toast.error("Consent was cancelled or expired. The account was not connected.");
      returnToProduct();
      return;
    }
    let attempt: { state: string; session_uri: string; service: string; return_to?: string; session_sha256?: string };
    try {
      if (returnedState) {
        if (!/^[A-Za-z0-9_-]{32,128}$/.test(returnedState) || !returnedSessionSha256 || !/^[0-9a-f]{64}$/.test(returnedSessionSha256)) throw new Error("Invalid session");
        attempt = { state: returnedState, session_uri: '', service: 'atlassian-mcp', session_sha256: returnedSessionSha256 };
      } else {
        attempt = JSON.parse(pending!);
        if (!attempt.state || !attempt.session_uri || !attempt.service) throw new Error("Invalid session");
      }
    } catch {
      loadOauthCredentials().catch(() => {});
      loadAtlassianStatus();
      toast.error("Consent session was lost. Please connect again.");
      returnToProduct();
      return;
    }
    setOauthBusy(attempt.service);
    if (attempt.service === 'atlassian-mcp') {
      const completion = attempt.session_sha256
        ? completeAtlassianConnection(attempt.state, undefined, attempt.session_sha256)
        : completeAtlassianConnection(attempt.state, attempt.session_uri);
      completion
        .then(loadAtlassianStatus)
        .then((status) => {
          if (!status?.connection) throw new Error('Connection not found after consent. Please refresh and try again.');
        })
        .then(() => loadOauthCredentials().catch(() => {}))
        .then(() => toast.success('Atlassian consent completed. Select a site to use tools.'))
        .catch((error: unknown) => {
          toast.error(extractCredentialError(error));
          loadAtlassianStatus();
          loadOauthCredentials().catch(() => {});
        })
        .finally(() => setOauthBusy(null));
      return;
    }
    completeOAuthConnection(attempt.state, attempt.session_uri)
      .then(loadOauthCredentials)
      .then((pointers) => {
        if (!pointers.some((credential) => credential.service === attempt.service)) {
          throw new Error("Connection not found after consent. Please refresh and try again.");
        }
        loadOAuthProviders();
        toast.success("Third-party account connected.");
        if (attempt.service === 'github' && attempt.return_to === '/settings/dev-box') navigate('/settings/dev-box', { replace: true });
      })
      .catch((error: unknown) => {
        toast.error(extractCredentialError(error));
        loadOauthCredentials().catch(() => {});
      })
      .finally(() => {
        setOauthBusy(null);
        if (attempt.service === 'github' && attempt.return_to === '/settings/dev-box') {
          // An explicitly requested Dev Box return takes precedence over abandoned Product setup.
          takeProductConnectionReturn(user, 'github');
        } else returnToProduct(attempt.service);
      });
  }, [loadAtlassianStatus, loadOauthCredentials, loadOAuthProviders, navigate, searchParams, setSearchParams, toast, returnToProduct, user]);

  const handleAtlassianConnect = async (capabilitySet: 'reads' | 'jira-writes' = 'reads') => {
    setOauthBusy('atlassian-mcp');
    try {
      if (capabilitySet === 'reads' && atlassianStatus?.pending_observation) {
        await resumeAtlassianConnection(atlassianStatus.pending_observation.attempt_id);
        const status = await loadAtlassianStatus();
        if (!status?.connection) throw new Error('Connection not found after verification. Please refresh and try again.');
        await loadOauthCredentials().catch(() => {});
        toast.success('Atlassian account verified.');
        setOauthBusy(null);
        return;
      }
      const started = await startAtlassianConnection(capabilitySet, atlassianStatus?.connection?.revision);
      if (!started.authorization_url.startsWith('https://') || !started.state || !started.session_uri) {
        throw new Error('Consent could not be started. Please try again.');
      }
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: started.state, session_uri: started.session_uri, service: 'atlassian-mcp',
      }));
      window.location.href = started.authorization_url;
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
      await loadAtlassianStatus();
      setOauthBusy(null);
    }
  };

  const handleAtlassianSite = async (siteId: string) => {
    const revision = atlassianStatus?.connection?.revision;
    if (!revision) return;
    setOauthBusy('atlassian-mcp');
    try {
      await selectAtlassianSite(revision, siteId);
      await loadAtlassianStatus();
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
      await loadAtlassianStatus();
    } finally {
      setOauthBusy(null);
    }
  };

  const handleAtlassianDisconnect = async () => {
    const revision = atlassianStatus?.connection?.revision;
    if (!revision || !window.confirm('Disconnect Atlassian from agents? New calls will be blocked; provider consent may remain active.')) return;
    setOauthBusy('atlassian-mcp');
    try {
      const result = await disconnectAtlassianConnection(revision);
      await loadAtlassianStatus();
      toast.success(result.revocation_pending ? 'Disconnected. Provider grant revocation is pending.' : 'Atlassian disconnected.');
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
      await loadAtlassianStatus();
    } finally {
      setOauthBusy(null);
    }
  };

  const handleAtlassianPrepare = async () => {
    setOauthBusy('atlassian-mcp');
    try {
      await prepareAtlassianConnection();
      await loadAtlassianStatus();
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
      await loadAtlassianStatus();
    } finally {
      setOauthBusy(null);
    }
  };

  const handleLegacyJiraDisconnect = async () => {
    if (!atlassianStatus?.connection?.selected_site_id || atlassianStatus.status !== 'ready'
      || !window.confirm('Disconnect your old Jira connection? Your selected Atlassian site will remain connected.')) return;
    const expectedRevision = atlassianStatus.connection.revision;
    setOauthBusy('jira');
    try {
      const current = await loadAtlassianStatus();
      if (current?.status !== 'ready' || !current.connection?.selected_site_id || current.connection.revision !== expectedRevision) {
        throw new Error('Atlassian connection changed or is unavailable. Your old Jira connection was kept.');
      }
      await disconnectOAuthConnection('atlassian');
      await loadOauthCredentials();
      toast.success('Old Jira connection disconnected. Your Atlassian site remains selected.');
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
      loadOauthCredentials().catch(() => {});
    } finally {
      setOauthBusy(null);
    }
  };

  // #8555: GitHub repository access for the owner's own tools (Dev Box gh/git, adp github token).
  const [githubRepositoryAccess, setGithubRepositoryAccess] = useState(false);

  const handleOAuthConnect = async (provider: string, service: string, chatWrite = false, reconnect = false, repositoryAccess = false) => {
    setOauthBusy(service);
    try {
      const started = repositoryAccess
        ? await startOAuthConnection(provider, false, reconnect, true)
        : reconnect ? await startOAuthConnection(provider, chatWrite, true) : chatWrite ? await startOAuthConnection(provider, true) : await startOAuthConnection(provider);
      if (!started.authorization_url.startsWith("https://") || !started.state || !started.session_uri) {
        throw new Error("Consent could not be started. Please try again.");
      }
      window.sessionStorage.setItem("oauth_connection_pending", JSON.stringify({
        state: started.state, session_uri: started.session_uri, service,
      }));
      if (service === 'github' || service === 'slack') rememberProductConnection(productContext, service, user);
      window.location.href = started.authorization_url;
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
      setOauthBusy(null);
    }
  };

  const handleOAuthDisconnect = async (provider: string, service: string) => {
    if (!window.confirm(`Disconnect ${service} from agents? New authorized calls will be blocked; provider consent may remain active.`)) return;
    setOauthBusy(service);
    try {
      await disconnectOAuthConnection(provider);
      await loadOauthCredentials();
      await loadOAuthProviders();
      toast.success("Third-party account disconnected from ADP. Provider consent may remain active.");
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
    } finally {
      setOauthBusy(null);
    }
  };

  const handleOAuthConsent = async (credential: CredentialItem) => {
    setOauthBusy(credential.service);
    try {
      const updated = await setCredentialAgentConsent(credential.id, !credential.agent_consent);
      setOauthCredentials((previous) => previous.map((item) => item.id === credential.id ? updated : item));
      await loadOAuthProviders();
    } catch (error: unknown) {
      toast.error(extractCredentialError(error));
    } finally {
      setOauthBusy(null);
    }
  };

  // -------------------------------------------------------------------------
  // Handle redirect back from GitHub (success or error or app registered)
  // -------------------------------------------------------------------------

  useEffect(() => {
    const success = searchParams.get("success");
    const errorCode = searchParams.get("error");
    const errorMessage = searchParams.get("message");
    const githubApp = searchParams.get("github_app");

    if (success === "1") {
      // Issue #3072: If auto-switched after org install, show the workspace banner
      // instead of the generic toast.
      const installed = searchParams.get("installed");
      const switchedFrom = searchParams.get("switched_from");
      if (installed && switchedFrom) {
        setSwitchBanner({ installed, switchedFrom });
      } else {
        toast.success(
          "GitHub connected! You can now trigger agents from this org.",
        );
      }
      setShowPostInstall(true);
      loadConnections();
      setSearchParams({}, { replace: true });
      returnToProduct('github');
    } else if (githubApp === "registered") {
      // Issue #2708: the register flow appends login_enabled=false when the
      // broker OAuth-secret write-through failed (e.g. IAM). Warn instead of
      // reporting plain success so the operator knows login isn't wired.
      if (searchParams.get("login_enabled") === "false") {
        toast.warning(
          'GitHub App registered, but "Sign in with GitHub" is not wired yet. See the connection status below.',
        );
      } else {
        toast.success("GitHub App registered successfully!");
      }
      loadAppStatus();
      setSearchParams({}, { replace: true });
      returnToProduct('github');
    } else if (errorCode) {
      const displayMessage =
        errorMessage ||
        `GitHub connection failed (${errorCode}). Please try again.`;
      toast.error(displayMessage);
      setSearchParams({}, { replace: true });
      returnToProduct('github');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -------------------------------------------------------------------------
  // Install handler — POST install-start → redirect to GitHub
  // -------------------------------------------------------------------------

  const handleInstall = async (installationId?: number) => {
    setIsInstalling(true);
    try {
      const result = await startGitHubInstall();
      rememberProductConnection(productContext, 'github', user);
      if (installationId) {
        const base = (deploymentSetting('VITE_API_URL') || '/api').replace(/\/$/, '');
        const params = new URLSearchParams({ installation_id: String(installationId), state: result.state_token });
        window.location.href = `${base}/admin/connections/github/install-callback?${params}`;
      } else {
        window.location.href = result.install_url;
      }
      // Note: page navigates away; isInstalling stays true intentionally
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to start GitHub install flow";
      toast.error(message);
      setIsInstalling(false);
    }
  };

  // -------------------------------------------------------------------------
  // Disconnect handler (installation-level)
  // -------------------------------------------------------------------------

  const handleDisconnect = async (installationId: number) => {
    try {
      const result = await deleteGitHubConnection(installationId);
      if (result?.warning) {
        toast.warning(result.warning);
      } else if (result?.provider_revoked === false) {
        toast.success("Local access revoked. The installation remains at GitHub.");
      } else {
        toast.success("GitHub installation disconnected.");
      }
      await loadConnections();
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to disconnect installation";
      toast.error(message);
      throw err; // Let InstallationCard reset its state
    }
  };

  // -------------------------------------------------------------------------
  // Registration handler — manifest flow (Issue #2596)
  // -------------------------------------------------------------------------

  const handleRegister = async (ownerType: "user" | "org", org?: string) => {
    try {
      const result = await startGitHubAppRegistration({
        owner_type: ownerType,
        org,
      });
      if (result.status === "already_registered") {
        toast.success("GitHub App is already registered.");
        await loadAppStatus();
        return;
      }
      if (result.post_url && result.manifest) {
        rememberProductConnection(productContext, 'github', user);
        // Submit manifest to GitHub in the same tab (preserves SPA session for
        // the callback redirect). State nonce is included as a form field so
        // GitHub echoes it back as ?state= on the redirect.
        submitManifestToGitHub(
          result.post_url,
          JSON.stringify(result.manifest),
          result.state,
        );
      }
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to start registration";
      toast.error(message);
      throw err;
    }
  };

  // -------------------------------------------------------------------------
  // Rotate key handler (Issue #2596)
  // -------------------------------------------------------------------------

  const handleRotateKey = async () => {
    try {
      const result = await rotateGitHubAppKey();
      toast.success(result.message || "Key rotated successfully.");
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to rotate key";
      toast.error(message);
      throw err;
    }
  };

  // -------------------------------------------------------------------------
  // Re-validate App config handler (Issue #4017)
  //
  // Refetches connections afterwards so the verification panel reflects the
  // result the operator was just shown — the backend clears its caches, but the
  // page holds its own copy of the last response.
  // -------------------------------------------------------------------------

  const handleRevalidateApp = async () => {
    try {
      const result = await revalidateGitHubAppConfig();
      const drifted =
        result.app_webhook_url_matches === false ||
        result.app_permissions_match === false ||
        result.app_events_match === false;
      if (drifted) {
        toast.error(result.message || "App configuration has drifted.");
      } else if (!result.checked) {
        // Not an error: unknown is not the same as broken.
        toast.info(
          result.message || "Could not read the App configuration from GitHub.",
        );
      } else {
        toast.success(result.message || "App configuration matches.");
      }
      await loadConnections();
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to re-validate App configuration";
      toast.error(message);
      throw err;
    }
  };

  // -------------------------------------------------------------------------
  // Disconnect app handler (Issue #2596)
  // -------------------------------------------------------------------------

  const handleDisconnectApp = async () => {
    try {
      const result = await disconnectGitHubApp();
      toast.success(result.message || "GitHub App disconnected.");
      setAppStatus({
        registered: false,
        app_slug: null,
        app_id: null,
        owner_type: null,
        created_at: null,
      });
      setConnections([]);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to disconnect app";
      toast.error(message);
      throw err;
    }
  };

  // -------------------------------------------------------------------------
  // Switch tenant handler (Issue #3071)
  // -------------------------------------------------------------------------

  const handleSwitchTenant = async (tenantId: string) => {
    try {
      await switchWorkspace(tenantId);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to switch workspace";
      toast.error(message);
      throw err;
    }
  };

  // -------------------------------------------------------------------------
  // Manual registration handler (Issue #3360)
  // -------------------------------------------------------------------------

  const handleRegisterManual = async (data: {
    app_id: string;
    private_key: string;
    webhook_secret?: string;
    client_id?: string;
    client_secret?: string;
  }): Promise<RegisterManualResponse> => {
    const result = await registerManualGitHubApp(data);
    // Lift warnings to page level BEFORE refetching status, so they survive
    // the form unmount that happens when status shows the App as registered.
    if (result.warnings?.length) {
      setManualRegWarnings(result.warnings);
    }
    if (result.registered) {
      toast.success("GitHub App connected successfully!");
      await loadAppStatus();
    }
    return result;
  };

  // -------------------------------------------------------------------------
  // Issue #3072: Switch-back handler for auto-switch banner
  // -------------------------------------------------------------------------

  const handleSwitchBack = async () => {
    if (!switchBanner) return;
    try {
      await switchWorkspace(switchBanner.switchedFrom);
      setSwitchBanner(null);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to switch workspace";
      toast.error(message);
    }
  };

  const renderOAuthAccount = ({ service, provider, name }: typeof OAUTH_PROVIDERS[number]) => {
    const credential = oauthCredentials.find((item) => item.service === service);
    const scopes = (credential?.scopes as Record<string, unknown> | null)?.oauth_scopes;
    const readiness = oauthProviders.find((item) => item.provider === provider);
    const status = readiness?.status;
    const reconnectRequired = readiness?.connection_status === "reconnect_required";
    const registered = !providersLoading && status === "registered";
    const needsSetup = !providersLoading && status === "setup_required";
    return (
      <div key={service} className={service === "github" ? "space-y-2" : "rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-gray-900"}>
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-gray-900 dark:text-gray-100">{service === "github" ? "Your account" : name}</h3>
          <span>{credential ? reconnectRequired ? "Reconnect required" : readiness?.connection_status === "verified" ? "Account verified" : "Connected" : providersLoading ? "Checking setup…" : needsSetup ? "Setup required" : registered ? "Not connected" : "Unavailable"}</span>
        </div>
        {!credential && registered && <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">Connect lets ADP agents use this account with the permissions you approve. You can pause access here.</p>}
        {!credential && registered && provider === "github" && <label className="mt-2 flex items-start gap-2 text-sm text-gray-600 dark:text-gray-300">
          <input type="checkbox" checked={githubRepositoryAccess} disabled={oauthBusy !== null}
            onChange={(event) => setGithubRepositoryAccess(event.target.checked)} aria-label="Include repository access" />
          <span>Include repository access, so <code>gh</code> and <code>git</code> on your Dev Box and <code>adp github token</code> on any machine act as you. You can grant it later too.</span>
        </label>}
        {(credential || registered) && <button type="button" disabled={oauthBusy !== null} onClick={() => credential
          ? handleOAuthDisconnect(provider, service)
          : handleOAuthConnect(provider, service, false, false, provider === "github" && githubRepositoryAccess)}
          className="mt-2 rounded border px-3 py-1 text-sm disabled:opacity-50"
        >
          {oauthBusy === service ? "Working…" : credential ? `Disconnect ${name}` : `Connect ${name}`}
        </button>}
        {credential && reconnectRequired && <div className="mt-2 text-sm">
          <p>Reconnect to verify this account for tools.{provider === "atlassian" && " Jira will ask permission to verify your identity, read issues, and keep access working between sessions."}</p>
          <button type="button" disabled={oauthBusy !== null || !registered || !credential.agent_consent}
            onClick={() => handleOAuthConnect(provider, service, false, true)} className="mt-2 rounded border px-3 py-1 disabled:opacity-50">Reconnect {name}</button>
        </div>}
        {credential && readiness?.connection_status === "verified" && <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
          {readiness.tool_setup_required ? "Your account is verified. A platform administrator still needs to activate tools for this service." : "Your account is verified. Tool access follows your workspace permissions and consent."}
        </p>}
        {needsSetup && !isPlatformAdmin && <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">A platform administrator needs to set up {name} before you can connect.</p>}
        {!providersLoading && !registered && !needsSetup && <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
          Connection setup is unavailable. <button type="button" onClick={loadOAuthProviders} className="underline">Check again</button>
        </p>}
        {isPlatformAdmin && needsSetup && focusedService !== service && <button type="button" className="mt-2 rounded border px-3 py-1 text-sm"
          onClick={() => setSetupProvider(setupProvider === provider ? null : provider)}>Set up {name}</button>}
        {credential && <label className="mt-2 flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
          <input type="checkbox" checked={credential.agent_consent} disabled={oauthBusy !== null}
            onChange={() => handleOAuthConsent(credential)} aria-label={`Allow agents to use ${name}`} />
          Allow agents to use this account
        </label>}
        {(credential || (isPlatformAdmin && !needsSetup)) && <details className="mt-3 text-sm"><summary className="cursor-pointer">Connection details</summary>
        {provider === "slack" && credential?.agent_consent && <div className="mt-2">
          <button type="button" disabled={oauthBusy !== null || !registered}
            onClick={() => handleOAuthConnect(provider, service, true)}
            className="rounded border px-3 py-1 text-sm disabled:opacity-50"
          >
            {oauthBusy === service ? "Working…" : "Re-consent Slack test-channel posting"}
          </button>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
            Requests optional chat:write permission. Posting remains restricted to an approved test channel and is not enabled by consent alone.
          </p>
        </div>}
        {provider === "github" && credential && (Array.isArray(scopes) && scopes.includes("repo")
          ? <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">Repository access granted: <code>gh</code> and <code>git</code> on your Dev Box and <code>adp github token</code> act as you.</p>
          : <div className="mt-2">
            <button type="button" disabled={oauthBusy !== null || !registered || !credential.agent_consent}
              onClick={() => handleOAuthConnect(provider, service, false, false, true)}
              className="rounded border px-3 py-1 text-sm disabled:opacity-50"
            >
              {oauthBusy === service ? "Working…" : "Grant repository access"}
            </button>
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
              Asks GitHub for repository and workflow permission so your Dev Box and <code>adp github token</code> can push as you.{!credential.agent_consent && " Allow agents to use this account first."}
            </p>
          </div>)}
        <p className="text-sm text-gray-600 dark:text-gray-300">
          Scopes: {credential && Array.isArray(scopes) ? scopes.join(", ") : "None granted"}
        </p>
        <p className="text-sm text-gray-600 dark:text-gray-300">
          Last used: {credential?.last_used_at ? new Date(credential.last_used_at).toLocaleString() : "Never"}
        </p>
        {isPlatformAdmin && !needsSetup && <button type="button" className="mt-2 underline"
          onClick={() => setSetupProvider(setupProvider === provider ? null : provider)}>App settings</button>}
        </details>}
        {isPlatformAdmin && (setupProvider === provider || (focusedService === service && needsSetup)) && <OAuthAppSetup provider={provider} name={name} onRegistered={loadOAuthProviders} />}
      </div>
    );
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className={focusedService ? 'space-y-4' : 'mx-auto max-w-3xl px-4 py-8'}>
      {/* Page header */}
      {!focusedService && <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          Connections
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Connect external services to enable agent triggers and integrations.
        </p>
      </div>}

      {/* Issue #3072: Auto-switch confirmation banner */}
      {switchBanner && (
        <div
          className="mb-6 flex items-center justify-between rounded-lg border border-green-200 bg-green-50 px-4 py-3 dark:border-green-800 dark:bg-green-950"
          role="status"
          data-testid="switch-banner"
        >
          <p className="text-sm font-medium text-green-800 dark:text-green-200">
            <strong>{switchBanner.installed}</strong> connected &mdash;
            select this organization above to start working in it.
          </p>
          <div className="flex items-center gap-2">
            <button
              onClick={handleSwitchBack}
              className="text-sm font-medium text-green-700 underline hover:text-green-900 dark:text-green-300 dark:hover:text-green-100"
              data-testid="switch-back-button"
            >
              Switch back
            </button>
            <button
              onClick={() => setSwitchBanner(null)}
              className="ml-2 text-green-600 hover:text-green-800 dark:text-green-400 dark:hover:text-green-200"
              aria-label="Dismiss"
              data-testid="dismiss-banner-button"
            >
              &times;
            </button>
          </div>
        </div>
      )}

      {/* Free tier banner — shown when user is on adp-default */}
      {!focusedService && user?.orgId === ADP_DEFAULT_ORG_ID && <FreeTierBanner />}

      {/* Issue #2984: Post-install what's-next panel */}
      {!focusedService && showPostInstall && (
        <div className="mb-6">
          <PostInstallPanel
            repoCount={
              connections.reduce(
                (sum, c) => sum + (c.repository_count || 0),
                0,
              ) || undefined
            }
          />
        </div>
      )}

      {/* Issue #3360: Page-level warnings from manual registration */}
      {manualRegWarnings.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-700 dark:bg-amber-900/20">
          <div className="flex items-start justify-between">
            <p className="mb-2 text-sm font-medium text-amber-800 dark:text-amber-200">
              Configuration warnings
            </p>
            <button
              type="button"
              onClick={() => setManualRegWarnings([])}
              className="text-sm text-amber-600 hover:text-amber-800 dark:text-amber-400 dark:hover:text-amber-200"
              aria-label="Dismiss warnings"
            >
              &times;
            </button>
          </div>
          <ul className="list-inside list-disc space-y-1 text-xs text-amber-700 dark:text-amber-300">
            {manualRegWarnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {/* GitHub section */}
      <section aria-label="Service connections" className="space-y-6">
        {showGithub && <GitHubTile
          setupOnly={!!focusedService}
          connections={connections}
          isLoading={isLoading || (isPlatformAdmin && appStatusLoading)}
          onInstall={() => handleInstall()}
          onConnectExisting={handleInstall}
          onDisconnect={handleDisconnect}
          isInstalling={isInstalling}
          isPlatformAdmin={isPlatformAdmin}
          appStatus={appStatus}
          appStatusError={appStatusError}
          onRegister={handleRegister}
          onRotateKey={handleRotateKey}
          onDisconnectApp={handleDisconnectApp}
          onSwitchTenant={handleSwitchTenant}
          onRegisterManual={isPlatformAdmin ? handleRegisterManual : undefined}
          platformVerification={platformVerification}
          onRevalidateApp={isPlatformAdmin ? handleRevalidateApp : undefined}
          personalAccount={!focusedService && !oauthLoading && !oauthError ? renderOAuthAccount(OAUTH_PROVIDERS[0]) : null}
        />}
        {focusedService === 'github' && <button type="button" className="underline" disabled={isLoading || isInstalling} onClick={() => { void loadConnections(); void loadAppStatus(); }}>Refresh GitHub connection</button>}

        <div className="space-y-3">
          {loadPersonalAccounts && oauthError && <p role="alert">Could not load third-party connections. <button type="button" className="underline" onClick={() => { void loadOauthCredentials().catch(() => {}); void loadOAuthProviders(); }}>Retry connection status</button></p>}
          {loadPersonalAccounts && oauthLoading && <p role="status">Loading third-party connections…</p>}
          {showAtlassian && <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-gray-900" aria-label="Atlassian — Jira & Confluence">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">Atlassian — Jira &amp; Confluence</h3>
              <span role="status">{atlassianLoading ? 'Checking connection…' : atlassianStatus?.status === 'policy_blocked' ? 'Policy approval needed' : atlassianStatus?.status === 'setup_required' ? 'Admin setup needed' : atlassianStatus?.status === 'consent_required' ? 'Account verification needed' : atlassianStatus?.status !== 'ready' ? 'Unavailable' : !atlassianStatus.connection ? 'Not connected' : !atlassianStatus.connection.selected_site_id ? 'Choose a site' : 'Site selected'}</span>
            </div>
            {!atlassianLoading && atlassianStatus?.status !== 'ready' && <p className="mt-2 text-sm" role="alert">
              {isPlatformAdmin && atlassianStatus?.status === 'setup_required' && atlassianStatus.owner === 'platform_admin'
                ? 'Complete and verify the deployment setup before users connect.'
                : isPlatformAdmin && atlassianStatus?.status === 'unavailable' && atlassianStatus.owner === 'platform_admin'
                  ? 'Reconcile the deployment configuration before trying setup again.'
                  : atlassianStatus?.action || 'Atlassian connection status is unavailable. Try again later.'}
            </p>}
            {isPlatformAdmin && atlassianStatus?.status === 'setup_required' && atlassianStatus.owner === 'platform_admin' && <button type="button" disabled={oauthBusy !== null} onClick={handleAtlassianPrepare} className="mt-2 rounded border px-3 py-1 text-sm disabled:opacity-50">Prepare Atlassian connection</button>}
            {!atlassianLoading && <button type="button" disabled={oauthBusy !== null} onClick={loadAtlassianStatus} className="mt-2 ml-2 underline disabled:opacity-50">Refresh Atlassian status</button>}
            {!atlassianLoading && (atlassianStatus?.status === 'ready' || atlassianStatus?.status === 'consent_required') && <>
              {!atlassianStatus.connection && <p className="mt-2 text-sm">{atlassianStatus.pending_observation ? 'Your Atlassian consent is saved. Finish connecting to verify your account.' : 'Connect using your Atlassian login and consent. No app credentials or MCP setup are needed.'}</p>}
              <button type="button" disabled={oauthBusy !== null} onClick={() => handleAtlassianConnect()} className="mt-2 ml-2 rounded border px-3 py-1 text-sm disabled:opacity-50">
                {atlassianStatus.pending_observation ? 'Finish connecting Atlassian' : atlassianStatus.connection ? 'Reconnect Atlassian' : 'Connect Atlassian'}
              </button>
              {atlassianStatus.connection && <>
                {atlassianStatus.connection.sites.length > 0 && <label className="mt-3 block text-sm">Atlassian site
                  <select value={atlassianStatus.connection.selected_site_id ?? ''} disabled={oauthBusy !== null} onChange={(event) => handleAtlassianSite(event.target.value)} className="ml-2 rounded border p-1 dark:bg-gray-900">
                    <option value="" disabled>Choose a site</option>
                    {atlassianStatus.connection.sites.map((site) => <option key={site} value={site}>{site}</option>)}
                  </select>
                </label>}
                {atlassianStatus.connection.sites.length === 0 && <p className="mt-2 text-sm">No eligible sites were returned. Reconnect to check your Atlassian access.</p>}
                {atlassianStatus.connection.selected_site_id && <p className="mt-2 text-sm">Your selected site is saved. Tool access depends on workspace permissions and service availability.</p>}
                {atlassianStatus.capabilities?.jira_writes === true && <button type="button" disabled={oauthBusy !== null} onClick={() => handleAtlassianConnect('jira-writes')} className="mt-2 rounded border px-3 py-1 text-sm disabled:opacity-50">Request Jira write access</button>}
                <button type="button" disabled={oauthBusy !== null} onClick={handleAtlassianDisconnect} className="mt-2 ml-2 rounded border px-3 py-1 text-sm disabled:opacity-50">Disconnect Atlassian</button>
              </>}
            </>}
            {isPlatformAdmin && <JiraSystemConnection />}
            {!oauthLoading && !oauthError && oauthCredentials.some((credential) => credential.service === 'jira') && <div className="mt-3 text-sm">
              <p>Your existing Jira connection is unchanged while you move to Atlassian. Cancelling or failing consent does not remove it.</p>
              {!atlassianLoading && atlassianStatus?.status === 'ready' && atlassianStatus.connection?.selected_site_id && <button type="button" disabled={oauthBusy !== null} onClick={handleLegacyJiraDisconnect} className="mt-2 underline disabled:opacity-50">Disconnect old Jira connection</button>}
            </div>}
          </div>}
          {showSlack && !oauthLoading && !oauthError && OAUTH_PROVIDERS.filter(({ service }) => service !== "github").map(renderOAuthAccount)}
          {focusedService === 'slack' && <button type="button" className="underline" disabled={oauthBusy !== null} onClick={() => { void loadOauthCredentials().catch(() => {}); void loadOAuthProviders(); }}>Refresh Slack connection</button>}
        </div>
      </section>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Helper: submit manifest to GitHub via form POST (same tab)
// ---------------------------------------------------------------------------

function submitManifestToGitHub(
  postUrl: string,
  manifest: string,
  state?: string | null,
): void {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = postUrl;
  // No target="_blank" — stay in the same tab so the callback redirect
  // lands in a window that already has the SPA auth session (Issue #2682).

  const manifestInput = document.createElement("input");
  manifestInput.type = "hidden";
  manifestInput.name = "manifest";
  manifestInput.value = manifest;
  form.appendChild(manifestInput);

  // GitHub echoes the state field back as ?state= on the post-create redirect.
  // Without this, the callback receives no state and fails with missing_state.
  if (state) {
    const stateInput = document.createElement("input");
    stateInput.type = "hidden";
    stateInput.name = "state";
    stateInput.value = state;
    form.appendChild(stateInput);
  }

  document.body.appendChild(form);
  form.submit();
  document.body.removeChild(form);
}
