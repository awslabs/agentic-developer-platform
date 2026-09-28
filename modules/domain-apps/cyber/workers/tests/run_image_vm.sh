#!/bin/bash
# Exercise the exported worker image under a modern kernel without host/KVM access.
set -euo pipefail
mkdir -p /work/root
# The rootfs is exported from the security-test stage (the production base plus
# pytest). It contains no AWS environment, credentials, host mounts or network.
tar --numeric-owner -xf /input/rootfs.tar -C /work/root
install -m 755 /test-tools/vm_init.py /work/root/adp-ci-init
truncate -s 5G /work/root.ext4
mkfs.ext4 -q -F -d /work/root /work/root.ext4
KERNEL=$(find /boot -maxdepth 1 -name 'vmlinuz-*' -print -quit)
INITRD=$(find /boot -maxdepth 1 -name 'initrd.img-*' -print -quit)
test -n "$KERNEL" && test -n "$INITRD"
# TCG is software virtualization: no /dev/kvm, privileged mount or new cloud
# resource. There is deliberately no NIC, host filesystem share or guest agent.
timeout 600 qemu-system-x86_64 \
  -accel tcg -cpu max -smp 2 -m 4096 \
  -nodefaults -no-reboot -display none -serial stdio -monitor none \
  -kernel "$KERNEL" -initrd "$INITRD" \
  -append 'console=ttyS0 root=/dev/vda ro rootwait init=/adp-ci-init lsm=landlock panic=-1' \
  -drive file=/work/root.ext4,format=raw,if=virtio,cache=unsafe \
  2>&1 | tee /work/kernel-test.log
# A timeout, boot failure, missing marker or nonzero pytest exit all fail CI.
grep -qx 'ADP_CYBER_IMAGE_RESULT=0' <(tr -d '\r' < /work/kernel-test.log)
