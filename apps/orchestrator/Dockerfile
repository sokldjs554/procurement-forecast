# syntax=docker/dockerfile:1.7
# The job API and download worker. ROLE=api|worker|all picks what the container runs.

FROM node:22-bookworm-slim AS deps
ENV PNPM_HOME=/pnpm
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
 && pnpm install --frozen-lockfile

FROM deps AS build
COPY . .
RUN pnpm build && pnpm prune --prod

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production PORT=8787
RUN useradd --system --uid 10001 orchestrator
WORKDIR /app
COPY --from=build --chown=orchestrator /app/package.json ./
COPY --from=build --chown=orchestrator /app/node_modules ./node_modules
COPY --from=build --chown=orchestrator /app/dist ./dist
USER orchestrator
EXPOSE 8787
CMD ["node", "dist/main.js"]
