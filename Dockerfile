# A container that starts the MCP server and answers introspection.
#
# Deliberately minimal. This exists so a directory can verify the server runs;
# it is not how you would run it for real. Two things are missing on purpose:
#
#   * **No credentials.** The server starts and lists all 39 tools without any.
#     Prices fall back to Yahoo, the account tools refuse and say why. Nothing
#     here should ever bake a key into an image layer.
#
#   * **No dashboard.** Streamlit and plotly are a large install, and the
#     dashboard is the half of this project a container cannot usefully offer:
#     it is the human-approval surface, and it wants a browser and your own
#     machine. `pip install 'hitl-finance-mcp[dashboard]'` if you want it.
#
# Order execution is unreachable from here regardless. The submit button lives
# in the dashboard, behind a click, and no tool can reach it.

FROM python:3.12-slim

# Nothing in this image needs to write to the source tree, and a server that
# speaks JSON-RPC on stdout should not be running as root by habit.
RUN useradd --create-home --uid 10001 finance
WORKDIR /app

# Copy the packaging metadata first so a dependency layer survives a source
# edit. The build needs the README because pyproject reads it for the
# description, and the LICENSE because it declares one.
COPY pyproject.toml README.md LICENSE ./
COPY finance_mcp.py ./
COPY dashboard ./dashboard

RUN pip install --no-cache-dir . \
    && rm -rf /root/.cache

USER finance

# stdio, not a port. An MCP client runs this and speaks JSON-RPC over the pipe;
# there is nothing to expose and nothing to bind.
ENTRYPOINT ["finance-mcp"]
