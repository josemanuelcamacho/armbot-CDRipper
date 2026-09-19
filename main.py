#!/usr/bin/env python3

"""
Arduino serial communication helper
-----------------------------------

Features:
- Connects to an Arduino-compatible board over USB serial
- Tests the connection at startup and at any time later
- Sends commands to the board with send_code()
- Returns either:
    - a confirmation from the board
    - or a descriptive failure message
- Includes comments explaining each section

Expected Arduino-side protocol (recommended):
- Python sends one text line ending with '\n'
- Board replies with one text line ending with '\n'
- For example:
    Python -> "MOVE_MOTOR\n"
    Board  -> "OK: MOVE_MOTOR completed\n"
or
    Board  -> "ERROR: motor jam detected\n"
"""

import time
from typing import Optional, Tuple
import serial
import serial.tools.list_ports

import cd_rip_verify

class ArduinoSerialController:
    """
    Handles serial communication with an Arduino-compatible board.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        timeout: float = 2.0,
        write_timeout: float = 2.0,
        auto_find: bool = True,
    ):
        """
        Initialize the controller.

        Args:
            port: Serial port name, e.g. 'COM3' on Windows or '/dev/ttyACM0' on Linux.
                  If None and auto_find=True, the code will try to detect the board.
            baudrate: Must match the baud rate configured in the Arduino sketch.
            timeout: Read timeout in seconds.
            write_timeout: Write timeout in seconds.
            auto_find: If True, automatically search for a likely Arduino serial port.
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.auto_find = auto_find
        self.ser: Optional[serial.Serial] = None

    def list_available_ports(self):
        """
        Return a list of serial ports currently available on the system.
        Useful for debugging if auto-detection does not find the board.
        """
        ports = serial.tools.list_ports.comports()
        return [
            {
                "device": p.device,
                "description": p.description,
                "manufacturer": p.manufacturer,
                "hwid": p.hwid,
            }
            for p in ports
        ]

    def find_arduino_port(self) -> Optional[str]:
        """
        Try to locate a likely Arduino-compatible serial port automatically.

        This checks common text clues in the port description/manufacturer.
        You can expand this function for your specific board model.
        """
        keywords = ["arduino", "wch", "usb serial", "cp210", "ch340", "ftdi", "acm", "uart"]

        for p in serial.tools.list_ports.comports():
            text = " ".join(
                filter(
                    None,
                    [
                        p.device,
                        p.description or "",
                        p.manufacturer or "",
                        p.hwid or "",
                    ],
                )
            ).lower()

            if any(k in text for k in keywords):
                return p.device

        return None

    def connect(self) -> Tuple[bool, str]:
        """
        Open the serial connection.

        Returns:
            (True, success_message) if connection succeeds
            (False, error_message) if connection fails
        """
        if self.ser and self.ser.is_open:
            return True, f"Already connected on {self.ser.port}."

        if self.port is None and self.auto_find:
            self.port = self.find_arduino_port()

        if self.port is None:
            return False, (
                "No serial port was specified and no Arduino-like device was found. "
                "Check the USB cable, drivers, and available ports."
            )

        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.write_timeout,
            )

            # Many Arduino boards reset when the serial port opens.
            # Waiting a bit avoids losing the first messages.
            time.sleep(2.0)

            # Clean any stale data left in buffers after opening.
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            return True, f"Connected successfully to {self.port} at {self.baudrate} baud."

        except serial.SerialException as e:
            self.ser = None
            return False, f"Failed to connect to {self.port}: {e}"

    def disconnect(self) -> Tuple[bool, str]:
        """
        Close the serial connection safely.
        """
        try:
            if self.ser and self.ser.is_open:
                port_name = self.ser.port
                self.ser.close()
                return True, f"Disconnected from {port_name}."
            return True, "Serial port was already closed."
        except Exception as e:
            return False, f"Failed to close serial port: {e}"

    def is_connected(self) -> bool:
        """
        Quick local check: returns True if the serial object exists and is open.
        """
        return self.ser is not None and self.ser.is_open

    def test_connection(self) -> Tuple[bool, str]:
        """
        Test whether the board is reachable right now.

        Strategy:
        1. Ensure the serial port is open
        2. Send a PING command
        3. Expect a PONG or OK response

        Arduino sketch should ideally answer:
            PING -> PONG
        or
            PING -> OK: PING

        Returns:
            (True, message) if test succeeds
            (False, message) if test fails
        """
        if not self.is_connected():
            ok, msg = self.connect()
            if not ok:
                return False, f"Connection test failed: {msg}"

        try:
            response = self.send_raw("PING", expect_reply=True)

            if response is None:
                return False, "Connection test failed: no response from board."

            normalized = response.strip().upper()
            if "WW:PONG" in normalized or normalized.startswith("OK") or "START" in normalized:
                return True, f"Connection OK. Board replied: {response}"

            return False, f"Connection uncertain. Unexpected board reply: {response}"

        except Exception as e:
            return False, f"Connection test raised an error: {e}"

    def reconnect(self) -> Tuple[bool, str]:
        """
        Force a reconnection attempt.
        Useful if the cable was unplugged or the board rebooted.
        """
        self.disconnect()
        time.sleep(0.5)
        return self.connect()

    def send_raw(self, message: str, expect_reply: bool = True) -> Optional[str]:
        """
        Low-level function to send one text line to the board.

        Args:
            message: Plain text command to send.
            expect_reply: If True, waits for a single line response.

        Returns:
            The board response as a string, or None if no reply is expected.

        Raises:
            RuntimeError: If not connected.
            serial.SerialTimeoutException / serial.SerialException: On serial errors.
        """
        if not self.is_connected():
            raise RuntimeError("Serial port is not connected.")

        # Ensure the board receives line-based commands.
        data = (message.strip() + "\n").encode("utf-8")

        # Write bytes to the serial port and flush them out.
        self.ser.write(data)
        self.ser.flush()

        if not expect_reply:
            return None

        # Read one response line from the board.
        raw = self.ser.readline()
        if not raw:
            return ""

        return raw.decode("utf-8", errors="replace").strip()

    def send_code(self, operation_code: str) -> str:
        """
        Send the next operation code to the external board.

        This is the main function requested:
        - it provides the board with the signal for the next action
        - it returns either a confirmation or a descriptive failure message

        Expected board-side behavior:
            "OP:<code>" -> reply with either:
                "OK: <description>"
            or
                "ERROR: <description>"

        Args:
            operation_code: The operation identifier to send to the board.

        Returns:
            A confirmation string from the board if successful,
            or a descriptive failure message if not.
        """
        if not operation_code or not operation_code.strip():
            return "Failure: operation_code is empty."

        # Check connection before sending commands.
        if not self.is_connected():
            ok, msg = self.connect()
            if not ok:
                return f"Failure: unable to connect before sending command. {msg}"

        try:
            # Optional live connection verification before the actual command.
            #ok, test_msg = self.test_connection()
            #if not ok:
            #    return f"Failure: board connection test failed before command. {test_msg}"

            command = f"OP:{operation_code.strip()}"
            print("###")
            print(command)
            response = self.send_raw(command, expect_reply=True)


            while True:
                print("***")
                print(response)

                if response is None or response == "":
                        print(f"Failure: no confimation received from board after sending '{operation.code}'.")
                        #return (
                        #        f"Failure: no confirmation received from board after sending '{operation_code}'."
                        #)

                normalized = response.strip().upper()

                if normalized.startswith("OK"):
                        return response

                if normalized.startswith("ERROR"):
                        return f"Failure reported by board: {response}"

                if not normalized.startswith("WW"):
                        return (
                                f"Failure: board returned an unexpected response for '{operation_code}': {response}"
                        )

                else:
                        raw = self.ser.readline()
                        if not raw:
                                response = ""
                        response = raw.decode("utf-8", errors="replace").strip()

        except serial.SerialTimeoutException:
                return f"Failure: timeout while sending '{operation_code}' to the board."
        except serial.SerialException as e:
                return f"Failure: serial communication error while sending '{operation_code}': {e}"
        except Exception as e:
                return f"Failure: unexpected error while sending '{operation_code}': {e}"


# ---------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------
if __name__ == "__main__":
    # Set port manually if you already know it, for example:
    # controller = ArduinoSerialController(port="COM3", baudrate=115200)
    # controller = ArduinoSerialController(port="/dev/ttyACM0", baudrate=115200)

    # Or let the code try to auto-detect the board:
    controller = ArduinoSerialController(port=None, baudrate=115200, auto_find=True, timeout=10.0)

    # Show currently available serial ports
    print("Available ports:")
    for p in controller.list_available_ports():
        print(f"  - {p['device']} | {p['description']} | {p['manufacturer']}")

    # Connect at startup
    ok, msg = controller.connect()
    print(msg)

    if ok:
        _first = 0

        for i in range(5):

                print("RIPPING CD: ", i)
                # Initial connection test
                ok, msg = controller.test_connection()
                print(msg)

                if _first == 0:
                        print("EJECTING")
                        _first = 1
                        result = cd_rip_verify.main2("eject")
                        print("eject result:", result)

                # Send an example operation to the board
                result = controller.send_code("LAUNCH")
                print("send_code result:", result)
                
                print("SLEEPING AS IF LAUNCHING")
                #time.sleep(5)
                result = cd_rip_verify.main2("timestamp")
                print("Ripping CD result:", result)

                if (result == 2) | (result == 3):
                        print("Exitin CD was not found or placed in thay.")
                        exit(1)
                # Another test later during runtime
                #ok, msg = controller.test_connection()
                #print("Mid-run connection test:", msg)

                time.sleep(5)
                print("PICKING CD")
                result = controller.send_code("PANG")
                print("send code result:", result)
                time.sleep(5)
                print("AFTER SIMULATING PICKUP, RETURN TO INIT")

    # Clean shutdown
    ok, msg = controller.disconnect()
    print(msg)

