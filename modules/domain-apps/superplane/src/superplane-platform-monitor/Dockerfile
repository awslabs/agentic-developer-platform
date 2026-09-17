# Build stage
FROM golang:1.23-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o /superplane-platform-monitor .

# Runtime stage
FROM alpine:3.19

RUN apk add --no-cache ca-certificates tzdata

# Non-root user for security.
RUN adduser -D -u 1000 monitor
USER monitor

COPY --from=builder /superplane-platform-monitor /usr/local/bin/superplane-platform-monitor

EXPOSE 9090

ENTRYPOINT ["superplane-platform-monitor"]
