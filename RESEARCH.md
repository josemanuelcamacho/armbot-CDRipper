# Engineering Documentation and Robotics and CD Ripping Concepts 
## (Request for information before the project itself was carried out)

> No information about the robotic arms ***grouped actions*** nor ***calibration*** is included. We refer to HiWonder documentation and this codebase for further details.
> 
## System Architecture & Dataflow

The automated CD ripping system distributes operational loads across two discrete processing tiers: the Raspberry Pi 4 acts as the orchestration and media processing host, while the Arduino Uno functions as a dedicated real-time kinematics controller. 

| Component | Primary Function | Hardware Interface | Software / Protocol |
| :--- | :--- | :--- | :--- |
| **Raspberry Pi 4** | State machine orchestrator, metadata fetching, FLAC encoding, AccurateRip verification. | USB 3.0 (Drive), USB 2.0 (Arduino) | Python (Orchestrator), `whipper` (Ripper), `udev` (Triggers) |
| **Arduino Uno** | Translates spatial coordinates into PWM signals for the HiWonder arm servos. | USB 2.0 (Serial to Pi), PWM (Servos) | C++ Firmware, Serial UART (115200 baud) |
| **HiWonder Arm** | Physical manipulation of optical media (Pick and Place). | PWM cables to Uno/Shield | Controlled via Serial packets |
| **Optical Drive** | Reading raw PCM audio and subcode data from the CD. | USB 3.0 / SATA to USB adapter | SCSI / ATAPI commands, `cdparanoia` / `whipper` |

## Serial Communication Protocol

Robust communication between the Pi and the Arduino is critical to prevent mid-movement failures or disc drops. Based on standard robotic serial implementations, the system must utilize a framed packet structure to prevent buffer overruns or partial command execution.

*   **Baud Rate:** 115200 (minimizes latency without introducing USB overhead instability).
*   **Packet Structure:** Frame commands with start (`<`) and end (`>`) markers. 
*   **Command Syntax:** `<COMMAND_ID, ..., PARAM_1, PARAM_2, PARAM_N>`
*   **Example Handshake:** 
    *   Pi sends: `<MOVE, 10, 120, 45, 90 90,>` (Command to move 5 servos to specific angles)
    *   Arduino acknowledges: `<ACK, MOVE>`
    *   Arduino completes motion: `<DONE, MOVE>`

This blocking command structure ensures the Pi's Python orchestrator waits for physical movement completion before triggering the CD drive tray to close or the ripping software to launch.

## Robotic Kinematics & Handling

Handling optical media mechanically requires high precision to avoid scratching the polycarbonate layer or damaging the drive tray spindle. 

*   **End-Effector Design:** Standard servo-driven claws are high-risk for CDs. The optimal pre-design choice is retrofitting the HiWonder arm with a small pneumatic suction cup and a 5V/12V vacuum pump, or using a specialized three-point inner-hub expanding gripper. 
*   **Coordinate Mapping:** The Arduino firmware must store pre-calibrated operational waypoints. Rather than having the Pi calculate kinematics in real-time, the Pi should send state-based commands (`<GOTO, INPUT_STACK>`, `<GOTO, DRIVE_TRAY>`, `<GOTO, SUCCESS_BIN>`).
*   **Trajectory Smoothing:** Implement the standard `Servo.h` library with a timing loop (or a specialized library like `VarSpeedServo`) to sweep the servos gradually. Jerky movements at maximum speed will cause discs to slip from the end-effector.

## Host Orchestration & State Machine (Raspberry Pi)

The central Python script on the Raspberry Pi manages the lifecycle of a single disc through a strict state machine.

1.  **STATE 0 (Idle/Homed):** Arm is clear of the drive. Pi verifies drive tray is empty.
2.  **STATE 1 (Pick):** Pi commands Arduino to retrieve a disc from the input spindle and hover over the open drive tray.
3.  **STATE 2 (Load):** Arm lowers the disc onto the drive spindle, releases, and retreats. Pi sends an OS-level command (`eject -t /dev/sr0`) to retract the tray.
4.  **STATE 3 (Detect):** Pi listens for `udev` block device events or uses a polling loop to confirm a standard Audio CD (CD-DA) is mounted.
5.  **STATE 4 (Rip):** Python calls the command-line ripping utility as a subprocess.
6.  **STATE 5 (Eject & Sort):** Drive opens. Arm picks the disc. Based on the exit code of the ripping utility, the arm places the disc in the "Completed" spindle or the "Failed/Scratched" spindle.

## Bit-Perfect Ripping Pipeline

To achieve archival-quality FLAC files equivalent to Exact Audio Copy (Windows), the Linux-based Raspberry Pi must utilize `whipper`. Standard utilities like `cdparanoia` alone do not verify rips against global checksum databases.

*   **Engine:** Install `whipper` and its dependencies (`cdparanoia`, `cdrdao`, `flac`, `sox`, `libsndfile1`).
*   **Drive Offset:** Before production, the CD drive's read offset must be calibrated using a known-good disc via `whipper drive analyze`.
*   **Execution Command:** The Python script triggers `whipper cd rip`. This automatically handles TOC (Table of Contents) reading, MusicBrainz metadata querying, ripping to FLAC, and verification against the AccurateRip database.

## Ripping Verification Databases & Software Implementations

While this headless Pi pipeline utilizes `whipper`, understanding how other popular ripping tools independently verify data against the AccurateRip and CUETools (CTDB) databases provides insight into software-level bit-perfect validation:

*   **abcde (A Better CD Encoder - Linux):** `abcde` relies primarily on `cdparanoia` and does not natively query the AccurateRip database during extraction. To achieve verification, users must manually configure the drive's hardware read offset by passing the `--sample-offset` argument into `CDPARANOIAOPTS`. Once the FLAC files are generated, an external Python script (such as `arverify`) must be triggered to compute the audio checksums and cross-reference them with the AccurateRip server. If all album tracks are present and the offset was correctly applied, the external script validates the rip.
*   **XLD (X Lossless Decoder - macOS):** XLD natively integrates both AccurateRip and CTDB verification. Upon reading the CD's Table of Contents (TOC) and extracting the PCM audio, XLD calculates the CRC32 checksums for each track. It then dynamically queries the databases. If the local checksums match those submitted by other users, XLD logs the rip as bit-perfect, providing absolute confidence in the extraction even without deep hardware-level secure read modes.
*   **CUETools / CUERipper (Windows):** CUETools interfaces with both AccurateRip and its own CUETools Database (CTDB), offering advanced features that standard rippers lack. 
    *   **Offset Detection:** AccurateRip usually requires the rip to be perfectly offset-corrected to match the database. CUETools, however, can mathematically shift the audio track data by up to ~6,000 samples during the verification process. This allows it to successfully verify rips against AccurateRip even if the user neglected to configure their drive offset prior to ripping.
    *   **Error Repair:** Unlike AccurateRip which only stores checksums, CTDB stores Reed-Solomon parity data. If a CD is scratched and produces localized read errors, CUETools can use the downloaded parity data from CTDB to mathematically reconstruct and repair the corrupted audio sectors, producing a flawless FLAC image from a damaged disc.

## Failure Modes & Mitigation Strategies

*   **Tray Collision:** If the script desyncs, the CD tray may close on the robotic arm. **Mitigation:** Query the optical drive state (`setcd -i /dev/sr0` or PyGame CDROM module) to definitively check if the tray is open or closed before allowing the arm to enter the tray envelope.
*   **Unreadable / Damaged Discs:** Ripping software will continuously retry reading scratched sectors, which can halt the automated queue indefinitely. **Mitigation:** Configure a hard timeout in the Python `subprocess` call. If a disc takes longer than 45 minutes, terminate the process, eject the disc, place it in the "Reject" pile, and proceed to the next disc.
*   **Misalignment on Spindle:** A disc dropped slightly off-center will jam the drive tray when closing. **Mitigation:** Implement a funnel-shaped 3D-printed guide on top of the drive tray to passively guide the disc perfectly onto the drive's internal spindle when dropped.


## Reference URLs

*   **Title:** Jack the DVD Ripping Robot (Hackaday)
    **Description:** An article showcasing an early automated disc-ripping robot project utilizing an arm and a custom script.
    **URL:** https://hackaday.com/2013/09/05/jack-the-dvd-ripping-robot/

*   **Title:** JackTheRipperBot PowerShell Script
    **Description:** GitHub repository containing the PC-side PowerShell logic to manage a robotic ripping system.
    **URL:** https://github.com/ajayre/JacktheRipperBot/blob/master/Software/PC/RipTVShows.ps1

*   **Title:** Auto CD ripping using a Raspberry Pi and Python
    **Description:** A Reddit thread discussing headless automation of CD ripping directly on a Raspberry Pi.
    **URL:** https://www.reddit.com/r/raspberry_pi/comments/1sf1es/auto_cd_ripping_using_a_raspberry_pi_and_python/

*   **Title:** Exact Audio Copy (EAC)
    **Description:** The official homepage for Exact Audio Copy, the standard Windows utility for secure audio extraction.
    **URL:** https://www.exactaudiocopy.de/

*   **Title:** EAC Drive Options (Hydrogenaudio Wiki)
    **Description:** Wiki documentation detailing CD drive offset calibration and secure reading modes for perfect rips.
    **URL:** https://wiki.hydrogenaudio.org/index.php?title=EAC_Drive_Options

*   **Title:** Automatic Ripping Machine (Hacker News)
    **Description:** A community discussion thread on Hacker News centered on automated disc archiving systems and tools.
    **URL:** https://news.ycombinator.com/item?id=33502880

*   **Title:** Whipper GitHub - Required Dependencies
    **Description:** Official repository documentation listing the Linux packages required to run the `whipper` headless ripper.
    **URL:** https://github.com/whipper-team/whipper?tab=readme-ov-file#required-dependencies

*   **Title:** Is AccurateRip totally necessary for CD ripping?
    **Description:** An audiophile subreddit discussion regarding the value and necessity of checksum verification for ripped audio.
    **URL:** https://www.reddit.com/r/audiophile/comments/1uzrem/is_accuraterip_totally_necessary_for_cd_ripping/?tl=es-es

*   **Title:** Building a Linux based headless automated ripping machine
    **Description:** A DataHoarder subreddit post discussing the construction of a hands-free optical media ripping server.
    **URL:** https://www.reddit.com/r/DataHoarder/comments/g7xy3f/building_a_linux_based_headless_automated_ripping/

*   **Title:** Automatic Ripping Machine (b3n.org)
    **Description:** A comprehensive blog tutorial covering the setup and usage of the popular Automatic Ripping Machine project.
    **URL:** https://b3n.org/automatic-ripping-machine/

*   **Title:** ARM v2 GitHub README
    **Description:** Official repository instructions for deploying the Automatic Ripping Machine version 2.
    **URL:** https://github.com/automatic-ripping-machine/automatic-ripping-machine/blob/v2_master/README.md

*   **Title:** dBpoweramp Professional
    **Description:** Commercial page for dBpoweramp, which supports batch processing and automated robotic ripping systems.
    **URL:** https://www.dbpoweramp.com/professional

*   **Title:** How to verify FLAC files against the AccurateRip database
    **Description:** A SuperUser thread addressing how to retroactively verify already-ripped FLAC files using AccurateRip databases.
    **URL:** https://superuser.com/questions/234979/how-to-verify-flac-files-against-the-accuraterip-database

*   **Title:** How can I send commands to Arduino by computer through USB
    **Description:** An Arduino forum post exploring basic methods for parsing USB serial commands on an Arduino board.
    **URL:** https://forum.arduino.cc/t/how-can-i-sent-commands-to-arduino-by-computer-through-usb/318836/4

*   **Title:** Arduino Language Reference: Serial
    **Description:** Official Arduino documentation for the Serial library used to communicate with host devices.
    **URL:** https://docs.arduino.cc/language-reference/en/functions/communication/serial/

*   **Title:** Interfacing with computer via USB (Arduino Forum)
    **Description:** A community discussion providing code examples and guidance on PC-to-Arduino USB communication.
    **URL:** https://forum.arduino.cc/t/newbie-question-interfacing-with-computer-via-usb/20211/9

*   **Title:** Simple and Robust Computer-Arduino Serial Communication
    **Description:** A Medium article detailing how to build a reliable, framed packet protocol for serial communication between a PC and an Arduino.
    **URL:** https://medium.com/@araffin/simple-and-robust-computer-arduino-serial-communication-f91b95596788

*   **Title:** Can I turn a Raspberry Pi into an audio CD ripper?
    **Description:** A Reddit projects thread exploring the hardware and software viability of using a Pi as a dedicated CD ripping hub.
    **URL:** https://www.reddit.com/r/RASPBERRY_PI_PROJECTS/comments/9w1fzl/can_i_turn_a_raspberry_pi_into_an_audio_cd_ripper/

*   **Title:** Raspberry Pi Auto CD Ripper Tutorial
    **Description:** A step-by-step blog guide on how to configure software for automatic disc detection and ripping on a Raspberry Pi.
    **URL:** https://www.stuffaboutcode.com/posts/raspberry-pi-auto-cd-ripper/

*   **Title:** Pygame CDROM Documentation
    **Description:** Python library documentation showing how to poll the status and interact with a physical CD-ROM drive programmatically.
    **URL:** https://www.pygame.org/docs/ref/cdrom.html#pygame.cdrom.CD

*   **Title:** Automated CD Ripping Robot Video
    **Description:** A YouTube video demonstration of a robotic arm system handling and ripping optical discs.
    **URL:** https://www.youtube.com/watch?v=CSUFpPlSbbY

*   **Title:** IDE v2 Serial Monitor Tutorial
    **Description:** Official documentation on how to use the modern Arduino IDE Serial Monitor to debug serial communication.
    **URL:** https://docs.arduino.cc/software/ide-v2/tutorials/ide-v2-serial-monitor/
