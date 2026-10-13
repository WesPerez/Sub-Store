ARG NODE_VERSION=24.15.0
FROM node:${NODE_VERSION}-alpine

ARG SOURCE_SHA
ARG SOURCE_REPOSITORY=WesPerez/Sub-Store
ARG FRONTEND_SHA
LABEL org.opencontainers.image.source="https://github.com/${SOURCE_REPOSITORY}" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      org.opencontainers.image.ref.name="mine" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      io.sub-store.frontend.revision="${FRONTEND_SHA}"

ENV NODE_ENV=production \
    NODE_OPTIONS=--max-old-space-size=384 \
    SUB_STORE_BACKEND_API_HOST=0.0.0.0 \
    SUB_STORE_BACKEND_API_PORT=3000 \
    SUB_STORE_BACKEND_MERGE=true \
    SUB_STORE_FRONTEND_BACKEND_PATH=/backend \
    SUB_STORE_FRONTEND_PATH=/opt/app/frontend \
    SUB_STORE_DATA_BASE_PATH=/opt/app/data

WORKDIR /opt/app
COPY --chown=node:node backend/dist/sub-store.bundle.js backend/dist/runtime-manifest.json ./
COPY --chown=node:node frontend-src/dist/ ./frontend/
COPY --chown=node:node deploy/container-health.mjs ./health.mjs
RUN mkdir -p /opt/app/data && chown node:node /opt/app/data
USER node
WORKDIR /opt/app/data
EXPOSE 3000
HEALTHCHECK --interval=20s --timeout=8s --start-period=20s --retries=3 CMD ["node", "/opt/app/health.mjs"]
CMD ["node", "/opt/app/sub-store.bundle.js"]
