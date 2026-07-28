# Reverse-engineering record: DrongScreen 3.2.11

Analyzed APK:

```text
filename: drongscreen.apk
SHA-256: 614c01b3939aa843af7db1f27424e1a2a754e52a02f3bea2ae1c6778cc6e6f4f
package: com.drongscreen.application
versionName: 3.2.11
versionCode: 20260410
internal namespace: com.hccast.*
```

The APK contains no native libraries. The USB transport, framing, screen capture,
H.264 encoding, settings exchange, and firmware-update client are Java/Kotlin DEX
code and were recoverable without executing the APK.

## The application supports both USB role arrangements

### `MODE_ACCESSORY`

```text
screen/accessory = USB host
Android           = USB peripheral/device
```

The app obtains an `UsbAccessory`, calls `UsbManager.openAccessory()`, and wraps
its shared file descriptor in `FileInputStream` and `FileOutputStream`.

Accessory filter:

```xml
<usb-accessory
    manufacturer="ElfCast, Inc."
    model="elfcast"
    version="3.700000" />
```

This is the mode most consistent with the physical monitor failing to enumerate
when connected host-to-host to a Mac.

### `MODE_DEVICE`

```text
Android = USB host
screen  = USB peripheral/device
```

The app searches every USB interface for one bulk IN and one bulk OUT endpoint,
then opens the device with `UsbManager.openDevice()`.

Device filters recovered from resources:

```text
05ac:12ad
abcd:0002
```

The direct-host backend in this repository supports this mode. An ordinary
USB-C-to-USB-C Mac test selected the opposite role and showed no new USB device.

### Hardware observation, separate from the APK-derived filters

On 2026-07-14, a USB-A host-forcing topology made the physical screen enumerate
transiently as `1cbe:0005`. This ID was **not** recovered from the APK and must not
be substituted into the APK filter record above. The observed device exposed:

```text
USB 2.0 High Speed, 480 Mb/s
interface 0: class/subclass/protocol ff/06/50
bulk IN:  0x81, max packet 512
bulk OUT: 0x02, max packet 512
userspace-visible lifetime in the first passive capture: about 0.706 seconds
kernel enumeration-to-loss interval: repeatedly about 0.730--0.733 seconds
```

Bounded discovery subsequently found it. A later claim-only run then opened and
claimed interface 0, found bulk OUT `0x02` and bulk IN `0x81`, and released cleanly
with status 0. A subsequent two-second hold confirmed genuine ownership via
IORegistry, but the device detached 0.721994 seconds after confirmed interface-open
and 0.732 seconds after enumeration. Release was attempted 1.283203 seconds after the
detach. The screen itself remained on its mirroring setup UI. Those claim-only runs
performed no application payload I/O. A later separately bounded run completed one
20-byte `SETR` bulk-OUT transfer, received zero bytes during a 500 ms bulk-IN window,
and then left `1cbe:0005` enumerated until DATA was removed approximately 13 minutes
46 seconds later. The LCD was black during that boot attempt, so the recovered manual's
display-only-off behavior makes its physical power state ambiguous. No `SETV`,
settings, screen information, video, firmware command, configuration activation, or
kernel-driver detach occurred. Whole-device open errors in the trace concern
`IOUSBHostDevice` and do not contradict the successful `IOUSBHostInterface` claim.
`1cbe:0005` remains only a hardware-observed direct-host candidate until it returns a
valid `SETV`.

The public identity adds a second evidence boundary. Texas Instruments' official
USB library header defines VID `0x1cbe` as TI and PID `0x0005` as its mass-storage
example (`USB_PID_MSC`). The screen's interface is not standard USB mass storage:
it reports vendor class `0xff`, although its subclass/protocol `06/50` echo the
SCSI-transparent / bulk-only values commonly associated with mass storage. The
State-controlled follow-up associated stable `1cbe:0005` with a powered
flashing-blue/black-LCD state. A normal white-light boot reached the visible setup UI,
exposed `1cbe:0005` for about one second, and then removed it while the UI continued.
Neither a second factory-format `SETR` with a three-second wait nor a read-only SCSI
INQUIRY returned any bytes. The personality may be transient, pre-protocol,
boot/diagnostic, dormant, or based on a borrowed descriptor template. None of these
observations proves HCCAST. The opposite-role gadget branch later produced the
hardware-verified result through direct Android Open Accessory identity
`18d1:2d00`.

Primary source: <https://software-dl.ti.com/simplelink/esd/simplelink_msp432e4_sdk/1.60.00.10/docs/usblib/msp432e4/html/usb-ids_8h.html>

USB class-code reference: <https://www.usb.org/defined-class-codes>

## HCCAST frame format

Every command is one stream frame:

```text
offset  size  encoding  meaning
0       4     BE u32    total length, including 16-byte header
4       4     BE u32    monotonically increasing sequence
8       4     bytes     command magic
12      4     BE u32    flags/argument
16      n     bytes     payload
```

Command bytes:

```text
VID   00 44 49 56
AUD   00 44 55 41
SETS  53 54 45 53
SETR  52 54 45 53
SETC  43 54 45 53
SETF  46 54 45 53
SETV  56 54 45 53
DBG   00 47 42 44
STOP  50 4f 54 53
UPG   00 47 50 55
UPGI  49 47 50 55
PING  47 4e 49 50
SINF  46 4e 49 53
```

`VID` and `AUD` flags are zero. `SETR` flags are one. All other observed command
flags are zero.

## Initial handshake

The factory app does this after opening either transport:

1. Send `SETR` every three seconds until the device is ready.
2. `SETR` payload is four zero bytes and flags are `1`.
3. Screen replies with `SETV`.
4. App parses the device information and marks the transport ready.
5. When mirroring starts, app sends `SINF`, then `VID` packets.

`SETV` layout, offsets relative to the 16-byte payload start:

```text
0       BE u32   mirror type
4       BE u32   mirror resolution preset
8       BE u32   audio enabled
12      32 B     NUL-terminated product string
44      4 B      packed version
48      256 B    NUL-terminated URL
304     BE u32   vertical mode (optional)
308     BE u32   vertical auto-revolve (optional)
312     BE u32   full mode (optional)
```

The factory app requires a total packet length of at least 320 bytes and accepts
optional fields when the total length exceeds 320/324/328.

## Settings and screen information

`SETS` payload is four BE u32 values:

```text
mirror_resolution
vertical_mode
vertical_auto_revolve
full_mode
```

`SINF` payload is five BE u32 values:

```text
orientation              0 portrait, 1 landscape
encoder_width
encoder_height
source_display_short_side
source_display_long_side
```

## Video

Factory pipeline:

```text
MediaProjection
  -> VirtualDisplay("screen-display", ...)
  -> MediaCodec.createEncoderByType("video/avc")
  -> HCCAST VID packets
```

Observed encoder settings:

```text
codec: H.264 / video/avc
frame rate: 60 fps
bitrate: 16,000,000 bit/s
I-frame interval: 10 seconds
input: Surface color format
```

The app copies each `MediaCodec` output buffer directly into a `VID` payload. It
detects an IDR by checking `buffer[4] == 0x65`, which strongly indicates that the
four-byte Annex-B start code is present:

```text
00 00 00 01 65 ...
```

Therefore this driver preserves Annex-B start codes. The earlier generated
prototype that stripped them was wrong.

## USB transfer behavior

In `MODE_DEVICE`, the APK splits each complete HCCAST frame into 16,384-byte USB
writes. It does **not** cap a logical HCCAST video frame at 64 KiB. If the full
frame length is an exact multiple of the high-speed max-packet size, it attempts
a zero-length packet.

In `MODE_ACCESSORY`, the app writes the complete byte array through the accessory
file descriptor. USB transfer boundaries are therefore not protocol boundaries;
receivers need a stream parser keyed by the 32-bit total-length field.

## Audio

The app can capture Android playback audio at 48 kHz, 16-bit stereo and send raw
chunks in `AUD` frames. Audio is intentionally deferred in this prototype until
video and USB role negotiation work on real hardware.

## Non-transport application components intentionally excluded

The factory application also contains Wi-Fi configuration, account/diagnostic,
and device-maintenance components. None is required for the local wired HCCAST
video transport. This repository neither implements nor contacts those services.
