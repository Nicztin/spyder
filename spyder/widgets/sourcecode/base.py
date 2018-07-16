# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""QPlainTextEdit base class"""

# pylint: disable=C0103
# pylint: disable=R0903
# pylint: disable=R0911
# pylint: disable=R0201

# Standard library imports
import os
import re
import sys

# Third party imports
from qtpy.compat import to_qvariant
from qtpy.QtCore import QEvent, QEventLoop, QPoint, Qt, Signal, Slot
from qtpy.QtGui import (QClipboard, QColor, QFont, QMouseEvent, QPalette,
                        QTextCharFormat, QTextFormat, QTextOption, QTextCursor)
from qtpy.QtWidgets import (QAbstractItemView, QApplication, QListWidget,
                            QListWidgetItem, QMainWindow, QPlainTextEdit,
                            QToolTip)

# Local imports
from spyder.config.gui import get_font
from spyder.config.main import CONF
from spyder.py3compat import PY3, str_lower, to_text_string
from spyder.utils import icon_manager as ima
from spyder.widgets.calltip import CallTipWidget
from spyder.widgets.mixins import BaseEditMixin
from spyder.widgets.sourcecode.terminal import ANSIEscapeCodeHandler
from spyder.widgets.sourcecode.api.decoration import (TextDecoration,
                                                      DRAW_ORDERS)
from spyder.widgets.sourcecode.utils.decoration import TextDecorationsManager


def insert_text_to(cursor, text, fmt):
    """Helper to print text, taking into account backspaces"""
    while True:
        index = text.find(chr(8))  # backspace
        if index == -1:
            break
        cursor.insertText(text[:index], fmt)
        if cursor.positionInBlock() > 0:
            cursor.deletePreviousChar()
        text = text[index+1:]
    cursor.insertText(text, fmt)


class CompletionWidget(QListWidget):
    """Completion list widget"""

    sig_show_completions = Signal(object)

    def __init__(self, parent, ancestor):
        QListWidget.__init__(self, ancestor)
        self.setWindowFlags(Qt.SubWindow | Qt.FramelessWindowHint)
        self.textedit = parent
        self.completion_list = None
        self.case_sensitive = False
        self.enter_select = None
        self.hide()
        self.itemActivated.connect(self.item_selected)
        
    def setup_appearance(self, size, font):
        self.resize(*size)
        self.setFont(font)
        
    def show_list(self, completion_list, automatic=True):
        types = [c[1] for c in completion_list]
        completion_list = [c[0] for c in completion_list]
        if len(completion_list) == 1 and not automatic:
            self.textedit.insert_completion(completion_list[0])
            return

        self.completion_list = completion_list
        self.clear()

        icons_map = {'instance': 'attribute',
                     'statement': 'attribute',
                     'method': 'method',
                     'function': 'function',
                     'class': 'class',
                     'module': 'module'}

        self.type_list = types
        if any(types):
            for (c, t) in zip(completion_list, types):
                icon = icons_map.get(t, 'no_match')
                self.addItem(QListWidgetItem(ima.icon(icon), c))
        else:
            self.addItems(completion_list)

        self.setCurrentRow(0)

        QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        self.show()
        self.setFocus()
        self.raise_()
        
        # Retrieving current screen height
        desktop = QApplication.desktop()
        srect = desktop.availableGeometry(desktop.screenNumber(self))
        screen_right = srect.right()
        screen_bottom = srect.bottom()
        
        point = self.textedit.cursorRect().bottomRight()
        point = self.textedit.calculate_real_position(point)
        point = self.textedit.mapToGlobal(point)

        # Computing completion widget and its parent right positions
        comp_right = point.x()+self.width()
        ancestor = self.parent()
        if ancestor is None:
            anc_right = screen_right
        else:
            anc_right = min([ancestor.x()+ancestor.width(), screen_right])
        
        # Moving completion widget to the left
        # if there is not enough space to the right
        if comp_right > anc_right:
            point.setX(point.x()-self.width())
        
        # Computing completion widget and its parent bottom positions
        comp_bottom = point.y()+self.height()
        ancestor = self.parent()
        if ancestor is None:
            anc_bottom = screen_bottom
        else:
            anc_bottom = min([ancestor.y()+ancestor.height(), screen_bottom])
        
        # Moving completion widget above if there is not enough space below
        x_position = point.x()
        if comp_bottom > anc_bottom:
            point = self.textedit.cursorRect().topRight()
            point = self.textedit.mapToGlobal(point)
            point.setX(x_position)
            point.setY(point.y()-self.height())
            
        if ancestor is not None:
            # Useful only if we set parent to 'ancestor' in __init__
            point = ancestor.mapFromGlobal(point)
        self.move(point)
        
        if to_text_string(self.textedit.completion_text):
            # When initialized, if completion text is not empty, we need 
            # to update the displayed list:
            self.update_current()
        
        # signal used for testing
        self.sig_show_completions.emit(completion_list)

    def hide(self):
        QListWidget.hide(self)
        self.textedit.setFocus()
        
    def keyPressEvent(self, event):
        text, key = event.text(), event.key()
        alt = event.modifiers() & Qt.AltModifier
        shift = event.modifiers() & Qt.ShiftModifier
        ctrl = event.modifiers() & Qt.ControlModifier
        modifier = shift or ctrl or alt
        if (key in (Qt.Key_Return, Qt.Key_Enter) and self.enter_select) \
           or key == Qt.Key_Tab:
            self.item_selected()
        elif key in (Qt.Key_Return, Qt.Key_Enter,
                     Qt.Key_Left, Qt.Key_Right) or text in ('.', ':'):
            self.hide()
            self.textedit.keyPressEvent(event)
        elif key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown,
                     Qt.Key_Home, Qt.Key_End,
                     Qt.Key_CapsLock) and not modifier:
            if key == Qt.Key_Up and self.currentRow() == 0:
                self.setCurrentRow(self.count() - 1)
            elif key == Qt.Key_Down and self.currentRow() == self.count()-1:
                self.setCurrentRow(0)
            else:
                QListWidget.keyPressEvent(self, event)
        elif len(text) or key == Qt.Key_Backspace:
            self.textedit.keyPressEvent(event)
            self.update_current()
        elif modifier:
            self.textedit.keyPressEvent(event)
        else:
            self.hide()
            QListWidget.keyPressEvent(self, event)

    def update_current(self):
        completion_text = to_text_string(self.textedit.completion_text)

        if completion_text:
            for row, completion in enumerate(self.completion_list):
                if not self.case_sensitive:
                    print(completion_text)  # spyder: test-skip
                    completion = completion.lower()
                    completion_text = completion_text.lower()
                if completion.startswith(completion_text):
                    self.setCurrentRow(row)
                    self.scrollTo(self.currentIndex(),
                                  QAbstractItemView.PositionAtTop)
                    break
            else:
                self.hide()
        else:
            self.hide()


    def focusOutEvent(self, event):
        event.ignore()
        # Don't hide it on Mac when main window loses focus because
        # keyboard input is lost
        # Fixes Issue 1318
        if sys.platform == "darwin":
            if event.reason() != Qt.ActiveWindowFocusReason:
                self.hide()
        else:
            self.hide()
        
    def item_selected(self, item=None):
        if item is None:
            item = self.currentItem()
        self.textedit.insert_completion( to_text_string(item.text()) )
        self.hide()


class TextEditBaseWidget(QPlainTextEdit, BaseEditMixin):
    """Text edit base widget"""
    BRACE_MATCHING_SCOPE = ('sof', 'eof')
    cell_separators = None
    focus_in = Signal()
    zoom_in = Signal()
    zoom_out = Signal()
    zoom_reset = Signal()
    focus_changed = Signal()
    sig_eol_chars_changed = Signal(str)
    
    def __init__(self, parent=None):
        QPlainTextEdit.__init__(self, parent)
        BaseEditMixin.__init__(self)
        self.setAttribute(Qt.WA_DeleteOnClose)
        
        self.extra_selections_dict = {}
        
        self.textChanged.connect(self.changed)
        self.cursorPositionChanged.connect(self.cursor_position_changed)
        
        self.indent_chars = " "*4
        self.tab_stop_width_spaces = 4
        
        # Code completion / calltips
        if parent is not None:
            mainwin = parent
            while not isinstance(mainwin, QMainWindow):
                mainwin = mainwin.parent()
                if mainwin is None:
                    break
            if mainwin is not None:
                parent = mainwin

        self.completion_widget = CompletionWidget(self, parent)
        self.codecompletion_auto = False
        self.codecompletion_case = True
        self.codecompletion_enter = False
        self.completion_text = ""
        self.setup_completion()

        self.calltip_widget = CallTipWidget(self, hide_timer_on=False)
        self.calltips = True
        self.calltip_position = None

        self.has_cell_separators = False
        self.highlight_current_cell_enabled = False
        
        # The color values may be overridden by the syntax highlighter
        # Highlight current line color
        self.currentline_color = QColor(Qt.red).lighter(190)
        self.currentcell_color = QColor(Qt.red).lighter(194)

        # Brace matching
        self.bracepos = None
        self.matched_p_color = QColor(Qt.green)
        self.unmatched_p_color = QColor(Qt.red)

        self.last_cursor_cell = None

        self.decorations = TextDecorationsManager(self)

    def setup_completion(self):
        size = CONF.get('main', 'completion/size')
        font = get_font()
        self.completion_widget.setup_appearance(size, font)

    def set_indent_chars(self, indent_chars):
        self.indent_chars = indent_chars

    def set_tab_stop_width_spaces(self, tab_stop_width_spaces):
        self.tab_stop_width_spaces = tab_stop_width_spaces
        self.update_tab_stop_width_spaces()

    def update_tab_stop_width_spaces(self):
        self.setTabStopWidth(self.fontMetrics().width(' ')
                             * self.tab_stop_width_spaces)

    def set_palette(self, background, foreground):
        """
        Set text editor palette colors:
        background color and caret (text cursor) color
        """
        palette = QPalette()
        palette.setColor(QPalette.Base, background)
        palette.setColor(QPalette.Text, foreground)
        self.setPalette(palette)

        # Set the right background color when changing color schemes
        # or creating new Editor windows. This seems to be a Qt bug.
        # Fixes Issue 2028
        if sys.platform == 'darwin':
            if self.objectName():
                style = "QPlainTextEdit#%s {background: %s; color: %s;}" % \
                        (self.objectName(), background.name(), foreground.name())
                self.setStyleSheet(style)


    #------Extra selections
    def get_extra_selections(self, key):
        """Return editor extra selections.

        Args:
            key (str) name of the extra selections group

        Returns:
            list of sourcecode.api.TextDecoration.
        """
        return self.extra_selections_dict.get(key, [])

    def set_extra_selections(self, key, extra_selections):
        """Set extra selections for a key.

        Also assign draw orders to leave current_cell and current_line
        in the backgrund (and avoid them to cover other decorations)

        NOTE: This will remove previous decorations added to  the same key.

        Args:
            key (str) name of the extra selections group.
            extra_selections (list of sourcecode.api.TextDecoration).
        """
        # use draw orders to highlight current_cell and current_line first
        draw_order = DRAW_ORDERS.get(key)
        if draw_order is None:
            draw_order = DRAW_ORDERS.get('on_top')

        for selection in extra_selections:
            selection.draw_order = draw_order

        self.clear_extra_selections(key)
        self.extra_selections_dict[key] = extra_selections

    def update_extra_selections(self):
        """Add extra selections to DecorationsManager.

        TODO: This method could be remove it and decorations could be
        added/removed in set_extra_selections/clear_extra_selections.
        """
        extra_selections = []

        for key, extra in list(self.extra_selections_dict.items()):
            extra_selections.extend(extra)
        self.decorations.add(extra_selections)

    def clear_extra_selections(self, key):
        """Remove decorations added through set_extra_selections.

        Args:
            key (str) name of the extra selections group.
        """
        for decoration in self.extra_selections_dict.get(key, []):
            self.decorations.remove(decoration)
        self.extra_selections_dict[key] = []

    def changed(self):
        """Emit changed signal"""
        self.modificationChanged.emit(self.document().isModified())


    #------Highlight current line
    def highlight_current_line(self):
        """Highlight current line"""
        selection = TextDecoration(self.textCursor())
        selection.format.setProperty(QTextFormat.FullWidthSelection,
                                     to_qvariant(True))
        selection.format.setBackground(self.currentline_color)
        selection.cursor.clearSelection()
        self.set_extra_selections('current_line', [selection])
        self.update_extra_selections()

    def unhighlight_current_line(self):
        """Unhighlight current line"""
        self.clear_extra_selections('current_line')

    #------Highlight current cell
    def highlight_current_cell(self):
        """Highlight current cell"""
        if self.cell_separators is None or \
          not self.highlight_current_cell_enabled:
            return
        cursor, whole_file_selected, whole_screen_selected =\
            self.select_current_cell_in_visible_portion()
        selection = TextDecoration(cursor)
        selection.format.setProperty(QTextFormat.FullWidthSelection,
                                     to_qvariant(True))
        selection.format.setBackground(self.currentcell_color)

        if whole_file_selected: 
            self.clear_extra_selections('current_cell')
        elif whole_screen_selected:
            if self.has_cell_separators:
                self.set_extra_selections('current_cell', [selection])
                self.update_extra_selections()
            else:
                self.clear_extra_selections('current_cell')
        else:
            self.set_extra_selections('current_cell', [selection])
            self.update_extra_selections()

    def unhighlight_current_cell(self):
        """Unhighlight current cell"""
        self.clear_extra_selections('current_cell')

    #------Brace matching
    def find_brace_match(self, position, brace, forward):
        start_pos, end_pos = self.BRACE_MATCHING_SCOPE
        if forward:
            bracemap = {'(': ')', '[': ']', '{': '}'}
            text = self.get_text(position, end_pos)
            i_start_open = 1
            i_start_close = 1
        else:
            bracemap = {')': '(', ']': '[', '}': '{'}
            text = self.get_text(start_pos, position)
            i_start_open = len(text)-1
            i_start_close = len(text)-1

        while True:
            if forward:
                i_close = text.find(bracemap[brace], i_start_close)
            else:
                i_close = text.rfind(bracemap[brace], 0, i_start_close+1)
            if i_close > -1:
                if forward:
                    i_start_close = i_close+1
                    i_open = text.find(brace, i_start_open, i_close)
                else:
                    i_start_close = i_close-1
                    i_open = text.rfind(brace, i_close, i_start_open+1)
                if i_open > -1:
                    if forward:
                        i_start_open = i_open+1
                    else:
                        i_start_open = i_open-1
                else:
                    # found matching brace
                    if forward:
                        return position+i_close
                    else:
                        return position-(len(text)-i_close)
            else:
                # no matching brace
                return
    
    def __highlight(self, positions, color=None, cancel=False):
        if cancel:
            self.clear_extra_selections('brace_matching')
            return
        extra_selections = []
        for position in positions:
            if position > self.get_position('eof'):
                return
            selection = TextDecoration(self.textCursor())
            selection.format.setBackground(color)
            selection.cursor.clearSelection()
            selection.cursor.setPosition(position)
            selection.cursor.movePosition(QTextCursor.NextCharacter,
                                          QTextCursor.KeepAnchor)
            extra_selections.append(selection)
        self.set_extra_selections('brace_matching', extra_selections)
        self.update_extra_selections()

    def cursor_position_changed(self):
        """Brace matching"""
        if self.bracepos is not None:
            self.__highlight(self.bracepos, cancel=True)
            self.bracepos = None
        cursor = self.textCursor()
        if cursor.position() == 0:
            return
        cursor.movePosition(QTextCursor.PreviousCharacter,
                            QTextCursor.KeepAnchor)
        text = to_text_string(cursor.selectedText())
        pos1 = cursor.position()
        if text in (')', ']', '}'):
            pos2 = self.find_brace_match(pos1, text, forward=False)
        elif text in ('(', '[', '{'):
            pos2 = self.find_brace_match(pos1, text, forward=True)
        else:
            return
        if pos2 is not None:
            self.bracepos = (pos1, pos2)
            self.__highlight(self.bracepos, color=self.matched_p_color)
        else:
            self.bracepos = (pos1,)
            self.__highlight(self.bracepos, color=self.unmatched_p_color)


    #-----Widget setup and options
    def set_codecompletion_auto(self, state):
        """Set code completion state"""
        self.codecompletion_auto = state
        
    def set_codecompletion_case(self, state):
        """Case sensitive completion"""
        self.codecompletion_case = state
        self.completion_widget.case_sensitive = state
        
    def set_codecompletion_enter(self, state):
        """Enable Enter key to select completion"""
        self.codecompletion_enter = state
        self.completion_widget.enter_select = state
        
    def set_calltips(self, state):
        """Set calltips state"""
        self.calltips = state

    def set_wrap_mode(self, mode=None):
        """
        Set wrap mode
        Valid *mode* values: None, 'word', 'character'
        """
        if mode == 'word':
            wrap_mode = QTextOption.WrapAtWordBoundaryOrAnywhere
        elif mode == 'character':
            wrap_mode = QTextOption.WrapAnywhere
        else:
            wrap_mode = QTextOption.NoWrap
        self.setWordWrapMode(wrap_mode)
        
        
    #------Reimplementing Qt methods
    @Slot()
    def copy(self):
        """
        Reimplement Qt method
        Copy text to clipboard with correct EOL chars
        """
        if self.get_selected_text():
            QApplication.clipboard().setText(self.get_selected_text())
    
    def toPlainText(self):
        """
        Reimplement Qt method
        Fix PyQt4 bug on Windows and Python 3
        """
        # Fix what appears to be a PyQt4 bug when getting file
        # contents under Windows and PY3. This bug leads to
        # corruptions when saving files with certain combinations
        # of unicode chars on them (like the one attached on
        # Issue 1546)
        if os.name == 'nt' and PY3:
            text = self.get_text('sof', 'eof')
            return text.replace('\u2028', '\n').replace('\u2029', '\n')\
                       .replace('\u0085', '\n')
        else:
            return super(TextEditBaseWidget, self).toPlainText()

    def keyPressEvent(self, event):
        text, key = event.text(), event.key()
        ctrl = event.modifiers() & Qt.ControlModifier
        meta = event.modifiers() & Qt.MetaModifier
        # Use our own copy method for {Ctrl,Cmd}+C to avoid Qt
        # copying text in HTML (See Issue 2285)
        if (ctrl or meta) and key == Qt.Key_C:
            self.copy()
        else:
            super(TextEditBaseWidget, self).keyPressEvent(event)

    #------Text: get, set, ...
    def get_selection_as_executable_code(self):
        """Return selected text as a processed text,
        to be executable in a Python/IPython interpreter"""
        ls = self.get_line_separator()
        
        _indent = lambda line: len(line)-len(line.lstrip())
        
        line_from, line_to = self.get_selection_bounds()
        text = self.get_selected_text()
        if not text:
            return

        lines = text.split(ls)
        if len(lines) > 1:
            # Multiline selection -> eventually fixing indentation
            original_indent = _indent(self.get_text_line(line_from))
            text = (" "*(original_indent-_indent(lines[0])))+text
            
        # If there is a common indent to all lines, find it.
        # Moving from bottom line to top line ensures that blank
        # lines inherit the indent of the line *below* it,
        # which is the desired behavior.
        min_indent = 999
        current_indent = 0
        lines = text.split(ls)
        for i in range(len(lines)-1, -1, -1):
            line = lines[i]
            if line.strip():
                current_indent = _indent(line)
                min_indent = min(current_indent, min_indent)
            else:
                lines[i] = ' ' * current_indent
        if min_indent:
            lines = [line[min_indent:] for line in lines]

        # Remove any leading whitespace or comment lines
        # since they confuse the reserved word detector that follows below
        while lines:
            first_line = lines[0].lstrip()
            if first_line == '' or first_line[0] == '#':
                lines.pop(0)
            else:
                break

        # Add an EOL character after indentation blocks that start with some
        # Python reserved words, so that it gets evaluated automatically
        # by the console
        varname = re.compile(r'[a-zA-Z0-9_]*')  # Matches valid variable names.
        maybe = False
        nextexcept = ()
        for n, line in enumerate(lines):
            if not _indent(line):
                word = varname.match(line).group()
                if maybe and word not in nextexcept:
                    lines[n-1] += ls
                    maybe = False
                if word:
                    if word in ('def', 'for', 'while', 'with', 'class'):
                        maybe = True
                        nextexcept = ()
                    elif word == 'if':
                        maybe = True
                        nextexcept = ('elif', 'else')
                    elif word == 'try':
                        maybe = True
                        nextexcept = ('except', 'finally')
        if maybe:
            if lines[-1].strip() == '':
                lines[-1] += ls
            else:
                lines.append(ls)
        
        return ls.join(lines)

    def __exec_cell(self):
        init_cursor = QTextCursor(self.textCursor())
        start_pos, end_pos = self.__save_selection()
        cursor, whole_file_selected = self.select_current_cell()
        if not whole_file_selected:
            self.setTextCursor(cursor)
        text = self.get_selection_as_executable_code()
        self.last_cursor_cell = init_cursor
        self.__restore_selection(start_pos, end_pos)
        if text is not None:
            text = text.rstrip()
        return text

    def get_cell_as_executable_code(self):
        """Return cell contents as executable code"""
        return self.__exec_cell()

    def get_last_cell_as_executable_code(self):
        text = None
        if self.last_cursor_cell:
            self.setTextCursor(self.last_cursor_cell)
            self.highlight_current_cell()
            text = self.__exec_cell()
        return text

    def is_cell_separator(self, cursor=None, block=None):
        """Return True if cursor (or text block) is on a block separator"""
        assert cursor is not None or block is not None
        if cursor is not None:
            cursor0 = QTextCursor(cursor)
            cursor0.select(QTextCursor.BlockUnderCursor)
            text = to_text_string(cursor0.selectedText())
        else:
            text = to_text_string(block.text())
        if self.cell_separators is None:
            return False
        else:
            return text.lstrip().startswith(self.cell_separators)

    def select_current_cell(self):
        """Select cell under cursor
        cell = group of lines separated by CELL_SEPARATORS
        returns the textCursor and a boolean indicating if the
        entire file is selected"""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        cur_pos = prev_pos = cursor.position()

        # Moving to the next line that is not a separator, if we are
        # exactly at one of them
        while self.is_cell_separator(cursor):
            cursor.movePosition(QTextCursor.NextBlock)
            prev_pos = cur_pos
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                return cursor, False
        prev_pos = cur_pos
        # If not, move backwards to find the previous separator
        while not self.is_cell_separator(cursor):
            cursor.movePosition(QTextCursor.PreviousBlock)
            prev_pos = cur_pos
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                if self.is_cell_separator(cursor):
                    return cursor, False
                else:
                    break
        cursor.setPosition(prev_pos)
        cell_at_file_start = cursor.atStart()
        # Once we find it (or reach the beginning of the file)
        # move to the next separator (or the end of the file)
        # so we can grab the cell contents
        while not self.is_cell_separator(cursor):
            cursor.movePosition(QTextCursor.NextBlock,
                                QTextCursor.KeepAnchor)
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                cursor.movePosition(QTextCursor.EndOfBlock,
                                    QTextCursor.KeepAnchor)
                break
            prev_pos = cur_pos
        cell_at_file_end = cursor.atEnd()
        return cursor, cell_at_file_start and cell_at_file_end

    def select_current_cell_in_visible_portion(self):
        """Select cell under cursor in the visible portion of the file
        cell = group of lines separated by CELL_SEPARATORS
        returns 
         -the textCursor
         -a boolean indicating if the entire file is selected
         -a boolean indicating if the entire visible portion of the file is selected"""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        cur_pos = prev_pos = cursor.position()

        beg_pos = self.cursorForPosition(QPoint(0, 0)).position()
        bottom_right = QPoint(self.viewport().width() - 1, 
                              self.viewport().height() - 1)
        end_pos = self.cursorForPosition(bottom_right).position()

        # Moving to the next line that is not a separator, if we are
        # exactly at one of them
        while self.is_cell_separator(cursor):
            cursor.movePosition(QTextCursor.NextBlock)
            prev_pos = cur_pos
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                return cursor, False, False
        prev_pos = cur_pos
        # If not, move backwards to find the previous separator
        while not self.is_cell_separator(cursor)\
          and cursor.position() >= beg_pos:
            cursor.movePosition(QTextCursor.PreviousBlock)
            prev_pos = cur_pos
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                if self.is_cell_separator(cursor):
                    return cursor, False, False
                else:
                    break
        cell_at_screen_start = cursor.position() <= beg_pos
        cursor.setPosition(prev_pos)
        cell_at_file_start = cursor.atStart()
        # Selecting cell header
        if not cell_at_file_start:
            cursor.movePosition(QTextCursor.PreviousBlock)
            cursor.movePosition(QTextCursor.NextBlock,
                                QTextCursor.KeepAnchor)
        # Once we find it (or reach the beginning of the file)
        # move to the next separator (or the end of the file)
        # so we can grab the cell contents
        while not self.is_cell_separator(cursor)\
          and cursor.position() <= end_pos:
            cursor.movePosition(QTextCursor.NextBlock,
                                QTextCursor.KeepAnchor)
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                cursor.movePosition(QTextCursor.EndOfBlock,
                                    QTextCursor.KeepAnchor)
                break
            prev_pos = cur_pos
        cell_at_file_end = cursor.atEnd()
        cell_at_screen_end = cursor.position() >= end_pos
        return cursor,\
               cell_at_file_start and cell_at_file_end,\
               cell_at_screen_start and cell_at_screen_end

    def go_to_next_cell(self):
        """Go to the next cell of lines"""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.NextBlock)
        cur_pos = prev_pos = cursor.position()
        while not self.is_cell_separator(cursor):
            # Moving to the next code cell
            cursor.movePosition(QTextCursor.NextBlock)
            prev_pos = cur_pos
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                return
        self.setTextCursor(cursor)

    def go_to_previous_cell(self):
        """Go to the previous cell of lines"""
        cursor = self.textCursor()
        cur_pos = prev_pos = cursor.position()

        if self.is_cell_separator(cursor):
            # Move to the previous cell
            cursor.movePosition(QTextCursor.PreviousBlock)
            cur_pos = prev_pos = cursor.position()

        while not self.is_cell_separator(cursor):
            # Move to the previous cell or the beginning of the current cell
            cursor.movePosition(QTextCursor.PreviousBlock)
            prev_pos = cur_pos
            cur_pos = cursor.position()
            if cur_pos == prev_pos:
                return

        self.setTextCursor(cursor)

    def get_line_count(self):
        """Return document total line number"""
        return self.blockCount()

    def __save_selection(self):
        """Save current cursor selection and return position bounds"""
        cursor = self.textCursor()
        return cursor.selectionStart(), cursor.selectionEnd()

    def __restore_selection(self, start_pos, end_pos):
        """Restore cursor selection from position bounds"""
        cursor = self.textCursor()
        cursor.setPosition(start_pos)
        cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def __duplicate_line_or_selection(self, after_current_line=True):
        """Duplicate current line or selected text"""
        cursor = self.textCursor()
        cursor.beginEditBlock()
        start_pos, end_pos = self.__save_selection()
        if to_text_string(cursor.selectedText()):
            cursor.setPosition(end_pos)
            # Check if end_pos is at the start of a block: if so, starting
            # changes from the previous block
            cursor.movePosition(QTextCursor.StartOfBlock,
                                QTextCursor.KeepAnchor)
            if not to_text_string(cursor.selectedText()):
                cursor.movePosition(QTextCursor.PreviousBlock)
                end_pos = cursor.position()
            
        cursor.setPosition(start_pos)
        cursor.movePosition(QTextCursor.StartOfBlock)
        while cursor.position() <= end_pos:
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            if cursor.atEnd():
                cursor_temp = QTextCursor(cursor)
                cursor_temp.clearSelection()
                cursor_temp.insertText(self.get_line_separator())
                break
            cursor.movePosition(QTextCursor.NextBlock, QTextCursor.KeepAnchor)            
        text = cursor.selectedText()
        cursor.clearSelection()
        
        if not after_current_line:
            # Moving cursor before current line/selected text
            cursor.setPosition(start_pos)
            cursor.movePosition(QTextCursor.StartOfBlock)
            start_pos += len(text)
            end_pos += len(text)
        
        cursor.insertText(text)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.__restore_selection(start_pos, end_pos)
    
    def duplicate_line(self):
        """
        Duplicate current line or selected text
        Paste the duplicated text *after* the current line/selected text
        """
        self.__duplicate_line_or_selection(after_current_line=True)
    
    def copy_line(self):
        """
        Copy current line or selected text
        Paste the duplicated text *before* the current line/selected text
        """
        self.__duplicate_line_or_selection(after_current_line=False)
        
    def __move_line_or_selection(self, after_current_line=True):
        """Move current line or selected text"""
        cursor = self.textCursor()
        cursor.beginEditBlock()
        start_pos, end_pos = self.__save_selection()
        last_line = False
        
        # ------ Select text
        
        # Get selection start location
        cursor.setPosition(start_pos)
        cursor.movePosition(QTextCursor.StartOfBlock)
        start_pos = cursor.position()
        
        # Get selection end location
        cursor.setPosition(end_pos)
        if not cursor.atBlockStart() or end_pos == start_pos:
            cursor.movePosition(QTextCursor.EndOfBlock)
            cursor.movePosition(QTextCursor.NextBlock)
        end_pos = cursor.position()
        
        # Check if selection ends on the last line of the document
        if cursor.atEnd():
            if not cursor.atBlockStart() or end_pos == start_pos:
                last_line = True
                
        # ------ Stop if at document boundary
        
        cursor.setPosition(start_pos)
        if cursor.atStart() and not after_current_line:
            # Stop if selection is already at top of the file while moving up
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            self.__restore_selection(start_pos, end_pos)
            return
                
        cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
        if last_line and after_current_line:
            # Stop if selection is already at end of the file while moving down
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            self.__restore_selection(start_pos, end_pos)
            return
        
        # ------ Move text
        
        sel_text = to_text_string(cursor.selectedText())
        cursor.removeSelectedText()
        
        
        if after_current_line:
            # Shift selection down
            text = to_text_string(cursor.block().text())  
            sel_text = os.linesep + sel_text[0:-1]  # Move linesep at the start
            cursor.movePosition(QTextCursor.EndOfBlock)
            start_pos += len(text)+1
            end_pos += len(text)
            if not cursor.atEnd():
                end_pos += 1        
        else:
            # Shift selection up
            if last_line:
                # Remove the last linesep and add it to the selected text
                cursor.deletePreviousChar()
                sel_text = sel_text + os.linesep
                cursor.movePosition(QTextCursor.StartOfBlock)
                end_pos += 1
            else:
                cursor.movePosition(QTextCursor.PreviousBlock)
            text = to_text_string(cursor.block().text())
            start_pos -= len(text)+1
            end_pos -= len(text)+1

        cursor.insertText(sel_text)

        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.__restore_selection(start_pos, end_pos)
    
    def move_line_up(self):
        """Move up current line or selected text"""
        self.__move_line_or_selection(after_current_line=False)
        
    def move_line_down(self):
        """Move down current line or selected text"""
        self.__move_line_or_selection(after_current_line=True)
        
    def go_to_new_line(self):
        """Go to the end of the current line and create a new line"""
        self.stdkey_end(False, False)
        self.insert_text(self.get_line_separator())
        
    def extend_selection_to_complete_lines(self):
        """Extend current selection to complete lines"""
        cursor = self.textCursor()
        start_pos, end_pos = cursor.selectionStart(), cursor.selectionEnd()
        cursor.setPosition(start_pos)
        cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
        if cursor.atBlockStart():
            cursor.movePosition(QTextCursor.PreviousBlock,
                                QTextCursor.KeepAnchor)
            cursor.movePosition(QTextCursor.EndOfBlock,
                                QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        
    def delete_line(self):
        """Delete current line"""
        cursor = self.textCursor()
        if self.has_selected_text():
            self.extend_selection_to_complete_lines()
            start_pos, end_pos = cursor.selectionStart(), cursor.selectionEnd()
            cursor.setPosition(start_pos)
        else:
            start_pos = end_pos = cursor.position()
        cursor.beginEditBlock()
        cursor.setPosition(start_pos)
        cursor.movePosition(QTextCursor.StartOfBlock)
        while cursor.position() <= end_pos:
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            if cursor.atEnd():
                break
            cursor.movePosition(QTextCursor.NextBlock, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        cursor.endEditBlock()
        self.ensureCursorVisible()

    def set_selection(self, start, end):
        cursor = self.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def truncate_selection(self, position_from):
        """Unselect read-only parts in shell, like prompt"""
        position_from = self.get_position(position_from)
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        if start < end:
            start = max([position_from, start])
        else:
            end = max([position_from, end])
        self.set_selection(start, end)

    def restrict_cursor_position(self, position_from, position_to):
        """In shell, avoid editing text except between prompt and EOF"""
        position_from = self.get_position(position_from)
        position_to = self.get_position(position_to)
        cursor = self.textCursor()
        cursor_position = cursor.position()
        if cursor_position < position_from or cursor_position > position_to:
            self.set_cursor_position(position_to)

    #------Code completion / Calltips
    def hide_tooltip_if_necessary(self, key):
        """Hide calltip when necessary"""
        try:
            calltip_char = self.get_character(self.calltip_position)
            before = self.is_cursor_before(self.calltip_position,
                                           char_offset=1)
            other = key in (Qt.Key_ParenRight, Qt.Key_Period, Qt.Key_Tab)
            if calltip_char not in ('?', '(') or before or other:
                QToolTip.hideText()
        except (IndexError, TypeError):
            QToolTip.hideText()

    def show_completion_widget(self, textlist, automatic=True):
        """Show completion widget"""
        self.completion_widget.show_list(textlist, automatic=automatic)
        
    def hide_completion_widget(self):
        """Hide completion widget"""
        self.completion_widget.hide()

    def show_completion_list(self, completions, completion_text="",
                             automatic=True):
        """Display the possible completions"""
        if not completions:
            return
        if not isinstance(completions[0], tuple):
            completions = [(c, '') for c in completions]
        if len(completions) == 1 and completions[0][0] == completion_text:
            return
        self.completion_text = completion_text
        # Sorting completion list (entries starting with underscore are
        # put at the end of the list):
        underscore = set([(comp, t) for (comp, t) in completions
                          if comp.startswith('_')])

        completions = sorted(set(completions) - underscore,
                             key=lambda x: str_lower(x[0]))
        completions += sorted(underscore, key=lambda x: str_lower(x[0]))
        self.show_completion_widget(completions, automatic=automatic)
        
    def select_completion_list(self):
        """Completion list is active, Enter was just pressed"""
        self.completion_widget.item_selected()
        
    def insert_completion(self, text):
        if text:
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.PreviousCharacter,
                                QTextCursor.KeepAnchor,
                                len(self.completion_text))
            cursor.removeSelectedText()
            self.insert_text(text)

    def is_completion_widget_visible(self):
        """Return True is completion list widget is visible"""
        return self.completion_widget.isVisible()
    
        
    #------Standard keys
    def stdkey_clear(self):
        if not self.has_selected_text():
            self.moveCursor(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
        self.remove_selected_text()
    
    def stdkey_backspace(self):
        if not self.has_selected_text():
            self.moveCursor(QTextCursor.PreviousCharacter,
                            QTextCursor.KeepAnchor)
        self.remove_selected_text()

    def __get_move_mode(self, shift):
        return QTextCursor.KeepAnchor if shift else QTextCursor.MoveAnchor

    def stdkey_up(self, shift):
        self.moveCursor(QTextCursor.Up, self.__get_move_mode(shift))

    def stdkey_down(self, shift):
        self.moveCursor(QTextCursor.Down, self.__get_move_mode(shift))

    def stdkey_tab(self):
        self.insert_text(self.indent_chars)

    def stdkey_home(self, shift, ctrl, prompt_pos=None):
        """Smart HOME feature: cursor is first moved at 
        indentation position, then at the start of the line"""
        move_mode = self.__get_move_mode(shift)
        if ctrl:
            self.moveCursor(QTextCursor.Start, move_mode)
        else:
            cursor = self.textCursor()
            if prompt_pos is None:
                start_position = self.get_position('sol')
            else:
                start_position = self.get_position(prompt_pos)
            text = self.get_text(start_position, 'eol')
            indent_pos = start_position+len(text)-len(text.lstrip())
            if cursor.position() != indent_pos:
                cursor.setPosition(indent_pos, move_mode)
            else:
                cursor.setPosition(start_position, move_mode)
            self.setTextCursor(cursor)

    def stdkey_end(self, shift, ctrl):
        move_mode = self.__get_move_mode(shift)
        if ctrl:
            self.moveCursor(QTextCursor.End, move_mode)
        else:
            self.moveCursor(QTextCursor.EndOfBlock, move_mode)

    def stdkey_pageup(self):
        pass

    def stdkey_pagedown(self):
        pass

    def stdkey_escape(self):
        pass

                
    #----Qt Events
    def mousePressEvent(self, event):
        """Reimplement Qt method"""
        if sys.platform.startswith('linux') and event.button() == Qt.MidButton:
            self.calltip_widget.hide()
            self.setFocus()
            event = QMouseEvent(QEvent.MouseButtonPress, event.pos(),
                                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
            QPlainTextEdit.mousePressEvent(self, event)
            QPlainTextEdit.mouseReleaseEvent(self, event)
            # Send selection text to clipboard to be able to use
            # the paste method and avoid the strange Issue 1445
            # NOTE: This issue seems a focusing problem but it
            # seems really hard to track
            mode_clip = QClipboard.Clipboard
            mode_sel = QClipboard.Selection
            text_clip = QApplication.clipboard().text(mode=mode_clip)
            text_sel = QApplication.clipboard().text(mode=mode_sel)
            QApplication.clipboard().setText(text_sel, mode=mode_clip)
            self.paste()
            QApplication.clipboard().setText(text_clip, mode=mode_clip)
        else:
            self.calltip_widget.hide()
            QPlainTextEdit.mousePressEvent(self, event)

    def focusInEvent(self, event):
        """Reimplemented to handle focus"""
        self.focus_changed.emit()
        self.focus_in.emit()
        self.highlight_current_cell()
        QPlainTextEdit.focusInEvent(self, event)
        
    def focusOutEvent(self, event):
        """Reimplemented to handle focus"""
        self.focus_changed.emit()
        QPlainTextEdit.focusOutEvent(self, event)
    
    def wheelEvent(self, event):
        """Reimplemented to emit zoom in/out signals when Ctrl is pressed"""
        # This feature is disabled on MacOS, see Issue 1510
        if sys.platform != 'darwin':
            if event.modifiers() & Qt.ControlModifier:
                if hasattr(event, 'angleDelta'):
                    if event.angleDelta().y() < 0:
                        self.zoom_out.emit()
                    elif event.angleDelta().y() > 0:
                        self.zoom_in.emit()
                elif hasattr(event, 'delta'):
                    if event.delta() < 0:
                        self.zoom_out.emit()
                    elif event.delta() > 0:
                        self.zoom_in.emit()
                return
        QPlainTextEdit.wheelEvent(self, event)
        self.highlight_current_cell()


class QtANSIEscapeCodeHandler(ANSIEscapeCodeHandler):
    def __init__(self):
        ANSIEscapeCodeHandler.__init__(self)
        self.base_format = None
        self.current_format = None
        
    def set_light_background(self, state):
        if state:
            self.default_foreground_color = 30
            self.default_background_color = 47
        else:
            self.default_foreground_color = 37
            self.default_background_color = 40
        
    def set_base_format(self, base_format):
        self.base_format = base_format
        
    def get_format(self):
        return self.current_format
        
    def set_style(self):
        """
        Set font style with the following attributes:
        'foreground_color', 'background_color', 'italic',
        'bold' and 'underline'
        """
        if self.current_format is None:
            assert self.base_format is not None
            self.current_format = QTextCharFormat(self.base_format)
        # Foreground color
        if self.foreground_color is None:
            qcolor = self.base_format.foreground()
        else:
            cstr = self.ANSI_COLORS[self.foreground_color-30][self.intensity]
            qcolor = QColor(cstr)
        self.current_format.setForeground(qcolor)        
        # Background color
        if self.background_color is None:
            qcolor = self.base_format.background()
        else:
            cstr = self.ANSI_COLORS[self.background_color-40][self.intensity]
            qcolor = QColor(cstr)
        self.current_format.setBackground(qcolor)
        
        font = self.current_format.font()
        # Italic
        if self.italic is None:
            italic = self.base_format.fontItalic()
        else:
            italic = self.italic
        font.setItalic(italic)
        # Bold
        if self.bold is None:
            bold = self.base_format.font().bold()
        else:
            bold = self.bold
        font.setBold(bold)
        # Underline
        if self.underline is None:
            underline = self.base_format.font().underline()
        else:
            underline = self.underline
        font.setUnderline(underline)
        self.current_format.setFont(font)


def inverse_color(color):
    color.setHsv(color.hue(), color.saturation(), 255-color.value())


class ConsoleFontStyle(object):
    def __init__(self, foregroundcolor, backgroundcolor, 
                 bold, italic, underline):
        self.foregroundcolor = foregroundcolor
        self.backgroundcolor = backgroundcolor
        self.bold = bold
        self.italic = italic
        self.underline = underline
        self.format = None
        
    def apply_style(self, font, light_background, is_default):
        self.format = QTextCharFormat()
        self.format.setFont(font)
        foreground = QColor(self.foregroundcolor)
        if not light_background and is_default:
            inverse_color(foreground)
        self.format.setForeground(foreground)
        background = QColor(self.backgroundcolor)
        if not light_background:
            inverse_color(background)
        self.format.setBackground(background)
        font = self.format.font()
        font.setBold(self.bold)
        font.setItalic(self.italic)
        font.setUnderline(self.underline)
        self.format.setFont(font)


class ConsoleBaseWidget(TextEditBaseWidget):
    """Console base widget"""
    BRACE_MATCHING_SCOPE = ('sol', 'eol')
    COLOR_PATTERN = re.compile(r'\x01?\x1b\[(.*?)m\x02?')
    exception_occurred = Signal(str, bool)
    userListActivated = Signal(int, str)
    completion_widget_activated = Signal(str)
    
    def __init__(self, parent=None):
        TextEditBaseWidget.__init__(self, parent)
        
        self.light_background = True

        self.setMaximumBlockCount(300)

        # ANSI escape code handler
        self.ansi_handler = QtANSIEscapeCodeHandler()
                
        # Disable undo/redo (nonsense for a console widget...):
        self.setUndoRedoEnabled(False)
        
        self.userListActivated.connect(lambda user_id, text:
                                   self.completion_widget_activated.emit(text))
        
        self.default_style = ConsoleFontStyle(
                            foregroundcolor=0x000000, backgroundcolor=0xFFFFFF,
                            bold=False, italic=False, underline=False)
        self.error_style  = ConsoleFontStyle(
                            foregroundcolor=0xFF0000, backgroundcolor=0xFFFFFF,
                            bold=False, italic=False, underline=False)
        self.traceback_link_style  = ConsoleFontStyle(
                            foregroundcolor=0x0000FF, backgroundcolor=0xFFFFFF,
                            bold=True, italic=False, underline=True)
        self.prompt_style  = ConsoleFontStyle(
                            foregroundcolor=0x00AA00, backgroundcolor=0xFFFFFF,
                            bold=True, italic=False, underline=False)
        self.font_styles = (self.default_style, self.error_style,
                            self.traceback_link_style, self.prompt_style)
        self.set_pythonshell_font()
        self.setMouseTracking(True)
        
    def set_light_background(self, state):
        self.light_background = state
        if state:
            self.set_palette(background=QColor(Qt.white),
                             foreground=QColor(Qt.darkGray))
        else:
            self.set_palette(background=QColor(Qt.black),
                             foreground=QColor(Qt.lightGray))
        self.ansi_handler.set_light_background(state)
        self.set_pythonshell_font()

    #------Python shell
    def insert_text(self, text):
        """Reimplement TextEditBaseWidget method"""
        # Eventually this maybe should wrap to insert_text_to if
        # backspace-handling is required
        self.textCursor().insertText(text, self.default_style.format)
        
    def paste(self):
        """Reimplement Qt method"""
        if self.has_selected_text():
            self.remove_selected_text()
        self.insert_text(QApplication.clipboard().text())
        
    def append_text_to_shell(self, text, error, prompt):
        """
        Append text to Python shell
        In a way, this method overrides the method 'insert_text' when text is 
        inserted at the end of the text widget for a Python shell
        
        Handles error messages and show blue underlined links
        Handles ANSI color sequences
        Handles ANSI FF sequence
        """
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        if '\r' in text:    # replace \r\n with \n
            text = text.replace('\r\n', '\n')
            text = text.replace('\r', '\n')
        while True:
            index = text.find(chr(12))
            if index == -1:
                break
            text = text[index+1:]
            self.clear()
        if error:
            is_traceback = False
            for text in text.splitlines(True):
                if text.startswith('  File') \
                and not text.startswith('  File "<'):
                    is_traceback = True
                    # Show error links in blue underlined text
                    cursor.insertText('  ', self.default_style.format)
                    cursor.insertText(text[2:],
                                      self.traceback_link_style.format)
                else:
                    # Show error/warning messages in red
                    cursor.insertText(text, self.error_style.format)
            self.exception_occurred.emit(text, is_traceback)
        elif prompt:
            # Show prompt in green
            insert_text_to(cursor, text, self.prompt_style.format)
        else:
            # Show other outputs in black
            last_end = 0
            for match in self.COLOR_PATTERN.finditer(text):
                insert_text_to(cursor, text[last_end:match.start()],
                               self.default_style.format)
                last_end = match.end()
                try:
                    for code in [int(_c) for _c in match.group(1).split(';')]:
                        self.ansi_handler.set_code(code)
                except ValueError:
                    pass
                self.default_style.format = self.ansi_handler.get_format()
            insert_text_to(cursor, text[last_end:], self.default_style.format)
#            # Slower alternative:
#            segments = self.COLOR_PATTERN.split(text)
#            cursor.insertText(segments.pop(0), self.default_style.format)
#            if segments:
#                for ansi_tags, text in zip(segments[::2], segments[1::2]):
#                    for ansi_tag in ansi_tags.split(';'):
#                        self.ansi_handler.set_code(int(ansi_tag))
#                    self.default_style.format = self.ansi_handler.get_format()
#                    cursor.insertText(text, self.default_style.format)
        self.set_cursor_position('eof')
        self.setCurrentCharFormat(self.default_style.format)

    def set_pythonshell_font(self, font=None):
        """Python Shell only"""
        if font is None:
            font = QFont()
        for style in self.font_styles:
            style.apply_style(font=font,
                              light_background=self.light_background,
                              is_default=style is self.default_style)
        self.ansi_handler.set_base_format(self.default_style.format)
