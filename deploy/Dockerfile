# ComfyFarm control plane image: router, gateway, builder and the UI worker container (one image, four commands).
# Published as ghcr.io/frozenrainy/comfyfarm:<version> (.github/workflows/image.yml); for a closed network pull or build it outside, then
#   docker save <image>:<version> | gzip > comfyfarm.tar.gz   →   docker load < comfyfarm.tar.gz   inside.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client postgresql-client git \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 comfyfarm \
 && useradd --system --create-home --uid 10001 --gid 10001 comfyfarm \
 && groupadd --system --gid 10002 comfyfarm-builder \
 && useradd --system --no-create-home --home-dir /var/lib/comfyfarm-builder --uid 10002 --gid 10002 comfyfarm-builder
# fixed gids too (999 is a host group). The builder's account must exist here: ssh-keygen, which signs the artifacts
# GPU hosts copy (docs/design/16), refuses to run for a uid without a passwd entry
WORKDIR /opt/comfyfarm
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# what worker setup sends a host that allows builds from the dashboard (core/hostsetup.py share_dir)
COPY recipes ./share/recipes
COPY worker_ext/comfyfarm_worker ./share/worker_ext/comfyfarm_worker
COPY ui_ext/comfyfarm_ui ./share/ui_ext/comfyfarm_ui
# /state: the easy deployment's volume — a new volume takes this directory's owner, so its init runs unprivileged
RUN pip install --no-cache-dir . && mkdir -p /var/log/comfyfarm /state && chown comfyfarm:comfyfarm /var/log/comfyfarm /state
# the builder service (docs/design/15): git and the pinned uv, checked against its pinned SHA-256 (worker_setup.py)
RUN python -c "import platform; from comfyfarm.worker_setup import uv_binary; \
open('/usr/local/bin/uv', 'wb').write(uv_binary({'amd64': 'x86_64', 'arm64': 'aarch64'}.get(platform.machine(), platform.machine())))" \
 && chmod 755 /usr/local/bin/uv && /usr/local/bin/uv --version
USER comfyfarm
# openssh-client: the `ssh` restart backend; postgresql-client: `comfyfarm backup/restore`; git, uv: the builder
EXPOSE 8188 8080
CMD ["comfyfarm", "serve", "--host", "0.0.0.0", "--port", "8188", "--log-dir", "/var/log/comfyfarm"]
