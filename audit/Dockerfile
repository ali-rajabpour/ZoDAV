FROM python:3.15.0-alpine@sha256:e7044ccc46c44b6174e0051dbdcb84c79e19d0f2c398d28d5d45521ea42b01e2

COPY zodav_audit.py /app/zodav_audit.py
RUN mkdir /reports && chown nobody:nobody /reports

# hadolint ignore=DL3066 # nobody is defined in the alpine base image
USER nobody
ENTRYPOINT ["python", "/app/zodav_audit.py"]
CMD ["watch", "/data/zotero", "--reports", "/reports", "--backup-state", "/backup-state"]
