# ComfyFarm router + gateway image (one image, two commands). Published as ghcr.io/frozenrainy/comfyfarm:<version>
# (.github/workflows/image.yml); for a closed network pull or build it outside, then
#   docker save <image>:<version> | gzip > comfyfarm.tar.gz   →   docker load < comfyfarm.tar.gz   inside.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client postgresql-client \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 comfyfarm \
 && useradd --system --create-home --uid 10001 --gid 10001 comfyfarm   # a fixed gid too: 999 is a host group
WORKDIR /opt/comfyfarm
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# what worker setup sends a host that allows builds from the dashboard (core/hostsetup.py share_dir)
COPY recipes ./share/recipes
COPY worker_ext/comfyfarm_worker ./share/worker_ext/comfyfarm_worker
COPY ui_ext/comfyfarm_ui ./share/ui_ext/comfyfarm_ui
# /state: the easy deployment's volume — a new volume takes this directory's owner, so its init runs unprivileged
RUN pip install --no-cache-dir . && mkdir -p /var/log/comfyfarm /state && chown comfyfarm:comfyfarm /var/log/comfyfarm /state
USER comfyfarm
# openssh-client: the `ssh` restart backend; postgresql-client: `comfyfarm backup/restore`
EXPOSE 8188 8080
CMD ["comfyfarm", "serve", "--host", "0.0.0.0", "--port", "8188", "--log-dir", "/var/log/comfyfarm"]
