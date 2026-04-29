# Debian Trixie preseed — fully unattended install
# Packer serves this via its built-in HTTP server during the ISO boot phase.
# After the installer finishes, cloud-init takes over for per-clone config.

# Locale / keyboard
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us

# Network — DHCP for the install phase; static IP set by cloud-init post-clone
d-i netcfg/choose_interface select auto
d-i netcfg/dhcp_timeout string 60
d-i netcfg/get_hostname string debian
d-i netcfg/get_domain string local
d-i netcfg/wireless_wep string

# Mirror
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string

# Clock / timezone
d-i clock-setup/utc boolean true
d-i time/zone string ${TZ}
d-i clock-setup/ntp boolean true

# Partitioning — single root, ext4, no swap (Swarm VMs manage swap via
# Ansible if needed; base template stays minimal)
d-i partman-auto/method string lvm
d-i partman-auto-lvm/guided_size string max
d-i partman-auto/choose_recipe select atomic
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-lvm/confirm boolean true
d-i partman-lvm/confirm_nooverwrite boolean true
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

# Base system
d-i base-installer/install-recommends boolean false
d-i base-installer/kernel/image string linux-image-amd64

# Root account — disabled; void user created by Packer shell provisioner
d-i passwd/root-login boolean false
d-i passwd/make-user boolean true
d-i passwd/user-fullname string ${user} user
d-i passwd/username string ${user}
d-i passwd/user-password password ${password}
d-i passwd/user-password-again password ${password}
d-i passwd/user-default-groups string sudo

# Package selection — only what's needed for Packer to SSH in;
# everything else is installed by shell provisioners post-boot
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string qemu-guest-agent cloud-init

# Bootloader
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean false
d-i grub-installer/bootdev string default

# Finish
d-i finish-install/reboot_in_progress note

# Post-install: allow Packer's SSH user to connect and run privileged commands
d-i preseed/late_command string \
  in-target sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; \
  in-target systemctl enable ssh; \
  in-target sh -c "echo '${user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/packer && chmod 440 /etc/sudoers.d/packer"
