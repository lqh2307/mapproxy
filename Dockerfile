FROM python:3.12-slim-bookworm AS builder

RUN export DEBIAN_FRONTEND=noninteractive && apt-get -y update && apt-get -y upgrade && apt-get -y install --no-install-recommends \
  python3-pil \
  python3-yaml \
  python3-pyproj \
  libgeos-dev \
  python3-lxml \
  libgdal-dev \
  python3-shapely \
  libxml2-dev \
  libxslt-dev && \
  apt-get -y --purge autoremove && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/*

WORKDIR /mapproxy

COPY setup.py MANIFEST.in README.md CHANGES.txt AUTHORS.txt COPYING.txt LICENSE.txt ./
COPY mapproxy mapproxy

RUN pip wheel . -w dist

RUN groupadd mapproxy && \
    useradd --home-dir /mapproxy -s /bin/bash -g mapproxy mapproxy && \
    chown -R mapproxy:mapproxy /mapproxy

USER mapproxy:mapproxy

ENV PATH="${PATH}:/mapproxy/.local/bin"

RUN pip install requests riak==2.4.2 redis boto3 azure-storage-blob Shapely && \
  pip install --find-links=./dist --no-index MapProxy && \
  pip cache purge

ENTRYPOINT ["./entrypoint.sh"]

CMD ["echo", "no CMD given"]


FROM base AS nginx

RUN export DEBIAN_FRONTEND=noninteractive && apt-get -y update && apt-get -y upgrade && apt-get -y install --no-install-recommends nginx gcc \
  && apt-get -y --purge autoremove \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

USER mapproxy:mapproxy

RUN pip install uwsgi && \
    pip cache purge

COPY docker/uwsgi.conf .
COPY docker/nginx-default.conf /etc/nginx/sites-enabled/default
COPY docker/run-nginx.sh .

EXPOSE 80

USER root:root

RUN chown -R mapproxy:mapproxy /var/log/nginx \
    && chown -R mapproxy:mapproxy /var/lib/nginx \
    && chown -R mapproxy:mapproxy /etc/nginx/conf.d \
    && touch /var/run/nginx.pid \
    && chown -R mapproxy:mapproxy /var/run/nginx.pid

USER mapproxy:mapproxy

CMD ["./run-nginx.sh"]
