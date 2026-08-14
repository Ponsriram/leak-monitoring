# Build context is the REPO ROOT, not apps/api — the API depends on the @leak/db workspace,
# which lives outside its own directory.
#
#   docker build -f apps/api/Dockerfile .

# ---------- shared manifests ----------
# Split out so the two install stages below share one cached layer.
FROM node:24-alpine AS manifests
WORKDIR /repo
COPY package.json package-lock.json tsconfig.base.json ./
COPY packages/db/package.json packages/db/
COPY apps/api/package.json apps/api/
COPY apps/web/package.json apps/web/

# ---------- production dependencies ----------
# A dedicated stage rather than `npm prune --omit=dev` after building.
#
# NOTE: no `--workspace` filters here. Combining them with `--omit=dev` produced a tree that
# was wrong in both directions — `better-auth` (a regular dependency) was absent while
# `@electric-sql/pglite` (a dev dependency) was present, and the API died at import with
# ERR_MODULE_NOT_FOUND. A plain `npm ci --omit=dev` resolves the whole workspace set
# correctly; it pulls the web app's runtime deps too, which is a few MB and worth the
# certainty.
FROM manifests AS prod-deps
RUN npm ci --omit=dev

# ---------- build ----------
FROM manifests AS build
RUN npm ci --workspace @leak/api --workspace @leak/db --include-workspace-root

COPY packages/db packages/db
COPY apps/api apps/api

RUN npm run build -w @leak/db && npm run build -w @leak/api

# ---------- runtime ----------
FROM node:24-alpine AS runtime
WORKDIR /repo

ENV NODE_ENV=production

# tini reaps zombies and forwards signals, so the graceful-shutdown handler in server.ts
# actually receives SIGTERM instead of the container being killed out from under it.
RUN apk add --no-cache tini

# Copy the production dependency tree WHOLE, not just /repo/node_modules.
#
# npm does not hoist everything to the root: `better-auth` resolves into
# `apps/api/node_modules/` because of a nested version conflict. Copying only the root
# tree silently dropped it, and the API failed at import with ERR_MODULE_NOT_FOUND while
# 127 other packages were present — which made it look like a bad prune rather than a
# missing directory.
COPY --from=prod-deps /repo /repo

# Compiled output goes on top.
COPY --from=build /repo/packages/db/dist ./packages/db/dist
COPY --from=build /repo/packages/db/migrations ./packages/db/migrations
COPY --from=build /repo/apps/api/dist ./apps/api/dist

USER node

EXPOSE 5000

# Uses the readiness endpoint, which checks the database — a container that can't reach
# Postgres is reported unhealthy rather than quietly serving 500s.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.API_PORT||5000)+'/readyz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "apps/api/dist/server.js"]
