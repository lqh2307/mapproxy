# WSGI module for use with Apache mod_wsgi or gunicorn

from logging.config import fileConfig
from mapproxy.wsgiapp import make_wsgi_app

fileConfig("config/log.ini", {
  "here": "config",
})

application = make_wsgi_app("config/mapproxy.yaml", reloader=True)
