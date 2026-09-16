#!/usr/bin/env bash
# Shared scope resolution. Updates never install an absent optional module.
resolve_deploy_scope() {
  DEPLOY_GATEWAY=true
  DEPLOY_WEBHOOK=true
  DEPLOY_FACTORY=true
  DEPLOY_AGENT_CONTEXT="${AGENT_CONTEXT_ENABLED:-false}"
  if [ "${UPDATE_MODE:-false}" = true ]; then
    DEPLOY_GATEWAY=false; DEPLOY_WEBHOOK=false; DEPLOY_FACTORY=false; DEPLOY_AGENT_CONTEXT=false
    case ",${UPGRADE_MODULES:-}," in *,gateway,*) DEPLOY_GATEWAY=true ;; esac
    case ",${UPGRADE_MODULES:-}," in *,webhook-ingress,*) DEPLOY_WEBHOOK=true ;; esac
    case ",${UPGRADE_MODULES:-}," in *,agent-factory,*) DEPLOY_FACTORY=true ;; esac
    case ",${UPGRADE_MODULES:-}," in *,agent-context,*) DEPLOY_AGENT_CONTEXT=true ;; esac
  elif [ "${AGENT_CONTEXT_ONLY:-false}" = true ]; then
    DEPLOY_AGENT_CONTEXT=true
  fi
  if [ "${GATEWAY_ONLY:-false}" = true ]; then
    DEPLOY_WEBHOOK=false; DEPLOY_FACTORY=false; DEPLOY_AGENT_CONTEXT=false
  fi
  if [ "${AGENT_FACTORY_ONLY:-false}" = true ]; then
    DEPLOY_GATEWAY=false; DEPLOY_AGENT_CONTEXT=false
  fi
  if [ "${AGENT_CONTEXT_ONLY:-false}" = true ]; then
    DEPLOY_GATEWAY=false; DEPLOY_WEBHOOK=false; DEPLOY_FACTORY=false
  fi
  [ "${SKIP_WEBHOOK_INGRESS:-false}" != true ] || DEPLOY_WEBHOOK=false
  [ "${SKIP_AGENT_CONTEXT:-false}" != true ] || DEPLOY_AGENT_CONTEXT=false
  if [ "${UPDATE_MODE:-false}" = true ]; then
    [ "${GATEWAY_ONLY:-false}" != true ] || [ "$DEPLOY_GATEWAY" = true ] || fail "Gateway is not deployed"
    [ "${AGENT_FACTORY_ONLY:-false}" != true ] || [ "$DEPLOY_FACTORY" = true ] || fail "Agent factory is not deployed"
    [ "${AGENT_CONTEXT_ONLY:-false}" != true ] || [ "$DEPLOY_AGENT_CONTEXT" = true ] || fail "Agent context is not deployed"
  fi
}
