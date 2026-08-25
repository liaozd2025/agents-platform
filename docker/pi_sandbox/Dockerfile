FROM enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox@sha256:742062f99915e5495df8d4bfeaf40a93197c87c7c47b4e2407cd2b6356df8f48

WORKDIR /opt/yuxi-pi-runner
COPY backend/package/yuxi/pi_runner/package.json backend/package/yuxi/pi_runner/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY backend/package/yuxi/pi_runner/runner.mjs ./runner.mjs
