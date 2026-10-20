# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# nginx serving the prebuilt Vite SPA (`dist/` must exist — see .dockerignore).
# Alpine from public ECR, not Docker Hub. Renovate keeps this digest current
# (dockerfile manager + pinDigests, held to the 1.29 line).
#
# No hardcoded --platform: CDK passes `--platform linux/arm64` via
# ContainerImage.fromAsset, so a plain `docker build` can still use the host
# architecture for local smoke-testing without qemu. Same as packages/vkg.
FROM public.ecr.aws/docker/library/nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de

# aws-cli backs the optional operator questions/ sync in the entrypoint.
RUN apk add --no-cache aws-cli

COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/snippets/ /etc/nginx/snippets/
COPY docker-entrypoint.sh /docker-entrypoint-coa.sh
COPY dist/ /usr/share/nginx/html/

# Runs as nginx (uid 101), which must write the pid file, the generated
# transport-headers snippet, runtime-config.json and the synced questions/.
# sed strips CRLF — this repo is developed on Windows.
RUN sed -i 's/\r$//' /docker-entrypoint-coa.sh \
  && chmod +x /docker-entrypoint-coa.sh \
  && mkdir -p /usr/share/nginx/html/questions \
  && chown -R nginx:nginx /usr/share/nginx/html /etc/nginx/snippets

USER nginx
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:8080/health || exit 1

ENTRYPOINT ["/docker-entrypoint-coa.sh"]
CMD ["nginx", "-g", "daemon off;"]
