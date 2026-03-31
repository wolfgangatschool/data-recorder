"""Physics Data Recorder — entry point.

Run with:
    python main.py
"""

import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from main_window import MainWindow


def main() -> None:
    # High-DPI support (Qt 6 default is already "on", but be explicit).
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("Physics Data Recorder")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
