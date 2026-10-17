# The dev-guides.home image (registry:5000/dev-guides-home): the hub's index page and root llms.txt
# at /, and every guide of every source repo under its own /<path>/, served by nginx on port 80.
# The Jenkinsfile beside this file builds it and pins it into DevGuidesDeploy.
#
# The build context is the directory that holds the source repos side by side (sources.txt): the
# Jenkins workspace after Checkout, or the copy of /work's working trees `kc project build` stages.

# ---- build: every guide built and checked, then the hub assembled -------------------------------
FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

# Every guide is built with its own toolchain, locked in its folder's uv.lock. uv uses the image's
# Python rather than downloading one.
ENV UV_PYTHON_PREFERENCE=only-system

WORKDIR /src
COPY . /src

# The repo's `kc project test`: every guide built and checked against the guide contract
# (tools/check_guide.py), then the index page and the root llms.txt assembled into out/ and their
# links checked. Jenkins runs no other check of the site before it ships.
RUN uv run DevGuides/tools/build_hub.py /src

# ---- ship: plain nginx --------------------------------------------------------------------------
FROM nginx:alpine

COPY DevGuides/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/DevGuides/out/ /usr/share/nginx/html/

# Jenkins never runs this container, so this is the only check the config gets before prd.
RUN nginx -t
