# Build context: this directory. Release input must name a reviewed PyTorch CUDA
# image by digest. The resulting image must also be used by its digest in the
# approved batch profile. No build or workload is triggered by this fixture.
ARG PYTORCH_IMAGE
ARG PILLOW_SECURITY_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:7b3df072a13b928bddbf70d71735fdc8572006442048868914627ebc8e635b68
FROM ${PILLOW_SECURITY_IMAGE} AS pillow-security
FROM ${PYTORCH_IMAGE}
USER root
# Keep PyTorch/CUDA and install the same-minor Python patch with its locked native dependencies.
ADD --checksum=sha256:9404ee878c6bd5b260655ab651366db43fac6be8d465fcc0acd0f7a50be91acb https://conda.anaconda.org/conda-forge/noarch/ca-certificates-2026.10.19-hbd8a1cb_0.conda /tmp/python-runtime/ca-certificates-2026.10.19-hbd8a1cb_0.conda
ADD --checksum=sha256:eb8a0db0aa570124f7d2a93d7c7f596e3390df5e047818d873baad32985fc736 https://conda.anaconda.org/conda-forge/linux-64/libzlib-1.3.2-h25fd6f3_3.conda /tmp/python-runtime/libzlib-1.3.2-h25fd6f3_3.conda
ADD --checksum=sha256:6de73033a7c5f712c9c5a54124e4581e025691029835e5bceaf10a333a7e7bc0 https://conda.anaconda.org/conda-forge/linux-64/libexpat-2.8.5-hd2095e1_0.conda /tmp/python-runtime/libexpat-2.8.5-hd2095e1_0.conda
ADD --checksum=sha256:c8c7583ef063bc3c430f1d48298e5ad24f796a35c22ed5b14183325825a61106 https://conda.anaconda.org/conda-forge/linux-64/libffi-3.7.0-h81df57d_1.conda /tmp/python-runtime/libffi-3.7.0-h81df57d_1.conda
ADD --checksum=sha256:9787df8c22a59c9a70d3e5a10db9ad663485e75e9ccc3f09bd092cb7b95e0dab https://conda.anaconda.org/conda-forge/linux-64/liblzma-5.8.3-hb03c661_1.conda /tmp/python-runtime/liblzma-5.8.3-hb03c661_1.conda
ADD --checksum=sha256:0f6e60634de2b55da15772fe65485965fd9b61afcce0d749c1446f625babba30 https://conda.anaconda.org/conda-forge/linux-64/libsqlite-3.53.4-h0737f62_102.conda /tmp/python-runtime/libsqlite-3.53.4-h0737f62_102.conda
ADD --checksum=sha256:f34b8d1de9a2bf62476d8608057d9b8a918d97444236dc53a47128f2fae79041 https://conda.anaconda.org/conda-forge/linux-64/libuuid-2.42.4-hcfc3c73_0.conda /tmp/python-runtime/libuuid-2.42.4-hcfc3c73_0.conda
ADD --checksum=sha256:f7e9292dd219a6435bbb1223da9586c3e70d66d169c5a92f08db3f2127df04e9 https://conda.anaconda.org/conda-forge/linux-64/libxcrypt-4.4.38-h280c20c_0.conda /tmp/python-runtime/libxcrypt-4.4.38-h280c20c_0.conda
ADD --checksum=sha256:5d46557214ed184381dafe835b7c94a474a1c3b307a08a250b1ea4779b44ffb3 https://conda.anaconda.org/conda-forge/linux-64/ncurses-6.6-hdb14827_1.conda /tmp/python-runtime/ncurses-6.6-hdb14827_1.conda
ADD --checksum=sha256:2f46a2753a354bd3d5bcda405623f0b3b51e67ed9f3c2f13aa3bc1fa78bc19bb https://conda.anaconda.org/conda-forge/linux-64/openssl-3.5.9-h781a0a9_0.conda /tmp/python-runtime/openssl-3.5.9-h781a0a9_0.conda
ADD --checksum=sha256:bad3de581c5b9f193a3b43d634a47df3e90e6fb89ff712b5f6903920640e4a07 https://conda.anaconda.org/conda-forge/linux-64/libpython-3.11.17-h0c77377_1_cpython.conda /tmp/python-runtime/libpython-3.11.17-h0c77377_1_cpython.conda
ADD --checksum=sha256:01b3fe073a66e321970d09e04b388708f8cbdca5cdfbfcb7c9eeb470ad10383d https://conda.anaconda.org/conda-forge/linux-64/readline-8.3-hd6e31c0_1.conda /tmp/python-runtime/readline-8.3-hd6e31c0_1.conda
ADD --checksum=sha256:524df25896667b5f7e391c2115495cac5e0077b97967981091af8afcc98fd312 https://conda.anaconda.org/conda-forge/linux-64/python-3.11.17-h5f976f7_1_cpython.conda /tmp/python-runtime/python-3.11.17-h5f976f7_1_cpython.conda
ADD --checksum=sha256:ee3bca803cac13760131b6147e2fe60c97e769978e7542d678842b65e285d239 https://conda.anaconda.org/conda-forge/noarch/certifi-2026.10.19-pyhd8ed1ab_0.conda /tmp/python-runtime/certifi-2026.10.19-pyhd8ed1ab_0.conda
ADD --checksum=sha256:83babe5e3c4021b061275164a7c28bd62d183cc77b4c2260ae80f19fcb0a98dd https://conda.anaconda.org/conda-forge/noarch/pip-26.2.2-pyh8b19718_0.conda /tmp/python-runtime/pip-26.2.2-pyh8b19718_0.conda
ADD --checksum=sha256:8dd147ac02fbf7d6213e5e1859096bb606265bce24dab6696c34c543ff93b97f https://conda.anaconda.org/conda-forge/linux-64/brotli-python-1.2.1-py311heb3be6f_0.conda /tmp/python-runtime/brotli-python-1.2.1-py311heb3be6f_0.conda
COPY python-runtime-lock.json maintain_python.py /tmp/python-runtime/
RUN --network=none python /tmp/python-runtime/maintain_python.py /tmp/python-runtime \
    && rm -rf /tmp/python-runtime
# The source-built Pillow 13 wheel also fixes its bundled Brotli decoder.
RUN --network=none --mount=type=bind,from=pillow-security,source=/wheels,target=/pillow-wheels \
    python -c "import glob; assert not glob.glob('/opt/conda/conda-meta/pillow-*.json')" \
    && python -m pip install --no-index --no-deps --force-reinstall --no-cache-dir /pillow-wheels/pillow-13.0.0+adp1-cp311-*.whl \
    && python -m pip check
COPY --from=pillow-security /maintenance/pillow/ /opt/adp-security/pillow/
COPY --from=pillow-security /maintenance/pillow-wheel-lock.json /opt/adp-security/pillow/wheel-lock.json
COPY cuda_probe.py /opt/superplane-demo/cuda_probe.py
USER 65532:65532
ENTRYPOINT ["python", "/opt/superplane-demo/cuda_probe.py"]
