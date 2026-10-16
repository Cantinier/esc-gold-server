FROM python:3.12-slim
WORKDIR /app
COPY relay/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home relay
COPY relay /app/relay
COPY signaldesk/core.py signaldesk/wire.py signaldesk/__init__.py /app/signaldesk/
RUN chmod -R a+rX /app/relay /app/signaldesk
USER relay
ENV SIGNALDESK_RELAY_DATA=/data
CMD ["gunicorn", "--bind", "0.0.0.0:4113", "--workers", "1", "--threads", "32", "--timeout", "0", "relay.server:create_app()"]
