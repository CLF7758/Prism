"""Owned in-place name editor shared by native library views."""
from PyQt6 import QtCore, QtWidgets


def edit_name(view, index, text, commit):
    old=getattr(view,'_inline_name_editor',None)
    if old is not None:old.close()
    class NameEdit(QtWidgets.QLineEdit):
        def keyPressEvent(self,event):
            if event.key()==QtCore.Qt.Key.Key_Escape:
                self.done=True;self.close();return
            super().keyPressEvent(event)
    editor=NameEdit(view.viewport());editor.done=False
    rect=view.visualRect(index)
    rect.setTop(max(rect.top(),rect.bottom()-28))
    editor.setGeometry(rect.adjusted(2,0,-2,0));editor.setText(text);editor.setMaxLength(160)
    def finish():
        if editor.done:return
        editor.done=True
        name=editor.text().strip()
        if name and name!=text:commit(name)
        editor.close()
    editor.editingFinished.connect(finish)
    editor.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
    view._inline_name_editor=editor
    def released():
        if getattr(view,'_inline_name_editor',None) is editor:view._inline_name_editor=None
    editor.destroyed.connect(released)
    editor.show();editor.setFocus();editor.selectAll()
