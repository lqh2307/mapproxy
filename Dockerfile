FROM python:3.12-slim-bookworm AS base-libs

# set proxy
ARG http_proxy=http://10.55.123.98:3333
ARG https_proxy=http://10.55.123.98:3333

RUN \
  export DEBIAN_FRONTEND=noninteractive \
  && apt-get -y update \
  && apt-get -y upgrade \
  && apt-get -y install --no-install-recommends \
    python3-pil \
    python3-yaml \
    python3-pyproj \
    libgeos-dev \
    python3-lxml \
    libgdal-dev \
    python3-shapely \
    libxml2-dev \
    libxslt-dev \
  && apt-get -y --purge autoremove \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*


FROM base-libs AS builder

# set proxy
ARG http_proxy=http://10.55.123.98:3333
ARG https_proxy=http://10.55.123.98:3333

WORKDIR /mapproxy

COPY setup.py MANIFEST.in .
COPY mapproxy mapproxy

RUN pip wheel . -w dist


FROM base-libs AS base

# set proxy
ARG http_proxy=http://10.55.123.98:3333
ARG https_proxy=http://10.55.123.98:3333

WORKDIR /mapproxy

ENV PATH=${PATH}:/mapproxy/.local/bin

COPY --from=builder /mapproxy/dist/* dist/

RUN \
  pip install requests riak==2.4.2 redis boto3 azure-storage-blob Shapely \
  && pip install --find-links=dist --no-index MapProxy \
  && pip cache purge

COPY docker/app.py docker/entrypoint.sh .

ENTRYPOINT ["./entrypoint.sh"]

CMD ["echo", "no CMD given"]


FROM base AS nginx

# set proxy
ARG http_proxy=http://10.55.123.98:3333
ARG https_proxy=http://10.55.123.98:3333

RUN \
  export DEBIAN_FRONTEND=noninteractive \
  && apt-get -y update \
  && apt-get -y upgrade \
  && apt-get -y install --no-install-recommends \
    nginx \
    gcc \
  && pip install uwsgi \
  && apt-get -y remove gcc \
  && apt-get -y --purge autoremove \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && pip cache purge

COPY docker/uwsgi.conf docker/run-nginx.sh .
COPY docker/nginx-default.conf /etc/nginx/sites-enabled/default

EXPOSE 80

CMD ["./run-nginx.sh"]
