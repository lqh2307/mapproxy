#!/bin/sh

# check /mapproxy/config/mapproxy.yaml and /mapproxy/config/seed.yaml
if [ -f /mapproxy/config/mapproxy.yaml ] && [ -f /mapproxy/config/seed.yaml ]; then
  echo "Found /mapproxy/config/mapproxy.yaml and /mapproxy/config/seed.yaml. Running seed..."
  
  mapproxy-seed -f /mapproxy/config/mapproxy.yaml -s /mapproxy/config/seed.yaml -c $(nproc)
elif [ ! -f /mapproxy/config/mapproxy.yaml ] || [ ! -f /mapproxy/config/seed.yaml ]; then
  echo "Missing one of /mapproxy/config/mapproxy.yaml and /mapproxy/config/seed.yaml. Creating new one from template..."

  mapproxy-util create -t base-config /mapproxy/config
fi

# run the remaining command
exec "$@"
