# Build stage
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
# Lockfile will be generated on first `npm install` — caller decides
# whether to commit it. Here we just `npm install` so the image builds
# even if no lockfile is present yet.
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

# Runtime stage — nginx serves the static bundle
FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# When deploying behind another reverse proxy (caddy / traefik / cloudflare),
# the outer proxy terminates TLS and forwards to this container on :80.
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O- http://127.0.0.1/ >/dev/null || exit 1
