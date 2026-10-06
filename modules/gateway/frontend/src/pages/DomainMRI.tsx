import { useEffect, useRef } from "react";
import { getAccessToken, clearTokens } from "@/services/auth";
import { deploymentSetting } from "@/config/runtime";
import markup from "./domain-mri/markup.html?raw";
import styles from "./domain-mri/styles.css?raw";
import { mountDomainMRI } from "./domain-mri/mount";

export default function DomainMRI() {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = host.current!;
    const root = element.shadowRoot || element.attachShadow({ mode: "open" });
    // Only repository-owned static markup; Task output is rendered as text or
    // inside a sandboxed report iframe. Shadow DOM isolates the demo's styles.
    root.innerHTML = `<style>${styles}</style>${markup}`;
    const lifetime = new AbortController();
    const fetcher: typeof fetch = async (input, init = {}) => {
      const token = getAccessToken();
      if (!token) {
        window.location.assign("/login");
        throw Error("Please sign in again.");
      }
      const path = String(input);
      const response = await fetch(
        (deploymentSetting("VITE_API_URL") || "/api") + path,
        {
          ...init,
          signal: init.signal
            ? AbortSignal.any([init.signal, lifetime.signal])
            : lifetime.signal,
          headers: { ...init.headers, Authorization: `Bearer ${token}` },
          cache: "no-store",
          redirect: "error",
        },
      );
      if (response.status === 401) {
        clearTokens();
        window.location.assign("/login");
      }
      return response;
    };
    // This is a storage namespace, never an authorization decision. The backend
    // independently validates Cognito and enforces Task ownership.
    let storageKey = "domain-mri:";
    try {
      const claims = JSON.parse(
        atob(
          getAccessToken()!.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"),
        ),
      );
      storageKey += `${claims.sub}:${claims["custom:org_id"] || claims.org_id || ""}:`;
    } catch {
      /* Server authentication remains authoritative. */
    }
    const cleanup = mountDomainMRI(root, fetcher, storageKey);
    void fetcher("/me/persona-models").then(async response => {
      if (!response.ok) return;
      const preferences = await response.json();
      const preference = preferences.entries?.find((entry: { persona_key: string }) => entry.persona_key === "agent-task-cyber");
      if (lifetime.signal.aborted) return;
      const model = preference?.effective_model_id || "Select a model in Agent Models";
      root.querySelector(".model-pill")!.textContent = model === "us.anthropic.claude-opus-5" ? "Claude Opus 5" : model;
    }).catch(() => {});
    return () => {
      lifetime.abort();
      cleanup();
      root.replaceChildren();
    };
  }, []);
  return <div ref={host} />;
}
