"""PySide6 desktop interface for SATPHONE."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)

from . import MAX_MESSAGE_BYTES, __version__
from .app_paths import (
    app_log_path,
    app_support_dir,
    bridge_config_path,
    bridge_state_path,
    ensure_app_directories,
    migrate_bridge_config,
)
from .app_service import DeviceService, DiagnosticResult, MessageSyncResult, SendResult
from .bridge_manager import BridgeManager
from .discord_bridge import (
    BridgeConfig,
    DISCORD_KEYCHAIN_SERVICE,
    DISCORD_TOKEN_ENV,
    NOTEHUB_KEYCHAIN_SERVICE,
    NOTEHUB_TOKEN_ENV,
    load_secret,
    store_secrets,
)
from .firmware import FirmwarePort, flash_tdeck, inspect_tdeck, list_firmware_ports
from .messages import message_size_bytes, validate_message
from .models import CheckLevel, Message, SyncPhase, SyncUpdate
from .notehub_admin import NotehubAdminClient, NotehubNote


class AppSignals(QObject):
    bridge_status = Signal(str)
    bridge_error = Signal(str)
    discord_queued = Signal(str)


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function: Callable[..., Any], with_progress: bool = False):
        super().__init__()
        self.function = function
        self.with_progress = with_progress
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            if self.with_progress:
                result = self.function(self.signals.progress.emit)
            else:
                result = self.function()
        except Exception as exc:
            self.signals.error.emit(str(exc))
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


def _level_color(level: CheckLevel) -> QColor:
    return {
        CheckLevel.PASS: QColor("#15803d"),
        CheckLevel.WARN: QColor("#b45309"),
        CheckLevel.FAIL: QColor("#b91c1c"),
        CheckLevel.INFO: QColor("#0369a1"),
    }[level]


def _timestamp(epoch: Optional[int]) -> str:
    if epoch is None:
        return "—"
    try:
        return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(epoch)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_app_directories()
        migrate_bridge_config()
        self.settings = QSettings("SATPHONE", "SATPHONE")
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(4)
        self.device = DeviceService(
            poll_seconds=float(self.settings.value("sync_poll_seconds", 10)),
            timeout_seconds=float(self.settings.value("sync_timeout_seconds", 900)),
        )
        self.app_signals = AppSignals()
        self.app_signals.bridge_status.connect(self._bridge_status_changed)
        self.app_signals.bridge_error.connect(self._bridge_error_received)
        self.app_signals.discord_queued.connect(self._discord_message_queued)
        self.bridge = BridgeManager(
            status_callback=self.app_signals.bridge_status.emit,
            queued_callback=self.app_signals.discord_queued.emit,
            error_callback=self.app_signals.bridge_error.emit,
        )
        self.usb_busy = False
        self.pending_auto_receive = False
        self.local_messages: List[Message] = []
        self.notehub_notes: List[NotehubNote] = []
        self.last_diagnostic: Optional[DiagnosticResult] = None
        self.bridge_should_run = bool(self.settings.value("bridge_should_run", True, type=bool))
        self.bridge_restart_attempts = 0

        self.setWindowTitle("SATPHONE")
        self.resize(1120, 760)
        self.setMinimumSize(900, 650)
        self._build_menu()
        self._build_ui()
        self._apply_style()
        self._load_connection_fields()
        self._refresh_notecard_ports()
        self._refresh_firmware_ports()
        self._log("SATPHONE application started.")

        self.bridge_watchdog = QTimer(self)
        self.bridge_watchdog.timeout.connect(self._bridge_watchdog_tick)
        self.bridge_watchdog.start(15000)
        if self.bridge_should_run:
            QTimer.singleShot(500, self._start_bridge)

    def _build_menu(self) -> None:
        help_action = QAction("SATPHONE Help", self)
        help_action.triggered.connect(lambda: self.tabs.setCurrentWidget(self.help_tab))
        self.menuBar().addMenu("Help").addAction(help_action)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 18)

        header = QHBoxLayout()
        title = QLabel("SATPHONE")
        title.setObjectName("appTitle")
        subtitle = QLabel("Notecard + StarNote control center")
        subtitle.setObjectName("subtitle")
        title_box = QVBoxLayout()
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.notecard_badge = QLabel("Notecard: checking")
        self.notecard_badge.setObjectName("statusBadge")
        self.bridge_badge = QLabel("Discord: stopped")
        self.bridge_badge.setObjectName("statusBadge")
        header.addWidget(self.notecard_badge)
        header.addWidget(self.bridge_badge)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.dashboard_tab = self._dashboard_tab()
        self.messages_tab = self._messages_tab()
        self.diagnostics_tab = self._diagnostics_tab()
        self.connections_tab = self._connections_tab()
        self.maintenance_tab = self._maintenance_tab()
        self.firmware_tab = self._firmware_tab()
        self.help_tab = self._help_tab()
        self.logs_tab = self._logs_tab()
        for label, widget in (
            ("Dashboard", self.dashboard_tab),
            ("Messages", self.messages_tab),
            ("Diagnostics & Repair", self.diagnostics_tab),
            ("Connections", self.connections_tab),
            ("Maintenance", self.maintenance_tab),
            ("Firmware", self.firmware_tab),
            ("Help & Setup", self.help_tab),
            ("Logs", self.logs_tab),
        ):
            self.tabs.addTab(widget, label)
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)

    def _dashboard_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(
            "One window controls the USB device, Discord bridge, satellite syncs, diagnostics, and maintenance."
        )
        intro.setWordWrap(True)
        intro.setObjectName("lead")
        layout.addWidget(intro)

        device_box = QGroupBox("Connected hardware")
        device_layout = QHBoxLayout(device_box)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(380)
        refresh = QPushButton("Refresh USB")
        refresh.clicked.connect(self._refresh_notecard_ports)
        device_layout.addWidget(QLabel("Notecard port:"))
        device_layout.addWidget(self.port_combo)
        device_layout.addWidget(refresh)
        device_layout.addStretch()
        layout.addWidget(device_box)

        actions = QGroupBox("Quick actions")
        action_layout = QGridLayout(actions)
        diag = QPushButton("Run Full Diagnostic")
        diag.clicked.connect(self._run_diagnostics)
        receive = QPushButton("Receive Now")
        receive.clicked.connect(self._receive_now)
        bridge = QPushButton("Repair / Restart Discord")
        bridge.clicked.connect(self._restart_bridge)
        safe = QPushButton("Fix Safe Local Issues")
        safe.clicked.connect(self._safe_repair)
        action_layout.addWidget(diag, 0, 0)
        action_layout.addWidget(receive, 0, 1)
        action_layout.addWidget(bridge, 1, 0)
        action_layout.addWidget(safe, 1, 1)
        layout.addWidget(actions)

        self.dashboard_summary = QTextBrowser()
        self.dashboard_summary.setHtml(
            "<h3>Ready for a test</h3><ol><li>Make sure the Notecard and StarNote are plugged in.</li>"
            "<li>Check that Discord says <b>online</b>.</li><li>Use <b>/satphone</b> in Discord.</li>"
            "<li>Leave <b>Auto receive</b> on, or click <b>Receive Now</b>.</li></ol>"
        )
        layout.addWidget(self.dashboard_summary, 1)
        return page

    def _messages_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        compose_box = QGroupBox("Send from the device to Discord")
        compose_layout = QVBoxLayout(compose_box)
        self.compose = QPlainTextEdit()
        self.compose.setPlaceholderText("Type a short message (160 UTF-8 bytes maximum)…")
        self.compose.setMaximumHeight(90)
        self.compose.textChanged.connect(self._update_message_count)
        compose_layout.addWidget(self.compose)
        bottom = QHBoxLayout()
        self.message_count = QLabel("0 / 160 bytes")
        bottom.addWidget(self.message_count)
        bottom.addStretch()
        send = QPushButton("Send Over Satellite")
        send.clicked.connect(self._send_message)
        bottom.addWidget(send)
        compose_layout.addLayout(bottom)
        layout.addWidget(compose_box)

        incoming_box = QGroupBox("Messages received by this device")
        incoming_layout = QVBoxLayout(incoming_box)
        controls = QHBoxLayout()
        receive = QPushButton("Receive Now")
        receive.clicked.connect(self._receive_now)
        refresh = QPushButton("Read Local Inbox")
        refresh.clicked.connect(self._read_local_messages)
        clear = QPushButton("Delete Displayed Local Messages…")
        clear.clicked.connect(self._delete_local_messages)
        self.auto_receive = QCheckBox("Auto receive after a Discord /satphone message is queued")
        self.auto_receive.setChecked(bool(self.settings.value("auto_receive", True, type=bool)))
        self.auto_receive.toggled.connect(lambda value: self.settings.setValue("auto_receive", value))
        controls.addWidget(receive)
        controls.addWidget(refresh)
        controls.addWidget(clear)
        controls.addStretch()
        controls.addWidget(self.auto_receive)
        incoming_layout.addLayout(controls)
        self.message_table = QTableWidget(0, 3)
        self.message_table.setHorizontalHeaderLabels(["Time", "Message", "Local Note ID"])
        self.message_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.message_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.message_table.setEditTriggers(QTableWidget.NoEditTriggers)
        incoming_layout.addWidget(self.message_table)
        layout.addWidget(incoming_box, 1)
        return page

    def _diagnostics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        run = QPushButton("Run Full Diagnostic")
        run.clicked.connect(self._run_diagnostics)
        safe = QPushButton("Fix Safe Local Issues")
        safe.clicked.connect(self._safe_repair)
        templates = QPushButton("Repair Message Templates…")
        templates.clicked.connect(self._repair_templates)
        transport = QPushButton("Restore Satellite Transport…")
        transport.clicked.connect(self._repair_transport)
        export = QPushButton("Export Report")
        export.clicked.connect(self._export_diagnostic)
        for widget in (run, safe, templates, transport, export):
            controls.addWidget(widget)
        controls.addStretch()
        layout.addLayout(controls)
        note = QLabel(
            "Diagnostics are read-only. Template and transport changes have separate confirmation buttons."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.diagnostic_table = QTableWidget(0, 4)
        self.diagnostic_table.setHorizontalHeaderLabels(
            ["Level", "Check", "Result", "Recommended action"]
        )
        self.diagnostic_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.diagnostic_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.diagnostic_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.diagnostic_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.diagnostic_table, 1)
        return page

    def _connections_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        config_box = QGroupBox("Discord and Notehub identifiers (not secrets)")
        form = QFormLayout(config_box)
        self.project_uid = QLineEdit()
        self.device_uid = QLineEdit()
        self.guild_id = QLineEdit()
        self.channel_id = QLineEdit()
        self.allowed_users = QLineEdit()
        self.allowed_users.setPlaceholderText("Discord user IDs separated by commas")
        self.trust_permissions = QCheckBox("Trust Discord's command permissions when no local user IDs are listed")
        form.addRow("Notehub ProjectUID", self.project_uid)
        form.addRow("Notecard DeviceUID", self.device_uid)
        form.addRow("Discord server ID", self.guild_id)
        form.addRow("Discord channel ID", self.channel_id)
        form.addRow("Allowed Discord user IDs", self.allowed_users)
        form.addRow("Authorization", self.trust_permissions)
        save = QPushButton("Save Connection Settings")
        save.clicked.connect(self._save_connection_fields)
        form.addRow("", save)
        layout.addWidget(config_box)

        secrets = QGroupBox("Credentials stored only in macOS Keychain")
        secrets_form = QFormLayout(secrets)
        self.discord_token = QLineEdit()
        self.discord_token.setEchoMode(QLineEdit.Password)
        self.discord_token.setPlaceholderText("Paste a new token only when changing it")
        self.notehub_token = QLineEdit()
        self.notehub_token.setEchoMode(QLineEdit.Password)
        self.notehub_token.setPlaceholderText("Paste an expiring Personal Access Token")
        save_secrets = QPushButton("Store Both in Keychain")
        save_secrets.clicked.connect(self._save_secrets)
        secrets_form.addRow("Discord bot token", self.discord_token)
        secrets_form.addRow("Notehub token", self.notehub_token)
        secrets_form.addRow("", save_secrets)
        layout.addWidget(secrets)

        bridge_box = QGroupBox("Discord bridge")
        bridge_layout = QHBoxLayout(bridge_box)
        start = QPushButton("Start")
        start.clicked.connect(self._start_bridge)
        stop = QPushButton("Stop")
        stop.clicked.connect(self._stop_bridge)
        restart = QPushButton("Restart")
        restart.clicked.connect(self._restart_bridge)
        register = QPushButton("Register /satphone Command…")
        register.clicked.connect(self._register_discord_command)
        for widget in (start, stop, restart, register):
            bridge_layout.addWidget(widget)
        bridge_layout.addStretch()
        layout.addWidget(bridge_box)
        layout.addStretch()
        return page

    def _maintenance_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        warning = QLabel(
            "This page shows the live messages.qi queue waiting in Notehub. Event history is a delivery record, not this queue, and this app does not erase event history."
        )
        warning.setWordWrap(True)
        warning.setObjectName("warning")
        layout.addWidget(warning)
        buttons = QHBoxLayout()
        preview = QPushButton("Preview Notehub Queue")
        preview.clicked.connect(self._preview_notehub)
        delete = QPushButton("Delete Selected Queued Notes…")
        delete.clicked.connect(self._delete_notehub_selected)
        buttons.addWidget(preview)
        buttons.addWidget(delete)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.notehub_table = QTableWidget(0, 3)
        self.notehub_table.setHorizontalHeaderLabels(["Time", "Message", "Notehub Note ID"])
        self.notehub_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.notehub_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.notehub_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.notehub_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.notehub_table, 1)
        local = QPushButton("Go to Local Inbox Cleanup")
        local.clicked.connect(lambda: self.tabs.setCurrentWidget(self.messages_tab))
        layout.addWidget(local, alignment=Qt.AlignLeft)
        return page

    def _firmware_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        lead = QLabel(
            "T-Deck firmware center — this flashes the T-Deck's ESP32-S3 only. It does not flash the Notecard or StarNote."
        )
        lead.setWordWrap(True)
        lead.setObjectName("lead")
        layout.addWidget(lead)

        form_box = QGroupBox("T-Deck ESP32-S3")
        form = QFormLayout(form_box)
        port_line = QHBoxLayout()
        self.firmware_port = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_firmware_ports)
        probe = QPushButton("Detect Chip")
        probe.clicked.connect(self._probe_tdeck)
        port_line.addWidget(self.firmware_port, 1)
        port_line.addWidget(refresh)
        port_line.addWidget(probe)
        self.firmware_file = QLineEdit()
        choose = QPushButton("Choose .bin…")
        choose.clicked.connect(self._choose_firmware)
        file_line = QHBoxLayout()
        file_line.addWidget(self.firmware_file, 1)
        file_line.addWidget(choose)
        self.flash_address = QLineEdit("0x0")
        self.flash_baud = QComboBox()
        self.flash_baud.addItems(["115200", "230400", "460800", "921600"])
        self.flash_baud.setCurrentText("460800")
        self.flash_ack = QCheckBox(
            "I checked the firmware release instructions and entered the address they specify."
        )
        flash = QPushButton("Flash T-Deck Firmware…")
        flash.clicked.connect(self._flash_tdeck)
        form.addRow("T-Deck USB port", port_line)
        form.addRow("Firmware image", file_line)
        form.addRow("Flash address", self.flash_address)
        form.addRow("Speed", self.flash_baud)
        form.addRow("Safety check", self.flash_ack)
        form.addRow("", flash)
        layout.addWidget(form_box)

        self.firmware_output = QPlainTextEdit()
        self.firmware_output.setReadOnly(True)
        self.firmware_output.setPlaceholderText("Chip detection and flash verification results appear here.")
        layout.addWidget(self.firmware_output, 1)
        guidance = QLabel(
            "Notecard firmware should use Blues' Notehub firmware workflow. StarNote flashing is an advanced service procedure because some kits need extra hardware. Open Help & Setup for links and explanations."
        )
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        return page

    def _help_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(
            """
            <h1>SATPHONE setup and testing</h1>
            <h2>What this app does</h2>
            <p>Think of the Notecard as the traffic manager and StarNote as the satellite radio. This Mac app gives them instructions over USB. Discord messages first wait in Notehub, then the device asks the satellite network to download them.</p>
            <h2>First-time setup</h2>
            <ol>
              <li>Plug the Notecard/StarNote kit into this Mac and close any Blues browser terminal.</li>
              <li>Open <b>Connections</b> and enter the Notehub project/device IDs and Discord server/channel IDs.</li>
              <li>Store the Discord bot token and an expiring Notehub Personal Access Token in Keychain.</li>
              <li>Click <b>Register /satphone Command</b> once, then start the bridge.</li>
              <li>Run <b>Full Diagnostic</b>. Approve template or transport repairs only after reading the warning.</li>
            </ol>
            <h2>Complete a message test</h2>
            <ol>
              <li>In Discord, type <b>/satphone</b>, fill in the message field, and send it. An ordinary channel post is not forwarded.</li>
              <li>Discord should say the message was queued in Notehub.</li>
              <li>If Auto receive is on, this app begins the inbound satellite sync. Otherwise click <b>Receive Now</b>.</li>
              <li>Move outside with a wide, clear view of the sky. A satellite attempt can take several minutes.</li>
              <li>The message appears in the Messages tab after the local sync completes.</li>
            </ol>
            <h2>Satellite versus Wi-Fi</h2>
            <p>Normal tests use the transport currently configured on the Notecard. The diagnostic shows that choice. A one-time Wi-Fi or cellular sync can be necessary after creating a new compact message template; after that, use <b>Restore Satellite Transport</b> to return to NTN-only testing.</p>
            <h2>What “communicating” means</h2>
            <p><b>Queued</b> means Notehub accepted the message. <b>Sync completed</b> means the Notecard finished its attempt. A Notehub event whose transport is <code>ntn:skylo</code> or <code>ntn:iridium</code> is the strongest proof that real satellite transport was used.</p>
            <h2>Automatic repair</h2>
            <p>The app reopens USB for every operation, serializes all device work, and restarts its own Discord bridge when it unexpectedly stops. It can refresh USB discovery safely. Permanent device changes, message deletion, and firmware flashing always need your confirmation.</p>
            <h2>Firmware</h2>
            <p>The Firmware tab supports a T-Deck ESP32-S3 <code>.bin</code> image at the exact address supplied by its release instructions. A wrong image or address can stop the T-Deck from booting. Notecard updates should use the <a href="https://dev.blues.io/notehub/host-firmware-updates/notecard-outboard-firmware-update/">Blues Notehub firmware workflow</a>. StarNote updates may require special hardware; follow the <a href="https://dev.blues.io/starnote/starnote-firmware-releases/">official StarNote release instructions</a>.</p>
            <h2>Opening an unsigned GitHub download</h2>
            <p>Because this is a community app and not notarized by Apple, macOS may warn the first time. In Finder, Control-click SATPHONE.app, choose <b>Open</b>, then confirm. Only download releases from the project’s GitHub page.</p>
            <h2>When something is busy</h2>
            <p>Only one app can use the Notecard USB port. Quit the older SATPHONE terminal, close the Blues browser terminal, and retry. The new app does not need either of the old command files.</p>
            """
        )
        layout.addWidget(browser)
        return page

    def _logs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        open_folder = QPushButton("Open App Data Folder")
        open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_support_dir())))
        )
        clear_view = QPushButton("Clear View")
        clear_view.clicked.connect(lambda: self.log_view.clear())
        controls.addWidget(open_folder)
        controls.addWidget(clear_view)
        controls.addStretch()
        layout.addLayout(controls)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Menlo", 11))
        layout.addWidget(self.log_view)
        return page

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f6f7f9; color: #172033; }
            QLabel#appTitle { font-size: 28px; font-weight: 800; color: #102a43; }
            QLabel#subtitle { color: #52667a; }
            QLabel#lead { font-size: 15px; color: #334e68; padding: 4px 0 8px 0; }
            QLabel#warning { background: #fff7ed; color: #9a3412; border: 1px solid #fdba74; border-radius: 7px; padding: 10px; }
            QLabel#statusBadge { background: white; border: 1px solid #cbd5e1; border-radius: 12px; padding: 6px 10px; }
            QGroupBox { background: white; border: 1px solid #d8e0e8; border-radius: 8px; margin-top: 10px; padding: 12px; font-weight: 650; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QPushButton { background: #155eef; color: white; border: none; border-radius: 6px; padding: 8px 13px; font-weight: 600; }
            QPushButton:hover { background: #004eeb; }
            QPushButton:disabled { background: #94a3b8; }
            QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QTableWidget { background: white; border: 1px solid #cbd5e1; border-radius: 5px; padding: 4px; }
            QTabWidget::pane { border: 1px solid #d8e0e8; background: #f8fafc; }
            QTabBar::tab { padding: 9px 12px; }
            QTabBar::tab:selected { color: #155eef; font-weight: 700; }
            """
        )

    def _selected_port(self) -> Optional[str]:
        return self.port_combo.currentData()

    def _refresh_notecard_ports(self) -> None:
        selected = self._selected_port()
        self.port_combo.clear()
        try:
            ports = self.device.ports()
        except Exception as exc:
            self.notecard_badge.setText("Notecard: USB error")
            self._log("USB discovery failed: {}".format(exc))
            return
        for port in ports:
            self.port_combo.addItem("{} — {}".format(port.device, port.description), port.device)
        if selected:
            index = self.port_combo.findData(selected)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        if ports:
            self.notecard_badge.setText("Notecard: detected")
            self._log("Detected Notecard on {}.".format(ports[0].device))
        else:
            self.notecard_badge.setText("Notecard: unplugged")
            self.port_combo.addItem("No Notecard detected", None)

    def _refresh_firmware_ports(self) -> None:
        selected = self.firmware_port.currentData() if hasattr(self, "firmware_port") else None
        if not hasattr(self, "firmware_port"):
            return
        self.firmware_port.clear()
        try:
            ports = list_firmware_ports()
        except Exception as exc:
            self.firmware_output.appendPlainText("Port discovery failed: {}".format(exc))
            return
        for port in ports:
            label = "{} — {}{}".format(
                port.device,
                port.description,
                " (likely ESP/T-Deck)" if port.likely_tdeck else "",
            )
            self.firmware_port.addItem(label, port.device)
        if selected:
            index = self.firmware_port.findData(selected)
            if index >= 0:
                self.firmware_port.setCurrentIndex(index)
        if not ports:
            self.firmware_port.addItem("No non-Notecard serial device found", None)

    def _run_worker(
        self,
        function: Callable[..., Any],
        on_result: Optional[Callable[[Any], None]] = None,
        *,
        label: str,
        usb: bool = False,
        with_progress: bool = False,
    ) -> bool:
        if usb and self.usb_busy:
            self._log("{} postponed because another USB operation is active.".format(label))
            QMessageBox.information(self, "SATPHONE is busy", "Wait for the current device operation to finish.")
            return False
        if usb:
            self.usb_busy = True
            self.notecard_badge.setText("Notecard: busy")
        self._log("{} started.".format(label))
        worker = Worker(function, with_progress=with_progress)
        if on_result:
            worker.signals.result.connect(on_result)
        worker.signals.error.connect(lambda message: self._task_error(label, message))
        worker.signals.progress.connect(self._task_progress)
        if usb:
            worker.signals.finished.connect(lambda: self._usb_finished(label))
        else:
            worker.signals.finished.connect(lambda: self._log("{} finished.".format(label)))
        self.thread_pool.start(worker)
        return True

    @Slot(object)
    def _task_progress(self, value: object) -> None:
        if isinstance(value, SyncUpdate):
            self._log("Sync {}s — {}: {}".format(value.elapsed, value.phase.value, value.message))
        else:
            self._log(str(value))

    def _task_error(self, label: str, message: str) -> None:
        owner = self._serial_owner_hint() if "open the Notecard" in message or "serial" in message.lower() else ""
        detail = "{}{}".format(message, owner)
        self._log("{} failed: {}".format(label, detail))
        QMessageBox.critical(self, "{} failed".format(label), detail)

    def _usb_finished(self, label: str) -> None:
        self.usb_busy = False
        self.notecard_badge.setText("Notecard: detected")
        self._log("{} finished.".format(label))
        if self.pending_auto_receive and self.auto_receive.isChecked():
            self.pending_auto_receive = False
            QTimer.singleShot(250, lambda: self._receive_now(automatic=True))

    def _serial_owner_hint(self) -> str:
        port = self._selected_port()
        if not port:
            return ""
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-t", str(port)],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        pids = [value for value in result.stdout.split() if value.isdigit()]
        if not pids:
            return ""
        return "\n\nUSB is currently owned by process {}. Close the old SATPHONE or Blues terminal, then retry.".format(
            ", ".join(pids)
        )

    def _run_diagnostics(self) -> None:
        self._run_worker(
            lambda: self.device.diagnostics(self._selected_port()),
            self._show_diagnostics,
            label="Full diagnostic",
            usb=True,
        )

    def _show_diagnostics(self, result: DiagnosticResult) -> None:
        self.last_diagnostic = result
        checks = result.snapshot.checks
        self.diagnostic_table.setRowCount(len(checks))
        pass_count = 0
        fail_count = 0
        for row, check in enumerate(checks):
            if check.level == CheckLevel.PASS:
                pass_count += 1
            if check.level == CheckLevel.FAIL:
                fail_count += 1
            level = QTableWidgetItem(check.level.value.upper())
            level.setForeground(_level_color(check.level))
            level.setFont(QFont(level.font().family(), level.font().pointSize(), QFont.Bold))
            self.diagnostic_table.setItem(row, 0, level)
            self.diagnostic_table.setItem(row, 1, QTableWidgetItem(check.name))
            self.diagnostic_table.setItem(row, 2, QTableWidgetItem(check.summary))
            self.diagnostic_table.setItem(row, 3, QTableWidgetItem(check.recommendation or "—"))
        self.dashboard_summary.setHtml(
            "<h3>Latest diagnostic</h3><p><b>{}</b> checks passed; <b>{}</b> need attention.</p>"
            "<p>Open <b>Diagnostics & Repair</b> for details. No persistent setting was changed.</p>".format(
                pass_count, fail_count
            )
        )
        self.tabs.setCurrentWidget(self.diagnostics_tab)
        self._log("Diagnostic completed with {} pass and {} fail checks.".format(pass_count, fail_count))

    def _receive_now(self, automatic: bool = False) -> None:
        if automatic and self.usb_busy:
            self.pending_auto_receive = True
            self._log("Automatic receive is waiting for the current USB operation.")
            return
        label = "Automatic inbound satellite sync" if automatic else "Inbound satellite sync"
        self._run_worker(
            lambda emit: self.device.receive_messages(
                self._selected_port(), callback=lambda update: emit(update)
            ),
            self._receive_finished,
            label=label,
            usb=True,
            with_progress=True,
        )

    def _receive_finished(self, result: MessageSyncResult) -> None:
        self._show_local_messages(result.messages)
        if result.sync.completed:
            message = "Inbound sync completed. {} local message(s) are available.".format(len(result.messages))
            QMessageBox.information(self, "Receive complete", message)
        else:
            QMessageBox.warning(
                self,
                "Receive not confirmed",
                result.sync.error or "The satellite sync did not reach a confirmed completion state.",
            )
        self.tabs.setCurrentWidget(self.messages_tab)

    def _read_local_messages(self) -> None:
        self._run_worker(
            lambda: self.device.read_local_messages(self._selected_port()),
            self._show_local_messages,
            label="Read local inbox",
            usb=True,
        )

    def _show_local_messages(self, messages: List[Message]) -> None:
        self.local_messages = list(messages)
        self.message_table.setRowCount(len(messages))
        for row, message in enumerate(messages):
            self.message_table.setItem(row, 0, QTableWidgetItem(_timestamp(message.time)))
            self.message_table.setItem(row, 1, QTableWidgetItem(message.text))
            self.message_table.setItem(row, 2, QTableWidgetItem(message.note_id or "—"))
        self._log("Local inbox contains {} displayed message(s).".format(len(messages)))

    def _delete_local_messages(self) -> None:
        if not self.local_messages:
            QMessageBox.information(self, "Local inbox", "No displayed local messages are available to delete.")
            return
        typed, accepted = QInputDialog.getText(
            self,
            "Confirm local deletion",
            "Type DELETE to permanently remove the {} displayed local message(s):".format(len(self.local_messages)),
        )
        if not accepted or typed != "DELETE":
            return
        expected = list(self.local_messages)
        self._run_worker(
            lambda: self.device.delete_local_messages(self._selected_port(), expected),
            self._local_delete_finished,
            label="Delete displayed local messages",
            usb=True,
        )

    def _local_delete_finished(self, deleted: List[Message]) -> None:
        self.local_messages = []
        self.message_table.setRowCount(0)
        QMessageBox.information(self, "Local inbox cleared", "Deleted {} local message(s).".format(len(deleted)))

    def _update_message_count(self) -> None:
        count = message_size_bytes(self.compose.toPlainText().strip())
        self.message_count.setText("{} / {} bytes".format(count, MAX_MESSAGE_BYTES))
        self.message_count.setStyleSheet("color: #b91c1c;" if count > MAX_MESSAGE_BYTES else "")

    def _send_message(self) -> None:
        try:
            text = validate_message(self.compose.toPlainText())
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Message not ready", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "Send over satellite?",
            "Queue this message locally and start one outbound sync? Satellite data may be consumed.",
        )
        if answer != QMessageBox.Yes:
            return
        self._run_worker(
            lambda emit: self.device.send_message(
                self._selected_port(), text, callback=lambda update: emit(update)
            ),
            self._send_finished,
            label="Outbound satellite message",
            usb=True,
            with_progress=True,
        )

    def _send_finished(self, result: SendResult) -> None:
        self.compose.clear()
        if result.sync.completed:
            QMessageBox.information(
                self,
                "Outbound sync complete",
                "The Notecard completed its outbound sync. Confirm the Notehub event transport to prove satellite delivery.",
            )
        else:
            QMessageBox.warning(
                self,
                "Message queued; sync unresolved",
                result.sync.error or "The message was queued locally, but sync completion was not confirmed.",
            )

    def _safe_repair(self) -> None:
        self._refresh_notecard_ports()
        self.bridge_restart_attempts = 0
        if self.bridge_should_run:
            if self.bridge.running:
                self._restart_bridge()
            else:
                self._start_bridge()
        self._log("Safe repair refreshed USB discovery and the Discord bridge. No device setting or message was changed.")
        QMessageBox.information(
            self,
            "Safe repair complete",
            "USB discovery was refreshed and the Discord bridge was started or restarted. No persistent device setting and no message was changed.",
        )

    def _repair_templates(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Repair persistent message templates?",
            "This writes messages.qi on port 56 and messages.qo on port 57. New or changed templates need one Wi-Fi or cellular sync before satellite use. Continue?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        expected = self.last_diagnostic.templates if self.last_diagnostic else None
        self._run_worker(
            lambda: self.device.repair_templates(self._selected_port(), expected),
            self._templates_repaired,
            label="Repair message templates",
            usb=True,
        )

    def _templates_repaired(self, result: Dict[str, Any]) -> None:
        valid = all(check.valid for check in result.values())
        text = "Both templates now verify." if valid else "One or more templates still do not verify."
        text += " Complete one terrestrial sync before NTN use if either template changed."
        QMessageBox.information(self, "Template repair result", text)
        self._run_diagnostics()

    def _repair_transport(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Restore satellite-only transport?",
            "This permanently changes card.transport to method=ntn. It can turn off Wi-Fi/cellular fallback. Continue?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        self._run_worker(
            lambda: self.device.repair_transport(self._selected_port()),
            lambda result: QMessageBox.information(
                self,
                "Transport verified",
                "Satellite-only transport is set and verified as method=ntn.",
            ),
            label="Restore satellite transport",
            usb=True,
        )

    def _export_diagnostic(self) -> None:
        if not self.last_diagnostic:
            QMessageBox.information(self, "No diagnostic", "Run a diagnostic before exporting a report.")
            return
        try:
            path = self.device.export_diagnostics(self.last_diagnostic)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._log("Diagnostic report exported to {}.".format(path))
        QMessageBox.information(
            self,
            "Report exported",
            "Saved to:\n{}\n\nReview it before sharing; it can contain device IDs, location, and messages.".format(path),
        )

    def _load_connection_fields(self) -> None:
        path = bridge_config_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.project_uid.setText(str(raw.get("project_uid", "")))
        self.device_uid.setText(str(raw.get("device_uid", "")))
        self.guild_id.setText(str(raw.get("discord_guild_id", "")))
        self.channel_id.setText(str(raw.get("discord_channel_id", "")))
        self.allowed_users.setText(", ".join(str(value) for value in raw.get("allowed_user_ids", [])))
        self.trust_permissions.setChecked(bool(raw.get("trust_discord_command_permissions", False)))

    def _save_connection_fields(self) -> None:
        try:
            users = [
                int(value.strip())
                for value in self.allowed_users.text().split(",")
                if value.strip()
            ]
            raw = {
                "project_uid": self.project_uid.text().strip(),
                "device_uid": self.device_uid.text().strip(),
                "discord_guild_id": int(self.guild_id.text().strip()),
                "discord_channel_id": int(self.channel_id.text().strip()),
                "allowed_user_ids": users,
                "trust_discord_command_permissions": self.trust_permissions.isChecked(),
                "notefile": "messages.qi",
                "state_path": str(bridge_state_path()),
            }
            path = bridge_config_path()
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            BridgeConfig.load(temporary)
            os.replace(temporary, path)
        except Exception as exc:
            try:
                bridge_config_path().with_suffix(".json.tmp").unlink(missing_ok=True)
            except OSError:
                pass
            QMessageBox.critical(self, "Settings not saved", str(exc))
            return
        self._log("Connection identifiers saved in Application Support.")
        QMessageBox.information(self, "Settings saved", "Connection settings were validated and saved. Restarting the bridge applies them.")
        self._restart_bridge()

    def _save_secrets(self) -> None:
        try:
            store_secrets(self.discord_token.text(), self.notehub_token.text())
        except Exception as exc:
            QMessageBox.critical(self, "Keychain update failed", str(exc))
            return
        self.discord_token.clear()
        self.notehub_token.clear()
        self._log("Discord and Notehub credentials updated in macOS Keychain.")
        QMessageBox.information(self, "Credentials saved", "Both credentials are stored in macOS Keychain. They were not written to the project folder.")
        self._restart_bridge()

    @Slot(str)
    def _bridge_status_changed(self, status: str) -> None:
        self.bridge_badge.setText("Discord: {}".format(status))
        self._log("Discord bridge status: {}.".format(status))
        if status == "online":
            self.bridge_restart_attempts = 0

    @Slot(str)
    def _bridge_error_received(self, message: str) -> None:
        self._log("Discord bridge error: {}".format(message))

    @Slot(str)
    def _discord_message_queued(self, _message: str) -> None:
        self._log("Discord queued a Notehub message. Message text was not written to the app log.")
        if not self.auto_receive.isChecked():
            return
        if self.usb_busy:
            self.pending_auto_receive = True
            self._log("Automatic receive is waiting for the current USB operation.")
        else:
            QTimer.singleShot(100, lambda: self._receive_now(automatic=True))

    def _start_bridge(self) -> None:
        self.bridge_should_run = True
        self.settings.setValue("bridge_should_run", True)
        try:
            started = self.bridge.start(bridge_config_path())
        except Exception as exc:
            self._bridge_error_received(str(exc))
            return
        if started:
            self._log("Discord bridge start requested.")

    def _stop_bridge(self) -> None:
        self.bridge_should_run = False
        self.settings.setValue("bridge_should_run", False)
        self._run_worker(
            self.bridge.stop,
            lambda stopped: self._log("Discord bridge stopped." if stopped else "Discord bridge is still stopping."),
            label="Stop Discord bridge",
        )

    def _restart_bridge(self) -> None:
        self.bridge_should_run = True
        self.settings.setValue("bridge_should_run", True)
        if self.bridge.running:
            self._run_worker(
                self.bridge.restart,
                lambda started: self._log("Discord bridge restart requested." if started else "Discord bridge restart did not complete."),
                label="Restart Discord bridge",
            )
        else:
            self._start_bridge()

    def _bridge_watchdog_tick(self) -> None:
        if not self.bridge_should_run or self.bridge.running:
            return
        if self.bridge_restart_attempts >= 3:
            return
        self.bridge_restart_attempts += 1
        self._log("Bridge watchdog restart attempt {} of 3.".format(self.bridge_restart_attempts))
        self._start_bridge()

    def _register_discord_command(self) -> None:
        answer = QMessageBox.question(
            self,
            "Register Discord command?",
            "This updates the dedicated app's guild-only /satphone command. It refuses to overwrite unrelated commands. Continue?",
        )
        if answer != QMessageBox.Yes:
            return
        self._run_worker(
            lambda: BridgeManager.register_commands(bridge_config_path()),
            lambda _result: QMessageBox.information(self, "Discord ready", "The /satphone command was registered for the configured server."),
            label="Register Discord command",
        )

    def _notehub_client(self) -> NotehubAdminClient:
        config = BridgeConfig.load(bridge_config_path())
        token = load_secret(NOTEHUB_TOKEN_ENV, NOTEHUB_KEYCHAIN_SERVICE)
        return NotehubAdminClient(config.project_uid, config.device_uid, token, config.notefile)

    def _preview_notehub(self) -> None:
        self._run_worker(
            lambda: self._notehub_client().list_pending(),
            self._show_notehub_notes,
            label="Preview Notehub queue",
        )

    def _show_notehub_notes(self, notes: List[NotehubNote]) -> None:
        self.notehub_notes = list(notes)
        self.notehub_table.setRowCount(len(notes))
        for row, note in enumerate(notes):
            self.notehub_table.setItem(row, 0, QTableWidgetItem(_timestamp(note.time)))
            self.notehub_table.setItem(row, 1, QTableWidgetItem(note.text))
            self.notehub_table.setItem(row, 2, QTableWidgetItem(note.note_id))
        self._log("Notehub preview returned {} queued Note(s).".format(len(notes)))
        if not notes:
            QMessageBox.information(self, "Notehub queue", "messages.qi is not currently holding any queued Notes.")

    def _delete_notehub_selected(self) -> None:
        rows = sorted({index.row() for index in self.notehub_table.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(self, "Nothing selected", "Preview the queue and select one or more rows first.")
            return
        selected = [self.notehub_notes[row] for row in rows]
        typed, accepted = QInputDialog.getText(
            self,
            "Confirm Notehub deletion",
            "Type DELETE to remove {} selected queued Note(s) from messages.qi:".format(len(selected)),
        )
        if not accepted or typed != "DELETE":
            return
        note_ids = [note.note_id for note in selected]
        self._run_worker(
            lambda: self._notehub_client().delete_notes(note_ids),
            self._notehub_delete_finished,
            label="Delete selected Notehub Notes",
        )

    def _notehub_delete_finished(self, deleted: List[str]) -> None:
        QMessageBox.information(self, "Notehub queue updated", "Deleted {} selected queued Note(s). Refreshing the preview.".format(len(deleted)))
        self._preview_notehub()

    def _choose_firmware(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose T-Deck firmware", str(Path.home() / "Downloads"), "ESP32 firmware (*.bin)")
        if path:
            self.firmware_file.setText(path)

    def _probe_tdeck(self) -> None:
        port = self.firmware_port.currentData()
        if not port:
            QMessageBox.warning(self, "No T-Deck port", "Plug in the T-Deck over USB and refresh the list.")
            return
        self._run_worker(
            lambda: inspect_tdeck(port),
            lambda output: self.firmware_output.setPlainText(output or "ESP32-S3 detected."),
            label="Detect T-Deck chip",
        )

    def _flash_tdeck(self) -> None:
        port = self.firmware_port.currentData()
        if not port:
            QMessageBox.warning(self, "No T-Deck port", "Select the T-Deck USB port.")
            return
        if not self.flash_ack.isChecked():
            QMessageBox.warning(self, "Review the release instructions", "Confirm that the firmware file and flash address match its release instructions.")
            return
        typed, accepted = QInputDialog.getText(
            self,
            "Final firmware confirmation",
            "Type FLASH to overwrite the selected T-Deck ESP32-S3 firmware:",
        )
        if not accepted or typed != "FLASH":
            return
        path = Path(self.firmware_file.text())
        address = self.flash_address.text()
        baud = int(self.flash_baud.currentText())
        self.firmware_output.clear()
        self._run_worker(
            lambda emit: flash_tdeck(port, path, address, baud, progress=lambda line: emit(line)),
            self._flash_finished,
            label="Flash T-Deck firmware",
            with_progress=True,
        )

    def _flash_finished(self, output: str) -> None:
        self.firmware_output.setPlainText(output or "Firmware written and verified.")
        self.flash_ack.setChecked(False)
        QMessageBox.information(self, "Firmware complete", "The T-Deck firmware was written, verified, and the ESP32-S3 was reset.")

    def _log(self, message: str) -> None:
        line = "{}  {}".format(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"), message)
        if hasattr(self, "log_view"):
            self.log_view.appendPlainText(line)
        try:
            with app_log_path().open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def closeEvent(self, event: Any) -> None:
        self.settings.setValue("auto_receive", self.auto_receive.isChecked())
        self.settings.sync()
        if self.bridge.running:
            self.bridge.stop(timeout=3.0)
        event.accept()


def run_gui(argv: Optional[List[str]] = None) -> int:
    os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("SATPHONE")
    app.setOrganizationName("SATPHONE")
    app.setApplicationVersion(__version__)
    window = MainWindow()
    window.show()
    return app.exec()
