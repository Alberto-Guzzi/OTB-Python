"""
Novecento+ AUX1 trapezoid-tracking script

Purpose
-------
- Connects to the OTBioelettronica Novecento+ over TCP.
- Streams AUX data only visually focusing on AUX1.
- Shows the participant's live MVC% together with the target trapezoid.
- Computes an accuracy score after you click "Start scoring".

Recommended workflow
--------------------
1. Start this Python script.
2. Confirm that it connects to the Novecento+ and the GUI opens.
3. In the OTB software, click "Start protocol".
4. Immediately click "Start scoring" in this Python window.
5. At the end of the trapezoid, the final accuracy is shown.

Important calibration
---------------------
Set MVC_AUX_VALUE to the AUX1 value corresponding to 100% MVC.
If AUX1 is already in MVC%, set MVC_AUX_VALUE = 100 and set AUX_TO_MVC_PERCENT_MODE = "already_percent".
"""

import socket
import threading
import time

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets


# -----------------------------
# User settings
# -----------------------------
TCP_IP = "169.254.1.10"
TCP_PORT = 23456

PlotTime = 1          # seconds of data processed per full received block
Update_time = 100     # GUI refresh interval in ms

# AUX sampling selector:
# 0 -> 500 Hz, 1 -> 2000 Hz, 2 -> 4000 Hz, 3 -> 8000 Hz
FSelAux = 0

# Target trapezoid: columns are [time_seconds, MVC_percent]
TARGET_POINTS = np.array([
    [0, 0],
    [5, 0],
    [13, 20],
    [43, 20],
    [51, 0],
    [56, 0],
], dtype=float)

# Accuracy definition:
# Mean absolute error of 0 percentage points -> 100%
# Mean absolute error of MAX_ACCEPTABLE_ERROR percentage points or more -> 0%
MAX_ACCEPTABLE_ERROR = 20.0

# ---- MVC calibration ----
# If AUX1 is a voltage/force/torque signal, set MVC_AUX_VALUE to the value measured at 100% MVC.
# Example: if maximal voluntary contraction corresponds to AUX1 = 2.4 V, use MVC_AUX_VALUE = 2.4.
MVC_AUX_VALUE = 1git

# Use "calibrate_from_aux" if AUX1 must be converted using MVC_AUX_VALUE.
# Use "already_percent" if AUX1 already arrives as MVC percent.
AUX_TO_MVC_PERCENT_MODE = "calibrate_from_aux"

# Optional smoothing for displayed and scored MVC signal.
# Increase this if the AUX signal is noisy. 1 disables smoothing.
SMOOTHING_WINDOW_SAMPLES = 10


# -----------------------------
# Novecento+ protocol helpers
# -----------------------------
def CRC8(Vector, Len):
    crc = 0
    j = 0

    while Len > 0:
        Extract = Vector[j]
        for _ in range(8, 0, -1):
            Sum = crc % 2 ^ Extract % 2
            crc //= 2

            if Sum > 0:
                a = format(crc, "08b")
                b = format(140, "08b")
                str_list = [0] * 8

                for k in range(8):
                    str_list[k] = int(a[k] != b[k])

                crc = int("".join(map(str, str_list)), 2)

            Extract //= 2

        Len -= 1
        j += 1

    return crc


def target_mvc_at_time(t_seconds):
    """Return target MVC% at each time point by linear interpolation."""
    return np.interp(t_seconds, TARGET_POINTS[:, 0], TARGET_POINTS[:, 1])


def moving_average(x, window):
    """Simple causal-ish smoothing for display/scoring."""
    if window <= 1 or len(x) < window:
        return x
    kernel = np.ones(window) / window
    return np.convolve(x, kernel, mode="same")


def aux1_to_mvc_percent(aux1_values):
    """Convert AUX1 values to MVC%."""
    if AUX_TO_MVC_PERCENT_MODE == "already_percent":
        return aux1_values

    if MVC_AUX_VALUE == 0:
        raise ValueError("MVC_AUX_VALUE cannot be zero.")

    return aux1_values / MVC_AUX_VALUE * 100.0


def compute_accuracy(actual_mvc, target_mvc, max_error=MAX_ACCEPTABLE_ERROR):
    """Convert mean absolute tracking error into an accuracy percentage."""
    mae = np.mean(np.abs(actual_mvc - target_mvc))
    accuracy = 100.0 * (1.0 - mae / max_error)
    return float(np.clip(accuracy, 0.0, 100.0)), float(mae)


# -----------------------------
# Acquisition configuration
# -----------------------------
IN_Active = [0] * 10
Mode = [0] * 10
Gain = [0] * 10
HRES = [0] * 10
HPF = [1] * 10
Fsamp = [1] * 10
NumChan = [0] * 10
Ptr_IN = [0] * 11
Size_IN = [0] * 11

# All IN inputs disabled: this script focuses on AUX1.
# The input configuration bytes still exist, but no IN data are requested.
for i in range(10):
    IN_Active[i] = 0
    Mode[i] = 0
    Gain[i] = 0
    HRES[i] = 0
    HPF[i] = 1
    Fsamp[i] = 1

ChVsType = [0, 14, 22, 38, 46, 70, 102, 0, 0, 0, 0, 0, 0, 0, 0, 0]
AuxFsamp = [0, 16, 32, 48]
FsampVal = [500, 2000, 4000, 8000]
SizeAux = [16, 64, 128, 256]

AnOutINSource = 2
AnOutChan = 1
AnOutGain = int("00100000", 2)

AuxGainFactor = 5 / 2**16 / 0.5

ConfString = [0] * 15
ConfString[0] = int("10000000", 2) + AuxFsamp[FSelAux] + IN_Active[9] * 2 + IN_Active[8]
ConfString[1] = 0
for i in range(8):
    ConfString[1] += IN_Active[i] * (2**i)
ConfString[2] = AnOutGain + AnOutINSource
ConfString[3] = AnOutChan
for i in range(10):
    ConfString[4 + i] = Mode[i] * 64 + Gain[i] * 16 + HPF[i] * 8 + HRES[i] * 4 + Fsamp[i]
ConfString[14] = CRC8(ConfString, 14)


# -----------------------------
# TCP connection
# -----------------------------
tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
tcp_socket.connect((TCP_IP, TCP_PORT))
print("Connected to the Socket")


def send_request(command):
    cmd = [command, CRC8([command], 1)]
    tcp_socket.sendall(bytearray(cmd))
    response = tcp_socket.recv(20)
    return response


firmware_version = send_request(2)
print("Firmware Version:", firmware_version[1:])

battery_level = send_request(3)
print("Battery Level: {}%".format(battery_level[1]))

settings = send_request(1)
if len(settings) >= 20:
    if settings[19] == 0:
        print("Error None")
    elif settings[19] == 255:
        print("Error CRC")
print("Probes configuration:", settings[1:11])

# Send configuration. With ConfString[0] bit 7 set, this likely starts the Novecento+ stream.
tcp_socket.sendall(bytearray(ConfString))

# Compute packet layout. Since all IN_Active are zero, Ptr_IN[10] should remain zero.
NumActInputs = 0
Ptr_IN[0] = 0
for i in range(10):
    probe_type = settings[i + 1] if len(settings) > i + 1 else 0
    NumChan[i] = ChVsType[probe_type] if probe_type < len(ChVsType) else 0

    if NumChan[i] == 0:
        IN_Active[i] = 0

    if IN_Active[i] == 1:
        Size_IN[i] = (HRES[i] + 1) * FsampVal[Fsamp[i]] // 500 * NumChan[i]
        NumActInputs += 1

    Ptr_IN[i + 1] = Ptr_IN[i] + Size_IN[i]

PacketSize1Block = Ptr_IN[10] + SizeAux[FSelAux] + 128
blockData = PacketSize1Block * 500 * PlotTime * 2

tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, blockData * 2)

print("AUX sample rate:", FsampVal[FSelAux], "Hz")
print("PacketSize1Block:", PacketSize1Block)
print("blockData:", blockData, "bytes")


# -----------------------------
# Shared data between receiver and GUI
# -----------------------------
Data = None
terminate_thread = threading.Event()
data_lock = threading.Lock()

# Scoring state
scoring_active = False
scoring_finished = False
score_start_time = None
all_actual_mvc = []
all_target_mvc = []
all_time = []
latest_accuracy = None
latest_mae = None

trial_duration = float(TARGET_POINTS[-1, 0])
fs_aux = FsampVal[FSelAux]


# -----------------------------
# Background receiver
# -----------------------------
def receive_data():
    global Data
    buffer = b""

    while not terminate_thread.is_set():
        try:
            chunk = tcp_socket.recv(blockData)
            if not chunk:
                print("Socket closed by device.")
                break

            buffer += chunk

            while len(buffer) >= blockData:
                packet = buffer[:blockData]
                buffer = buffer[blockData:]

                temp = np.frombuffer(packet, dtype="<i2")
                expected_len = PacketSize1Block * 500

                if len(temp) == expected_len:
                    new_data = temp.reshape(PacketSize1Block, 500, order="F")
                    with data_lock:
                        Data = new_data
                else:
                    print(f"Unexpected packet size: {len(temp)}; expected {expected_len}")

        except (OSError, ValueError) as e:
            if not terminate_thread.is_set():
                print(f"Error receiving data: {e}")
            break


# -----------------------------
# GUI
# -----------------------------
app = QtWidgets.QApplication([])
main_widget = QtWidgets.QWidget()
main_widget.setWindowTitle("Novecento+ AUX1 trapezoid tracking")
main_widget.resize(1000, 700)

layout = QtWidgets.QVBoxLayout(main_widget)

status_label = QtWidgets.QLabel("Start the OTB protocol, then click 'Start scoring'.")
status_label.setStyleSheet("font-size: 16px;")
layout.addWidget(status_label)

button_layout = QtWidgets.QHBoxLayout()
start_button = QtWidgets.QPushButton("Start scoring")
reset_button = QtWidgets.QPushButton("Reset scoring")
button_layout.addWidget(start_button)
button_layout.addWidget(reset_button)
layout.addLayout(button_layout)

plot = pg.PlotWidget(title="AUX1 MVC% vs Target")
plot.showGrid(x=True, y=True)
plot.setLabel("bottom", "Time", units="s")
plot.setLabel("left", "MVC", units="%")
plot.setYRange(-5, max(30, np.max(TARGET_POINTS[:, 1]) + 10))
layout.addWidget(plot)

actual_curve = plot.plot(pen=pg.mkPen(width=2), name="Actual MVC%")
target_curve = plot.plot(pen=pg.mkPen(style=QtCore.Qt.DashLine, width=2), name="Target MVC%")

# Draw full target reference from the beginning.
target_time_dense = np.linspace(0, trial_duration, int(trial_duration * 20) + 1)
target_curve.setData(target_time_dense, target_mvc_at_time(target_time_dense))

main_widget.show()


def reset_scoring():
    global scoring_active, scoring_finished, score_start_time
    global all_actual_mvc, all_target_mvc, all_time, latest_accuracy, latest_mae

    scoring_active = False
    scoring_finished = False
    score_start_time = None
    all_actual_mvc = []
    all_target_mvc = []
    all_time = []
    latest_accuracy = None
    latest_mae = None
    actual_curve.setData([], [])
    status_label.setText("Reset. Start the OTB protocol, then click 'Start scoring'.")


def start_scoring():
    global scoring_active, scoring_finished, score_start_time
    global all_actual_mvc, all_target_mvc, all_time, latest_accuracy, latest_mae

    scoring_active = True
    scoring_finished = False
    score_start_time = time.perf_counter()
    all_actual_mvc = []
    all_target_mvc = []
    all_time = []
    latest_accuracy = None
    latest_mae = None
    actual_curve.setData([], [])
    status_label.setText("Scoring started. Follow the target trapezoid.")


start_button.clicked.connect(start_scoring)
reset_button.clicked.connect(reset_scoring)


def update_plot():
    global scoring_active, scoring_finished, latest_accuracy, latest_mae

    with data_lock:
        data_snapshot = None if Data is None else Data.copy()

    if data_snapshot is None:
        return

    try:
        # AUX data block is between the end of IN data and the accessory block.
        aux_flat = data_snapshot[Ptr_IN[10]:-128, :].reshape(
            1,
            16 * fs_aux * PlotTime,
            order="F",
        )
        sig_aux = aux_flat.reshape(16, fs_aux * PlotTime, order="F").astype(np.int32)

        # AUX1 is channel index 0.
        aux1 = sig_aux[0, :] * AuxGainFactor
        actual_mvc = aux1_to_mvc_percent(aux1)
        actual_mvc = moving_average(actual_mvc, SMOOTHING_WINDOW_SAMPLES)

    except Exception as e:
        status_label.setText(f"AUX parsing error: {e}")
        return

    if not scoring_active or scoring_finished:
        # Before scoring, show live AUX1 as a short preview on a 0..PlotTime time axis.
        t_preview = np.arange(len(actual_mvc)) / fs_aux
        actual_curve.setData(t_preview, actual_mvc)
        return

    elapsed_now = time.perf_counter() - score_start_time
    t_block = elapsed_now - PlotTime + np.arange(len(actual_mvc)) / fs_aux

    # Keep only samples inside the protocol duration.
    valid = (t_block >= 0) & (t_block <= trial_duration)
    if not np.any(valid):
        return

    t_valid = t_block[valid]
    actual_valid = actual_mvc[valid]
    target_valid = target_mvc_at_time(t_valid)

    all_time.extend(t_valid.tolist())
    all_actual_mvc.extend(actual_valid.tolist())
    all_target_mvc.extend(target_valid.tolist())

    actual_curve.setData(np.array(all_time), np.array(all_actual_mvc))

    latest_accuracy, latest_mae = compute_accuracy(
        np.array(all_actual_mvc),
        np.array(all_target_mvc),
        MAX_ACCEPTABLE_ERROR,
    )

    status_label.setText(
        f"Live accuracy: {latest_accuracy:.1f}% | Mean absolute error: {latest_mae:.2f} MVC percentage points"
    )

    if elapsed_now >= trial_duration:
        scoring_active = False
        scoring_finished = True
        status_label.setText(
            f"Finished. You were: {latest_accuracy:.1f}% accurate! "
            f"Mean absolute error: {latest_mae:.2f} MVC percentage points."
        )
        print(
            f"Finished. You were: {latest_accuracy:.1f}% accurate! "
            f"Mean absolute error: {latest_mae:.2f} MVC percentage points."
        )


def clean_shutdown():
    terminate_thread.set()

    try:
        stop_conf = ConfString.copy()
        stop_conf[0] = int("00000000", 2)
        stop_conf[14] = CRC8(stop_conf, 14)
        tcp_socket.sendall(bytearray(stop_conf))
    except OSError:
        pass

    try:
        tcp_socket.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass

    try:
        tcp_socket.close()
    except OSError:
        pass

    print("Socket closed")


app.aboutToQuit.connect(clean_shutdown)

receiver_thread = threading.Thread(target=receive_data, daemon=True)
receiver_thread.start()

timer = QtCore.QTimer()
timer.timeout.connect(update_plot)
timer.start(Update_time)


if __name__ == "__main__":
    app.exec_()
