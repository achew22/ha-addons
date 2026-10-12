ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 tzdata
COPY fb_reader.py /fb_reader.py
CMD ["python3", "-u", "/fb_reader.py"]
