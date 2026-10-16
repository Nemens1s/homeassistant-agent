ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-debian:bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

# The app ships on the same Python the tests run on (.python-version);
# bookworm's own python3 is 3.11. uv installs a standalone build outside
# /root so the venv's interpreter symlink stays valid in the image.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_LINK_MODE=copy
WORKDIR /app
COPY .python-version pyproject.toml uv.lock ./
RUN uv python install \
    && uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY app ./app
COPY frontend ./frontend
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
