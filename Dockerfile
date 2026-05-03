FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/home/cursoragent/.local/bin:${PATH}" \
    CURSOR2TELEGRAM_CONFIG=/etc/cursor2telegram/config.toml

RUN apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=3 update \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=3 install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
        openssh-client \
        sudo \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash cursoragent \
    && echo "cursoragent ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/90-cursoragent \
    && chmod 0440 /etc/sudoers.d/90-cursoragent \
    && install -d -m 0750 -o cursoragent -g cursoragent /var/lib/cursor2telegram /var/log/cursor2telegram \
    && install -d -m 0755 -o cursoragent -g cursoragent /var/lib/cursor2telegram/workspaces /app

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --upgrade pip wheel \
    && pip install .

USER cursoragent

RUN curl -fsS https://cursor.com/install | bash || true

VOLUME ["/etc/cursor2telegram", "/var/lib/cursor2telegram", "/var/log/cursor2telegram", "/home/cursoragent/.cursor", "/home/cursoragent/.telegram-mcp"]

CMD ["cursor2telegram"]
