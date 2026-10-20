#!/usr/bin/env sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Renders the per-environment bits the image cannot bake, then starts nginx.
# sh, not bash: the alpine base has no bash.
set -eu

RUNTIME_CONFIG_JSON="${RUNTIME_CONFIG_JSON:?RUNTIME_CONFIG_JSON is required}"
CSP_HEADER="${CSP_HEADER:?CSP_HEADER is required}"

WEBROOT=/usr/share/nginx/html
SNIPPETS=/etc/nginx/snippets

# The SPA fetches this at mount; see RuntimeContextProvider.
printf '%s' "$RUNTIME_CONFIG_JSON" > "$WEBROOT/runtime-config.json"

# HSTS only when the ALB terminates TLS — on plain HTTP it is ignored anyway and
# would pin the host to HTTPS if a cert were added later.
{
  printf 'add_header Content-Security-Policy "%s" always;\n' "$CSP_HEADER"
  if [ -z "${INSECURE_TRANSPORT:-}" ]; then
    printf 'add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;\n'
  fi
} > "$SNIPPETS/transport-headers.conf"

# Operator-supplied example prompts. Never fatal: a missing bucket, missing
# prefix or denied read just means the playground shows no sample prompts.
if [ -n "${UI_CONTENT_BUCKET:-}" ]; then
  attempt=1
  while [ "$attempt" -le 3 ]; do
    if aws s3 sync "s3://${UI_CONTENT_BUCKET}/questions/" "$WEBROOT/questions/" \
      --only-show-errors; then
      break
    fi
    echo "[web] questions sync attempt ${attempt} failed" >&2
    sleep $((attempt * 2))
    attempt=$((attempt + 1))
  done
fi

exec "$@"
