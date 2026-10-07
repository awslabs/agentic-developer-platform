# LLM proxy capacity profile

This opt-in profile uses two Python workers per pod, a one-core CPU scaling target, two baseline replicas, and a bounded IAM-authenticated PostgreSQL pool. It requires an image containing the `rds_pool_enabled` setting and the session-provider changes; setting an environment variable on an older image does not install connection pooling.

The gateway continues to verify TLS and resolve fresh IAM credentials for new connections. Existing authenticated database connections can remain open after the token used to establish them expires. Pooling does not cache authorization decisions or skip per-request quota/budget checks. Five pooled connections plus five overflow connections per worker means at most 20 per pod, or 240 at the 12-pod ceiling; leave database capacity for other services and rollout overlap.

Apply only to the intended, verified cluster after deploying the compatible image:

```bash
kubectl --context "$TARGET_CONTEXT" -n adp-gateway patch deployment bedrockgateway \
  --type strategic --patch-file modules/gateway/k8s/profiles/llm-proxy/deployment-patch.yaml
kubectl --context "$TARGET_CONTEXT" apply -f modules/gateway/k8s/profiles/llm-proxy/hpa.yaml
kubectl --context "$TARGET_CONTEXT" apply -f modules/gateway/k8s/profiles/llm-proxy/pdb.yaml
kubectl --context "$TARGET_CONTEXT" -n adp-gateway rollout status deployment/bedrockgateway
```

Save the original Deployment, HPA and PDB first. This profile preserves the installed image, environment, instrumentation, routing, probes and security configuration. It does not disable HPA during rollout or set a fixed Deployment replica count. Ensure ordinary deployment automation reapplies the selected profile: the repository's top-level default manifests otherwise restore their default resource and scaling configuration.

CPU is an absolute average value, independent of memory requests. The request of one CPU per pod prevents the scheduler from assuming less steady-state CPU capacity than the autoscaling target. The two-core limit leaves burst capacity while nodes and pods start. Memory should have an alert near the limit; fixed worker memory is not a horizontal scaling signal.

The 120-second downscale stabilization and one-pod-per-minute policy limit churn. The pre-stop delay allows routing to stop sending new requests before process shutdown. The 960-second grace period preserves streams within that bound; longer streams need their own validated drain policy. Use the accompanying Locust client to revalidate changed images, model mixes, prompt sizes or concurrency. This profile is not a universal capacity guarantee.
