ARG CORE_BASE_IMAGE=debian:12-slim
FROM ${CORE_BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

# Network tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    nmap \
    traceroute \
    mtr-tiny \
    dnsutils \
    openssh-client \
    telnet \
    ncat \
    tcpdump \
    curl \
    wget \
    iproute2 \
    iputils-ping \
    whois \
    net-tools \
    python3 \
    python3-pip \
    python3-venv \
    sudo \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (sudo whitelist configured at runtime by entrypoint)
RUN useradd -m -s /bin/bash hcli

# Playwright + Chromium for browser_interact
RUN python3 -m pip install --no-cache-dir --break-system-packages "playwright>=1.58,<2" \
    && python3 -m playwright install --with-deps chromium

# Shared logging library
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir --break-system-packages /tmp/shared/ && rm -rf /tmp/shared/

# App directory
WORKDIR /app

COPY core/requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY core/mcp_server.py .
COPY core/memory_server.py .

COPY skills/public/ /app/skills/public/
COPY hssh_llm/h-ssh/ /app/h-ssh/
RUN pip install --no-cache-dir --break-system-packages -r /app/h-ssh/requirements.txt
ENV PATH="/app/h-ssh:${PATH}"

COPY core/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8083 8084

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-u", "mcp_server.py"]
