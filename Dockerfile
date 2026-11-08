# ---------------------------------------------------------------------------
# Etapa 1: convertidor DWG -> DXF (LibreDWG, software libre GPL-3).
# Si la descarga o la compilación fallan, la imagen se construye igual y la app
# informa que por ahora solo acepta DXF (no se rompe el despliegue).
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS libredwg
ARG LIBREDWG_VERSION=0.13.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential autoconf automake libtool pkg-config git texinfo ca-certificates \
 && mkdir -p /opt/libredwg \
 && ( git clone --depth 1 --branch "${LIBREDWG_VERSION}" --recurse-submodules --shallow-submodules \
        https://github.com/LibreDWG/libredwg /src \
      && cd /src && sh autogen.sh \
      && ./configure --prefix=/opt/libredwg --disable-bindings --enable-release \
      && make -j"$(nproc)" && make install \
      || echo "AVISO: no se pudo compilar LibreDWG; la app aceptará solo DXF" ) \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Etapa 2: la app
# ---------------------------------------------------------------------------
FROM python:3.12-slim

COPY --from=libredwg /opt/libredwg /opt/libredwg
ENV PATH="/opt/libredwg/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/libredwg/lib"

WORKDIR /srv
COPY parser/ parser/
COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir ./parser -r app/requirements.txt

COPY . .

ENV CAD2REVIT_DATOS=/datos
EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app.servidor:app --host 0.0.0.0 --port ${PORT:-8000}"]
