# Spike image (never shipped): Kernel Gateway with python3, rust (evcxr) and deno kernels (ADR-0003),
# run hardened as in ADR-0014. The pilot crates are pre-built into a read-only evcxr cache.
FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates build-essential unzip \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir jupyter_kernel_gateway==3.0.1 ipykernel==7.4.0

ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo DENO_INSTALL=/opt/deno \
    PATH=/opt/cargo/bin:/opt/deno/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable \
 && cargo install --locked evcxr_jupyter@0.23.0 evcxr_repl@0.23.0 \
 && rm -rf /opt/cargo/registry /opt/cargo/git
RUN curl -fsSL https://deno.land/install.sh | sh -s -- v2.9.8 --no-modify-path

# Kernelspecs go to the system data dir so the non-root user sees them.
RUN evcxr_jupyter --install && deno jupyter --install \
 && mkdir -p /usr/local/share/jupyter/kernels \
 && cp -r /root/.local/share/jupyter/kernels/* /usr/local/share/jupyter/kernels/ \
 && rm -rf /root/.local/share/jupyter

RUN useradd -m -d /home/lab -u 1000 lab

# evcxr reads its init file and cache from XDG dirs; both live outside the tmpfs home.
# The lab user gets its own CARGO_HOME holding the downloaded pilot crates (no egress at runtime).
ENV XDG_CONFIG_HOME=/opt/evcxr-config XDG_CACHE_HOME=/opt/evcxr-cache CARGO_HOME=/opt/cargo-lab
COPY prebuild.evcxr /opt/evcxr-prebuild.evcxr
RUN mkdir -p /opt/evcxr-config/evcxr /opt/evcxr-cache /opt/cargo-lab \
 && echo ':cache 2000' > /opt/evcxr-config/evcxr/init.evcxr \
 && chown -R lab /opt/evcxr-cache /opt/cargo-lab
USER lab
RUN evcxr < /opt/evcxr-prebuild.evcxr 2>&1 | tail -5 && du -sh /opt/evcxr-cache /opt/cargo-lab

WORKDIR /home/lab
EXPOSE 8888
# The rust kernelspec starts evcxr through a launcher that gives each kernel a private work dir and
# cache copy in the exec tmpfs /run/evcxr (see evcxr-kernel.sh).
USER root
COPY evcxr-kernel.sh /usr/local/bin/evcxr-kernel
RUN chmod 755 /usr/local/bin/evcxr-kernel && python -c "import json; p='/usr/local/share/jupyter/kernels/rust/kernel.json'; \
k=json.load(open(p)); k['argv'][0]='/usr/local/bin/evcxr-kernel'; json.dump(k, open(p, 'w'))"
USER lab
CMD ["jupyter", "kernelgateway", "--KernelGatewayApp.ip=0.0.0.0", "--KernelGatewayApp.port=8888", \
     "--KernelGatewayApp.api=kernel_gateway.jupyter_websocket", "--KernelGatewayApp.auth_token="]
