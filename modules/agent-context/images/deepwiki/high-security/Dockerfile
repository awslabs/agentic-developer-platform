FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:c7cf12e4f48eb63a1eb2f86fccc27bec8d7c92f89f0df258ea54f4806ec6b3eb
USER root
RUN /usr/local/bin/python3.11 -m pip install --no-cache-dir --upgrade setuptools==84.0.0 && /usr/local/bin/python3.11 -m pip check
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl /tmp/critical-python/certifi-2026.10.19-py3-none-any.whl
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/critical-python/pip-26.2.2-py3-none-any.whl
ADD --checksum=sha256:6b3c3d5794212a54434b4856f808f7bdf7ad48eacadaadb5b2346fc9a639365a https://files.pythonhosted.org/packages/71/7e/c1d314406ce47a39180fc66114681d9ac10c2c667e14e4dc1a5e5089f4fb/brotli-1.2.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl /tmp/critical-python/brotli-1.2.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
RUN --network=none /opt/venv/bin/python -m pip install --no-index --no-deps --no-cache-dir /tmp/critical-python/*.whl \
    && /opt/venv/bin/python -m pip check \
    && /usr/local/bin/python3.11 -m pip install --no-index --no-deps --no-cache-dir /tmp/critical-python/pip-26.2.2-py3-none-any.whl \
    && /usr/local/bin/python3.11 -m pip check && rm -rf /tmp/critical-python
# GHSA-2464-fhr4-w9xw: upstream-supported sharp fix for Next image optimization.
ADD --checksum=sha256:3b6bbbe6b308f5c383938ca4a31926cd5d79c4651ed8c5fda16a97da2014a9ac https://registry.npmjs.org/sharp/-/sharp-0.35.5.tgz /tmp/critical-sharp/sharp-0.35.5.tgz
ADD --checksum=sha256:556787285d3244c07686a355de5b5a4c4d0019a12785bf24764e756a96ea3334 https://registry.npmjs.org/@img/sharp-linux-x64/-/sharp-linux-x64-0.35.5.tgz /tmp/critical-sharp/sharp-linux-x64-0.35.5.tgz
ADD --checksum=sha256:e371cb713fc8822c96a0d3fb60a3d092799d0aefab6d7f1fb3b72d8d75f1d109 https://registry.npmjs.org/@img/sharp-libvips-linux-x64/-/sharp-libvips-linux-x64-1.3.4.tgz /tmp/critical-sharp/sharp-libvips-linux-x64-1.3.4.tgz
RUN --network=none set -e; \
    if [ -d /app/node_modules/next ]; then \
      node -e 'const fs=require("fs"),semver=require("/app/node_modules/semver"); const p=JSON.parse(fs.readFileSync("/app/node_modules/sharp/package.json")); if(!["0.35.4","0.35.5"].includes(p.version)) throw Error("Unreviewed sharp input"); for(const [n,v] of Object.entries({semver:"^7.8.5","@img/colour":"^1.1.0","detect-libc":"^2.1.2"})) if(!semver.satisfies(JSON.parse(fs.readFileSync("/app/node_modules/"+n+"/package.json")).version,v)) throw Error("Incompatible sharp dependency: "+n);'; \
      rm -rf /app/node_modules/sharp; mkdir -p /app/node_modules/sharp; \
      tar -xzf /tmp/critical-sharp/sharp-0.35.5.tgz --strip-components=1 -C /app/node_modules/sharp; \
      rm -rf /app/node_modules/@img/sharp-linux-x64; mkdir -p /app/node_modules/@img/sharp-linux-x64; \
      tar -xzf /tmp/critical-sharp/sharp-linux-x64-0.35.5.tgz --strip-components=1 -C /app/node_modules/@img/sharp-linux-x64; \
      rm -rf /app/node_modules/@img/sharp-libvips-linux-x64; mkdir -p /app/node_modules/@img/sharp-libvips-linux-x64; \
      tar -xzf /tmp/critical-sharp/sharp-libvips-linux-x64-1.3.4.tgz --strip-components=1 -C /app/node_modules/@img/sharp-libvips-linux-x64; \
      node -e 'const {createRequire}=require("module");const sharp=createRequire("/app/node_modules/next/package.json")("sharp");if(sharp.versions.sharp!=="0.35.5") throw Error("Next resolves an affected sharp");'; \
    fi; rm -rf /tmp/critical-sharp

USER 10001:10001
