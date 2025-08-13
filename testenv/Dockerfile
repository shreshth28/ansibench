# throwaway linux box with sshd, used as a fake remote for tests
FROM ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        openssh-server python3 git build-essential sudo ca-certificates util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

RUN useradd -m -s /bin/bash bench \
    && echo "bench ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/bench

COPY id_ed25519.pub /home/bench/.ssh/authorized_keys
RUN chown -R bench:bench /home/bench/.ssh && chmod 700 /home/bench/.ssh && chmod 600 /home/bench/.ssh/authorized_keys

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
