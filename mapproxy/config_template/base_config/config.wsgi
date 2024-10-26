# WSGI module for use with Apache mod_wsgi or gunicorn

from logging.config import fileConfig
from mapproxy.wsgiapp import make_wsgi_app

fileConfig("config/log.ini", {
  "here": "config",
})

from mapproxy.wsgiapp import make_wsgi_app
application = make_wsgi_app(r'%(mapproxy_conf)s')
