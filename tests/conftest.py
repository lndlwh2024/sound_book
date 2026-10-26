"""Keep one native Qt application alive across function/module UI fixtures."""
import os

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest


@pytest.fixture(scope='session',autouse=True)
def qt_application():
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app
