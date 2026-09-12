ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt .
# langgraph-checkpoint-sqlite requires sqlite-vec, which ships no musllinux wheel.
# Install it with --no-deps; aiosqlite is the only dep not already pulled by langgraph.
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt \
    && pip3 install --no-cache-dir --no-deps --break-system-packages \
        "langgraph-checkpoint-sqlite==3.1.1"

COPY app ./app
COPY frontend ./frontend
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
