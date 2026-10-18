"""A small native SQL IDE (PyQt5) on top of the SQLPad core.

Top pane: write SQL. Ctrl+Enter runs the selection, or the statement under the
cursor; Ctrl+Shift+Enter runs the whole editor. Results show in a grid below.
Left: a database browser (database -> tables -> columns). Every subfolder of
data/ is a database; double-click one to make it the active database (same as
`USE name;`). Toolbar: dialect switcher, open/save .sql, export results to CSV,
Sync (load new/changed CSVs). Same DuckDB files as the CLI.
"""

from __future__ import annotations

import csv
import os
import re

from . import catalog, core, dialects, render

try:
    from PyQt5.QtCore import Qt, QUrl
    from PyQt5.QtGui import (
        QColor, QDesktopServices, QFont, QKeySequence, QSyntaxHighlighter,
        QTextCharFormat,
    )
    from PyQt5.QtWidgets import (
        QAbstractItemView, QAction, QApplication, QComboBox, QFileDialog,
        QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
        QShortcut, QSplitter, QTableWidget, QTableWidgetItem, QToolBar,
        QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    )
    _HAVE_QT = True
except Exception:  # noqa: BLE001
    _HAVE_QT = False


# --------------------------------------------------------------------------- #
#  Syntax highlighting
# --------------------------------------------------------------------------- #
_KEYWORDS = (
    "select from where group by having order limit offset join inner left right "
    "full outer cross on using union all intersect except with as distinct case "
    "when then else end and or not in is null like between exists asc desc "
    "insert into values update set delete create replace table view temp temporary "
    "drop alter add column primary key foreign references default cast try_cast "
    "qualify over partition range rows unbounded preceding following current row "
    "count sum avg min max coalesce nullif round date timestamp interval "
    "generate_series unnest describe show pragma explain"
).split()


def _fmt(color: str, *, bold: bool = False, italic: bool = False) -> "QTextCharFormat":
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


class SqlHighlighter(QSyntaxHighlighter):
    """Keyword / number / string / comment colouring for a dark theme."""

    def __init__(self, document):
        super().__init__(document)
        kw = _fmt("#4fc1ff", bold=True)
        self.rules = [(re.compile(rf"\b{k}\b", re.IGNORECASE), kw) for k in _KEYWORDS]
        self.rules.append((re.compile(r"\b\d+(\.\d+)?\b"), _fmt("#b5cea8")))
        self.rules.append((re.compile(r"'[^']*'"), _fmt("#ce9178")))
        self.rules.append((re.compile(r'"[^"]*"'), _fmt("#ce9178")))
        self.rules.append((re.compile(r"`[^`]*`"), _fmt("#ce9178")))
        self.rules.append((re.compile(r"--[^\n]*"), _fmt("#6a9955", italic=True)))
        self._block_fmt = _fmt("#6a9955", italic=True)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt signature)
        for pattern, fmt in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)

        # multi-line /* ... */ comments, overriding the above within their span
        self.setCurrentBlockState(0)
        start = 0 if self.previousBlockState() == 1 else text.find("/*")
        while start >= 0:
            end = text.find("*/", start)
            if end == -1:
                self.setCurrentBlockState(1)
                length = len(text) - start
            else:
                length = end - start + 2
            self.setFormat(start, length, self._block_fmt)
            start = text.find("/*", start + length)


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
_STARTER = (
    "-- Ctrl+Enter runs the statement under the cursor (or selection).\n"
    "-- Ctrl+Shift+Enter runs everything.\n"
    "-- Double-click a database on the left to switch to it (or: USE <database>;)\n"
    "SHOW TABLES;\n"
)

# tree item roles
_ROLE = Qt.UserRole if _HAVE_QT else 0
_ACTIVE_COLOR = "#4fc1ff"
_MUTED_COLOR = "#8a8a8a"

_QSS = """
QMainWindow, QWidget { background:#1e1e1e; color:#d4d4d4; }
QPlainTextEdit { background:#1e1e1e; color:#d4d4d4; border:none;
                 selection-background-color:#264f78; }
QToolBar { background:#252526; border:0; spacing:6px; padding:4px; }
QToolBar QToolButton { color:#d4d4d4; padding:4px 8px; border-radius:4px; }
QToolBar QToolButton:hover { background:#37373d; }
QComboBox { background:#3c3c3c; color:#d4d4d4; padding:3px 6px; border-radius:4px; }
QTreeWidget { background:#252526; color:#d4d4d4; border:0; outline:0; }
QTreeWidget::item { padding:2px 0; }
QTreeWidget::item:hover { background:#2a2d2e; }
QTreeWidget::item:selected { background:#37373d; color:#ffffff; }
QMenu { background:#252526; color:#d4d4d4; border:1px solid #3c3c3c; }
QMenu::item { padding:5px 22px; }
QMenu::item:selected { background:#37373d; }
QMenu::separator { height:1px; background:#3c3c3c; margin:4px 0; }
QTableWidget { background:#1e1e1e; color:#d4d4d4; gridline-color:#333;
               alternate-background-color:#262626;
               selection-background-color:#264f78; selection-color:#ffffff; }
QTableWidget::item { color:#d4d4d4; }
QHeaderView::section { background:#2d2d30; color:#cccccc; padding:4px; border:0; }
QTableCornerButton::section { background:#2d2d30; border:0; }
QLabel#status { color:#9cdcfe; padding:4px 8px; }
QSplitter::handle { background:#333; }
QScrollBar:vertical { background:#1e1e1e; width:12px; margin:0; }
QScrollBar:horizontal { background:#1e1e1e; height:12px; margin:0; }
QScrollBar::handle:vertical { background:#424242; min-height:28px; border-radius:4px; margin:2px; }
QScrollBar::handle:horizontal { background:#424242; min-width:28px; border-radius:4px; margin:2px; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background:#5a5a5a; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:none; }
"""


class IdeWindow(QMainWindow):
    def __init__(self, db_path: str, dialect: dialects.Dialect):
        super().__init__()
        self.db_path = db_path
        self.dialect = dialect
        self.con, report = core.connect_ex(db_path, prelude=dialect.prelude)
        self.active_db = catalog.current_database(self.con)
        self.current_file: str | None = None
        self.last_result: tuple[list[str], list] | None = None

        self.setWindowTitle("SQLPad IDE")
        self.resize(1100, 720)
        self._build_toolbar()
        self._build_body()
        self._build_shortcuts()
        self.refresh_tree()
        self._update_db_label()
        if report is not None and (report.loaded or report.created or report.errors):
            self._show_sync_report(report)
        else:
            self.set_status("Ready")

    # ---- UI construction -------------------------------------------------- #
    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        run = QAction("▶ Run", self); run.triggered.connect(self.run_current)
        run_all = QAction("⏩ Run all", self); run_all.triggered.connect(self.run_all)
        open_a = QAction("Open", self); open_a.triggered.connect(self.open_file)
        save_a = QAction("Save", self); save_a.triggered.connect(self.save_file)
        export = QAction("Export CSV", self); export.triggered.connect(self.export_csv)
        sync = QAction("⟳ Sync", self)
        sync.setToolTip("Load new or changed CSV files from the data/ folders")
        sync.triggered.connect(self.sync_databases)
        for a in (run, run_all, open_a, save_a, export, sync):
            tb.addAction(a)
        tb.addSeparator()

        self.db_label = QLabel()
        tb.addWidget(self.db_label)
        tb.addSeparator()

        tb.addWidget(QLabel(" Dialect: "))
        self.dialect_combo = QComboBox()
        for key in ("standard", "mysql", "bigquery"):
            self.dialect_combo.addItem(dialects.DIALECTS[key].title, key)
        self.dialect_combo.setCurrentIndex(
            ["standard", "mysql", "bigquery"].index(self.dialect.key))
        self.dialect_combo.currentIndexChanged.connect(self.on_dialect_changed)
        tb.addWidget(self.dialect_combo)

    def _build_body(self) -> None:
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.itemDoubleClicked.connect(self.on_tree_double)
        self.tree.customContextMenuRequested.connect(self.on_tree_menu)

        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 12))
        self.editor.setTabStopDistance(4 * 8)
        self.editor.setPlainText(_STARTER)
        self.highlighter = SqlHighlighter(self.editor.document())

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)

        self.status = QLabel("Ready")
        self.status.setObjectName("status")

        results = QWidget()
        rlay = QVBoxLayout(results)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.addWidget(self.table)
        rlay.addWidget(self.status)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(self.editor)
        vsplit.addWidget(results)
        vsplit.setSizes([380, 340])

        hsplit = QSplitter(Qt.Horizontal)
        hsplit.addWidget(self.tree)
        hsplit.addWidget(vsplit)
        hsplit.setSizes([240, 860])
        self.setCentralWidget(hsplit)

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+Return"), self, self.run_current)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self.run_current)
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self, self.run_all)
        QShortcut(QKeySequence("Ctrl+Shift+Enter"), self, self.run_all)
        QShortcut(QKeySequence.Open, self, self.open_file)
        QShortcut(QKeySequence.Save, self, self.save_file)

    # ---- Actions ---------------------------------------------------------- #
    def run_current(self) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        if selected.strip():
            # QPlainTextEdit uses U+2029 as the newline in selected text
            sql = selected.replace(" ", "\n")
        else:
            sql = render.statement_at(self.editor.toPlainText(), cursor.position()) or ""
        self._execute(sql)

    def run_all(self) -> None:
        self._execute(self.editor.toPlainText())

    def _execute(self, sql: str) -> None:
        statements = render.split_statements(sql)
        if not statements:
            self.set_status("nothing to run")
            return
        last = None
        total_ms = 0.0
        for stmt in statements:
            res = core.run_statement(self.con, stmt)
            total_ms += res.elapsed_ms
            if not res.ok:
                self._clear_table()
                self.last_result = None
                self.set_status(f"✗ {res.error}", error=True)
                self._after_execute()   # earlier statements may have changed things
                return
            if res.has_rows:
                last = res
        if last is not None:
            self._show_result(last.columns, last.rows)
            self.last_result = (last.columns, last.rows)
            self.set_status(f"{len(last.rows)} rows · {total_ms:.1f} ms")
        else:
            self._clear_table()
            self.last_result = None
            self.set_status(f"OK · {total_ms:.1f} ms")
        self._after_execute()

    def _show_result(self, columns, rows, cap: int = 10000) -> None:
        shown = rows[:cap]
        t = self.table
        t.clear()
        t.setColumnCount(len(columns))
        t.setRowCount(len(shown))
        t.setHorizontalHeaderLabels([str(c) for c in columns])
        null_color = QColor("#777777")
        for r, row in enumerate(shown):
            for c, val in enumerate(row):
                item = QTableWidgetItem("NULL" if val is None else str(val))
                if val is None:
                    item.setForeground(null_color)
                t.setItem(r, c, item)
        t.resizeColumnsToContents()
        if len(rows) > cap:
            self.set_status(f"{len(rows)} rows (showing first {cap})")

    def _clear_table(self) -> None:
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)

    # ---- Databases tree --------------------------------------------------- #
    def _after_execute(self) -> None:
        """A statement may have run USE / DDL: pick up the active db, redraw tree."""
        self._pick_up_active_db()
        self.refresh_tree()

    def _pick_up_active_db(self) -> None:
        try:
            cur = catalog.current_database(self.con)
        except Exception:  # noqa: BLE001
            return
        if cur != self.active_db:
            self.active_db = cur
            catalog.remember_active(cur)
            self._update_db_label()

    def _update_db_label(self) -> None:
        self.db_label.setText(
            f" Database: <b style='color:{_ACTIVE_COLOR}'>{self.active_db}</b> ")

    def refresh_tree(self) -> None:
        """Rebuild database -> table -> column tree, keeping what was expanded."""
        expanded: set[tuple] = set()
        first_build = self.tree.topLevelItemCount() == 0
        for i in range(self.tree.topLevelItemCount()):
            db_item = self.tree.topLevelItem(i)
            if db_item.isExpanded():
                expanded.add((db_item.data(0, _ROLE)[1],))
            for j in range(db_item.childCount()):
                t_item = db_item.child(j)
                if t_item.isExpanded():
                    expanded.add(t_item.data(0, _ROLE)[1:4])

        try:
            infos = catalog.list_databases(self.con)
            described = catalog.describe_all(self.con)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"cannot read databases: {exc}", error=True)
            return

        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for info in infos:
            tables = described.get(info.name, [])
            is_active = info.name == self.active_db
            db_item = QTreeWidgetItem([f"{info.name}  ({len(tables)})"])
            db_item.setData(0, _ROLE, ("db", info.name))
            if is_active:
                db_item.setFont(0, bold)
                db_item.setForeground(0, QColor(_ACTIVE_COLOR))
            source = f"data/{info.folder.name}/" if info.folder else "default database (tables you create)"
            db_item.setToolTip(0, f"{source}\nDouble-click to use · right-click for more")
            self.tree.addTopLevelItem(db_item)

            for schema, table, columns in tables:
                label = table if schema == "main" else f"{schema}.{table}"
                t_item = QTreeWidgetItem([label])
                t_item.setData(0, _ROLE, ("table", info.name, schema, table))
                db_item.addChild(t_item)
                for col, dtype in columns:
                    c_item = QTreeWidgetItem([f"{col}   {dtype}"])
                    c_item.setData(0, _ROLE, ("column", info.name, schema, table, col))
                    c_item.setForeground(0, QColor(_MUTED_COLOR))
                    t_item.addChild(c_item)

            if (info.name,) in expanded or (first_build and is_active):
                db_item.setExpanded(True)
            for j in range(db_item.childCount()):
                t_item = db_item.child(j)
                if t_item.data(0, _ROLE)[1:4] in expanded:
                    t_item.setExpanded(True)

    @staticmethod
    def _qualified(db: str, schema: str, table: str) -> str:
        return f"{catalog.qi(db)}.{catalog.qi(schema)}.{catalog.qi(table)}"

    def on_tree_double(self, item, _column: int = 0) -> None:
        data = item.data(0, _ROLE)
        if not data:
            return
        if data[0] == "db":
            self.switch_database(data[1])
        elif data[0] == "table":
            _kind, db, schema, table = data
            self._execute(f"SELECT * FROM {self._qualified(db, schema, table)} LIMIT 100")
        elif data[0] == "column":
            self.editor.insertPlainText(data[4])
            self.editor.setFocus()

    def on_tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        data = item.data(0, _ROLE) if item else None
        if not data:
            return
        menu = QMenu(self)
        if data[0] == "db":
            name = data[1]
            menu.addAction("Use this database", lambda: self.switch_database(name))
            info = next((i for i in catalog.list_databases(self.con) if i.name == name), None)
            if info is not None and info.folder is not None:
                menu.addSeparator()
                menu.addAction("Open data folder",
                               lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(info.folder))))
                menu.addAction("Reset from CSV files…", lambda: self.reset_database(name))
        elif data[0] == "table":
            _kind, db, schema, table = data
            q = self._qualified(db, schema, table)
            menu.addAction("Preview (100 rows)", lambda: self._execute(f"SELECT * FROM {q} LIMIT 100"))
            menu.addAction("Count rows", lambda: self._execute(f"SELECT COUNT(*) AS n FROM {q}"))
            menu.addAction("Insert name into editor",
                           lambda: (self.editor.insertPlainText(table), self.editor.setFocus()))
        else:
            return
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

    def switch_database(self, name: str) -> None:
        try:
            catalog.use(self.con, name)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"✗ {str(exc).splitlines()[0]}", error=True)
            return
        self.active_db = name
        self._update_db_label()
        self.refresh_tree()
        self.set_status(f"active database: {name}")

    def _show_sync_report(self, report) -> None:
        text = report.summary()
        if report.errors:
            text += " — " + report.errors[0]
        self.set_status(text, error=bool(report.errors))

    def sync_databases(self) -> None:
        try:
            report = catalog.sync(self.con)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"✗ sync failed: {exc}", error=True)
            return
        self._after_execute()
        self._show_sync_report(report)

    def reset_database(self, name: str) -> None:
        answer = QMessageBox.warning(
            self, "Reset database",
            f"Reset '{name}' from its CSV files?\n\n"
            "This discards ALL changes you made in this database (updates, deletes, "
            "dropped or created tables) and reloads it exactly as the CSVs are.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            report = catalog.reset(self.con, name)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"✗ {str(exc).splitlines()[0]}", error=True)
            return
        self._after_execute()
        self._show_sync_report(report)

    def on_dialect_changed(self, index: int) -> None:
        key = self.dialect_combo.itemData(index)
        new = dialects.resolve(key)
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass
        self.con = core.connect(self.db_path, prelude=new.prelude)
        self.dialect = new
        self.active_db = catalog.current_database(self.con)
        self._update_db_label()
        self.refresh_tree()
        self.set_status(f"dialect: {new.title}")

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SQL", os.path.dirname(os.path.abspath(self.db_path)),
            "SQL files (*.sql);;All files (*.*)")
        if not path:
            return
        with open(path, "r", encoding="utf-8") as fh:
            self.editor.setPlainText(fh.read())
        self.current_file = path
        self.setWindowTitle(f"SQLPad IDE — {os.path.basename(path)}")
        self.set_status(f"opened {path}")

    def save_file(self) -> None:
        path = self.current_file
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save SQL",
                os.path.dirname(os.path.abspath(self.db_path)),
                "SQL files (*.sql);;All files (*.*)")
            if not path:
                return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.editor.toPlainText())
        self.current_file = path
        self.setWindowTitle(f"SQLPad IDE — {os.path.basename(path)}")
        self.set_status(f"saved {path}")

    def export_csv(self) -> None:
        if not self.last_result:
            self.set_status("no result to export — run a query first", error=True)
            return
        columns, rows = self.last_result
        path, _ = QFileDialog.getSaveFileName(
            self, "Export results to CSV",
            os.path.dirname(os.path.abspath(self.db_path)),
            "CSV files (*.csv);;All files (*.*)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(columns)
            w.writerows(rows)
        self.set_status(f"exported {len(rows)} rows → {path}")

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("color:#f48771;" if error else "color:#9cdcfe;")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt signature)
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


def run_ide(db_path: str = core.DEFAULT_DB,
            dialect: dialects.Dialect | None = None) -> int:
    if not _HAVE_QT:
        print("PyQt5 is not available — cannot start the IDE.")
        return 1
    dial = dialect or dialects.DIALECTS[dialects.DEFAULT_DIALECT]
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(_QSS)
    win = IdeWindow(db_path, dial)
    win.show()
    app.exec_()
    return 0
