FROM python:3.13-alpine3.22
RUN apk add --no-cache tzdata
COPY fb_reader.py /fb_reader.py
CMD ["python3", "-u", "/fb_reader.py"]
