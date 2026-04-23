# projctl — single static binary image.
#
# Published as ghcr.io/<org>/projctl:<VERSION>. Baked into each project image
# via `COPY --from=ghcr.io/<org>/projctl:<ver> /projctl /usr/local/bin/projctl`.
#
# Build:
#   docker build -t ghcr.io/<org>/projctl:dev projctl/

FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY VERSION ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=linux \
    go build -trimpath -ldflags="-s -w -X main.version=$(cat VERSION)" \
    -o /out/projctl ./cmd/projctl

FROM scratch AS runtime
COPY --from=build /out/projctl /projctl
ENTRYPOINT ["/projctl"]
