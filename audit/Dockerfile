FROM python:3.14.8-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

COPY zodav_audit.py /app/zodav_audit.py
RUN mkdir /reports && chown nobody:nobody /reports

# hadolint ignore=DL3066 # nobody is defined in the alpine base image
USER nobody
ENTRYPOINT ["python", "/app/zodav_audit.py"]
CMD ["watch", "/data/zotero", "--reports", "/reports", "--backup-state", "/backup-state"]
