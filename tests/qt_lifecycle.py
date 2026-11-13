from PySide6.QtCore import QCoreApplication, QEvent


def flush_deferred_deletes():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def dispose_widget(widget):
    widget.close()
    widget.deleteLater()
    flush_deferred_deletes()
