FROM python:3.12.14-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111

COPY zodav_audit.py /app/zodav_audit.py
RUN mkdir /reports && chown nobody:nobody /reports

# hadolint ignore=DL3066 # nobody is defined in the alpine base image
USER nobody
ENTRYPOINT ["python", "/app/zodav_audit.py"]
CMD ["watch", "/data/zotero", "--reports", "/reports", "--backup-state", "/backup-state"]
