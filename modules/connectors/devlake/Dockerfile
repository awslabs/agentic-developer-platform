ARG BUILDER_IMAGE
ARG RUNTIME_IMAGE
ARG SOURCE_REVISION
FROM ${BUILDER_IMAGE} AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -mod=readonly -trimpath -ldflags="-buildid=" -o /out/connector-api ./cmd/adp-connector-api

FROM ${RUNTIME_IMAGE}
ARG SOURCE_REVISION
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
COPY --from=build /out/connector-api /usr/local/bin/connector-api
COPY --from=build /src/adp/LICENSE /usr/share/licenses/devlake/LICENSE
COPY --from=build /src/adp/NOTICE /usr/share/licenses/devlake/NOTICE
COPY --from=build /src/adp/source-manifest.json /usr/share/devlake/source-manifest.json
COPY --from=build /src/adp/schema-manifest.json /usr/share/devlake/schema-manifest.json
COPY --from=build /src/adp/module-contract.json /usr/share/devlake/module-contract.json
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/connector-api"]
