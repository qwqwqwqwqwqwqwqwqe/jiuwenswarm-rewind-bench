FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git make bash gcc g++ sqlite3 patch && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy pytest django pypdf cryptography pyyaml packaging requests
RUN pip install --no-cache-dir sphinx==8.2.3
RUN pip install --no-cache-dir Cython setuptools wheel
RUN python -c "import Cython, setuptools; import shutil; assert shutil.which('sphinx-build')"
RUN pip install --no-cache-dir grpcio grpcio-tools
RUN python -c "import grpc, grpc_tools.protoc, jinja2"
RUN pip install --no-cache-dir Pillow jsonschema
RUN python -c "from PIL import Image; import jsonschema"
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /testbed
