ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk update --no-cache && apk upgrade --no-cache

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY rootfs /

RUN chmod +x /etc/services.d/cytech_ai/run
