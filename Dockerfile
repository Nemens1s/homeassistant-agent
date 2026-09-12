ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt .
# sqlite-vec (pulled by langgraph-checkpoint-sqlite) has no musllinux wheel,
# so it must compile from source — provide build tools and remove them after.
RUN apk add --no-cache --virtual .build-deps gcc musl-dev python3-dev \
    && pip3 install --no-cache-dir --break-system-packages -r requirements.txt \
    && apk del .build-deps

COPY app ./app
COPY frontend ./frontend
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
