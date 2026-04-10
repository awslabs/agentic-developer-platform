# ARC Runner Scale Set Helm Values Template
# Replace ${REPO_NAME}, ${GITHUB_ORG}, ${RUNNER_ROLE_ARN} when deploying

githubConfigUrl: "https://github.com/${GITHUB_ORG}/${REPO_NAME}"
githubConfigSecret: github-arc-secret

minRunners: 0
maxRunners: 5

runnerGroup: "default"

template:
  metadata:
    annotations:
      # Prevent Karpenter from consolidating nodes while jobs are running
      karpenter.sh/do-not-disrupt: "true"
  spec:
    serviceAccountName: github-runner-sa
    volumes:
      - name: work
        emptyDir: {}
    containers:
      # Main runner container
      - name: runner
        image: ghcr.io/actions/actions-runner:latest
        command: ["/home/runner/run.sh"]
        env:
          - name: ACTIONS_RUNNER_REQUIRE_JOB_CONTAINER
            value: "false"
          - name: DOCKER_HOST
            value: tcp://localhost:2375
        resources:
          requests:
            cpu: "1"
            memory: "4Gi"
          limits:
            cpu: "4"
            memory: "8Gi"
        volumeMounts:
          - name: work
            mountPath: /home/runner/_work
      # Docker-in-Docker sidecar for container builds
      - name: dind
        image: docker:dind
        env:
          - name: DOCKER_TLS_CERTDIR
            value: ""
        securityContext:
          privileged: true
        resources:
          requests:
            cpu: "500m"
            memory: "1Gi"
          limits:
            cpu: "2"
            memory: "4Gi"
        volumeMounts:
          - name: work
            mountPath: /home/runner/_work
