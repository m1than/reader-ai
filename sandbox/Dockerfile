# reader-kernel:local — the python3 lab kernel image (spec/modules/OPS-SANDBOX.yaml). Runs under OPS-DEPLOY's
# `x-kernel` template: read-only root, uid 1000, tmpfs /home/lab, /tmp and /run/evcxr, no capabilities.
# Build context: sandbox/. Python packages come only from the hash-pinned lock (`bash sandbox/lock.sh`), as
# binary wheels; no fetch-and-run installers, no compiler.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

COPY requirements.lock /opt/reader-kernel/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r /opt/reader-kernel/requirements.lock \
 && pip check \
 && rm -rf /root/.cache

# The lab user (FR-SANDBOX-2). Its home is a tmpfs at run time.
RUN groupadd --gid 1000 lab && useradd --uid 1000 --gid 1000 --home-dir /home/lab --no-create-home lab

COPY --chmod=0755 reader_kernel_entry.py /usr/local/bin/reader-kernel-entry
COPY --chmod=0755 reader_kernel_health.py /usr/local/bin/reader-kernel-health

# Exactly one kernelspec, python3, installed by ipykernel into the system data dir (FR-SANDBOX-2).
RUN test "$(ls /usr/local/share/jupyter/kernels)" = "python3" \
 && chown -R root:root /usr/local && chmod -R go-w /usr/local

# PATH without the base image's duplicate /usr/local/bin (FR-SANDBOX-8).
ENV HOME=/home/lab LANG=C.UTF-8 PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /home/lab
USER 1000:1000
EXPOSE 8888 8889
ENTRYPOINT ["/usr/local/bin/reader-kernel-entry"]
