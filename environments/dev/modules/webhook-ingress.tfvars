# Dev-only webhook configuration. The shared wrapper loads this explicitly;
# other environments use portable defaults and their own optional overlay.

# The dev sandbox cannot reserve Lambda concurrency (#2928). Other targets
# retain variables.tf's reserved-concurrency throttle isolation by default.
enable_lambda_reserved_concurrency = false

# Dev OTel export to ADOT / CloudWatch / X-Ray (#1630); content unmasking is off.
enable_agent_otel = true

# Dev CI has the existing gateway internal-api-key secret. The CLI disables
# this for fresh dev deployments until the secret exists. Other environments
# retain the false module default unless explicitly configured.
enable_adversarial_e2e = true

# Reviewed dev dispatch identity, inert while the portable rule flag is false.
# Enabling the rule still requires the producer IAM grant first (#4450 / #4559).
eventbridge_security_agent_repo = "aws-e/adp"
eventbridge_security_agent_org  = "aws-e"

# The existing gateway role has reached AWS's aggregate inline-policy quota.
# These new authority grants use managed policies with identical permissions.
gateway_authority_managed_policies = true

# Saved persona preferences resolve before dispatch; worker authority stays independent.
persona_model_mapping_enabled = true

# Shared progress runtime plus structured Codex planning workflows (PR #6765).
agent_image = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:50cbe76e5968bf65f9fe9f4e498198cd6eeb96707d5fa2bd5fdd74112bbc79f8"

# Task API T4 was OOMKilled at 8Gi during worker regression tests (2026-09-24).
# Reserve additional node capacity as well as raising the per-worker ceiling.
agent_worker_memory_request = "8Gi"
agent_worker_memory_limit   = "16Gi"

# Protected workers use workload proof and gateway run services.
agent_authority_prepared   = true
agent_authority_enabled    = true
task_api_worker_enabled    = true
task_api_admission_enabled = true
task_api_human_enabled     = true
task_api_recovery_enabled  = true

# Existing ingress resolves canonical identities through the protected gateway.
gateway_api_url                 = "https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev"
internal_api_key_parameter_name = "/adp/dev/gateway/internal-api-key"

# Controlled rollout: legacy workers drained; customer source has no platform EKS access.
agent_authority_runtime_ready          = true
agent_authority_legacy_workers_drained = true
agent_task_source_isolation_confirmed  = true
agent_legacy_worker_admin_retired      = true
agent_worker_admission_paused          = false
shared_run_reporting_enabled           = true
shared_worker_continuation_enabled     = false
agent_authority_worker_image_digests   = ["sha256:067ed30b07cde6abaecaa59193874f9cdfd115937d4f2f4a01f083a091f65456", "sha256:15bfd65ad2313a0674e1fceea2d80971697003b36edfddd1f9f955693c6fa788", "sha256:16f1f93b98d258bb6dd85127aa6176bba66c19a8215216fa1b2fdd6e6dcb6e46", "sha256:1cb3550ee64d72b3b5261ccba7c874378a309d71ca277cb985dc4c911edce51f", "sha256:1fdd9728fa04e4ef61939c7b5ce37fa633914269017c51bb3c6d45938c1bcb20", "sha256:257ec130ee7f95757ddb542ef47e54c729ac5ee237b6b63bb07631c1b84f4f68", "sha256:2923326ff83e0335cbf9e17a0c0f80b6c2ab0b01c70c76fac9fd059851b6eb94", "sha256:2d70f6de7f083592d882d2773f50ff61743e8c18ea823579e1af1285e48b3179", "sha256:34e063a0c8c97eb3f7681063b98886461825ecb67bbdf47296007e460f460851", "sha256:37dc4e2addec4a33856161af9500fae9bb8b7cb0363f0c3f8c983f91c373f85b", "sha256:38a272ccbd3a31f23b420cd419f1f8f061a13212b5c8083996ae29dee96297dc", "sha256:3c00a4392fd2fc9a52ae2df79a175be049e68fd73b3865072c267489a8d9e2da", "sha256:3d1275bea78f6b400ddc7402822abf4785c2be284b7ffcc9d6d47e7710522010", "sha256:3d19d3fba77538bba11d57c9ef05026e54b70bf465515bca42746f3534cf96e4", "sha256:4100c2dbfad88251969303347ce09c67ed8345e2fd946d96ea293bb88f811287", "sha256:436d8afe8392e54ce5601d45955b6266e3c572d397ef0d6fc773f2cc98a7e62a", "sha256:4b78f9f460d3a1a65e0b5e85e8e7d96f83d6b72e7e13124c6e852ca9e303b837", "sha256:50cbe76e5968bf65f9fe9f4e498198cd6eeb96707d5fa2bd5fdd74112bbc79f8", "sha256:5261daf50313d49d2753d63ab8b4ea3dbd2b6babce75fa821dd1b874c086c515", "sha256:5812f3f05370553ee07340794659e483169c91a0b8e413edc13605ae3c61261c", "sha256:5c0b7c9f0a8da85f81df0a38480fe59dff60c221e30c3bed43ad167bee59b2cd", "sha256:5d3e952c1be21b1a2656b783fbebf0d653893469d9bc17e7f0ca623cef3f8572", "sha256:5fe1725eeb8a5f6ac4956a46d533d65c39c4c2c83903225fecd248864fbf4a12", "sha256:6161de7d7fc0602afe196cae61f7529cae764bb07a263caa686442dc83e5dba1", "sha256:66a2283f1524ec89083c468f56f46f88bf5f5d743a9e8a18ec60300ea05ecd68", "sha256:67326522f090df298d7081057004fa65e2b2c93132c82a24b443ff964efd0421", "sha256:68569bf10df1e76603d3ece24338a940920865720597986acd77474d8de28d47", "sha256:68ebf7e6568ed5a6f38de030e6c83c9015ce9790b4e4cf2c2ee4f0187a8305f1", "sha256:6a72726f20850a24d1a2bf1c636334bd21747303a614d639f7cb25f77b796dec", "sha256:6beab1ad04b9249aa72b2f25bf6b8bbfc3f8ecfc82eac250ac2eb8e083819d67", "sha256:6ec63a920371820b2697053864f85565e6bc57f8545fabf80e85f20d9818be16", "sha256:6f3a2668d162a4c81035fb13c5f77c0413e54ae41bca9641e4a753c7aefd7dc4", "sha256:756a05ae93adcf68209e305fcb6c3de71122e60ca8b37779c042766b49dc30a9", "sha256:767d1a2e568e8f9cceaaf07943648e606b5a054bab54af0e38525257c0c42b33", "sha256:7c1541d515717d54da677797ebd37cac67b39f3e8de116cd6e391f0de08b201d", "sha256:7d6bcd626dd69316a4fc1e5205aedcfdbd69b592dc5e7575e02d59c58650f1c9", "sha256:80227d82b7c6208eba931f1cf9b9d512f606d35412afbefe411b109244629856", "sha256:83acce2d8694b298d5bc2c3830cf8edbedcd443ea91cc57c425add42a6fac981", "sha256:8a3af964d0a786a6c27b5e66d44ac32e8e451c23d375046663936d2007658db1", "sha256:90a473eb1636a71315de6119a182cff23fd4e5ff70da41ad183305a5d9730f0f", "sha256:a396eeae0ebd032866b33550a876aeae7a317d0bce00163608d790b4bafc55c3", "sha256:b0a1818251face459588d3e7082649314de0a4512242a192bc95f88d9c8f7b2c", "sha256:beae9a4b9e6c6ebd4f442eebf2a65965d077b072a76baa0563caa689289979d4", "sha256:c1b86d1d43af2eccf15fcf317ec0c5a81346da3514d7cb53618f55253438fb08", "sha256:c4f6986d5110f2fe3aed20ca12399ce0372582ffd6d73691e52ed7b1bbe26bd2", "sha256:cdde81fb9cf747676942136c2e68de51bf7e3bb6dd9bbeefee613abcaac69d1e", "sha256:cf0678b1bef6f1562eab17a06a12568081350b6eac7f187bd1df6b2166ee95cf", "sha256:cfbe0b18dcc3e055738f52ef1004624aefe62cc165bef444b7edc933992ef89a", "sha256:d6e3585e7b7ef4fe0f9f04d433347b25caaefc4f435d51f7c1df4e09121b525f", "sha256:d9fd92e5fa7d3c87077c35ef8fa6c6b3136fc0d30a35e0f34eb9c5b8df985809", "sha256:db7ed8b0efbd05cde91e269b94942a93c9f0d8efc40a515d68afee99886c197d", "sha256:e25a54c3037f9bde26a3aa400e3af9bf367288e7a6977cece2d508f5f259fe27", "sha256:e63943bf5de2beefeefb33c12a71fea5be9920914f1ecf5ea94de9977e918094", "sha256:f14998af44cc77df24365371675ffe7fef54878d7a254c83e65a9dcbcbfdbabe", "sha256:f85fef27d28bb817b9a6ebbaaab8698f2cde71e8786fefadff302b84c82e38cb", "sha256:f90e802c40b20edffd7ae7ccaa7ba6af1072158e7ae349535d6a1728e2bfa63d", "sha256:f9f7ac000a52c403f95af5519466bdcf39fb2c40beae5fdd3a4bdcc961dcef92"]

# Six-hour Task deadline plus startup/cleanup headroom for the owning pod.
agent_pod_deadline_seconds = 22200

# Authenticated Agent Activity explanation stream; independent of mutation controls.
agent_explanations_enabled = true
# Explicit operator activation on 2026-09-25; acceptance evidence remains tracked separately.
agent_control_enabled = true

# Native reviewer deployment default; explicit user selections take precedence.
codex_reviewer_model = "openai.gpt-6-astra"

# Only the four implemented repository report hosts.
codex_github_personas = ["agent-codex-architect", "agent-codex-product", "agent-codex-pm", "agent-codex-intent-refinement"]

# Structured Task report and clarification hosts.
codex_task_personas = ["agent-task-gpt-architect", "agent-task-gpt-product", "agent-task-gpt-pm", "agent-task-gpt-intent-refinement"]
