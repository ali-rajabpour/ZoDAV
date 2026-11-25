FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

COPY zodav_audit.py /app/zodav_audit.py
RUN mkdir /reports && chown nobody:nobody /reports

# hadolint ignore=DL3066 # nobody is defined in the alpine base image
USER nobody
ENTRYPOINT ["python", "/app/zodav_audit.py"]
CMD ["watch", "/data/zotero", "--reports", "/reports", "--backup-state", "/backup-state"]
