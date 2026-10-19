# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN groupadd --gid 10001 platform && useradd --uid 10001 --gid platform --create-home platform
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY sovite ./sovite
COPY scripts ./scripts
RUN pip install --no-deps --no-cache-dir . && mkdir -p /state /workspaces && chown -R 10001:10001 /state /workspaces
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["sovite"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]

FROM node:24-bookworm-slim AS codex-package
ARG CODEX_VERSION=0.163.0
RUN npm install --global --ignore-scripts @openai/codex@${CODEX_VERSION}

FROM runtime AS native-codex
USER root
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates git bubblewrap libstdc++6 libatomic1 && rm -rf /var/lib/apt/lists/*
COPY --from=codex-package /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-package /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
USER 10001:10001
CMD ["worker"]
