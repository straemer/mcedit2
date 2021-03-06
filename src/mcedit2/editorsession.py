from __future__ import absolute_import, division, print_function, unicode_literals
import logging
import os

from PySide import QtGui, QtCore
from PySide.QtCore import Qt
from mcedit2.rendering.blockmodels import BlockModels

from mcedit2 import editortools
from mcedit2.command import SimpleRevisionCommand
from mcedit2.editorcommands.fill import fillCommand
from mcedit2.editorcommands.find_replace import FindReplaceDialog
from mcedit2.editorcommands.analyze import AnalyzeOutputDialog
from mcedit2.editortools.select import SelectCommand
from mcedit2.imports import PendingImport
from mcedit2.panels.player import PlayerPanel
from mcedit2.panels.map import MapPanel
from mcedit2.panels.worldinfo import WorldInfoPanel
from mcedit2.util import minecraftinstall
from mcedit2.util.dialogs import NotImplementedYet
from mcedit2.util.directories import getUserSchematicsDirectory
from mcedit2.util.mimeformats import MimeFormats
from mcedit2.util.resources import resourcePath
from mcedit2.widgets.mcedockwidget import MCEDockWidget
from mcedit2.widgets.spinslider import SpinSlider
from mceditlib.util import exhaust
from mceditlib.util.lazyprop import weakrefprop
from mcedit2.util.raycast import rayCastInBounds
from mcedit2.util.showprogress import showProgress, MCEProgressDialog
from mcedit2.util.undostack import MCEUndoStack
from mcedit2.widgets.inspector import InspectorWidget
from mcedit2.worldview.viewaction import UseToolMouseAction, TrackingMouseAction
from mcedit2.rendering import chunkloader
from mcedit2.rendering.scenegraph import scenenode
from mcedit2.rendering.geometrycache import GeometryCache
from mcedit2.rendering.textureatlas import TextureAtlas
from mcedit2.widgets.layout import Column, Row
from mcedit2.util.settings import Settings
from mcedit2.worldview.camera import CameraWorldViewFrame
from mcedit2.worldview.cutaway import CutawayWorldViewFrame
from mcedit2.worldview.minimap import MinimapWorldView
from mcedit2.worldview.overhead import OverheadWorldViewFrame
from mceditlib import util, nbt, faces
from mceditlib.anvil.biome_types import BiomeTypes
from mceditlib.geometry import Vector
from mceditlib.operations import ComposeOperations
from mceditlib.operations.entity import RemoveEntitiesOperation
from mceditlib.selection import BoundingBox
from mceditlib.exceptions import PlayerNotFound, ChunkNotPresent
from mceditlib.revisionhistory import UndoFolderExists, RevisionChanges
from mceditlib.worldeditor import WorldEditor
from mceditlib.blocktypes import BlockType

log = logging.getLogger(__name__)

sessionSettings = Settings().getNamespace("editorsession")
currentViewSetting = sessionSettings.getOption("currentview", unicode, "cam")


class PasteImportCommand(QtGui.QUndoCommand):
    def __init__(self, editorSession, pendingImport, text, *args, **kwargs):
        super(PasteImportCommand, self).__init__(*args, **kwargs)
        self.setText(text)
        self.editorSession = editorSession
        self.pendingImport = pendingImport

    def undo(self):
        self.editorSession.moveTool.currentImport = None
        self.editorSession.chooseTool("Select")

    def redo(self):
        self.editorSession.moveTool.currentImport = self.pendingImport
        self.editorSession.chooseTool("Move")


class EditorSession(QtCore.QObject):
    """
    An EditorSession is a world currently opened for editing, the state of the editor
    including the current selection box, the editor tab containing its viewports,
    its command history, its shared OpenGL context, a separate instance of each editor
    tool (why?), and the ChunkLoader that coordinates loading chunks into its viewports.

    Parameters
    ----------
    filename: unicode
        Path to file to open in editor.
    configuredBlocks: list of BlockDefinitions
        Blocks definitions set by the user, from ConfigureBlocksDialog.getDefinedBlocks()
    readonly: bool
        If True, editing is disabled and the world cannot be modified or saved.
    progressCallback: function(int, int, unicode)
        Called while initializing the EditorSession to report progress. Parameters to the
        callback are (current, maximum, status):

            current: int
                Current progress
            maximum: int
                Maximum progress
            status: unicode
                Status text to display

    Attributes
    ----------

    Haha, good luck.
    """
    def __init__(self, filename, configuredBlocks, readonly=False,
                 progressCallback=None):
        from mcedit2 import __version__ as v

        progressMax = 8  # fixme
        if progressCallback is None:
            def progress(status):
                pass
        else:

            def progress(status):
                progressCallback(progress.progressCount, progressMax, status)
                progress.progressCount += 1

            progress.progressCount = 0

        QtCore.QObject.__init__(self)
        self.undoStack = MCEUndoStack()
        self.lastSaveIndex = 0

        self.resourceLoader = minecraftinstall.getResourceLoaderForFilename(filename)
        self.currentDimension = None

        self.loader = None
        self.blockModels = None
        self.textureAtlas = None
        self.editorTab = None

        self.filename = filename
        self.dockWidgets = []
        self.undoBlock = None
        self.currentTool = None
        self.dirty = False
        self.configuredBlocks = None

        self.copiedSchematic = None  # xxx should be app global!!
        """:type : WorldEditor"""

        # --- Open world editor ---
        try:
            progress("Creating WorldEditor...")
            self.worldEditor = WorldEditor(filename, readonly=readonly)
        except UndoFolderExists:
            msgBox = QtGui.QMessageBox()
            msgBox.setIcon(QtGui.QMessageBox.Warning)
            msgBox.setWindowTitle(self.tr("MCEdit %(version)s") % {"version": v})
            msgBox.setText(self.tr("This world was not properly closed by MCEdit."))
            msgBox.setInformativeText(self.tr(
                "MCEdit may have crashed. An undo history was found for this world. You may try "
                "to resume editing with the saved undo history, or start over with the current "
                "state of the world."))
            resumeBtn = msgBox.addButton("Resume Editing", QtGui.QMessageBox.ApplyRole)
            msgBox.addButton("Discard History", QtGui.QMessageBox.DestructiveRole)
            # msgBox.exec_()
            # clicked = msgBox.clickedButton()

            # xxxxx resume editing not implemented in session - need to restore undo history!
            clicked = None
            resume = clicked is resumeBtn
            try:
                self.worldEditor = WorldEditor(filename, readonly=readonly, resume=resume)
            except NotImplementedError:
                NotImplementedYet()
                raise IOError("Uh-oh")

        self.worldEditor.requireRevisions()

        progress("Creating menus...")

        # --- Menus ---

        self.menus = []

        # - Edit -

        self.menuEdit = QtGui.QMenu(self.tr("Edit"))
        self.menuEdit.setObjectName("menuEdit")

        self.actionCut = QtGui.QAction(self.tr("Cut"), self, triggered=self.cut, enabled=False)
        self.actionCut.setShortcut(QtGui.QKeySequence.Cut)
        self.actionCut.setObjectName("actionCut")

        self.actionCopy = QtGui.QAction(self.tr("Copy"), self, triggered=self.copy, enabled=False)
        self.actionCopy.setShortcut(QtGui.QKeySequence.Copy)
        self.actionCopy.setObjectName("actionCopy")

        self.actionPaste = QtGui.QAction(self.tr("Paste"), self, triggered=self.paste,
                                         enabled=False)
        self.actionPaste.setShortcut(QtGui.QKeySequence.Paste)
        self.actionPaste.setObjectName("actionPaste")

        self.actionPaste_Blocks = QtGui.QAction(self.tr("Paste Blocks"), self,
                                                triggered=self.pasteBlocks, enabled=False)
        self.actionPaste_Blocks.setShortcut(QtGui.QKeySequence("Ctrl+Shift+V"))
        self.actionPaste_Blocks.setObjectName("actionPaste_Blocks")

        self.actionPaste_Entities = QtGui.QAction(self.tr("Paste Entities"), self,
                                                  triggered=self.pasteEntities, enabled=False)
        self.actionPaste_Entities.setShortcut(QtGui.QKeySequence("Ctrl+Alt+V"))
        self.actionPaste_Entities.setObjectName("actionPaste_Entities")

        self.actionClear = QtGui.QAction(self.tr("Delete"), self, triggered=self.deleteSelection,
                                         enabled=False)
        self.actionClear.setShortcut(QtGui.QKeySequence.Delete)
        self.actionClear.setObjectName("actionClear")

        self.actionDeleteBlocks = QtGui.QAction(self.tr("Delete Blocks"), self,
                                                triggered=self.deleteBlocks, enabled=False)
        self.actionDeleteBlocks.setShortcut(QtGui.QKeySequence("Shift+Del"))
        self.actionDeleteBlocks.setObjectName("actionDeleteBlocks")

        self.actionDeleteEntities = QtGui.QAction(self.tr("Delete Entities"), self,
                                                  triggered=self.deleteEntities, enabled=False)
        self.actionDeleteEntities.setShortcut(QtGui.QKeySequence("Shift+Alt+Del"))
        self.actionDeleteEntities.setObjectName("actionDeleteEntities")

        self.actionFill = QtGui.QAction(self.tr("Fill"), self, triggered=self.fill, enabled=False)
        self.actionFill.setShortcut(QtGui.QKeySequence("Shift+Ctrl+F"))
        self.actionFill.setObjectName("actionFill")

        self.actionFindReplace = QtGui.QAction(self.tr("Find/Replace"), self,
                                               triggered=self.findReplace, enabled=True)
        self.actionFindReplace.setShortcut(QtGui.QKeySequence.Find)
        self.actionFindReplace.setObjectName("actionFindReplace")

        self.actionAnalyze = QtGui.QAction(self.tr("Analyze"), self, triggered=self.analyze,
                                           enabled=True)
        # self.actionAnalyze.setShortcut(QtGui.QKeySequence.Analyze)
        self.actionAnalyze.setObjectName("actionAnalyze")

        undoAction = self.undoStack.createUndoAction(self.menuEdit)
        undoAction.setShortcut(QtGui.QKeySequence.Undo)
        redoAction = self.undoStack.createRedoAction(self.menuEdit)
        redoAction.setShortcut(QtGui.QKeySequence.Redo)

        self.menuEdit.addAction(undoAction)
        self.menuEdit.addAction(redoAction)
        self.menuEdit.addSeparator()
        self.menuEdit.addAction(self.actionCut)
        self.menuEdit.addAction(self.actionCopy)
        self.menuEdit.addAction(self.actionPaste)
        self.menuEdit.addAction(self.actionPaste_Blocks)
        self.menuEdit.addAction(self.actionPaste_Entities)
        self.menuEdit.addSeparator()
        self.menuEdit.addAction(self.actionClear)
        self.menuEdit.addAction(self.actionDeleteBlocks)
        self.menuEdit.addAction(self.actionDeleteEntities)
        self.menuEdit.addSeparator()
        self.menuEdit.addAction(self.actionFill)
        self.menuEdit.addSeparator()
        self.menuEdit.addAction(self.actionFindReplace)
        self.menuEdit.addAction(self.actionAnalyze)

        self.menus.append(self.menuEdit)

        # - Select -

        self.menuSelect = QtGui.QMenu(self.tr("Select"))

        self.actionSelectAll = QtGui.QAction(self.tr("Select All"), self, triggered=self.selectAll)
        self.actionSelectAll.setShortcut(QtGui.QKeySequence.SelectAll)
        self.menuSelect.addAction(self.actionSelectAll)

        self.actionDeselect = QtGui.QAction(self.tr("Deselect"), self, triggered=self.deselect)
        self.actionDeselect.setShortcut(QtGui.QKeySequence("Ctrl+D"))
        self.menuSelect.addAction(self.actionDeselect)

        self.menus.append(self.menuSelect)

        # - Import/Export -

        self.menuImportExport = QtGui.QMenu(self.tr("Import/Export"))

        self.actionExport = QtGui.QAction(self.tr("Export"), self, triggered=self.export)
        self.actionExport.setShortcut(QtGui.QKeySequence("Ctrl+Shift+E"))
        self.menuImportExport.addAction(self.actionExport)

        self.actionImport = QtGui.QAction(self.tr("Import"), self, triggered=self.import_)
        self.actionImport.setShortcut(QtGui.QKeySequence("Ctrl+Shift+D"))
        self.menuImportExport.addAction(self.actionImport)

        self.actionImport = QtGui.QAction(self.tr("Show Exports Library"), self,
                                          triggered=QtGui.qApp.libraryDockWidget.toggleViewAction().trigger)

        self.actionImport.setShortcut(QtGui.QKeySequence("Ctrl+Shift+L"))
        self.menuImportExport.addAction(self.actionImport)

        self.menus.append(self.menuImportExport)

        # - Chunk -

        self.menuChunk = QtGui.QMenu(self.tr("Chunk"))

        self.actionDeleteChunks = QtGui.QAction(self.tr("Delete Chunks"), self, triggered=self.deleteChunks)
        self.actionCreateChunks = QtGui.QAction(self.tr("Create Chunks"), self, triggered=self.createChunks)
        self.actionRepopChunks = QtGui.QAction(self.tr("Mark Chunks For Repopulation"),
                                               self, triggered=self.repopChunks)

        self.menuChunk.addAction(self.actionDeleteChunks)
        self.menuChunk.addAction(self.actionCreateChunks)
        self.menuChunk.addAction(self.actionRepopChunks)
        self.menus.append(self.menuChunk)

        # --- Resources ---

        self.geometryCache = GeometryCache()

        progress("Loading textures and models...")
        self.setConfiguredBlocks(configuredBlocks)  # Must be called after resourceLoader is in place

        self.editorOverlay = scenenode.Node()

        self.biomeTypes = BiomeTypes()

        # --- Panels ---
        progress("Loading panels...")

        self.playerPanel = PlayerPanel(self)
        self.mapPanel = MapPanel(self)
        self.worldInfoPanel = WorldInfoPanel(self)
        self.panels = [self.playerPanel, self.worldInfoPanel, self.mapPanel]
        self.topToolbarActions = []

        fillIcon = QtGui.QIcon(resourcePath("mcedit2/assets/mcedit2/icons/fill.png"))
        self.actionFill.setIcon(fillIcon)
        self.topToolbarActions.append(self.actionFill)

        saveIcon = QtGui.QIcon(resourcePath("mcedit2/assets/mcedit2/icons/save.png"))
        saveIcon.addFile(resourcePath("mcedit2/assets/mcedit2/icons/save_ok.png"), mode=QtGui.QIcon.Disabled)

        self.actionSave = QtGui.QAction(saveIcon, self.tr("Save"), self, triggered=self.save)
        self.actionSave.setEnabled(False)

        self.topToolbarActions.append(self.actionSave)


        # --- Tools ---

        progress("Loading tools...")

        self.toolClasses = list(editortools.ToolClasses())
        self.toolActionGroup = QtGui.QActionGroup(self)
        self.tools = [cls(self) for cls in self.toolClasses]
        self.toolActions = [tool.pickToolAction() for tool in self.tools]
        self.actionsByName = {action.toolName: action for action in self.toolActions}
        for tool in self.tools:
            tool.toolPicked.connect(self.chooseTool)
        for action in self.toolActions:
            self.toolActionGroup.addAction(action)

        self.selectionTool = self.getTool("Select")
        self.moveTool = self.getTool("Move")

        # --- Dimensions ---

        dimButton = self.changeDimensionButton = QtGui.QToolButton()
        dimButton.setText(self.dimensionMenuLabel(""))
        dimAction = self.changeDimensionAction = QtGui.QWidgetAction(self)
        dimAction.setDefaultWidget(dimButton)
        dimMenu = self.dimensionsMenu = QtGui.QMenu()

        self.dimMapper = QtCore.QSignalMapper()
        self.dimMapper.mapped[str].connect(self.gotoDimension)

        for dimName in self.worldEditor.listDimensions():
            displayName = self.dimensionDisplayName(dimName)
            action = dimMenu.addAction(displayName)
            self.dimMapper.setMapping(action, dimName)
            action.triggered.connect(self.dimMapper.map)

        dimButton.setMenu(dimMenu)
        dimButton.setPopupMode(QtGui.QToolButton.InstantPopup)

        self.topToolbarActions.append(dimAction)
        self.topToolbarActions.append(None)

        # --- Versions/Resource Packs ---

        versionRPAction = self.versionRPAction = QtGui.QWidgetAction(self)

        self.mcVersionButton = mcVersionButton = self.changeMCVersionButton = QtGui.QToolButton(autoRaise=True)
        mcVersionButton.setText(self.minecraftVersionLabel())
        self.mcVersionMenu = QtGui.QMenu()
        mcVersionButton.setMenu(self.mcVersionMenu)
        mcVersionButton.setPopupMode(QtGui.QToolButton.InstantPopup)

        self.resourcePackButton = resourcePackButton = self.changeResourcePackButton = QtGui.QToolButton(autoRaise=True)
        resourcePackButton.setText(self.resourcePackLabel())
        self.resourcePackMenu = QtGui.QMenu()
        resourcePackButton.setMenu(self.resourcePackMenu)
        resourcePackButton.setPopupMode(QtGui.QToolButton.InstantPopup)

        self.versionRPWidget = QtGui.QStackedWidget()
        self.versionRPWidget.setSizePolicy(QtGui.QSizePolicy.Minimum, QtGui.QSizePolicy.Minimum)
        self.versionRPAction.setDefaultWidget(self.versionRPWidget)
        self.topToolbarActions.append(versionRPAction)

        QtGui.qApp.toolbarTextToggled.connect(self.toolbarTextChanged)
        self.toolbarTextChanged(True)  # xxx

        self._updateVersionsAndResourcePacks()

        progress("Loading overworld dimension")
        self.gotoDimension("")

        # --- Editor stuff ---
        progress("Creating EditorTab...")

        self.editorTab = EditorTab(self)
        self.toolChanged.connect(self.toolDidChange)

        self.editorTab.urlsDropped.connect(self.urlsWereDropped)
        self.editorTab.mapItemDropped.connect(self.mapItemWasDropped)

        self.undoStack.indexChanged.connect(self.undoIndexChanged)

        self.findReplaceDialog = FindReplaceDialog(self)
        for resultsWidget in self.findReplaceDialog.resultsWidgets:
            self.dockWidgets.append((Qt.BottomDockWidgetArea, resultsWidget))

        self.inspectorWidget = InspectorWidget(self)
        self.inspectorDockWidget = MCEDockWidget(self.tr("Inspector"), objectName="inspector")
        self.inspectorDockWidget.setWidget(self.inspectorWidget)
        self.inspectorDockWidget.setUnfocusedOpacity(0.8)

        self.inspectorDockWidget.hide()
        self.dockWidgets.append((Qt.RightDockWidgetArea, self.inspectorDockWidget))

        if len(self.toolActions):
            # Must be called after toolChanged is connected to editorTab
            self.toolActions[0].trigger()

        if hasattr(progress, 'progressCount') and progress.progressCount != progressMax:
            log.info("Update progressMax to %d, please.", progress.progressCount)


    def _updateVersionsAndResourcePacks(self):
        self.mcVersionMapper = QtCore.QSignalMapper()
        self.mcVersionMapper.mapped[str].connect(self.changeMCVersion)
        self.resourcePackMapper = QtCore.QSignalMapper()
        self.resourcePackMapper.mapped[str].connect(self.changeResourcePack)

        self.mcVersionMenu.clear()
        self.resourcePackMenu.clear()

        defaultAction = self.resourcePackMenu.addAction(self.tr("(Default)"))
        defaultAction.triggered.connect(self.resourcePackMapper.map)
        self.resourcePackMapper.setMapping(defaultAction, "")

        install = minecraftinstall.GetInstalls().getCurrentInstall()

        for version in sorted(install.versions, reverse=True):
            versionAction = self.mcVersionMenu.addAction(version)
            self.mcVersionMapper.setMapping(versionAction, version)
            versionAction.triggered.connect(self.mcVersionMapper.map)

        for resourcePack in sorted(install.resourcePacks):
            resourcePackAction = self.resourcePackMenu.addAction(resourcePack)
            self.resourcePackMapper.setMapping(resourcePackAction, resourcePack)
            resourcePackAction.triggered.connect(self.resourcePackMapper.map)

    def toolbarTextChanged(self, enable):
        if enable:
            Box = Column
        else:
            Box = Row

        widget = self.versionRPAction.defaultWidget()
        if widget:
            layout = widget.layout()
            while layout.count():
                layout.takeAt(0)

        self.mcVersionButton.setParent(None)
        self.resourcePackButton.setParent(None)
        versionRPColumn = Box(self.mcVersionButton, self.resourcePackButton, margin=0)
        versionRPColumn.addStretch()
        widget = QtGui.QWidget()
        widget.setSizePolicy(QtGui.QSizePolicy.Minimum, QtGui.QSizePolicy.Minimum)
        widget.setLayout(versionRPColumn)

        while self.versionRPWidget.count():
            self.versionRPWidget.takeAt(0)

        self.versionRPWidget.addWidget(widget)

    def changeResourcePack(self, packName):
        packDisplayName = packName or "(default)"
        log.info("Changing to resource pack %s", packName)
        dialog = MCEProgressDialog(QtGui.qApp.mainWindow)
        dialog.setRange(0, 0)
        dialog.setValue(0)
        dialog.setWindowTitle(self.tr("Changing resource pack..."))
        dialog.setLabelText(self.tr("Changing to resource pack %s") % packDisplayName)
        dialog.setMinimumDuration(0)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.show()
        QtGui.qApp.processEvents()
        minecraftinstall.currentResourcePackOption.setValue(packName or "")
        self.resourceLoader = minecraftinstall.getResourceLoaderForFilename(self.filename)
        self.changeResourcePackButton.setText(self.resourcePackLabel())
        self.reloadModels()
        dialog.hide()

    def changeMCVersion(self, version):
        versionDisplayName = version or "(current)"
        dialog = MCEProgressDialog(QtGui.qApp.mainWindow)
        dialog.setRange(0, 0)
        dialog.setValue(0)
        dialog.setWindowTitle(self.tr("Changing Minecraft version..."))
        dialog.setLabelText(self.tr("Changing to Minecraft version %s") % versionDisplayName)
        dialog.setMinimumDuration(0)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.show()
        QtGui.qApp.processEvents()

        minecraftinstall.currentVersionOption.setValue(version)
        self.resourceLoader = minecraftinstall.getResourceLoaderForFilename(self.filename)
        self.changeMCVersionButton.setText(self.minecraftVersionLabel())
        self.reloadModels()
        dialog.hide()

    def minecraftVersionLabel(self):
        version = minecraftinstall.currentVersionOption.value() or self.tr("(Not set)")
        return self.tr("Minecraft Version: %s") % version

    def resourcePackLabel(self):
        resourcePack = minecraftinstall.currentResourcePackOption.value() or self.tr("(Default)")
        return self.tr("Resource Pack: %s") % resourcePack

    # Connecting these signals to the EditorTab creates a circular reference through
    # the Qt objects, preventing the EditorSession from being destroyed

    def focusWorldView(self):
        self.editorTab.currentView().setFocus()

    def updateView(self):
        if self.editorTab:
            self.editorTab.currentView().update()

    def toolDidChange(self, tool):
        self.editorTab.toolDidChange(tool)

    # --- Block config ---

    # Emitted when configuredBlocks is changed. TextureAtlas and BlockModels will also have changed.
    configuredBlocksChanged = QtCore.Signal()

    def setConfiguredBlocks(self, configuredBlocks):
        blocktypes = self.worldEditor.blocktypes
        if self.configuredBlocks is not None:
            # Remove all previously configured blocks
            deadJsons = []
            for json in blocktypes.blockJsons:
                if '__configured__' in json:
                    deadJsons.append(json)

            deadIDs = set((j['internalName'], j['meta']) for j in deadJsons)
            blocktypes.allBlocks[:] = [
                bt for bt in blocktypes.allBlocks
                if (bt.internalName, bt.meta) not in deadIDs
            ]

            for json in deadJsons:
                internalName = json['internalName']
                fakeState = json['blockState']
                blocktypes.blockJsons.remove(json)
                ID = blocktypes.IDsByName[internalName]

                del blocktypes.IDsByState[internalName + fakeState]
                del blocktypes.statesByID[ID, json['meta']]

        for blockDef in configuredBlocks:
            internalName = blockDef.internalName
            if internalName not in blocktypes.IDsByName:
                # no ID mapped to this name, skip
                continue

            if blockDef.meta == 0:
                blockType = blocktypes[internalName]
                blockJson = blockType.json
            else:
                # not automatically created by FML mapping loader
                ID = blocktypes.IDsByName[internalName]
                fakeState = '[%d]' % blockDef.meta
                nameAndState = internalName + fakeState
                blocktypes.blockJsons[nameAndState] = {
                    'displayName': internalName,
                    'internalName': internalName,
                    'blockState': fakeState,
                    'unknown': False,
                    'meta': blockDef.meta,
                }
                blockType = BlockType(ID, blockDef.meta, blocktypes)
                blocktypes.allBlocks.append(blockType)
                blocktypes.IDsByState[nameAndState] = ID, blockDef.meta
                blocktypes.statesByID[ID, blockDef.meta] = nameAndState

                blockJson = blockType.json

            blockJson['forcedModel'] = blockDef.modelPath
            blockJson['forcedModelTextures'] = blockDef.modelTextures
            blockJson['forcedModelRotation'] = blockDef.modelRotations
            blockJson['forcedRotationFlags'] = blockDef.rotationFlags
            blockJson['__configured__'] = True

        self.configuredBlocks = configuredBlocks
        self.reloadModels()
        self.configuredBlocksChanged.emit()

    def reloadModels(self):
        self.blockModels = BlockModels(self.worldEditor.blocktypes, self.resourceLoader)
        self.textureAtlas = TextureAtlas(self.worldEditor, self.resourceLoader, self.blockModels)
        # May be called before editorTab is created
        if self.editorTab:
            for view in self.editorTab.views:
                view.setTextureAtlas(self.textureAtlas)


    # --- Selection ---

    selectionChanged = QtCore.Signal(BoundingBox)
    _currentSelection = None

    @property
    def currentSelection(self):
        return self._currentSelection

    @currentSelection.setter
    def currentSelection(self, box):
        self._currentSelection = box
        self.enableSelectionCommands(box is not None and box.volume != 0)
        self.enableChunkSelectionCommands(box is not None)
        self.selectionChanged.emit(box)

    def enableSelectionCommands(self, enable):
        self.actionCut.setEnabled(enable)
        self.actionCopy.setEnabled(enable)
        self.actionPaste.setEnabled(enable)
        self.actionPaste_Blocks.setEnabled(enable)
        self.actionPaste_Entities.setEnabled(enable)
        self.actionClear.setEnabled(enable)
        self.actionDeleteBlocks.setEnabled(enable)
        self.actionDeleteEntities.setEnabled(enable)
        self.actionFill.setEnabled(enable)
        self.actionExport.setEnabled(enable)

    def enableChunkSelectionCommands(self, enable):
        self.actionDeleteChunks.setEnabled(enable)
        self.actionCreateChunks.setEnabled(enable)
        self.actionRepopChunks.setEnabled(enable)

    # --- Menu commands ---

    # - World -

    def save(self):
        self.undoStack.clearUndoBlock()

        saveTask = self.worldEditor.saveChangesIter()
        showProgress("Saving...", saveTask)
        self.dirty = False
        self.actionSave.setEnabled(False)
        self.lastSaveIndex = self.undoStack.index()

    # - Edit -

    def cut(self):
        command = SimpleRevisionCommand(self, "Cut")
        with command.begin():
            task = self.currentDimension.exportSchematicIter(self.currentSelection)
            self.copiedSchematic = showProgress("Cutting...", task)
            task = self.currentDimension.fillBlocksIter(self.currentSelection, "air")
            showProgress("Cutting...", task)
        self.undoStack.push(command)

    def copy(self):
        task = self.currentDimension.exportSchematicIter(self.currentSelection)
        self.copiedSchematic = showProgress("Copying...", task)

    def paste(self):
        if self.copiedSchematic is None:
            return
        view = self.editorTab.currentView()
        dim = self.copiedSchematic.getDimension()
        imp = PendingImport(dim, view.mouseBlockPos, dim.bounds, self.tr("<Pasted Object>"))
        command = PasteImportCommand(self, imp, "Paste")
        self.undoStack.push(command)

    def pasteBlocks(self):
        NotImplementedYet()

    def pasteEntities(self):
        NotImplementedYet()

    def findReplace(self):
        self.findReplaceDialog.exec_()

    def analyze(self):
        if self.currentSelection is None:
            return
        task = self.currentDimension.analyzeIter(self.currentSelection)
        showProgress("Analyzing...", task)
        outputDialog = AnalyzeOutputDialog(self, task.blocks,
                                           task.entityCounts,
                                           task.tileEntityCounts,
                                           task.dimension.worldEditor.displayName)

    def deleteSelection(self):
        command = SimpleRevisionCommand(self, "Delete")
        with command.begin():
            fillTask = self.currentDimension.fillBlocksIter(self.currentSelection, "air")
            entitiesTask = RemoveEntitiesOperation(self.currentDimension, self.currentSelection)
            task = ComposeOperations(fillTask, entitiesTask)
            showProgress("Deleting...", task)
        self.pushCommand(command)

    def deleteBlocks(self):
        command = SimpleRevisionCommand(self, "Delete Blocks")
        with command.begin():
            fillTask = self.currentDimension.fillBlocksIter(self.currentSelection, "air")
            showProgress("Deleting...", fillTask)
        self.pushCommand(command)

    def deleteEntities(self):
        command = SimpleRevisionCommand(self, "Delete Entities")
        with command.begin():
            entitiesTask = RemoveEntitiesOperation(self.currentDimension, self.currentSelection)
            showProgress("Deleting...", entitiesTask)
        self.pushCommand(command)

    def fill(self):
        fillCommand(self)

    # - Select -

    def selectAll(self):
        command = SelectCommand(self, self.currentDimension.bounds, self.tr("Select All"))
        self.pushCommand(command)

    def deselect(self):
        command = SelectCommand(self, None)
        command.setText(self.tr("Deselect"))
        self.pushCommand(command)

    # - Chunk -

    def deleteChunks(self):
        if self.currentSelection is None:
            return

        command = SimpleRevisionCommand(self, self.tr("Delete Chunks"))
        with command.begin():
            for cx in range(self.currentSelection.mincx, self.currentSelection.maxcx):
                for cz in range(self.currentSelection.mincz, self.currentSelection.maxcz):
                    self.currentDimension.deleteChunk(cx, cz)
        self.pushCommand(command)

    def createChunks(self):
        QtGui.QMessageBox.warning(QtGui.qApp.mainWindow, "Not implemented.", "Create chunks is not implemented yet!")

    def repopChunks(self):
        QtGui.QMessageBox.warning(QtGui.qApp.mainWindow, "Not implemented.", "Repop chunks is not implemented yet!")

    # - Dimensions -

    dimensionChanged = QtCore.Signal(object)

    _dimDisplayNames = {"": "Overworld",
                        "DIM-1": "Nether",
                        "DIM1": "The End",
                        }

    def dimensionDisplayName(self, dimName):
        return self._dimDisplayNames.get(dimName, dimName)

    def dimensionMenuLabel(self, dimName):
        return self.tr("Dimension: %s" % self.dimensionDisplayName(dimName))

    def gotoDimension(self, dimName):
        dim = self.worldEditor.getDimension(dimName)
        if dim is self.currentDimension:
            return
        log.info("Going to dimension %s", dimName)
        self.changeDimensionButton.setText(self.dimensionMenuLabel(dimName))
        self.currentDimension = dim
        self.loader = chunkloader.ChunkLoader(self.currentDimension)

        self.loader.chunkCompleted.connect(self.chunkDidComplete)
        self.loader.allChunksDone.connect(self.updateView)
        self.revisionChanged.connect(self.loader.revisionDidChange)

        self.dimensionChanged.emit(dim)

    # - Import/export -

    def import_(self):
        # prompt for a file to import
        startingDir = Settings().value("import_dialog/starting_dir", getUserSchematicsDirectory())
        result = QtGui.QFileDialog.getOpenFileName(QtGui.qApp.mainWindow, self.tr("Import"),
                                                   startingDir,
                                                   "All files (*.*)")
        if result:
            filename = result[0]
            if filename:
                self.importSchematic(filename)

    def export(self):
        # prompt for filename and format. maybe use custom browser to save to export library??
        startingDir = Settings().value("import_dialog/starting_dir", getUserSchematicsDirectory())
        result = QtGui.QFileDialog.getSaveFileName(QtGui.qApp.mainWindow,
                                                   self.tr("Export Schematic"),
                                                   startingDir,
                                                   "Schematic files (*.schematic)")

        if result:
            filename = result[0]
            if filename:
                task = self.currentDimension.exportSchematicIter(self.currentSelection)
                schematic = showProgress("Copying...", task)
                schematic.saveToFile(filename)

    # --- Drag-and-drop ---

    def urlsWereDropped(self, mimeData, position, face):
        log.info("URLs dropped:\n%s", mimeData.urls())
        for url in mimeData.urls():
            if url.isLocalFile():
                filename = url.toLocalFile()
                self.importSchematic(filename, position + face.vector)
                break

    def mapItemWasDropped(self, mimeData, position, face):
        log.info("Map item dropped.")
        assert mimeData.hasFormat(MimeFormats.MapItem)
        mapIDString = mimeData.data(MimeFormats.MapItem).data()
        mapIDs = mapIDString.split(", ")
        mapIDs = [int(m) for m in mapIDs]
        mapID = mapIDs[0]  # xxx only one at a time for now

        position = position + face.vector
        x, y, z = position
        cx = x >> 4
        cz = z >> 4
        try:
            chunk = self.currentDimension.getChunk(cx, cz)
        except ChunkNotPresent:
            log.info("Refusing to import map into non-existent chunk %s", (cx, cz))
            return

        ref = self.worldEditor.createEntity("ItemFrame")
        if ref is None:
            return

        facing = ref.facingForMCEditFace(face)
        if facing is None:
            # xxx by camera vector?
            facing = ref.SouthFacing

        ref.Item.Damage = mapID
        ref.Item.id = "minecraft:filled_map"
        ref.Position = position + (0.5, 0.5, 0.5)
        ref.TilePos = position  # 1.7/1.8 issues should be handled by ref...
        ref.Facing = facing

        log.info("Created map ItemFrame with ID %s, importing...", mapID)

        command = SimpleRevisionCommand(self, self.tr("Import map %(mapID)s") % {"mapID": mapID})
        with command.begin():
            chunk.addEntity(ref)
            log.info(nbt.nested_string(ref.rootTag))
        self.pushCommand(command)

    # --- Library support ---

    def importSchematic(self, filename, importPos=None):
        schematic = WorldEditor(filename, readonly=True)
        ray = self.editorTab.currentView().rayAtCenter()
        if importPos is not None:
            pos = importPos
        else:
            pos, face = rayCastInBounds(ray, self.currentDimension)
            if pos is None:
                pos = ray.point
            else:
                pos = pos + face.vector

        name = os.path.basename(filename)
        dim = schematic.getDimension()
        center = dim.bounds.center
        bottomCenter = pos - (center[0], 0, center[2])

        imp = PendingImport(schematic.getDimension(), bottomCenter, dim.bounds, name)
        command = PasteImportCommand(self, imp, "Import %s" % name)
        self.undoStack.push(command)

    # --- Undo support ---

    revisionChanged = QtCore.Signal(RevisionChanges)

    def undoIndexChanged(self, index):
        self.updateView()
        self.actionSave.setEnabled(index != self.lastSaveIndex)

    def pushCommand(self, command):
        log.info("Pushing command %s" % command.text())
        self.undoStack.push(command)
        self.actionSave.setEnabled(True)

    def setUndoBlock(self, callback):
        self.undoStack.setUndoBlock(callback)

    def removeUndoBlock(self, callback):
        self.undoStack.removeUndoBlock(callback)

    def beginUndo(self):
        self.undoStack.clearUndoBlock()
        self.dirty = True
        self.worldEditor.beginUndo()

    def commitUndo(self):
        exhaust(self.commitUndoIter())

    def commitUndoIter(self):
        for status in self.worldEditor.commitUndoIter():
            yield status
        changes = self.worldEditor.getRevisionChanges(self.currentRevision-1, self.currentRevision)
        self.revisionChanged.emit(changes)

    def undoForward(self):
        self.worldEditor.redo()
        changes = self.worldEditor.getRevisionChanges(self.currentRevision-1, self.currentRevision)
        self.revisionChanged.emit(changes)

    def undoBackward(self):
        self.worldEditor.undo()
        changes = self.worldEditor.getRevisionChanges(self.currentRevision, self.currentRevision+1)
        self.revisionChanged.emit(changes)

    def gotoRevision(self, index):
        if index != self.currentRevision:
            changes = self.worldEditor.getRevisionChanges(self.currentRevision, index)
            self.worldEditor.gotoRevision(index)
            self.revisionChanged.emit(changes)

    @property
    def currentRevision(self):
        return self.worldEditor.currentRevision

    # --- Misplaced startup code? ---

    def loadDone(self):
        # Called by MCEditApp after the view is on screen to make sure view.center() works correctly
        # xxx was needed because view.centerOnPoint used a depthbuffer read for that, now what?
        try:
            try:
                player = self.worldEditor.getPlayer()
                center = Vector(*player.Position) + (0, 1.8, 0)
                dimNo = player.Dimension
                dimName = self.worldEditor.dimNameFromNumber(dimNo)
                log.info("Setting view angle to single-player player's view in dimension %s.",
                         dimName)
                rotation = player.Rotation
                if dimName:
                    self.gotoDimension(dimName)
                try:
                    self.editorTab.currentView().yawPitch = rotation
                except AttributeError:
                    pass
            except PlayerNotFound:
                try:
                    center = self.worldEditor.getWorldMetadata().Spawn
                    log.info("Centering on spawn position.")
                except AttributeError:
                    log.info("Centering on world center")
                    center = self.currentDimension.bounds.origin + (self.currentDimension.bounds.size * 0.5)
            self.editorTab.miniMap.centerOnPoint(center)
            self.editorTab.currentView().centerOnPoint(center, distance=0)
        except Exception as e:
            log.exception("Error while centering on player for world editor: %s", e)

    # --- Tools ---

    def toolShortcut(self, name):
        toolShortcuts = {
            "Select": "S",
            "Create": "D",
        }
        return toolShortcuts.get(name, "")

    def getTool(self, name):
        for t in self.tools:
            if t.name == name:
                return t

    def chooseTool(self, name):
        oldTool = self.currentTool
        self.currentTool = self.getTool(name)
        if oldTool is not self.currentTool:
            if oldTool:
                oldTool.toolInactive()
            self.currentTool.toolActive()
            self.toolChanged.emit(self.currentTool)
        self.actionsByName[name].setChecked(True)

    toolChanged = QtCore.Signal(object)

    def chunkDidComplete(self):
        from mcedit2 import editorapp

        editorapp.MCEditApp.app.updateStatusLabel(None, None, None, self.loader.cps,
                                                  self.editorTab.currentView().fps)

    def updateStatusFromEvent(self, event):
        from mcedit2 import editorapp

        if event.blockPosition:
            id = self.currentDimension.getBlockID(*event.blockPosition)
            data = self.currentDimension.getBlockData(*event.blockPosition)
            block = self.worldEditor.blocktypes[id, data]
            biomeID = self.currentDimension.getBiomeID(event.blockPosition[0],
                                                       event.blockPosition[2])
            biome = self.biomeTypes.types.get(biomeID)
            if biome is not None:
                biomeName = biome.name
            else:
                biomeName = "Unknown biome"

            biomeText = "%s (%d)" % (biomeName, biomeID)
            editorapp.MCEditApp.app.updateStatusLabel(event.blockPosition, block, biomeText,
                                                      self.loader.cps, event.view.fps)
        else:
            editorapp.MCEditApp.app.updateStatusLabel('(N/A)', None, None, self.loader.cps,
                                                      event.view.fps)

    def viewMousePress(self, event):
        self.updateStatusFromEvent(event)
        if hasattr(self.currentTool, 'mousePress') and event.blockPosition is not None:
            self.currentTool.mousePress(event)
        self.updateView()

    def viewMouseMove(self, event):
        self.updateStatusFromEvent(event)
        if hasattr(self.currentTool, 'mouseMove'):
            self.currentTool.mouseMove(event)
        self.updateView()

    def viewMouseDrag(self, event):
        self.updateStatusFromEvent(event)
        if hasattr(self.currentTool, 'mouseDrag'):
            self.currentTool.mouseDrag(event)
        self.updateView()

    def viewMouseRelease(self, event):
        self.updateStatusFromEvent(event)
        if hasattr(self.currentTool, 'mouseRelease'):
            self.currentTool.mouseRelease(event)
        self.updateView()

    # --- EditorTab handling ---

    def tabCaption(self):
        return util.displayName(self.filename)

    def closeTab(self):
        if self.worldEditor is None:
            return True

        if self.dirty:
            msgBox = QtGui.QMessageBox(self.editorTab.window())
            msgBox.setText("The world has been modified.")
            msgBox.setInformativeText("Do you want to save your changes?")
            msgBox.setStandardButtons(
                QtGui.QMessageBox.Save | QtGui.QMessageBox.Discard | QtGui.QMessageBox.Cancel)
            msgBox.setDefaultButton(QtGui.QMessageBox.Save)
            ret = msgBox.exec_()

            if ret == QtGui.QMessageBox.Save:
                self.save()

            if ret == QtGui.QMessageBox.Cancel:
                return False

        for panel in self.panels:
            panel.close()
            panel.setParent(None)

        self.panels = None

        self.editorTab.saveState()
        self.worldEditor.close()
        self.worldEditor = None
        # Break all reference cycles just to be absolutely sure.
        d = {'menus': self.menus, 'undoStack': self.undoStack}
        self.__dict__.clear()
        self.__dict__.update(d)
        return True

    # --- Inspector ---

    def inspectBlock(self, pos):
        self.inspectorDockWidget.show()
        self.inspectorWidget.inspectBlock(pos)

    def inspectEntity(self, entity):
        self.inspectorDockWidget.show()
        self.inspectorWidget.inspectEntity(entity)

    def inspectChunk(self, cx, cz):
        self.inspectorDockWidget.show()
        self.inspectorWidget.inspectChunk(cx, cz)

    # --- Zooming ---

    def zoomAndInspectBlock(self, pos):
        self.zoomToPoint(pos)
        self.inspectBlock(pos)

    def zoomAndInspectEntity(self, entity):
        self.zoomToPoint(entity.Position)
        self.inspectEntity(entity)

    def zoomToPoint(self, point):
        self.editorTab.currentView().centerOnPoint(point, 15)

    # --- Blocktype handling ---

    def unknownBlocks(self):
        for blocktype in self.worldEditor.blocktypes:
            if blocktype.unknown:
                yield blocktype.internalName


class EditorTab(QtGui.QWidget):
    def __init__(self, editorSession):
        """
        EditorTab is the widget containing the editor viewports, the minimap, and
        the settings panel for the currently selected tool and its dockwidget.

        :type editorSession: mcedit2.editorsession.EditorSession
        :rtype: EditorTab
        """

        QtGui.QWidget.__init__(self)
        self.setContentsMargins(0, 0, 0, 0)

        self.editorSession = editorSession
        self.editorSession.dimensionChanged.connect(self.dimensionDidChange)
        self.debugLastCenters = []

        self.viewButtonGroup = QtGui.QButtonGroup(self)
        self.viewButtonToolbar = QtGui.QToolBar()
        self.viewButtons = {}
        self.views = []

        for name, handler in (
                ("2D", self.showCutawayView),
                ("Over", self.showOverheadView),
                # ("Iso", self.showIsoView),
                ("Cam", self.showCameraView),
                # ("4-up", self.showFourUpView),
        ):
            button = QtGui.QToolButton(text=name, checkable=True)
            button.clicked.connect(handler)
            self.viewButtonGroup.addButton(button)
            self.viewButtonToolbar.addWidget(button)
            self.viewButtons[name] = button

        self.viewStack = QtGui.QStackedWidget()

        self.miniMap = MinimapWorldView(editorSession.currentDimension, editorSession.textureAtlas, editorSession.geometryCache)
        self.miniMapDockWidget = MCEDockWidget("Minimap", objectName="MinimapWidget", floating=True)
        self.miniMapDockWidget.setWidget(self.miniMap)
        self.miniMapDockWidget.setFixedSize(256, 256)
        self.miniMapDockWidget.setUnfocusedOpacity(0.9)

        self.views.append(self.miniMap)

        self.toolOptionsArea = QtGui.QScrollArea()
        self.toolOptionsArea.setWidgetResizable(True)

        self.toolOptionsDockWidget = MCEDockWidget("Tool Options", objectName="ToolOptionsWidget", floating=True)
        self.toolOptionsDockWidget.setWidget(self.toolOptionsArea)
        self.toolOptionsDockWidget.setUnfocusedOpacity(0.8)

        editorSession.dockWidgets.append((Qt.LeftDockWidgetArea, self.miniMapDockWidget))
        editorSession.dockWidgets.append((Qt.LeftDockWidgetArea, self.toolOptionsDockWidget))

        editorSession.loader.addClient(self.miniMap)

        self.overheadViewFrame = OverheadWorldViewFrame(editorSession.currentDimension, editorSession.textureAtlas, editorSession.geometryCache, self.miniMap)
        self.overheadViewFrame.worldView.viewID = "Over"
        self._addView(self.overheadViewFrame)

        self.cutawayViewFrame = CutawayWorldViewFrame(editorSession.currentDimension, editorSession.textureAtlas, editorSession.geometryCache, self.miniMap)
        self.cutawayViewFrame.worldView.viewID = "2D"
        self._addView(self.cutawayViewFrame)
        #
        # self.fourUpViewFrame = FourUpWorldViewFrame(editorSession.currentDimension, editorSession.textureAtlas, editorSession.geometryCache, self.miniMap)
        # self.fourUpViewFrame.worldView.viewID = "4-up"
        # self._addView(self.fourUpViewFrame)

        self.cameraViewFrame = CameraWorldViewFrame(editorSession.currentDimension, editorSession.textureAtlas, editorSession.geometryCache, self.miniMap)
        self.cameraViewFrame.worldView.viewID = "Cam"
        self.cameraView = self.cameraViewFrame.worldView
        self._addView(self.cameraViewFrame)

        self.viewStack.currentChanged.connect(self._viewChanged)
        self.viewChanged.connect(self.viewDidChange)

        self.setLayout(Column(self.viewButtonToolbar,
                              Row(self.viewStack, margin=0), margin=0))

        currentViewName = currentViewSetting.value()
        if currentViewName not in self.viewButtons:
            currentViewName = "Cam"
        self.viewButtons[currentViewName].click()

        self.dayTimeInput = SpinSlider(double=True,
                                       minimum=0.0, maximum=1.0, value=1.0)

        self.gammaInput = SpinSlider(double=True,
                                     minimum=0.0, maximum=3.0, value=1.0)

        self.dayTimeInput.valueChanged.connect(self.setDayTime)
        self.gammaInput.valueChanged.connect(self.setGamma)

        self.viewButtonToolbar.addSeparator()
        self.viewButtonToolbar.addWidget(QtGui.QLabel("Time of day:"))
        self.viewButtonToolbar.addWidget(self.dayTimeInput)

        self.viewButtonToolbar.addSeparator()
        self.viewButtonToolbar.addWidget(QtGui.QLabel("Brightness:"))
        self.viewButtonToolbar.addWidget(self.gammaInput)

        spacer = QtGui.QWidget()
        spacer.setSizePolicy(QtGui.QSizePolicy.Expanding, QtGui.QSizePolicy.Fixed)
        self.viewButtonToolbar.addWidget(spacer)

    def destroy(self):
        self.editorSession = None
        for view in self.views:
            view.destroy()

        super(EditorTab, self).destroy()

    def setDayTime(self, value):
        if self.editorSession.textureAtlas:
            self.editorSession.textureAtlas.dayTime = value
            for view in self.views:
                view.setDayTime(value)
            self.currentView().update()

    def setGamma(self, value):
        if self.editorSession.textureAtlas:
            self.editorSession.textureAtlas.gamma = value
            self.currentView().update()

    editorSession = weakrefprop()

    urlsDropped = QtCore.Signal(QtCore.QMimeData, Vector, faces.Face)
    mapItemDropped = QtCore.Signal(QtCore.QMimeData, Vector, faces.Face)

    def dimensionDidChange(self, dim):
        for view in self.views:
            view.setDimension(dim)
        # EditorSession has a new loader now, so re-add minimap and current view

        self.editorSession.loader.addClient(self.miniMap)
        view = self.currentView()
        if view is not None:
            self.editorSession.loader.addClient(view)


    def toolDidChange(self, tool):
        if tool.toolWidget:
            self.toolOptionsArea.takeWidget()  # setWidget gives ownership to the scroll area
            self.toolOptionsArea.setWidget(tool.toolWidget)
            self.toolOptionsDockWidget.setWindowTitle(self.tr(tool.name) + self.tr(" Tool Options"))
        log.info("Setting cursor %r for tool %r on view %r", tool.cursorNode, tool,
                 self.currentView())
        self.currentView().setToolCursor(tool.cursorNode)

    def saveState(self):
        pass

    viewChanged = QtCore.Signal(object)

    def _viewChanged(self, index):
        self.viewChanged.emit(self.currentView())

    def viewDidChange(self, view):
        self.miniMap.centerOnPoint(view.viewCenter())
        if self.editorSession.currentTool:
            view.setToolCursor(self.editorSession.currentTool.cursorNode)

        overlayNodes = [tool.overlayNode
                        for tool in self.editorSession.tools
                        if tool.overlayNode is not None]

        overlayNodes.insert(0, self.editorSession.editorOverlay)
        view.setToolOverlays(overlayNodes)
        view.setFocus()

    def viewOffsetChanged(self, view):
        self.miniMap.centerOnPoint(view.viewCenter())
        self.miniMap.currentViewMatrixChanged(view)

    def _addView(self, frame):
        self.views.append(frame.worldView)
        frame.stackIndex = self.viewStack.addWidget(frame)
        frame.worldView.viewportMoved.connect(self.viewOffsetChanged)
        frame.worldView.viewActions.extend([
            UseToolMouseAction(self),
            TrackingMouseAction(self)
        ])
        frame.worldView.urlsDropped.connect(self.urlsDropped.emit)
        frame.worldView.mapItemDropped.connect(self.mapItemDropped.emit)

    def currentView(self):
        """

        :rtype: mcedit2.worldview.worldview.WorldView
        """
        widget = self.viewStack.currentWidget()
        if widget is None:
            return None
        return widget.worldView

    def showViewFrame(self, frame):
        center = self.currentView().viewCenter()
        self.debugLastCenters.append(center)
        log.info("Going from %s to %s: Center was %s", self.currentView(), frame.worldView, center)

        self.editorSession.loader.removeClient(self.currentView())
        self.editorSession.loader.addClient(frame.worldView, 0)
        self.viewStack.setCurrentIndex(frame.stackIndex)

        frame.worldView.centerOnPoint(center)

        log.info("Center is now %s", self.currentView().viewCenter())

    def showOverheadView(self):
        self.showViewFrame(self.overheadViewFrame)

    #
    # def showIsoView(self):
    #     self.showViewFrame(self.isoViewFrame)
    #
    # def showFourUpView(self):
    #     self.showViewFrame(self.fourUpViewFrame)

    def showCutawayView(self):
        self.showViewFrame(self.cutawayViewFrame)

    def showCameraView(self):
        self.showViewFrame(self.cameraViewFrame)
