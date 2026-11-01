FROM python:3.12-slim

WORKDIR /srv
COPY parser/ parser/
COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir ./parser -r app/requirements.txt

COPY . .

ENV CAD2REVIT_DATOS=/datos \
    PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app.servidor:app --host 0.0.0.0 --port ${PORT:-8000}"]
