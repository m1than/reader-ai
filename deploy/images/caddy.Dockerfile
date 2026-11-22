# reader-caddy:local — the edge with the built SPA (OPS-DEPLOY FR-DEPLOY-2, FR-DEPLOY-5; V.OPS-DEPLOY-web).
# Build context: the repository root. Stage `web` builds web/dist from the committed sources and the vendored
# library tarball (WEB-UI FR-UI-1); the runtime stage is the pinned caddy image with the SPA at /srv/spa.
FROM node:26-trixie-slim@sha256:e6bedef217677b276c591795e169840edd9096dbf13f29df8ef8d1856b8820bc AS web
WORKDIR /web
# Only what the build needs (FR-DEPLOY-2 allow-list); caddy.Dockerfile.dockerignore keeps node_modules, dist,
# storybook-static and any .env out of the context.
COPY web/package.json web/package-lock.json /web/
COPY web/vendor /web/vendor
RUN npm ci --no-audit --no-fund
COPY web/index.html web/vite.config.ts web/tsconfig.json /web/
COPY web/src /web/src
RUN npx vite build

FROM caddy:2-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
COPY --from=web /web/dist /srv/spa
