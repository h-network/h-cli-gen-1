FROM python:3.12-slim

# Non-root user
RUN useradd -r -u 1000 -m -s /bin/bash hcli

# Shared logging library
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared/ && rm -rf /tmp/shared/

WORKDIR /app

COPY interfaces/web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY interfaces/web/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY interfaces/web/ .

USER hcli
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-u", "bot.py"]
