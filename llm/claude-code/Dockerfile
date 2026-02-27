FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates gnupg python3 python3-pip \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@2.1.39

# Non-root user — ubuntu:24.04 ships with ubuntu(1000), so use that
RUN usermod -l hcli -d /home/hcli -m ubuntu \
    && groupmod -n hcli ubuntu

# Shared logging library
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir --break-system-packages /tmp/shared/ "redis>=7.1,<8" "mcp>=1.26,<2" "psycopg2-binary>=2.9,<3" && rm -rf /tmp/shared/

WORKDIR /app

# Orchestration modules — split into bus (Redis), worker (Claude), dispatcher (main loop)
COPY orchestration/bus.py .
COPY orchestration/worker.py .
COPY orchestration/dispatcher.py .
COPY llm/claude-code/firewall.py .
COPY llm/claude-code/memory_proxy.py .
COPY llm/claude-code/mcp-config.json .
COPY llm/claude-code/CLAUDE.md .
COPY llm/groundRules.md .
# context.md is optional — copied from llm/context.md.template by user
COPY llm/context.md* .
COPY llm/claude-code/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
RUN mkdir -p /app/skills

USER hcli

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-u", "dispatcher.py"]
