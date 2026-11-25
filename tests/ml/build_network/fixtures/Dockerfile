FROM golang:1.26.10-alpine AS build-trust
COPY scripts/build_trust/main.go /tmp/build-trust.go
RUN CGO_ENABLED=0 GOTOOLCHAIN=local go build -trimpath -ldflags="-s -w" -o /tmp/build-trust /tmp/build-trust.go

FROM python:3.12-slim AS apt
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
RUN --mount=type=bind,from=build-trust,source=/tmp/build-trust,target=/tmp/build-trust,ro \
    --mount=type=secret,id=corporate_ca,required=false \
    /tmp/build-trust apt-get update && /tmp/build-trust apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

FROM golang:1.26.10-alpine AS go-git
RUN --mount=type=bind,from=build-trust,source=/tmp/build-trust,target=/tmp/build-trust,ro \
    --mount=type=secret,id=corporate_ca,required=false \
    /tmp/build-trust apk add --no-cache git ca-certificates
RUN --mount=type=bind,from=build-trust,source=/tmp/build-trust,target=/tmp/build-trust,ro \
    --mount=type=secret,id=corporate_ca,required=false \
    /tmp/build-trust go mod download -json github.com/minio/minio@9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
RUN --mount=type=bind,from=build-trust,source=/tmp/build-trust,target=/tmp/build-trust,ro \
    --mount=type=secret,id=corporate_ca,required=false \
    /tmp/build-trust git ls-remote https://github.com/minio/minio.git refs/tags/RELEASE.2025-10-15T17-29-55Z
