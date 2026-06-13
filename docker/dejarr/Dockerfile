FROM python:3.12-alpine

RUN apk add --no-cache tzdata

WORKDIR /app
COPY dejarr.py /app/dejarr.py

EXPOSE 8000

CMD ["python", "/app/dejarr.py"]
