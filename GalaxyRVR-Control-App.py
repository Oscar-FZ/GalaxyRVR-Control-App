import os
import sys
import asyncio
import json
import csv
import cv2
import websockets

# Evitar problemas en Wayland
if 'WAYLAND_DISPLAY' in os.environ:
    os.environ['QT_QPA_PLATFORM'] = 'xcb'

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QTextEdit, QFileDialog, QStackedWidget, QGroupBox
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QImage, QPixmap
from qasync import QEventLoop

from qt_material import apply_stylesheet 

SEND_PERIOD_MS = 100  # Frecuencia de envío para Manual y Sistemático


# ==============================
#   Cliente universal (WebSocket)
# ==============================
class RoverClient:
    def __init__(self, ip="192.168.4.1", port=8765, on_message=None, on_disconnect=None):
        self.ip = ip
        self.port = port
        self.websocket = None
        self.connected = False
        self.receive_task = None
        self.on_message = on_message
        self.on_disconnect = on_disconnect

    async def connect(self):
        try:
            if self.websocket:
                await self.websocket.close()
            self.websocket = await websockets.connect(f"ws://{self.ip}:{self.port}")
            self.connected = True
            if self.on_message:
                self.on_message({"status": "connected"})
            if self.receive_task and not self.receive_task.done():
                self.receive_task.cancel()
            self.receive_task = asyncio.create_task(self.receive_data())
        except Exception as e:
            self.connected = False
            if self.on_disconnect:
                self.on_disconnect(f"Error de conexión: {e}")

    async def disconnect(self):
        try:
            if self.websocket:
                await self.websocket.close()
        finally:
            self.websocket = None
            self.connected = False

    async def send(self, data: dict):
        if not self.connected or not self.websocket:
            return
        try:
            await self.websocket.send(json.dumps(data))
        except Exception as e:
            self.connected = False
            if self.on_disconnect:
                self.on_disconnect(f"Error envío: {e}")

    async def receive_data(self):
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    if self.on_message:
                        self.on_message(data)
                except Exception as e:
                    print(f"Error procesando mensaje: {e}")
        except Exception as e:
            print(f"Conexión cerrada: {e}")
        finally:
            self.connected = False
            if self.on_disconnect:
                self.on_disconnect("Desconectado")


# ========= HILO DE VIDEO ==========
class VideoThread(QThread):
    frame_ready = pyqtSignal(QImage)

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.cap = None
        self.running = True

    def run(self):
        self.cap = cv2.VideoCapture(self.url)
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame = cv2.rotate(frame, cv2.ROTATE_180)
                h, w, ch = frame.shape
                qt_img = QImage(frame.data, w, h, ch * w, QImage.Format_RGB888)
                self.frame_ready.emit(qt_img)
            self.msleep(100)

    def stop(self):
        self.running = False
        try:
            if self.cap:
                self.cap.release()
        finally:
            self.quit()
            self.wait()


# ==============================
#          PÁGINAS / SUBMENÚS
# ==============================
class ManualPage(QWidget):
    """Submenú Manual: sliders + control de teclado WASD (con combinaciones)."""
    def __init__(self, request_connect, client: RoverClient):
        super().__init__()
        self.request_connect = request_connect
        self.client = client

        # Estados
        self.left_motor = 0
        self.right_motor = 0
        self.servo = 90
        self.lamp = 0
        self.mode_e = 0
        self.mode_f = 0

        # Teclado
        self.pressed_keys = set()
        self.KEY_SPEED = 80
        self.KEY_TURN = 80

        layout = QVBoxLayout(self)

        # Header
        row = QHBoxLayout()
        self.status_label = QLabel("Estado: ---"); self.status_label.setFont(QFont("Arial", 11))
        self.connect_btn = QPushButton("Conectar / Reintentar")
        self.connect_btn.clicked.connect(lambda: asyncio.create_task(self.request_connect(self.handle_message)))
        row.addWidget(self.status_label); row.addWidget(self.connect_btn)
        layout.addLayout(row)

        # Instrucciones de teclado
        hint = QLabel("WASD: W Avanza | S Retrocede | A Gira Izq | D Gira Der")
        hint.setStyleSheet("color: #888;")
        layout.addWidget(hint)

        # Sliders
        layout.addWidget(self._slider_box("Motor Izquierdo", -100, 100, "left_motor"))
        layout.addWidget(self._slider_box("Motor Derecho", -100, 100, "right_motor"))
        layout.addWidget(self._slider_box("Servo", 0, 180, "servo"))

        # Luz y modos
        group = QGroupBox("Luz y Modos")
        g_layout = QHBoxLayout(group)
        self.lamp_btn = QPushButton("Luz: OFF"); self.lamp_btn.setCheckable(True)
        self.lamp_btn.clicked.connect(self._toggle_lamp)
        self.btn_manual = QPushButton("Modo Manual"); self.btn_manual.clicked.connect(lambda: self._set_mode("none"))
        self.btn_avoid = QPushButton("Avoid"); self.btn_avoid.clicked.connect(lambda: self._set_mode("avoid"))
        self.btn_follow = QPushButton("Follow"); self.btn_follow.clicked.connect(lambda: self._set_mode("follow"))
        for b in (self.lamp_btn, self.btn_manual, self.btn_avoid, self.btn_follow):
            g_layout.addWidget(b)
        layout.addWidget(group)

        layout.addStretch(1)

        # Timer envío periódico
        self.timer = QTimer()
        self.timer.timeout.connect(lambda: asyncio.create_task(self._send_controls()))

        # Captura de teclado robusta
        self.setFocusPolicy(Qt.StrongFocus)

    def _slider_box(self, title, minv, maxv, attr_name):
        box = QWidget()
        h = QHBoxLayout(box)
        label = QLabel(title)
        slider = QSlider(Qt.Horizontal); slider.setMinimum(minv); slider.setMaximum(maxv)
        value_label = QLabel("0"); value_label.setFont(QFont("Arial", 10, QFont.Bold))
        slider.valueChanged.connect(lambda v: setattr(self, attr_name, v))
        slider.valueChanged.connect(lambda v: value_label.setText(str(v)))
        if "Motor" in title:
            slider.sliderReleased.connect(lambda: slider.setValue(0))
        h.addWidget(label); h.addWidget(slider); h.addWidget(value_label)
        return box

    def _toggle_lamp(self):
        self.lamp = 1 if self.lamp_btn.isChecked() else 0
        self.lamp_btn.setText(f"Luz: {'ON' if self.lamp else 'OFF'}")

    def _set_mode(self, mode):
        if mode == "avoid":
            self.mode_e, self.mode_f = 1, 0
        elif mode == "follow":
            self.mode_e, self.mode_f = 0, 1
        else:
            self.mode_e, self.mode_f = 0, 0

    # ====== Teclado WASD ======
    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        if key in (Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D):
            self.pressed_keys.add(key)
            self._recompute_from_keys()

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        if key in self.pressed_keys:
            self.pressed_keys.remove(key)
            self._recompute_from_keys()

    def _recompute_from_keys(self):
        # Velocidad y giro a partir del conjunto de teclas
        speed = 0
        turn = 0
        if Qt.Key_W in self.pressed_keys:
            speed += self.KEY_SPEED
        if Qt.Key_S in self.pressed_keys:
            speed -= self.KEY_SPEED
        if Qt.Key_D in self.pressed_keys:
            turn += self.KEY_TURN
        if Qt.Key_A in self.pressed_keys:
            turn -= self.KEY_TURN

        # Mezcla diferencial
        left = speed - turn
        right = speed + turn
        # Saturación a [-100, 100]
        self.left_motor = max(-100, min(100, left))
        self.right_motor = max(-100, min(100, right))

    async def _send_controls(self):
        if not self.client.connected:
            return
        data = {
            "K": int(self.left_motor),
            "Q": int(self.right_motor),
            "D": int(self.servo),
            "M": int(self.lamp),
            "E": int(self.mode_e),
            "F": int(self.mode_f),
            "ping": 1
        }
        await self.client.send(data)

    def handle_message(self, data: dict):
        if "status" in data:
            self.status_label.setText(f"Estado: {data['status']}")

    # Ciclo de vida
    def on_enter(self):
        # Asegurar captura de teclado incluso si otros widgets piden foco:
        self.grabKeyboard()
        self.timer.start(SEND_PERIOD_MS)

    def on_leave(self):
        self.timer.stop()
        self.releaseKeyboard()
        # Frenar al salir
        self.pressed_keys.clear()
        self.left_motor = 0
        self.right_motor = 0

    def stop(self):
        self.timer.stop()
        self.releaseKeyboard()


class SystematicPage(QWidget):
    """Submenú Sistemático: envía periódicamente los controles y la secuencia solo actualiza valores."""
    def __init__(self, request_connect, client: RoverClient):
        super().__init__()
        self.request_connect = request_connect
        self.client = client

        self.throttle1 = 0
        self.throttle2 = 0
        self.servo = 90
        self.lamp = 0
        self.mode_e = 0
        self.mode_f = 0

        self.sequence_running = False

        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self.status_label = QLabel("Estado: ---"); self.status_label.setFont(QFont("Arial", 11))
        self.connect_btn = QPushButton("Conectar / Reintentar")
        self.connect_btn.clicked.connect(lambda: asyncio.create_task(self.request_connect(self.handle_message)))
        row.addWidget(self.status_label); row.addWidget(self.connect_btn)
        layout.addLayout(row)

        self.editor = QTextEdit()
        self.editor.setPlaceholderText(
            "Instrucciones por línea: K,Q,D,M,E,F,duracion_ms\n"
            "Ejemplo:\n50,50,90,0,0,0,2000\n0,50,90,1,0,0,1000\n-50,50,90,0,1,0,1500\n0,0,45,0,0,1,2000"
        )
        self.editor.setMaximumHeight(160)
        layout.addWidget(self.editor)

        btns = QHBoxLayout()
        self.btn_load = QPushButton("Cargar CSV")
        self.btn_load.clicked.connect(self._load_csv)
        self.btn_run = QPushButton("Ejecutar Secuencia")
        self.btn_run.clicked.connect(lambda: asyncio.create_task(self._run_sequence()))
        self.btn_stop = QPushButton("Detener")
        self.btn_stop.clicked.connect(self._stop_sequence)
        btns.addWidget(self.btn_load); btns.addWidget(self.btn_run); btns.addWidget(self.btn_stop)
        layout.addLayout(btns)

        self.seq_status = QLabel("Listo"); self.seq_status.setStyleSheet("color: blue;")
        layout.addWidget(self.seq_status)

        self.timer = QTimer()
        self.timer.timeout.connect(lambda: asyncio.create_task(self._send_control_values()))

    def _load_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir plan", "", "CSV (*.csv);;Todos (*)")
        if not path:
            return
        try:
            lines = []
            with open(path, newline='') as csvfile:
                reader = csv.reader(csvfile)
                for row in reader:
                    if not row or row[0].strip().startswith('#'):
                        continue
                    if len(row) != 7:
                        continue
                    lines.append(','.join(r.strip() for r in row))
            self.editor.setPlainText('\n'.join(lines))
            self.seq_status.setText(f"Cargadas {len(lines)} instrucciones")
            self.seq_status.setStyleSheet("color: green;")
        except Exception as e:
            self.seq_status.setText(f"Error CSV: {e}")
            self.seq_status.setStyleSheet("color: red;")

    async def _run_sequence(self):
        if self.sequence_running:
            return
        text = self.editor.toPlainText().strip()
        if not text:
            self.seq_status.setText("No hay instrucciones válidas")
            self.seq_status.setStyleSheet("color: red;")
            return

        sequence = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '#' in line:
                line = line.split('#')[0].strip()
            parts = [p.strip() for p in line.split(',')]
            if len(parts) != 7:
                continue
            try:
                K, Q, D, M, E, F, T = map(int, parts)
                K = max(-100, min(100, K))
                Q = max(-100, min(100, Q))
                D = max(0, min(180, D))
                M = 1 if M else 0
                E = 1 if E else 0
                F = 1 if F else 0
                T = max(100, T)
                sequence.append((K, Q, D, M, E, F, T))
            except ValueError:
                continue

        if not sequence:
            self.seq_status.setText("No hay instrucciones válidas")
            self.seq_status.setStyleSheet("color: red;")
            return

        self.sequence_running = True
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)

        try:
            for i, (K, Q, D, M, E, F, T) in enumerate(sequence, 1):
                if not self.sequence_running:
                    break
                self.throttle1 = K
                self.throttle2 = Q
                self.servo = D
                self.lamp = M
                self.mode_e = E
                self.mode_f = F

                self.seq_status.setText(f"Paso {i}/{len(sequence)}")
                self.seq_status.setStyleSheet("color: orange;")

                steps = max(1, T // 100)
                for _ in range(steps):
                    if not self.sequence_running:
                        break
                    await asyncio.sleep(0.1)

            self.throttle1 = 0
            self.throttle2 = 0
            if self.sequence_running:
                self.seq_status.setText("Secuencia finalizada")
                self.seq_status.setStyleSheet("color: green;")
        except Exception as e:
            self.seq_status.setText(f"Error: {e}")
            self.seq_status.setStyleSheet("color: red;")
        finally:
            self.sequence_running = False
            self.btn_run.setEnabled(True)
            self.btn_stop.setEnabled(False)

    def _stop_sequence(self):
        self.sequence_running = False
        self.throttle1 = 0
        self.throttle2 = 0
        self.seq_status.setText("Secuencia detenida")
        self.seq_status.setStyleSheet("color: red;")

    async def _send_control_values(self):
        if not self.client.connected:
            return
        try:
            data = {
                "K": self.throttle1,
                "Q": self.throttle2,
                "D": self.servo,
                "M": self.lamp,
                "E": self.mode_e,
                "F": self.mode_f,
                "ping": 1
            }
            await self.client.send(data)
        except Exception as e:
            print(f"Send error: {e}")

    def handle_message(self, data: dict):
        if "status" in data:
            self.status_label.setText(f"Estado: {data['status']}")

    def on_enter(self):
        self.timer.start(SEND_PERIOD_MS)

    def on_leave(self):
        self.timer.stop()
        self.throttle1 = 0
        self.throttle2 = 0

    def stop(self):
        self.timer.stop()
        self._stop_sequence()


class MonitorPage(QWidget):
    def __init__(self, request_connect, client: RoverClient):
        super().__init__()
        self.request_connect = request_connect
        self.client = client
        self.video_url = "http://192.168.4.1:9000/mjpg"
        self.video_thread = None

        # Estados
        self.servo = 90
        self.lamp = 0

        # ====== Layout general en dos columnas ======
        main_layout = QHBoxLayout(self)

        # -----------------------------------
        # Columna izquierda: sensores + cámara + controles
        # -----------------------------------
        left = QVBoxLayout()
        main_layout.addLayout(left, 2)  # Peso mayor para que esta parte sea más ancha

        # Estado y conexión
        row = QHBoxLayout()
        self.status_label = QLabel("Estado: ---")
        self.status_label.setFont(QFont("Arial", 11))
        self.connect_btn = QPushButton("Conectar / Reintentar")
        self.connect_btn.clicked.connect(lambda: asyncio.create_task(self.request_connect(self.handle_message)))
        row.addWidget(self.status_label)
        row.addWidget(self.connect_btn)
        left.addLayout(row)

        # Sensores
        self.bv = QLabel("Batería: -- V")
        self.ir_l = QLabel("IR Izq: --")
        self.ir_r = QLabel("IR Der: --")
        self.us = QLabel("Ultrasonido: -- cm")
        for w in (self.bv, self.ir_l, self.ir_r, self.us):
            w.setFont(QFont("Arial", 11))
            left.addWidget(w)

        # Cámara
        cam_box = QGroupBox("Cámara")
        cam_layout = QVBoxLayout(cam_box)
        self.video_label = QLabel("Cámara detenida")
        self.video_label.setFixedSize(320, 240)
        cam_layout.addWidget(self.video_label, alignment=Qt.AlignCenter)

        btn_row = QHBoxLayout()
        self.camera_btn = QPushButton("🎥 Iniciar Cámara")
        self.camera_btn.setCheckable(True)
        self.camera_btn.clicked.connect(self._toggle_camera)
        self.lamp_btn = QPushButton("💡 Luz: OFF")
        self.lamp_btn.setCheckable(True)
        self.lamp_btn.clicked.connect(self._toggle_lamp)
        btn_row.addWidget(self.camera_btn)
        btn_row.addWidget(self.lamp_btn)
        cam_layout.addLayout(btn_row)
        left.addWidget(cam_box)

        # Servo
        servo_box = QGroupBox("Control del Servo (0°–180°)")
        servo_layout = QHBoxLayout(servo_box)
        self.servo_slider = QSlider(Qt.Horizontal)
        self.servo_slider.setRange(0, 180)
        self.servo_slider.setValue(90)
        self.servo_slider.valueChanged.connect(self._servo_changed)
        self.servo_label = QLabel("90°")
        servo_layout.addWidget(self.servo_slider)
        servo_layout.addWidget(self.servo_label)
        left.addWidget(servo_box)

        left.addStretch(1)

        # -----------------------------------
        # Columna derecha: consumo de stack
        # -----------------------------------
        right = QVBoxLayout()
        main_layout.addLayout(right, 1)

        agents_box = QGroupBox("Consumo de Stack (Watermark)")
        agents_layout = QVBoxLayout(agents_box)

        self.agent_labels = {
            "A1": QLabel("Agente WiFi: --"),
            "A2": QLabel("Agente Manejador de Modos: --"),
            "A3": QLabel("Agente Luces RGB: --"),
            "A4": QLabel("Agente Motores de las Ruedas: --"),
            "A5": QLabel("Agente Servomotor de la Cámara: --"),
            "A6": QLabel("Agente Sensores Infrarrojos: --"),
            "A7": QLabel("Agente Sensor Ultrasonico: --"),
            "A8": QLabel("Agente Modo Evasión de Obstáculos: --"),
            "A9": QLabel("Agente Modo Seguimiento de Objetos: --"),
        }

        for lbl in self.agent_labels.values():
            lbl.setFont(QFont("Arial", 10))
            agents_layout.addWidget(lbl)

        agents_box.setMinimumWidth(250)
        right.addWidget(agents_box)
        right.addStretch(1)

        # -----------------------------------
        #  Timer de ping
        # -----------------------------------
        self.timer = QTimer()
        self.timer.timeout.connect(lambda: asyncio.create_task(self._send_ping()))

    # ======== Funciones internas ========
    async def _send_ping(self):
        if not self.client.connected:
            return
        try:
            await self.client.send({
                "ping": 1,
                "D": self.servo,
                "M": self.lamp
            })
        except Exception as e:
            print(f"Error enviando ping: {e}")

    def _toggle_camera(self):
        if self.camera_btn.isChecked():
            if self.video_thread and self.video_thread.isRunning():
                return
            self.video_thread = VideoThread(self.video_url)
            self.video_thread.frame_ready.connect(self._update_video)
            self.video_thread.start()
            self.camera_btn.setText("Detener Cámara")
        else:
            if self.video_thread and self.video_thread.isRunning():
                self.video_thread.stop()
            self.video_label.setText("Cámara detenida")
            self.camera_btn.setText("🎥 Iniciar Cámara")

    def _update_video(self, img: QImage):
        self.video_label.setPixmap(QPixmap.fromImage(img))

    def _toggle_lamp(self):
        self.lamp = 1 if self.lamp_btn.isChecked() else 0
        self.lamp_btn.setText(f"💡 Luz: {'ON' if self.lamp else 'OFF'}")

    def _servo_changed(self, val):
        self.servo = val
        self.servo_label.setText(f"{val}°")

    def handle_message(self, data: dict):
        if "status" in data:
            self.status_label.setText(f"Estado: {data['status']}")
            return

        # Datos de sensores
        if "BV" in data:
            try:
                self.bv.setText(f"Batería: {float(data['BV']):.2f} V")
            except Exception:
                self.bv.setText(f"Batería: {data['BV']}")
        if "N" in data:
            self.ir_l.setText(f"IR Izq: {'Obstáculo' if data['N'] else 'Libre'}")
        if "P" in data:
            self.ir_r.setText(f"IR Der: {'Obstáculo' if data['P'] else 'Libre'}")
        if "O" in data:
            try:
                self.us.setText(f"Ultrasonido: {float(data['O']):.2f} cm")
            except Exception:
                self.us.setText(f"Ultrasonido: {data['O']}")

        # Datos de agentes (watermarks)
        for key, label in self.agent_labels.items():
            if key in data:
                try:
                    value = int(data[key])
                    label.setText(f"{label.text().split(':')[0]}: {value} bytes")
                except Exception:
                    label.setText(f"{label.text().split(':')[0]}: {data[key]}")

    def on_enter(self):
        self.timer.start(500)  # cada 500 ms

    def on_leave(self):
        self.timer.stop()

    def stop(self):
        self.timer.stop()
        if self.video_thread and self.video_thread.isRunning():
            self.video_thread.stop()



# ==============================
#        VENTANA PRINCIPAL
# ==============================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GalaxyRVR - Control")
        self.setGeometry(200, 200, 520, 560)

        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.global_status = QLabel("Estado global: Desconectado")
        self.global_status.setFont(QFont("Arial", 11, QFont.Bold))
        root.addWidget(self.global_status)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.current_handler = None
        self.current_page_name = "menu"

        self.client = RoverClient(on_message=self._dispatch_message,
                                  on_disconnect=self._on_disconnect)

        # Menú principal (simple; puedes reemplazar por tu menú mejorado)
        self.menu_page = QWidget()
        v = QVBoxLayout(self.menu_page)
        lbl = QLabel("Selecciona un modo"); lbl.setFont(QFont("Arial", 13, QFont.Bold))
        v.addWidget(lbl)

        b1 = QPushButton("Modo Manual"); b1.clicked.connect(lambda: self._goto("manual"))
        b2 = QPushButton("Modo Sistemático"); b2.clicked.connect(lambda: self._goto("systematic"))
        b3 = QPushButton("Modo Monitor"); b3.clicked.connect(lambda: self._goto("monitor"))
        for b in (b1, b2, b3): v.addWidget(b)

        self.stack.addWidget(self.menu_page)  # index 0

        # Subpáginas
        self.manual_page = ManualPage(self.request_connect, self.client)
        self.systematic_page = SystematicPage(self.request_connect, self.client)
        self.monitor_page = MonitorPage(self.request_connect, self.client)

        # contenedores con botón "Volver"
        self.manual_idx = self.stack.addWidget(self._wrap_with_back(self.manual_page, "Modo Manual"))      # 1
        self.systematic_idx = self.stack.addWidget(self._wrap_with_back(self.systematic_page, "Modo Sistemático"))  # 2
        self.monitor_idx = self.stack.addWidget(self._wrap_with_back(self.monitor_page, "Modo Monitor"))    # 3

        self.stack.setCurrentIndex(0)

    def _wrap_with_back(self, inner_widget: QWidget, title: str) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        header = QHBoxLayout()
        htitle = QLabel(title); htitle.setFont(QFont("Arial", 12, QFont.Bold))
        back = QPushButton("← Volver al Menú")
        back.clicked.connect(lambda: self._goto("menu"))
        header.addWidget(htitle); header.addStretch(1); header.addWidget(back)
        v.addLayout(header)
        v.addWidget(inner_widget, 1)
        return w

    def _get_page_by_name(self, name: str):
        if name == "manual":
            return self.manual_page, self.manual_idx
        if name == "systematic":
            return self.systematic_page, self.systematic_idx
        if name == "monitor":
            return self.monitor_page, self.monitor_idx
        return None, 0  # menu

    def _goto(self, where: str):
        # Llamar on_leave de la página actual si aplica
        old_page, _ = self._get_page_by_name(self.current_page_name)
        if old_page and hasattr(old_page, "on_leave"):
            try:
                old_page.on_leave()
            except Exception as e:
                print(f"on_leave error: {e}")

        # Cambiar índice
        if where == "menu":
            self.current_handler = None
            self.stack.setCurrentIndex(0)
        else:
            new_page, idx = self._get_page_by_name(where)
            self.current_handler = new_page.handle_message
            self.stack.setCurrentIndex(idx)
            # on_enter de la nueva
            if hasattr(new_page, "on_enter"):
                try:
                    new_page.on_enter()
                except Exception as e:
                    print(f"on_enter error: {e}")
        self.current_page_name = where

    def request_connect(self, handler):
        self.current_handler = handler
        return self.client.connect()

    def _dispatch_message(self, data: dict):
        if "status" in data and data["status"] == "connected":
            self.global_status.setText("Estado global: Conectado ✓")
        if self.current_handler:
            try:
                self.current_handler(data)
            except Exception as e:
                print(f"Handler error: {e}")

    def _on_disconnect(self, reason: str):
        self.global_status.setText(f"Estado global: {reason}")

    def closeEvent(self, event):
        try:
            if hasattr(self.manual_page, "stop"): self.manual_page.stop()
            if hasattr(self.systematic_page, "stop"): self.systematic_page.stop()
            if hasattr(self.monitor_page, "stop"): self.monitor_page.stop()
        except Exception:
            pass
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(self.client.disconnect())
        event.accept()


# ==============================
#             MAIN
# ==============================
def main():
    app = QApplication(sys.argv)
    apply_stylesheet(app, theme='dark_teal.xml')
    app.setStyleSheet(app.styleSheet() + """
        QGroupBox {
            border: 1px solid #00bfa5;
            border-radius: 8px;
            margin-top: 8px;
            color: #e0e0e0;
        }
        QLabel {
            color: #e0e0e0;
        }
        QPushButton {
            font-weight: bold;
        }
    """)


    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = MainWindow()
    window.show()
    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
