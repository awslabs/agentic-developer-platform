ARG KUBECTL_SECURITY_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner@sha256:5a51c41fe984154d7618ccce424e4bd5a5aff50b7e98c5a95f55da1475ed4c0b
# Build context: this directory. Pillow is built from locked upstream source.
FROM quay.io/pypa/manylinux_2_28_x86_64@sha256:f6adcfd450f0927a05c66ded9b4c3bb538fbab625b1ca3f35d71ac093b24b57f AS pillow-build
ENV CIBW_ARCHS=x86_64 CIBW_PLATFORM=linux
RUN /opt/python/cp310-cp310/bin/python -m pip install setuptools==84.0.0 pybind11==3.0.4
ADD --checksum=sha256:0458511ac96e7d44e574484efff9c6210f8211046b3e7ec1dca2381cea0f8430 https://codeload.github.com/python-pillow/Pillow/tar.gz/293218ad059d994baf1777d882911bed6a49704a /inputs/upstream.tar.gz
ADD --checksum=sha256:e50c410a625333404ba318f109b082ff04b1edb1e18e92109e38897c9561db09 https://codeload.github.com/multi-build/multibuild/tar.gz/c893f305c6066b6d940e10d3333be8cb27fa3947 /inputs/multibuild.tar.gz
ADD --checksum=sha256:bebb7e92376e98273c9c2de1cac027c73e0074174788f0e65abcf5a60b70fa57 https://codeload.github.com/python-pillow/pillow-depends/tar.gz/93950a8f9ada1165a4ab8c719326d5017e3f6c72 /inputs/depends.tar.gz
ADD --checksum=sha256:a830b1a40919539d07806aa58e1b114df53ddd43213d9c8b75847eee6c0182b5 https://files.pythonhosted.org/packages/8c/21/c2bcdd5906101a30244eaffc1b6e6ce71a31bd0742a01eb89e660ebfac2d/pillow-12.2.0.tar.gz /inputs/pillow-12.2.0.tar.gz
ADD --checksum=sha256:3b8182a766685eaa002637e28b4ec8d6b18819a0c71f579bf0dbaa5830297cce https://files.pythonhosted.org/packages/1c/3d/bb7fca845737cf9d7dbde16ed1843984665ff2e0a518f5db43e77ec540b9/pillow-12.3.0.tar.gz /inputs/pillow-12.3.0.tar.gz
RUN mkdir -p /upstream/wheels/multibuild /upstream/build/pillow-depends-main \
    && tar -xzf /inputs/upstream.tar.gz --strip-components=1 -C /upstream \
    && tar -xzf /inputs/multibuild.tar.gz --strip-components=1 -C /upstream/wheels/multibuild \
    && tar -xzf /inputs/depends.tar.gz --strip-components=1 -C /upstream/build/pillow-depends-main
WORKDIR /upstream
RUN bash .github/workflows/wheels-dependencies.sh
ADD --checksum=sha256:cdcd1ba258178f7356d74170c9a81ba4eab6a5d55748bc402a5a2a6fe6be3510 https://files.pythonhosted.org/packages/ce/a4/9cc30690a394a8305e8be4c9036aa16d0dfddadd09079860b582f562496d/pillow-13.0.0.tar.gz /inputs/pillow-13.0.0.tar.gz
ADD --checksum=sha256:98d35d11a78dc453aebb7d10c3748f3f73ff78c8267c0dc697b8631f67bf7e8f https://codeload.github.com/google/brotli/tar.gz/refs/tags/v1.2.1 /inputs/brotli-1.2.1.tar.gz
RUN mkdir /brotli-fixed && tar -xzf /inputs/brotli-1.2.1.tar.gz --strip-components=1 -C /brotli-fixed \
    && cmake -S /brotli-fixed -B /brotli-fixed/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_INSTALL_LIBDIR=/usr/local/lib \
    && cmake --build /brotli-fixed/build --parallel 4 && cmake --install /brotli-fixed/build \
    && ldconfig \
    && python3 -c 'import ctypes; d=ctypes.CDLL("/usr/local/lib/libbrotlidec.so.1"); d.BrotliDecoderVersion.restype=ctypes.c_uint32; assert d.BrotliDecoderVersion() == 0x01002001'
RUN /opt/python/cp311-cp311/bin/python -m pip install setuptools==84.0.0 pybind11==3.0.4 \
    && /opt/python/cp313-cp313/bin/python -m pip install setuptools==84.0.0 pybind11==3.0.4
COPY pillow/source-lock.json pillow/build.py /build-tools/
COPY pillow/patches/ /build-tools/patches/
RUN python3 /build-tools/build.py

FROM ${KUBECTL_SECURITY_IMAGE} AS kubectl-security
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:ca5fe2fbf4836112563ae1eee8bac8ba829109f741dd838474c3d7718c37c56e AS go-security
FROM public.ecr.aws/docker/library/python:3.10.22-slim@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d AS python-security
RUN rm -rf /usr/local/lib/python3.10/site-packages
FROM python-security AS seed-build
ADD --checksum=sha256:1bea5af7463f59c4719db48fe739579a2a4f569c96f26c086edda85c96da9f59 https://files.pythonhosted.org/packages/9a/ce/e74453531b0c49a58d0e8a4f9bab4495705859fee4a4f7de27d58f4a791a/virtualenv-21.7.13-py3-none-any.whl /seed-inputs/virtualenv-21.7.13-py3-none-any.whl
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl /seed-inputs/certifi-2026.10.19-py3-none-any.whl
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /seed-inputs/pip-26.2.2-py3-none-any.whl
COPY seed-source-lock.json build_seeds.py /seed-inputs/
RUN python /seed-inputs/build_seeds.py
FROM scratch
COPY --from=seed-build /seed-out/*.whl /wheels/
COPY --from=seed-build /seed-out/*.json /maintenance/
COPY --from=kubectl-security /bin/kubectl /go-tools/kubectl
COPY --from=kubectl-security /licenses/kubectl/ /licenses/kubectl/
COPY --from=python-security /usr/local/bin/python3.10 /python-runtime/bin/python3.10
COPY --from=python-security /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.so /python-runtime/lib/
COPY --from=python-security /usr/local/lib/python3.10 /python-runtime/lib/python3.10/
COPY --from=python-security /usr/local/include/python3.10 /python-runtime/include/python3.10/
COPY --from=go-security /bin/ /go-tools/
COPY --from=go-security /usr/share/licenses/skypilot-go-tools/ /licenses/
ADD --checksum=sha256:7002251225e10e29d2e1f49e6532613fe5d5d9f0b6f1f02997a52b38fe56899e https://files.pythonhosted.org/packages/d6/0b/29d7965215f8ef830a7ca1f42997fe13e5693d85e9edb18f938d063ef5f2/gitpython-3.1.62-py3-none-any.whl /wheels/gitpython-3.1.62-py3-none-any.whl
ADD --checksum=sha256:0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3 https://files.pythonhosted.org/packages/92/9d/c4e665119135114480843e7ab388fa94d8480650450e6f8e26b70d323a4c/urllib3-2.8.0-py3-none-any.whl /wheels/urllib3-2.8.0-py3-none-any.whl
ADD --checksum=sha256:d4e244cf17b8b29819ed78003d55fbacf86eda23425b075454fff9271b79377a https://files.pythonhosted.org/packages/43/5e/21abf578182fb15006a57faf3711a1e659e29d600d19b6e557eae908c81d/python_discovery-1.6.0-py3-none-any.whl /wheels/python_discovery-1.6.0-py3-none-any.whl
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl /wheels/certifi-2026.10.19-py3-none-any.whl
ADD --checksum=sha256:05e4332791b4d55b6dde8695b7fa6079b26991519f44760887c5dfbd5ca4fe53 https://files.pythonhosted.org/packages/4b/a1/de02c0913726728eca56ee258875cbba9c7b0572c63f494fd9f55c7852e3/uv-0.13.4-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl /wheels/uv-0.13.4-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /wheels/pip-26.2.2-py3-none-any.whl
ADD --checksum=sha256:f760d9a9c441bfc87eee720ddb0946d3e19995b20983ae14038e033f1664efcb https://files.pythonhosted.org/packages/aa/f6/8965fda7552aaa285804b9e07c6eac7c475d25fd1de8696103bf8e192bfb/brotli-1.2.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl /wheels/brotli-1.2.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
COPY --from=pillow-build /out/*.whl /wheels/
COPY --from=pillow-build /out/pillow-wheel-lock.json /maintenance/
COPY --from=pillow-build /out/patched-source.json /out/source-lock.json /maintenance/pillow/
COPY pillow/patches/ /maintenance/pillow/patches/
COPY wheel-lock.json uv-lock.json go-lock.json install.py install_python.py verify_runtime.py verify_critical.py verify_tls.py verify_brotli.py pillow_regression.py /maintenance/
USER 65532:65532
