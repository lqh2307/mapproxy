from setuptools import setup, find_packages

install_requires = [
  "PyYAML>=3.0",
  "future",
  "pyproj>=2",
  "jsonschema>=4",
  "werkzeug",
  "Pillow !=2.4.0,!=8.3.0,!=8.3.1"
]

setup(
  name="MapProxy",
  version="1.0.0",
  description="An accelerating proxy for tile and web map services",
  packages=find_packages(),
  include_package_data=True,
  entry_points={
    "console_scripts": [
      "mapproxy-seed = mapproxy.seed.script:main",
      "mapproxy-util = mapproxy.script.util:main",
    ],
  },
  package_data={"": ["*.xml", "*.yaml", "*.ttf", "*.wsgi", "*.ini", "*.json"]},
  install_requires=install_requires,
  classifiers=[
    "Development Status :: 5 - Production/Stable",
    "License :: OSI Approved :: Apache Software License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3.8",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Internet :: Proxy Servers",
    "Topic :: Internet :: WWW/HTTP :: WSGI",
    "Topic :: Scientific/Engineering :: GIS",
  ],
  zip_safe=False
)
