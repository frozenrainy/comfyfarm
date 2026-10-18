# ComfyFarm router + gateway image (one image, two commands). Build outside the closed network, then
#   docker save comfyfarm:<version> | gzip > comfyfarm.tar.gz   →   docker load < comfyfarm.tar.gz   inside.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client postgresql-client \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --uid 10001 comfyfarm
WORKDIR /opt/comfyfarm
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /var/log/comfyfarm && chown comfyfarm /var/log/comfyfarm
USER comfyfarm
# openssh-client: the `ssh` restart backend; postgresql-client: `comfyfarm backup/restore`
EXPOSE 8188 8080
CMD ["comfyfarm", "serve", "--host", "0.0.0.0", "--port", "8188", "--log-dir", "/var/log/comfyfarm"]
