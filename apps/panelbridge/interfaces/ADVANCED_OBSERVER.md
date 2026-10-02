# Trusted Linux advanced observer

IMPLEMENTED / UNIT-TESTED with synthetic fixtures. Live Pi behavior is unverified.
This module does not install a service, grant authority, enroll readiness or
perform a boot trial. The integration requirements in [ADVANCED.md](ADVANCED.md)
still apply.

## Contract and authority

`helper.advanced_observer.LinuxAdvancedObserver()` subclasses `LinuxControls` and
provides `observe_boot() -> BootFacts` and `health() -> HealthSample`. These two
methods only observe. Inherited CPU controls and reboot are separate, explicit
operations; neither is called by the observer. Production construction takes no
client-controlled paths, commands, facts or I/O objects. Fixture injection is
only for trusted tests. Run in the trusted root service and retain its platform
fingerprint privately.

The I/O adapter permits named proc/sysfs/boot files, numeric block-device sysfs
links, `/dev/kmsg`, and four exact `/usr/bin/vcgencmd` argument lists. It uses no
shell, credentials, network, EEPROM access, OTP query or screen firmware access.
Regular reads require root-owned files without group/other write permission;
the final component is opened without following symlinks. The service must own
its mount namespace and trusted system paths. AdvancedHelper separately pins and
checks boot-directory ancestors and ownership before any mutation.

## Boot observations

- Required CPU facts: a Pi 5 Model B revision string and compatible identifiers,
  a valid current Linux boot ID, an explicit firmware tryboot value of zero or
  one, and effective `arm_freq` from `vcgencmd get_config int`. Missing or malformed
  required facts raise `BootObservationUnavailable`. The boot ID is read again
  before return. `arm_freq` is configuration, not an instantaneous clock sample.
  [Raspberry Pi configuration commands](https://www.raspberrypi.com/documentation/computers/config_txt.html)
- Tryboot and other integer device-tree properties use four-byte big-endian
  parsing. An absent `signed` property stays unknown. A nonzero value prevents
  trial qualification. Firmware version and build time come from the bootloader
  device-tree node; no EEPROM is queried.
  [Firmware device-tree properties](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/configuration/reference.adoc)
- `boot_ramdisk` and `tryboot_a_b` each require an explicit zero/one response to
  their own `get_config` query. Missing, unsupported or ambiguous responses stay
  unknown. A/B mode and firmware boot ramdisks change which configuration or
  filesystem is used, so they are unsupported. Ordinary `auto_initramfs=1` is
  allowed. [A/B configuration](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/config_txt/autoboot.adoc),
  [firmware ramdisks](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/config_txt/boot.adoc)
- The supported mounted layout has exactly one writable FAT mount at
  `/boot/firmware` on partition 1 and one writable ext4 root on partition 2 of the
  same sysfs disk. Firmware partition and SD/USB/NVMe mode must match. Duplicate,
  bind, alias, alternate filesystem and split-disk layouts remain unverified.
  Existing autoboot/image/signature assets, config includes and alternate boot
  directives also prevent qualification. `tryboot.txt` ownership belongs to
  AdvancedHelper, allowing its active candidate to remain observable.
  [Mount identity fields](https://www.kernel.org/doc/html/latest/filesystems/proc.html),
  [boot modes](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#BOOT_ORDER)

`layout_verified` requires that supported layout and explicit false values for
all three exceptional boot modes. Missing optional layout/identity evidence
preserves the required CPU facts, so runtime CPU controls remain independently
usable. The platform SHA-256 covers the private hardware identity, OS release,
kernel release/build, reported firmware Git version/build time, mounted disk
identity and exact normal-config hash. Boot ID, active tryboot flag and trial MHz
are excluded so an unchanged platform can be compared across a trial reboot.

Mounted-layout consistency alone cannot establish which physical disk the
firmware actually read. Root safety enrollment must bind that medium and verify
the live firmware semantics, cooling, supply, stock load, backups and independent
recovery. A hash is not a signature or a substitute for those observations.

## Health coverage and limits

Temperature comes from thermal zone 0 only when its type is `cpu-thermal`;
throttling comes from the exact `get_throttled` response. Unknown values or new
unrecognized mask bits fail. The Pi kernel defines that CPU thermal zone in its
[BCM2712 source](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm64/boot/dts/broadcom/bcm2712-ds.dtsi).

Each sample opens `/dev/kmsg` read-only and nonblocking, then requires contiguous
records from sequence zero through the caught-up `EAGAIN` boundary, an early
kernel Linux-version marker, ordered timestamps and the same current boot ID.
Only kernel-facility records count as faults: userspace cannot supply that
facility through this device. Overrun, missing early records, fragments,
suppression notices, malformed data or access failure mean unknown coverage and
raise `HealthUnavailable`; no false `data_errors` value is returned.
[Linux kmsg ABI](https://github.com/torvalds/linux/blob/master/Documentation/ABI/testing/dev-kmsg)

`data_errors` is true for kernel error-or-higher priority or recognized storage,
filesystem, memory corruption, hardware exception, panic/oops and lockup text.
False means those signals were absent from the complete available kernel stream.
It does not detect silent corruption or prove hardware stability. Kernel logs
are parsed in memory and never included in results or errors. Ring rollover can
make a later sample unavailable; this conservative adapter does not reconstruct
lost evidence from private journals.

Boot and health observations have eight- and six-second budgets. Each command
has at most two seconds plus a bounded 0.2-second reap attempt and 64 KiB output.
File reads are capped at 64 KiB or less. Kernel reads cap each record at 16 KiB,
the sample at 16,384 records and 4 MiB. Results arriving after the observation
deadline fail. Ordinary filesystem reads cannot be forcibly interrupted while
the kernel is stalled; integration must supervise lost health independently and
must not claim these deadlines guarantee escape from a kernel hang. The sample
timestamp marks observation start, not completion.

The fixture tests perform no real Linux observations, writes or reboots. They
cover byte order, malformed/unknown facts, supported-layout boundaries, identity
changes, boot changes, health gaps, trusted sensor identity and production I/O
allowlists, deadlines, caps and cleanup. No hardware acceptance is implied.
