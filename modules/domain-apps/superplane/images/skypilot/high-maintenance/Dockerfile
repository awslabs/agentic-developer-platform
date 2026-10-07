FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:65e92431d58468d1a662591c0158a0c623e11915304eb3a9ac2a1728037b9164 AS go-security
FROM scratch
COPY --from=go-security /bin/ /go-tools/
COPY --from=go-security /usr/share/licenses/skypilot-go-tools/ /licenses/
ADD --checksum=sha256:1bea5af7463f59c4719db48fe739579a2a4f569c96f26c086edda85c96da9f59 https://files.pythonhosted.org/packages/9a/ce/e74453531b0c49a58d0e8a4f9bab4495705859fee4a4f7de27d58f4a791a/virtualenv-21.7.13-py3-none-any.whl /wheels/virtualenv-21.7.13-py3-none-any.whl
ADD --checksum=sha256:39548bffb8fa0f3a548133348868bb4838e79d73283052207dc97781a569b6b4 https://files.pythonhosted.org/packages/71/63/ba28697918b7c190af9f3f21940d03e8814e25dd4ddd39d6929f3a553995/gitpython-3.1.60-py3-none-any.whl /wheels/gitpython-3.1.60-py3-none-any.whl
ADD --checksum=sha256:0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3 https://files.pythonhosted.org/packages/92/9d/c4e665119135114480843e7ab388fa94d8480650450e6f8e26b70d323a4c/urllib3-2.8.0-py3-none-any.whl /wheels/urllib3-2.8.0-py3-none-any.whl
ADD --checksum=sha256:d4e244cf17b8b29819ed78003d55fbacf86eda23425b075454fff9271b79377a https://files.pythonhosted.org/packages/43/5e/21abf578182fb15006a57faf3711a1e659e29d600d19b6e557eae908c81d/python_discovery-1.6.0-py3-none-any.whl /wheels/python_discovery-1.6.0-py3-none-any.whl
COPY wheel-lock.json uv-lock.json go-lock.json install.py verify_runtime.py /maintenance/
USER 65532:65532
