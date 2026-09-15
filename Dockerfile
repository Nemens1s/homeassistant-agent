ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

RUN apk add --no-cache python3

WORKDIR /app
COPY pyproject.toml uv.lock ./
# sqlite-vec ships no musllinux wheel; skip it — langgraph-checkpoint-sqlite handles the absence.
RUN UV_SYSTEM_PYTHON=1 uv sync --frozen --no-dev \
        --no-install-package sqlite-vec

COPY app ./app
COPY frontend ./frontend
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
