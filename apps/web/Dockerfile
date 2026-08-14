# Build context is the REPO ROOT.
#
#   docker build -f apps/web/Dockerfile .

# ---------- build ----------
FROM node:24-alpine AS build
WORKDIR /repo

COPY package.json package-lock.json tsconfig.base.json ./
COPY apps/web/package.json apps/web/

RUN npm ci --workspace @leak/web --include-workspace-root

COPY apps/web apps/web

# Vite bakes VITE_* values in at build time — they are not runtime configuration. Empty
# means "same origin", which is what we want when nginx proxies /api to the API container.
ARG VITE_API_URL=""
ENV VITE_API_URL=$VITE_API_URL

RUN npm run build -w @leak/web

# ---------- runtime ----------
FROM nginx:1.27-alpine AS runtime

COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1
