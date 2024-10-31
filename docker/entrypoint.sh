#!/bin/sh

# check config/mapproxy.yaml and config/seed.yaml
if [ -f config/mapproxy.yaml ] && [ -f config/seed.yaml ]; then
  echo "Found config/mapproxy.yaml and config/seed.yaml"
elif [ ! -f config/mapproxy.yaml ] && [ ! -f config/seed.yaml ]; then
  echo "Missing config/mapproxy.yaml and config/seed.yaml. Creating new one from template..."

  mapproxy-util create -t base-config config
fi

# check config/log.ini
if [ -f config/log.ini ]; then
  echo "Found config/log.ini"
else
  echo "Missing config/log.ini. Creating new one from template..."

  mapproxy-util create -t log-ini config/log.ini
fi

# setup seed
if [ "${NO_SEED}" != "YES" ]; then
  SEED_NUM_CORE=${SEED_NUM_CORE:-$(nproc)}

  echo "Seeding with ${SEED_NUM_CORE} cores..."

  mapproxy-seed -f config/mapproxy.yaml -s config/seed.yaml -c ${SEED_NUM_CORE} &
fi

# run the remaining command
exec "$@"
