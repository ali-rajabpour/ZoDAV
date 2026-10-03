FROM python:3.14.8-alpine@sha256:8acac70227ce3b34da9453120c375cc5b66cd0b062d4dc6bc74286f81a3819e1

COPY zodav_audit.py /app/zodav_audit.py
RUN mkdir /reports && chown nobody:nobody /reports

# hadolint ignore=DL3066 # nobody is defined in the alpine base image
USER nobody
ENTRYPOINT ["python", "/app/zodav_audit.py"]
CMD ["watch", "/data/zotero", "--reports", "/reports", "--backup-state", "/backup-state"]
