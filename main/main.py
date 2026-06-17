import json
import logging
import os
from typing import Any

import vtk

import qt
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper

from slicer import vtkMRMLScalarVolumeNode


#
# main
#


class main(ScriptedLoadableModule):
    """3D Slicer scripted module for detection box visualization."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Detection Viewer")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Examples")]
        self.parent.dependencies = []
        self.parent.contributors = ["DetectionViewer contributors"]
        self.parent.helpText = _("""Visualize detection boxes from JSON files on a loaded CT volume.""")
        self.parent.acknowledgementText = _("""This module was created as a 3D Slicer extension.""")


#
# mainParameterNode
#


@parameterNodeWrapper
class mainParameterNode:
    """Parameters stored with the Slicer scene."""

    inputVolume: vtkMRMLScalarVolumeNode


#
# mainWidget
#


class mainWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Widget for loading and displaying detection boxes."""

    def __init__(self, parent=None) -> None:
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None
        self._parameterNodeObserved = False
        self._updatingLocateIndexOptions = False
        self._updatingAnnotationOptions = False
        self._updatingAnnotationEditor = False
        self._observedAnnotationNodeIds = set()
        self._autoRefreshingDetectionBoxes = False
        self._loadedAnnotationPath = None
        self._sceneClosing = False

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/main.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = mainLogic()
        self.logic.clearAnnotationLabelNodes()

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        self.ui.loadVolumeButton.connect("clicked(bool)", self.onLoadVolumeButton)
        self.ui.browseDetectionButton.connect("clicked(bool)", self.onBrowseDetectionButton)
        self.ui.inputSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onInputVolumeChanged)
        self.ui.detectionPathLineEdit.connect("textChanged(QString)", self.autoRefreshDetectionBoxes)
        self.ui.minScoreSpinBox.connect("valueChanged(double)", self.autoRefreshDetectionBoxes)
        self.ui.maxDetectionsSpinBox.connect("valueChanged(int)", self.autoRefreshDetectionBoxes)
        self.ui.detectLpsRadioButton.connect("toggled(bool)", self.autoRefreshDetectionBoxes)
        self.ui.detectRasRadioButton.connect("toggled(bool)", self.autoRefreshDetectionBoxes)
        self.ui.refreshDisplayButton.connect("clicked(bool)", self.onRefreshDisplayButton)
        self.ui.locateIndexComboBox.connect("currentIndexChanged(int)", self.onLocateIndexChanged)
        self.ui.previousBoxButton.connect("clicked(bool)", self.onPreviousBoxButton)
        self.ui.nextBoxButton.connect("clicked(bool)", self.onNextBoxButton)
        self.ui.showDetectionBoxesCheckBox.connect("toggled(bool)", self.onShowDetectionBoxesChanged)
        self.ui.locateAutoFovCheckBox.connect("toggled(bool)", self.onLocateFovControlChanged)
        self.ui.locateFovZoomSpinBox.connect("valueChanged(double)", self.onLocateFovControlChanged)
        self.ui.copyViewedDetectionButton.connect("clicked(bool)", self.onAddSelectedDetectionButton)
        self.ui.annotationSelectorComboBox.connect("currentIndexChanged(int)", self.onAnnotationSelectionChanged)
        self.ui.addAnnotationButton.connect("clicked(bool)", self.onAddEmptyAnnotationButton)
        self.ui.updateAnnotationButton.connect("clicked(bool)", self.onUpdateAnnotationButton)
        self.ui.deleteAnnotationButton.connect("clicked(bool)", self.onDeleteAnnotationButton)
        self.ui.saveAnnotationsButton.connect("clicked(bool)", self.onSaveAnnotationsButton)
        self.setupLocateInfoTable()
        self.setupAnnotationInfoTable()
        self.refreshAnnotationOptions()

        self.initializeParameterNode()

    def cleanup(self) -> None:
        self.removeObservers()
        self._observedAnnotationNodeIds.clear()
        self._parameterNodeObserved = False

    def enter(self) -> None:
        self.initializeParameterNode()

    def exit(self) -> None:
        if self._parameterNode:
            if self._parameterNodeGuiTag is not None:
                self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None
            self.removeParameterNodeObserver()

    def onSceneStartClose(self, caller, event) -> None:
        self._sceneClosing = True
        self._observedAnnotationNodeIds.clear()
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        self._sceneClosing = False
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        self.setParameterNode(self.logic.getParameterNode())

        if not self._parameterNode.inputVolume:
            firstVolumeNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
            if firstVolumeNode:
                self._parameterNode.inputVolume = firstVolumeNode

    def setParameterNode(self, inputParameterNode: mainParameterNode | None) -> None:
        if self._parameterNode:
            if self._parameterNodeGuiTag is not None:
                self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self.removeParameterNodeObserver()

        self._parameterNode = inputParameterNode

        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
            self._parameterNodeObserved = True
            self._checkCanApply()

    def removeParameterNodeObserver(self) -> None:
        if self._parameterNode and self._parameterNodeObserved:
            if self.hasObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply):
                self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
            self._parameterNodeObserved = False

    def _checkCanApply(self, caller=None, event=None) -> None:
        self.autoRefreshDetectionBoxes()

    def onBrowseDetectionButton(self) -> None:
        startPath = self.ui.detectionPathLineEdit.text.strip()
        if not startPath:
            startPath = os.path.expanduser("~")
        filePath = qt.QFileDialog.getOpenFileName(
            self.parent,
            _("Select detection JSON"),
            startPath,
            _("JSON files (*.json);;All files (*)"),
        )
        if isinstance(filePath, tuple):
            filePath = filePath[0]
        if filePath:
            self.ui.detectionPathLineEdit.text = filePath

    def onLoadVolumeButton(self) -> None:
        filePath = qt.QFileDialog.getOpenFileName(
            self.parent,
            _("Select image volume"),
            os.path.expanduser("~"),
            _("Image volumes (*.nii *.nii.gz *.nrrd *.mha *.mhd);;All files (*)"),
        )
        if isinstance(filePath, tuple):
            filePath = filePath[0]
        if not filePath:
            return

        with slicer.util.tryWithErrorDisplay(_("Failed to load image volume."), waitCursor=True):
            volumeNode = self.logic.loadVolume(filePath)
            self._parameterNode.inputVolume = volumeNode
            self.ui.inputSelector.setCurrentNode(volumeNode)
            detectionPath = self.logic.findDetectionJsonNextToVolume(filePath)
            if detectionPath:
                self.ui.detectionPathLineEdit.text = detectionPath
            else:
                self.ui.detectionPathLineEdit.text = ""
                self.ui.statusLabel.text = _("Loaded volume; no detection JSON found in the same directory")
                self.autoRefreshDetectionBoxes()

    def onInputVolumeChanged(self, currentNode=None) -> None:
        if self._autoRefreshingDetectionBoxes or self._sceneClosing:
            return

        inputVolume = self.ui.inputSelector.currentNode()
        if not inputVolume:
            self.ui.detectionPathLineEdit.text = ""
            self.autoRefreshDetectionBoxes()
            return

        detectionPath = self.logic.findDetectionJsonForVolumeNode(inputVolume)
        if detectionPath:
            if self.ui.detectionPathLineEdit.text == detectionPath:
                self.autoRefreshDetectionBoxes()
            else:
                self.ui.detectionPathLineEdit.text = detectionPath
        else:
            self.ui.detectionPathLineEdit.text = ""
            self.autoRefreshDetectionBoxes()

    def onRefreshDisplayButton(self) -> None:
        self._loadedAnnotationPath = None
        self.autoRefreshDetectionBoxes()

    def autoRefreshDetectionBoxes(self, caller=None, event=None) -> None:
        if self._autoRefreshingDetectionBoxes or self._sceneClosing:
            return

        self._autoRefreshingDetectionBoxes = True
        try:
            inputVolume = self.ui.inputSelector.currentNode()
            detectionPath = self.ui.detectionPathLineEdit.text.strip()
            if inputVolume and (not detectionPath or not os.path.isfile(detectionPath)):
                detectedPath = self.logic.findDetectionJsonForVolumeNode(inputVolume)
                if detectedPath:
                    self.ui.detectionPathLineEdit.text = detectedPath
                    detectionPath = detectedPath

            if not inputVolume or not os.path.isfile(detectionPath):
                self.logic.clearDetectionBoxes()
                self.unobserveAnnotationNodes()
                self.logic.clearAnnotations()
                self._loadedAnnotationPath = None
                self.refreshAnnotationOptions()
                self.refreshLocateIndexOptions([])
                self.clearLocateInfoTable()
                if not inputVolume:
                    self.ui.statusLabel.text = _("Select an input volume")
                elif detectionPath:
                    self.ui.statusLabel.text = _("Detection JSON not found")
                else:
                    self.ui.statusLabel.text = _("Select a detection JSON")
                return

            maxDetections = int(self.ui.maxDetectionsSpinBox.value)
            minScore = float(self.ui.minScoreSpinBox.value)
            createdNodes = self.logic.createDetectionBoxes(
                detectionPath,
                inputVolume,
                minScore=minScore,
                maxDetections=maxDetections if maxDetections > 0 else None,
                lpsToRas=self.ui.detectLpsRadioButton.checked,
            )
            self.ui.statusLabel.text = _("Displayed {0} detection boxes").format(len(createdNodes))
            self.logic.setDetectionBoxesVisible(self.ui.showDetectionBoxesCheckBox.checked)
            self.refreshLocateIndexOptions(createdNodes)
            self.clearLocateInfoTable()
            if self._loadedAnnotationPath != os.path.abspath(detectionPath):
                self.unobserveAnnotationNodes()
                annotationNodes = self.logic.loadAnnotationsFromDetectionPath(detectionPath)
                self._loadedAnnotationPath = os.path.abspath(detectionPath)
                self.refreshAnnotationOptions(annotationNodes[0] if annotationNodes else None)
                if annotationNodes:
                    self.ui.statusLabel.text = _("Displayed {0} detection boxes; loaded {1} annotations").format(
                        len(createdNodes),
                        len(annotationNodes),
                    )
        except Exception as exc:
            logging.exception("Failed to auto-refresh detection boxes")
            self.logic.clearDetectionBoxes()
            self.refreshLocateIndexOptions([])
            self.clearLocateInfoTable()
            self.ui.statusLabel.text = _("Failed to display detection boxes: {0}").format(exc)
        finally:
            self._autoRefreshingDetectionBoxes = False

    def onLocateIndexChanged(self, index: int) -> None:
        if self._updatingLocateIndexOptions or index < 0:
            return
        self.locateSelectedBox()

    def onPreviousBoxButton(self) -> None:
        count = self.ui.locateIndexComboBox.count
        if count <= 1:
            self.ui.statusLabel.text = _("No detection boxes are displayed")
            return
        currentIndex = self.ui.locateIndexComboBox.currentIndex
        nextIndex = count - 1 if currentIndex <= 1 else currentIndex - 1
        self._updatingLocateIndexOptions = True
        self.ui.locateIndexComboBox.setCurrentIndex(nextIndex)
        self._updatingLocateIndexOptions = False
        self.locateSelectedBox()

    def onNextBoxButton(self) -> None:
        count = self.ui.locateIndexComboBox.count
        if count <= 1:
            self.ui.statusLabel.text = _("No detection boxes are displayed")
            return
        currentIndex = self.ui.locateIndexComboBox.currentIndex
        nextIndex = 1 if currentIndex <= 0 or currentIndex >= count - 1 else currentIndex + 1
        self._updatingLocateIndexOptions = True
        self.ui.locateIndexComboBox.setCurrentIndex(nextIndex)
        self._updatingLocateIndexOptions = False
        self.locateSelectedBox()

    def locateSelectedBox(self) -> None:
        currentText = self.ui.locateIndexComboBox.currentText.strip()
        if currentText == "":
            self.logic.clearDetectionHighlight()
            self.clearLocateInfoTable()
            self.ui.statusLabel.text = _("No detection box selected for view")
            return

        if self.ui.locateIndexComboBox.count <= 1:
            self.ui.statusLabel.text = _("No detection boxes are displayed")
            return

        detectionIndex = int(currentText)
        boxNode = self.logic.findDetectionBoxByIndex(detectionIndex)
        if boxNode is None:
            self.ui.statusLabel.text = _("Detection index {0} is not displayed").format(detectionIndex)
            return

        self.logic.centerViewsOnBoxes(
            [boxNode],
            fitToBounds=self.ui.locateAutoFovCheckBox.checked,
            fovZoomFactor=float(self.ui.locateFovZoomSpinBox.value),
        )
        self.logic.highlightDetectionBox(boxNode)
        self.setLocateInfoRows(self.logic.detectionBoxInfoRows(boxNode))
        self.ui.statusLabel.text = _("Viewing detection index {0}").format(detectionIndex)

    def onLocateFovControlChanged(self, value=None) -> None:
        if self.ui.locateIndexComboBox.currentText.strip():
            self.locateSelectedBox()

    def onShowDetectionBoxesChanged(self, checked: bool) -> None:
        self.logic.setDetectionBoxesVisible(checked)

    def setupLocateInfoTable(self) -> None:
        table = self.ui.locateInfoTable
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Field", "Value"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)

    def clearLocateInfoTable(self) -> None:
        self.ui.locateInfoTable.setRowCount(0)

    def setLocateInfoRows(self, rows: list[tuple[str, str]]) -> None:
        table = self.ui.locateInfoTable
        table.setRowCount(len(rows))
        for rowIndex, (field, value) in enumerate(rows):
            table.setItem(rowIndex, 0, qt.QTableWidgetItem(field))
            table.setItem(rowIndex, 1, qt.QTableWidgetItem(value))
        table.resizeRowsToContents()

    def refreshLocateIndexOptions(self, boxNodes) -> None:
        indexes = sorted(
            {
                int(node.GetAttribute("DetectionViewer.Index"))
                for node in boxNodes
                if node.GetAttribute("DetectionViewer.Index") is not None
            }
        )

        self._updatingLocateIndexOptions = True
        try:
            self.ui.locateIndexComboBox.clear()
            self.ui.locateIndexComboBox.addItem("")
            for detectionIndex in indexes:
                self.ui.locateIndexComboBox.addItem(str(detectionIndex))
        finally:
            self._updatingLocateIndexOptions = False

    def onAnnotationSelectionChanged(self, index: int) -> None:
        if self._updatingAnnotationOptions or index < 0:
            return
        annotationNode = self.selectedAnnotationNode()
        if annotationNode is not None:
            self.logic.clearDetectionHighlight()
        self.logic.setSelectedAnnotationHandles(annotationNode)
        self.setAnnotationEditorFromNode(annotationNode)

    def onAddSelectedDetectionButton(self) -> None:
        currentText = self.ui.locateIndexComboBox.currentText.strip()
        if not currentText:
            self.ui.statusLabel.text = _("Select a detection index in View first")
            return

        detectionNode = self.logic.findDetectionBoxByIndex(int(currentText))
        if detectionNode is None:
            self.ui.statusLabel.text = _("Selected detection is not displayed")
            return

        annotationNode = self.logic.createAnnotationFromDetectionNode(detectionNode, self.annotationEditorLabel())
        self.refreshAnnotationOptions(annotationNode)
        self.setAnnotationEditorFromNode(annotationNode)
        self.ui.statusLabel.text = _("Copied detection {0} to annotations").format(currentText)

    def onAddEmptyAnnotationButton(self) -> None:
        center = self.logic.currentSliceCenterRAS()
        if center is None:
            center = self.logic.volumeCenterRAS(self.ui.inputSelector.currentNode())
        if center is None:
            self.ui.statusLabel.text = _("No valid view or volume for adding annotation")
            return

        annotationNode = self.logic.createEmptyAnnotation(center, self.annotationEditorLabel())
        self.refreshAnnotationOptions(annotationNode)
        self.setAnnotationEditorFromNode(annotationNode)
        self.ui.statusLabel.text = _("Added empty annotation")

    def onUpdateAnnotationButton(self) -> None:
        annotationNode = self.selectedAnnotationNode()
        if annotationNode is None:
            self.ui.statusLabel.text = _("Select an annotation first")
            return

        self.logic.updateAnnotationNode(annotationNode, self.annotationEditorLabel())
        self.refreshAnnotationOptions(annotationNode)
        self.setAnnotationEditorFromNode(annotationNode)
        self.ui.statusLabel.text = _("Updated annotation")

    def onDeleteAnnotationButton(self) -> None:
        annotationNode = self.selectedAnnotationNode()
        if annotationNode is None:
            self.ui.statusLabel.text = _("Select an annotation first")
            return

        self.unobserveAnnotationNode(annotationNode)
        self.logic.removeAnnotationNode(annotationNode)
        self.refreshAnnotationOptions()
        self.ui.statusLabel.text = _("Deleted annotation")

    def onSaveAnnotationsButton(self) -> None:
        detectionPath = self.ui.detectionPathLineEdit.text.strip()
        if not os.path.isfile(detectionPath):
            self.ui.statusLabel.text = _("Select a detection JSON first")
            return

        if self.logic.detectionJsonHasAnnotation(detectionPath):
            answer = qt.QMessageBox.question(
                self.parent,
                _("Overwrite annotations"),
                _("This detection JSON already contains annotation. Overwrite it?"),
                qt.QMessageBox.Yes | qt.QMessageBox.No,
                qt.QMessageBox.No,
            )
            if answer != qt.QMessageBox.Yes:
                self.ui.statusLabel.text = _("Annotation save canceled")
                return

        with slicer.util.tryWithErrorDisplay(_("Failed to save annotations."), waitCursor=True):
            count = self.logic.saveAnnotationsToDetectionJson(detectionPath)
            self._loadedAnnotationPath = os.path.abspath(detectionPath)
            self.ui.statusLabel.text = _("Saved {0} annotations to detection JSON").format(count)

    def annotationEditorLabel(self) -> str:
        label = self.ui.annotationLabelLineEdit.text.strip()
        return label if label else "lesion"

    def selectedAnnotationNode(self):
        index = self.ui.annotationSelectorComboBox.currentIndex
        if index < 0:
            return None
        nodeId = self.ui.annotationSelectorComboBox.itemData(index)
        if not nodeId:
            return None
        return slicer.mrmlScene.GetNodeByID(str(nodeId))

    def refreshAnnotationOptions(self, selectedNode=None) -> None:
        selectedNodeId = selectedNode.GetID() if selectedNode is not None else None
        if selectedNodeId is None and self.ui.annotationSelectorComboBox.currentIndex >= 0:
            currentData = self.ui.annotationSelectorComboBox.itemData(self.ui.annotationSelectorComboBox.currentIndex)
            selectedNodeId = str(currentData) if currentData else None

        annotationNodes = self.logic.annotationNodes()
        self._updatingAnnotationOptions = True
        try:
            self.ui.annotationSelectorComboBox.clear()
            self.ui.annotationSelectorComboBox.addItem("")
            for annotationNode in annotationNodes:
                self.ui.annotationSelectorComboBox.addItem(self.logic.annotationDisplayName(annotationNode))
                self.ui.annotationSelectorComboBox.setItemData(
                    self.ui.annotationSelectorComboBox.count - 1,
                    annotationNode.GetID(),
                )

            if selectedNodeId:
                for index in range(self.ui.annotationSelectorComboBox.count):
                    if str(self.ui.annotationSelectorComboBox.itemData(index)) == selectedNodeId:
                        self.ui.annotationSelectorComboBox.setCurrentIndex(index)
                        break
        finally:
            self._updatingAnnotationOptions = False

        selectedAnnotationNode = self.selectedAnnotationNode()
        if selectedAnnotationNode is not None:
            self.logic.clearDetectionHighlight()
        self.logic.setSelectedAnnotationHandles(selectedAnnotationNode)
        self.setAnnotationEditorFromNode(selectedAnnotationNode)

    def setAnnotationEditorFromNode(self, annotationNode) -> None:
        if annotationNode is None:
            self.clearAnnotationInfoTable()
            return
        self._updatingAnnotationEditor = True
        try:
            annotationNodeId = annotationNode.GetID()
            if annotationNodeId and annotationNodeId not in self._observedAnnotationNodeIds:
                self.addObserver(annotationNode, vtk.vtkCommand.ModifiedEvent, self.onSelectedAnnotationModified)
                self._observedAnnotationNodeIds.add(annotationNodeId)
        except Exception:
            logging.debug("Could not observe selected annotation node", exc_info=True)
        try:
            label = annotationNode.GetAttribute("DetectionViewer.AnnotationLabel") or "lesion"
            self.ui.annotationLabelLineEdit.text = label
            self.setAnnotationInfoRows(self.logic.annotationInfoRows(annotationNode))
        finally:
            self._updatingAnnotationEditor = False

    def onSelectedAnnotationModified(self, caller=None, event=None) -> None:
        if self._updatingAnnotationEditor or caller != self.selectedAnnotationNode():
            return
        self.logic.setSelectedAnnotationHandles(caller)
        self.setAnnotationEditorFromNode(caller)

    def setupAnnotationInfoTable(self) -> None:
        table = self.ui.annotationInfoTable
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Field", "Value"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)

    def clearAnnotationInfoTable(self) -> None:
        self.ui.annotationInfoTable.setRowCount(0)

    def setAnnotationInfoRows(self, rows: list[tuple[str, str]]) -> None:
        table = self.ui.annotationInfoTable
        table.setRowCount(len(rows))
        for rowIndex, (field, value) in enumerate(rows):
            table.setItem(rowIndex, 0, qt.QTableWidgetItem(field))
            table.setItem(rowIndex, 1, qt.QTableWidgetItem(value))
        table.resizeRowsToContents()

    def unobserveAnnotationNode(self, annotationNode) -> None:
        annotationNodeId = annotationNode.GetID()
        if annotationNodeId not in self._observedAnnotationNodeIds:
            return
        try:
            if self.hasObserver(annotationNode, vtk.vtkCommand.ModifiedEvent, self.onSelectedAnnotationModified):
                self.removeObserver(annotationNode, vtk.vtkCommand.ModifiedEvent, self.onSelectedAnnotationModified)
        except Exception:
            logging.debug("Could not remove annotation observer", exc_info=True)
        self._observedAnnotationNodeIds.discard(annotationNodeId)

    def unobserveAnnotationNodes(self) -> None:
        for annotationNode in self.logic.annotationNodes():
            self.unobserveAnnotationNode(annotationNode)


#
# mainLogic
#


class mainLogic(ScriptedLoadableModuleLogic):
    """Logic for reading detections and creating MRML model box nodes."""

    GENERATED_BOX_ATTRIBUTE = "DetectionViewer.GeneratedBox"
    ANNOTATION_BOX_ATTRIBUTE = "DetectionViewer.AnnotationBox"
    ANNOTATION_LABEL_ATTRIBUTE = "DetectionViewer.AnnotationLabelNode"
    ANNOTATION_LABEL_FOR_ATTRIBUTE = "DetectionViewer.AnnotationLabelFor"

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return mainParameterNode(super().getParameterNode())

    def defaultTestCasePath(self) -> str:
        modulePath = os.path.abspath(os.path.dirname(__file__))
        candidatePaths = [
            os.path.join(modulePath, "..", "test", "00016-0800237946"),
            os.path.join(modulePath, "..", "..", "test", "00016-0800237946"),
        ]
        for candidatePath in candidatePaths:
            normalizedPath = os.path.abspath(candidatePath)
            if os.path.isdir(normalizedPath):
                return normalizedPath
        return os.path.abspath(candidatePaths[-1])

    def findTestData(self) -> tuple[str, str]:
        testCasePath = self.defaultTestCasePath()
        if not os.path.isdir(testCasePath):
            raise FileNotFoundError(f"Test data directory not found: {testCasePath}")

        detectionPath = os.path.join(testCasePath, "detection.json")
        if not os.path.isfile(detectionPath):
            raise FileNotFoundError(f"Detection JSON not found: {detectionPath}")

        volumeCandidates = [
            os.path.join(testCasePath, fileName)
            for fileName in os.listdir(testCasePath)
            if fileName.lower().endswith((".nii", ".nii.gz", ".nrrd", ".mha", ".mhd"))
        ]
        if not volumeCandidates:
            raise FileNotFoundError(f"No supported volume file found in: {testCasePath}")

        return volumeCandidates[0], detectionPath

    def loadVolume(self, volumePath: str) -> vtkMRMLScalarVolumeNode:
        if not os.path.isfile(volumePath):
            raise FileNotFoundError(f"Volume file not found: {volumePath}")

        volumeNode = slicer.util.loadVolume(volumePath)
        if isinstance(volumeNode, bool):
            loaded, volumeNode = slicer.util.loadVolume(volumePath, returnNode=True)
            if not loaded:
                volumeNode = None
        if volumeNode is None:
            raise RuntimeError(f"Failed to load volume: {volumePath}")
        return volumeNode

    def findDetectionJsonNextToVolume(self, volumePath: str) -> str | None:
        volumeDirectory = os.path.dirname(os.path.abspath(volumePath))
        preferredPath = os.path.join(volumeDirectory, "detection.json")
        if os.path.isfile(preferredPath):
            return preferredPath

        jsonPaths = [
            os.path.join(volumeDirectory, fileName)
            for fileName in os.listdir(volumeDirectory)
            if fileName.lower().endswith(".json")
        ]
        if len(jsonPaths) == 1:
            return jsonPaths[0]
        return None

    def findDetectionJsonForVolumeNode(self, volumeNode: vtkMRMLScalarVolumeNode) -> str | None:
        storageNode = volumeNode.GetStorageNode()
        if storageNode is None:
            return None
        volumePath = storageNode.GetFileName()
        if not volumePath:
            return None
        return self.findDetectionJsonNextToVolume(volumePath)

    def readDetectionData(self, detectionPath: str) -> dict[str, Any]:
        if not os.path.isfile(detectionPath):
            raise FileNotFoundError(f"Detection JSON not found: {detectionPath}")
        with open(detectionPath, "r", encoding="utf-8-sig") as detectionFile:
            return json.load(detectionFile)

    def detectionsFromData(
        self,
        detectionData: dict[str, Any],
        minScore: float = 0.0,
        maxDetections: int | None = None,
    ) -> list[dict[str, Any]]:
        detections = detectionData.get("raw_detections")
        if detections is None:
            detections = detectionData.get("detections", [])

        filteredDetections = [
            detection
            for detection in detections
            if float(detection.get("score", 1.0)) >= minScore
        ]
        if maxDetections is not None:
            filteredDetections = filteredDetections[:maxDetections]
        return filteredDetections

    def createDetectionBoxes(
        self,
        detectionPath: str,
        inputVolume: vtkMRMLScalarVolumeNode | None = None,
        minScore: float = 0.0,
        maxDetections: int | None = None,
        lpsToRas: bool = False,
    ) -> list[vtk.vtkObject]:
        detectionData = self.readDetectionData(detectionPath)
        detections = self.detectionsFromData(detectionData, minScore, maxDetections)

        self.clearDetectionBoxes()

        createdNodes = []
        for displayIndex, detection in enumerate(detections, start=1):
            bounds = self.detectionBoundsRAS(detection, inputVolume=inputVolume, lpsToRas=lpsToRas)
            boxNode = self.createBoxNode(bounds, detection, displayIndex)
            createdNodes.append(boxNode)

        logging.info("Created %d detection box nodes from %s", len(createdNodes), detectionPath)
        return createdNodes

    def detectionBoundsRAS(
        self,
        detection: dict[str, Any],
        inputVolume: vtkMRMLScalarVolumeNode | None = None,
        lpsToRas: bool = False,
    ) -> tuple[float, float, float, float, float, float]:
        if "box_xyzxyz_world" in detection:
            bounds = self.boundsFromXyzxyz(detection["box_xyzxyz_world"])
            return self.boundsLpsToRas(bounds) if lpsToRas else bounds

        if "box_cccwhd_world" in detection:
            bounds = self.boundsFromCccwhd(detection["box_cccwhd_world"])
            return self.boundsLpsToRas(bounds) if lpsToRas else bounds

        if "box_xyzxyz_ijk" in detection:
            if inputVolume is None:
                raise ValueError("IJK detections require an input volume")
            return self.ijkBoundsToRas(self.boundsFromXyzxyz(detection["box_xyzxyz_ijk"]), inputVolume)

        if "box_cccwhd_ijk" in detection:
            if inputVolume is None:
                raise ValueError("IJK detections require an input volume")
            return self.ijkBoundsToRas(self.boundsFromCccwhd(detection["box_cccwhd_ijk"]), inputVolume)

        raise ValueError(f"Detection is missing a supported box field: {detection}")

    def boundsFromXyzxyz(self, values: list[float]) -> tuple[float, float, float, float, float, float]:
        if len(values) != 6:
            raise ValueError(f"Expected 6 box values, got {len(values)}")
        x1, y1, z1, x2, y2, z2 = [float(value) for value in values]
        return min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2), min(z1, z2), max(z1, z2)

    def boundsFromCccwhd(self, values: list[float]) -> tuple[float, float, float, float, float, float]:
        if len(values) != 6:
            raise ValueError(f"Expected 6 box values, got {len(values)}")
        cx, cy, cz, width, height, depth = [float(value) for value in values]
        return (
            cx - width / 2.0,
            cx + width / 2.0,
            cy - height / 2.0,
            cy + height / 2.0,
            cz - depth / 2.0,
            cz + depth / 2.0,
        )

    def boundsLpsToRas(
        self,
        bounds: tuple[float, float, float, float, float, float],
    ) -> tuple[float, float, float, float, float, float]:
        xMin, xMax, yMin, yMax, zMin, zMax = bounds
        return -xMax, -xMin, -yMax, -yMin, zMin, zMax

    def ijkBoundsToRas(
        self,
        ijkBounds: tuple[float, float, float, float, float, float],
        inputVolume: vtkMRMLScalarVolumeNode,
    ) -> tuple[float, float, float, float, float, float]:
        ijkToRas = vtk.vtkMatrix4x4()
        inputVolume.GetIJKToRASMatrix(ijkToRas)
        xMin, xMax, yMin, yMax, zMin, zMax = ijkBounds
        rasPoints = []
        for i in (xMin, xMax):
            for j in (yMin, yMax):
                for k in (zMin, zMax):
                    rasPoints.append(ijkToRas.MultiplyPoint((i, j, k, 1.0))[:3])
        xs = [point[0] for point in rasPoints]
        ys = [point[1] for point in rasPoints]
        zs = [point[2] for point in rasPoints]
        return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)

    def cubePolyDataFromBounds(self, bounds: tuple[float, float, float, float, float, float]):
        xMin, xMax, yMin, yMax, zMin, zMax = bounds
        cubeSource = vtk.vtkCubeSource()
        cubeSource.SetBounds(xMin, xMax, yMin, yMax, zMin, zMax)
        cubeSource.Update()

        cubePolyData = vtk.vtkPolyData()
        cubePolyData.DeepCopy(cubeSource.GetOutput())
        return cubePolyData

    def createBoxNode(
        self,
        bounds: tuple[float, float, float, float, float, float],
        detection: dict[str, Any],
        displayIndex: int,
    ):
        score = float(detection.get("score", 0.0))
        originalIndex = detection.get("index", displayIndex)
        nodeName = slicer.mrmlScene.GenerateUniqueName(f"Detection {originalIndex} score {score:.3f}")
        modelNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", nodeName)
        modelNode.SetAndObservePolyData(self.cubePolyDataFromBounds(bounds))
        modelNode.SetAttribute(self.GENERATED_BOX_ATTRIBUTE, "1")
        modelNode.SetAttribute("DetectionViewer.Score", f"{score:.6f}")
        modelNode.SetAttribute("DetectionViewer.Index", str(originalIndex))
        modelNode.SetAttribute("DetectionViewer.Label", str(detection.get("label", "")))
        if detection.get("diameter_mm") is not None:
            modelNode.SetAttribute("DetectionViewer.DiameterMm", f"{float(detection['diameter_mm']):.3f}")
        if detection.get("size_mm") is not None:
            modelNode.SetAttribute(
                "DetectionViewer.SizeMm",
                ", ".join(f"{float(value):.3f}" for value in detection["size_mm"]),
            )

        modelNode.CreateDefaultDisplayNodes()
        self.configureBoxDisplay(modelNode, score)
        return modelNode

    def createAnnotationFromDetectionNode(self, detectionNode, label: str):
        bounds = self.nodeBounds(detectionNode)
        annotationNode = self.createAnnotationNode(bounds, label)
        annotationNode.SetAttribute("DetectionViewer.SourceDetectionIndex", detectionNode.GetAttribute("DetectionViewer.Index") or "")
        annotationNode.SetAttribute("DetectionViewer.SourceScore", detectionNode.GetAttribute("DetectionViewer.Score") or "")
        return annotationNode

    def createEmptyAnnotation(self, center: tuple[float, float, float], label: str):
        bounds = self.boundsFromCenterSize(center, self.defaultAnnotationSize())
        return self.createAnnotationNode(bounds, label)

    def defaultAnnotationSize(self) -> tuple[float, float, float]:
        return 10.0, 10.0, 10.0

    def loadAnnotationsFromDetectionPath(self, detectionPath: str) -> list[vtk.vtkObject]:
        detectionData = self.readDetectionData(detectionPath)
        annotationData = self.annotationDataFromDetectionData(detectionData)

        self.clearAnnotations()
        if not annotationData:
            return []

        annotationNodes = []
        for annotation in annotationData:
            annotationNodes.append(self.createAnnotationFromData(annotation))
        return annotationNodes

    def annotationDataFromDetectionData(self, detectionData: dict[str, Any]) -> list[dict[str, Any]]:
        annotationData = detectionData.get("annotation", [])
        if isinstance(annotationData, dict):
            annotationData = annotationData.get("annotations", [])
        if annotationData is None:
            return []
        if not isinstance(annotationData, list):
            raise ValueError("Expected detection JSON 'annotation' to be a list")
        return [annotation for annotation in annotationData if isinstance(annotation, dict)]

    def createAnnotationFromData(self, annotation: dict[str, Any]):
        bounds = self.annotationBoundsRAS(annotation)
        label = str(annotation.get("label") or "lesion")
        annotationNode = self.createAnnotationNode(bounds, label)

        if annotation.get("index") is not None:
            annotationNode.SetAttribute("DetectionViewer.AnnotationIndex", str(int(annotation["index"])))
        sourceIndex = annotation.get("source_detection_index")
        if sourceIndex is not None:
            annotationNode.SetAttribute("DetectionViewer.SourceDetectionIndex", str(sourceIndex))
        sourceScore = annotation.get("source_score")
        if sourceScore is not None:
            annotationNode.SetAttribute("DetectionViewer.SourceScore", f"{float(sourceScore):.6f}")
        return annotationNode

    def annotationBoundsRAS(self, annotation: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
        if "box_xyzxyz_ras" in annotation:
            values = annotation["box_xyzxyz_ras"]
            if len(values) != 6:
                raise ValueError("Expected 6 values in box_xyzxyz_ras")
            x1, y1, z1, x2, y2, z2 = [float(value) for value in values]
            return min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2), min(z1, z2), max(z1, z2)

        if "box_cccwhd_ras" in annotation:
            return self.boundsFromCccwhd(annotation["box_cccwhd_ras"])

        if "box_xyzxyz_world" in annotation:
            return self.boundsFromXyzxyz(annotation["box_xyzxyz_world"])

        if "box_cccwhd_world" in annotation:
            return self.boundsFromCccwhd(annotation["box_cccwhd_world"])

        raise ValueError(f"Annotation is missing a supported box field: {annotation}")

    def createAnnotationNode(self, bounds: tuple[float, float, float, float, float, float], label: str):
        annotationIndex = self.nextAnnotationIndex()
        nodeName = slicer.mrmlScene.GenerateUniqueName(f"Annotation {annotationIndex} {label}")
        roiNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsROINode", nodeName)
        roiNode.SetAttribute(self.ANNOTATION_BOX_ATTRIBUTE, "1")
        roiNode.SetAttribute("DetectionViewer.AnnotationIndex", str(annotationIndex))
        roiNode.SetAttribute("DetectionViewer.AnnotationLabel", label)
        self.setRoiBounds(roiNode, bounds)
        roiNode.SetLocked(False)
        roiNode.CreateDefaultDisplayNodes()
        self.configureAnnotationDisplay(roiNode)
        return roiNode

    def updateAnnotationNode(self, annotationNode, label: str) -> None:
        annotationNode.SetAttribute("DetectionViewer.AnnotationLabel", label)
        annotationNode.SetName(
            slicer.mrmlScene.GenerateUniqueName(
                f"Annotation {annotationNode.GetAttribute('DetectionViewer.AnnotationIndex') or ''} {label}"
            )
        )
        self.configureAnnotationDisplay(annotationNode)

    def annotationInfoRows(self, annotationNode) -> list[tuple[str, str]]:
        center = self.annotationNodeCenter(annotationNode)
        size = self.annotationNodeSize(annotationNode)
        xMin, xMax, yMin, yMax, zMin, zMax = self.nodeBounds(annotationNode)
        rows = [
            ("Index", annotationNode.GetAttribute("DetectionViewer.AnnotationIndex") or "-"),
            ("Label", annotationNode.GetAttribute("DetectionViewer.AnnotationLabel") or "-"),
            ("Center RAS", f"({center[0]:.1f}, {center[1]:.1f}, {center[2]:.1f})"),
            ("Size RAS", f"{size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm"),
            ("Diameter", f"{max(size):.1f} mm"),
            ("Bounds RAS", self.formatBounds(xMin, xMax, yMin, yMax, zMin, zMax)),
        ]

        sourceIndex = annotationNode.GetAttribute("DetectionViewer.SourceDetectionIndex")
        if sourceIndex:
            rows.append(("Source detection", sourceIndex))
        sourceScore = annotationNode.GetAttribute("DetectionViewer.SourceScore")
        if sourceScore:
            rows.append(("Source score", f"{float(sourceScore):.3f}"))
        return rows

    def formatBounds(self, xMin: float, xMax: float, yMin: float, yMax: float, zMin: float, zMax: float) -> str:
        return (
            f"X [{xMin:.1f}, {xMax:.1f}], "
            f"Y [{yMin:.1f}, {yMax:.1f}], "
            f"Z [{zMin:.1f}, {zMax:.1f}]"
        )

    def configureAnnotationDisplay(self, annotationNode) -> None:
        displayNode = annotationNode.GetDisplayNode()
        if displayNode is None:
            annotationNode.CreateDefaultDisplayNodes()
            displayNode = annotationNode.GetDisplayNode()
        if displayNode is None:
            return
        color = self.annotationColor()
        try:
            displayNode.SetColor(color)
        except TypeError:
            displayNode.SetColor(*color)
        if hasattr(displayNode, "SetSelectedColor"):
            try:
                displayNode.SetSelectedColor(color)
            except TypeError:
                displayNode.SetSelectedColor(*color)
        if hasattr(displayNode, "SetFillVisibility"):
            displayNode.SetFillVisibility(False)
        if hasattr(displayNode, "SetOpacity"):
            displayNode.SetOpacity(1.0)
        if hasattr(displayNode, "SetRepresentationToSurface"):
            displayNode.SetRepresentationToSurface()
        if hasattr(displayNode, "SetEdgeVisibility"):
            displayNode.SetEdgeVisibility(True)
        if hasattr(displayNode, "SetLineThickness"):
            displayNode.SetLineThickness(0.35)
        if hasattr(displayNode, "SetSliceIntersectionThickness"):
            displayNode.SetSliceIntersectionThickness(2)
        self.setMarkupHandlesVisible(displayNode, False)
        if hasattr(displayNode, "SetGlyphScale"):
            displayNode.SetGlyphScale(1.0)
        if hasattr(displayNode, "SetTextScale"):
            displayNode.SetTextScale(0.0)
        if hasattr(displayNode, "SetPointLabelsVisibility"):
            displayNode.SetPointLabelsVisibility(False)
        if hasattr(displayNode, "SetPropertiesLabelVisibility"):
            displayNode.SetPropertiesLabelVisibility(False)
        displayNode.SetVisibility(True)
        if hasattr(displayNode, "SetVisibility2D"):
            displayNode.SetVisibility2D(True)
        if hasattr(displayNode, "SetVisibility3D"):
            displayNode.SetVisibility3D(True)

    def setSelectedAnnotationHandles(self, selectedAnnotationNode) -> None:
        for annotationNode in self.annotationNodes():
            displayNode = annotationNode.GetDisplayNode()
            if displayNode is None:
                continue
            handlesVisible = annotationNode == selectedAnnotationNode
            color = self.annotationEditingColor() if handlesVisible else self.annotationColor()
            try:
                displayNode.SetColor(color)
            except TypeError:
                displayNode.SetColor(*color)
            if hasattr(displayNode, "SetSelectedColor"):
                try:
                    displayNode.SetSelectedColor(color)
                except TypeError:
                    displayNode.SetSelectedColor(*color)
            self.setMarkupHandlesVisible(displayNode, handlesVisible)

    def setMarkupHandlesVisible(self, displayNode, visible: bool) -> None:
        if hasattr(displayNode, "SetHandlesInteractive"):
            displayNode.SetHandlesInteractive(visible)

        handled = False
        if hasattr(displayNode, "SetScaleHandleVisibility"):
            displayNode.SetScaleHandleVisibility(visible)
            handled = True
        if hasattr(displayNode, "SetTranslationHandleVisibility"):
            displayNode.SetTranslationHandleVisibility(visible)
            handled = True
        if hasattr(displayNode, "SetRotationHandleVisibility"):
            displayNode.SetRotationHandleVisibility(False)
            handled = True
        if handled:
            return

        if not hasattr(displayNode, "SetHandleVisibility"):
            return

        try:
            displayNode.SetHandleVisibility(visible)
            return
        except TypeError:
            pass

        for handleType in self.markupHandleTypes(displayNode):
            try:
                displayNode.SetHandleVisibility(handleType, visible)
            except TypeError:
                continue

    def markupHandleTypes(self, displayNode) -> list[int]:
        handleTypeNames = (
            "ScaleHandle",
            "TranslationHandle",
            "RotationHandle",
            "InteractionHandle",
        )
        handleTypes = []
        for handleTypeName in handleTypeNames:
            if hasattr(displayNode, handleTypeName):
                handleTypes.append(int(getattr(displayNode, handleTypeName)))
            elif hasattr(slicer.vtkMRMLMarkupsDisplayNode, handleTypeName):
                handleTypes.append(int(getattr(slicer.vtkMRMLMarkupsDisplayNode, handleTypeName)))
        if handleTypes:
            return handleTypes
        return list(range(1, 8))

    def annotationColor(self) -> tuple[float, float, float]:
        return 0.0, 0.8, 0.25

    def annotationEditingColor(self) -> tuple[float, float, float]:
        return 1.0, 0.0, 1.0

    def configureBoxDisplay(self, boxNode, score: float) -> None:
        displayNode = boxNode.GetDisplayNode()
        if displayNode is None:
            boxNode.CreateDefaultDisplayNodes()
            displayNode = boxNode.GetDisplayNode()
        if displayNode is None:
            return

        color = self.defaultBoxColor()
        try:
            displayNode.SetColor(color)
        except TypeError:
            displayNode.SetColor(*color)
        if hasattr(displayNode, "SetSelectedColor"):
            try:
                displayNode.SetSelectedColor(color)
            except TypeError:
                displayNode.SetSelectedColor(*color)
        if hasattr(displayNode, "SetFillVisibility"):
            displayNode.SetFillVisibility(False)
        if hasattr(displayNode, "SetOpacity"):
            displayNode.SetOpacity(0.18)
        if hasattr(displayNode, "SetRepresentationToSurface"):
            displayNode.SetRepresentationToSurface()
        if hasattr(displayNode, "SetEdgeVisibility"):
            displayNode.SetEdgeVisibility(True)
        if hasattr(displayNode, "SetLineThickness"):
            displayNode.SetLineThickness(0.35)
        if hasattr(displayNode, "SetSliceIntersectionThickness"):
            displayNode.SetSliceIntersectionThickness(2)
        if hasattr(displayNode, "SetGlyphScale"):
            displayNode.SetGlyphScale(0.0)
        if hasattr(displayNode, "SetTextScale"):
            displayNode.SetTextScale(0.0)
        if hasattr(displayNode, "SetHandlesInteractive"):
            displayNode.SetHandlesInteractive(False)
        displayNode.SetVisibility(True)
        if hasattr(displayNode, "SetVisibility2D"):
            displayNode.SetVisibility2D(True)
        if hasattr(displayNode, "SetVisibility3D"):
            displayNode.SetVisibility3D(True)

    def defaultBoxColor(self) -> tuple[float, float, float]:
        return 1.0, 0.9, 0.0

    def clearDetectionBoxes(self) -> int:
        nodesToRemove = self.generatedDetectionBoxNodes()
        for node in nodesToRemove:
            slicer.mrmlScene.RemoveNode(node)
        return len(nodesToRemove)

    def clearAnnotations(self) -> int:
        nodesToRemove = self.annotationNodes()
        for node in nodesToRemove:
            self.removeAnnotationNode(node)
        return len(nodesToRemove)

    def removeAnnotationNode(self, annotationNode) -> None:
        self.removeAnnotationLabelNodesFor(annotationNode)
        slicer.mrmlScene.RemoveNode(annotationNode)

    def clearAnnotationLabelNodes(self) -> int:
        nodesToRemove = self.annotationLabelNodes()
        for labelNode in nodesToRemove:
            slicer.mrmlScene.RemoveNode(labelNode)
        for annotationNode in self.annotationNodes():
            annotationNode.SetAttribute("DetectionViewer.AnnotationLabelNodeID", "")
        return len(nodesToRemove)

    def removeAnnotationLabelNodesFor(self, annotationNode) -> int:
        annotationNodeId = annotationNode.GetID()
        labelNodeId = annotationNode.GetAttribute("DetectionViewer.AnnotationLabelNodeID")
        nodesToRemove = []
        if labelNodeId:
            labelNode = slicer.mrmlScene.GetNodeByID(labelNodeId)
            if labelNode is not None:
                nodesToRemove.append(labelNode)
        nodesToRemove.extend(
            labelNode
            for labelNode in self.annotationLabelNodes()
            if labelNode.GetAttribute(self.ANNOTATION_LABEL_FOR_ATTRIBUTE) == annotationNodeId
            and labelNode not in nodesToRemove
        )
        for labelNode in nodesToRemove:
            slicer.mrmlScene.RemoveNode(labelNode)
        annotationNode.SetAttribute("DetectionViewer.AnnotationLabelNodeID", "")
        return len(nodesToRemove)

    def annotationLabelNodes(self) -> list[vtk.vtkObject]:
        return [
            node
            for node in slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode")
            if node.GetAttribute(self.ANNOTATION_LABEL_ATTRIBUTE) == "1"
        ]

    def generatedDetectionBoxNodes(self) -> list[vtk.vtkObject]:
        nodes = []
        for className in ("vtkMRMLModelNode", "vtkMRMLMarkupsROINode"):
            nodes.extend(
                node
                for node in slicer.util.getNodesByClass(className)
                if node.GetAttribute(self.GENERATED_BOX_ATTRIBUTE) == "1"
            )
        return nodes

    def setDetectionBoxesVisible(self, visible: bool) -> None:
        for node in self.generatedDetectionBoxNodes():
            displayNode = node.GetDisplayNode()
            if displayNode is None:
                continue
            displayNode.SetVisibility(visible)
            if hasattr(displayNode, "SetVisibility2D"):
                displayNode.SetVisibility2D(visible)
            if hasattr(displayNode, "SetVisibility3D"):
                displayNode.SetVisibility3D(visible)

    def annotationNodes(self) -> list[vtk.vtkObject]:
        nodes = [
            node
            for node in slicer.util.getNodesByClass("vtkMRMLMarkupsROINode")
            if node.GetAttribute(self.ANNOTATION_BOX_ATTRIBUTE) == "1"
        ]
        return sorted(nodes, key=lambda node: int(node.GetAttribute("DetectionViewer.AnnotationIndex") or 0))

    def nextAnnotationIndex(self) -> int:
        indexes = [
            int(node.GetAttribute("DetectionViewer.AnnotationIndex") or 0)
            for node in self.annotationNodes()
        ]
        return max(indexes, default=0) + 1

    def annotationDisplayName(self, annotationNode) -> str:
        index = annotationNode.GetAttribute("DetectionViewer.AnnotationIndex") or "-"
        label = annotationNode.GetAttribute("DetectionViewer.AnnotationLabel") or "lesion"
        sourceIndex = annotationNode.GetAttribute("DetectionViewer.SourceDetectionIndex")
        if sourceIndex:
            return f"A{index} {label} (det {sourceIndex})"
        return f"A{index} {label}"

    def nodeBounds(self, node) -> tuple[float, float, float, float, float, float]:
        if node.IsA("vtkMRMLMarkupsROINode"):
            center = self.roiCenter(node)
            size = self.roiSize(node)
            return self.boundsFromCenterSize(center, size)

        bounds = [0.0] * 6
        try:
            node.GetBounds(bounds)
        except TypeError:
            bounds = list(node.GetBounds())
        return tuple(float(value) for value in bounds)

    def annotationNodeCenter(self, annotationNode) -> tuple[float, float, float]:
        if annotationNode.IsA("vtkMRMLMarkupsROINode"):
            return self.roiCenter(annotationNode)
        xMin, xMax, yMin, yMax, zMin, zMax = self.nodeBounds(annotationNode)
        return (xMin + xMax) / 2.0, (yMin + yMax) / 2.0, (zMin + zMax) / 2.0

    def annotationNodeSize(self, annotationNode) -> tuple[float, float, float]:
        if annotationNode.IsA("vtkMRMLMarkupsROINode"):
            return self.roiSize(annotationNode)
        xMin, xMax, yMin, yMax, zMin, zMax = self.nodeBounds(annotationNode)
        return max(xMax - xMin, 0.1), max(yMax - yMin, 0.1), max(zMax - zMin, 0.1)

    def setRoiBounds(self, roiNode, bounds: tuple[float, float, float, float, float, float]) -> None:
        center = (
            (bounds[0] + bounds[1]) / 2.0,
            (bounds[2] + bounds[3]) / 2.0,
            (bounds[4] + bounds[5]) / 2.0,
        )
        size = (
            max(bounds[1] - bounds[0], 0.1),
            max(bounds[3] - bounds[2], 0.1),
            max(bounds[5] - bounds[4], 0.1),
        )
        if hasattr(roiNode, "SetROIType") and hasattr(slicer.vtkMRMLMarkupsROINode, "ROITypeBox"):
            roiNode.SetROIType(slicer.vtkMRMLMarkupsROINode.ROITypeBox)
        self.setRoiCenter(roiNode, center)
        self.setRoiSize(roiNode, size)

    def setRoiCenter(self, roiNode, center: tuple[float, float, float]) -> None:
        if hasattr(roiNode, "SetCenter"):
            try:
                roiNode.SetCenter(center)
            except TypeError:
                roiNode.SetCenter(*center)
        elif hasattr(roiNode, "SetXYZ"):
            roiNode.SetXYZ(*center)
        else:
            raise RuntimeError("ROI node does not support center editing")

    def setRoiSize(self, roiNode, size: tuple[float, float, float]) -> None:
        if hasattr(roiNode, "SetSize"):
            try:
                roiNode.SetSize(size)
            except TypeError:
                roiNode.SetSize(*size)
        elif hasattr(roiNode, "SetRadiusXYZ"):
            roiNode.SetRadiusXYZ(size[0] / 2.0, size[1] / 2.0, size[2] / 2.0)
        else:
            raise RuntimeError("ROI node does not support size editing")

    def roiCenter(self, roiNode) -> tuple[float, float, float]:
        center = [0.0, 0.0, 0.0]
        if hasattr(roiNode, "GetCenter"):
            try:
                roiNode.GetCenter(center)
                return tuple(float(value) for value in center)
            except TypeError:
                return tuple(float(value) for value in roiNode.GetCenter())
        if hasattr(roiNode, "GetXYZ"):
            roiNode.GetXYZ(center)
            return tuple(float(value) for value in center)
        raise RuntimeError("ROI node does not support center reading")

    def roiSize(self, roiNode) -> tuple[float, float, float]:
        size = [0.0, 0.0, 0.0]
        if hasattr(roiNode, "GetSize"):
            try:
                roiNode.GetSize(size)
                return tuple(max(float(value), 0.1) for value in size)
            except TypeError:
                return tuple(max(float(value), 0.1) for value in roiNode.GetSize())
        if hasattr(roiNode, "GetRadiusXYZ"):
            roiNode.GetRadiusXYZ(size)
            return tuple(max(float(value) * 2.0, 0.1) for value in size)
        raise RuntimeError("ROI node does not support size reading")

    def boundsFromCenterSize(
        self,
        center: tuple[float, float, float],
        size: tuple[float, float, float],
    ) -> tuple[float, float, float, float, float, float]:
        cx, cy, cz = center
        sx, sy, sz = [max(float(value), 0.1) for value in size]
        return (
            cx - sx / 2.0,
            cx + sx / 2.0,
            cy - sy / 2.0,
            cy + sy / 2.0,
            cz - sz / 2.0,
            cz + sz / 2.0,
        )

    def currentSliceCenterRAS(self) -> tuple[float, float, float] | None:
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return None
        for sliceName in ("Red", "Yellow", "Green"):
            sliceWidget = layoutManager.sliceWidget(sliceName)
            if sliceWidget is None:
                continue
            sliceNode = sliceWidget.mrmlSliceNode()
            if sliceNode is None or sliceNode.GetSliceToRAS() is None:
                continue
            ras = sliceNode.GetSliceToRAS().MultiplyPoint((0.0, 0.0, 0.0, 1.0))
            return ras[0], ras[1], ras[2]
        return None

    def volumeCenterRAS(self, volumeNode: vtkMRMLScalarVolumeNode | None) -> tuple[float, float, float] | None:
        if volumeNode is None:
            return None
        bounds = [0.0] * 6
        try:
            volumeNode.GetRASBounds(bounds)
        except Exception:
            return None
        if bounds[0] > bounds[1] or bounds[2] > bounds[3] or bounds[4] > bounds[5]:
            return None
        return (
            (bounds[0] + bounds[1]) / 2.0,
            (bounds[2] + bounds[3]) / 2.0,
            (bounds[4] + bounds[5]) / 2.0,
        )

    def detectionJsonHasAnnotation(self, detectionPath: str) -> bool:
        detectionData = self.readDetectionData(detectionPath)
        return "annotation" in detectionData

    def saveAnnotationsToDetectionJson(self, detectionPath: str) -> int:
        detectionData = self.readDetectionData(detectionPath)
        annotations = [self.annotationNodeToDict(node) for node in self.annotationNodes()]
        detectionData["annotation"] = annotations
        with open(detectionPath, "w", encoding="utf-8") as detectionFile:
            json.dump(detectionData, detectionFile, ensure_ascii=False, indent=2)
        return len(annotations)

    def annotationNodeToDict(self, annotationNode) -> dict[str, Any]:
        xMin, xMax, yMin, yMax, zMin, zMax = self.nodeBounds(annotationNode)
        center = self.annotationNodeCenter(annotationNode)
        size = self.annotationNodeSize(annotationNode)
        annotation = {
            "index": int(annotationNode.GetAttribute("DetectionViewer.AnnotationIndex") or 0),
            "label": annotationNode.GetAttribute("DetectionViewer.AnnotationLabel") or "",
            "box_mode": "xyzxyz",
            "box_xyzxyz_ras": [xMin, yMin, zMin, xMax, yMax, zMax],
            "box_cccwhd_ras": [center[0], center[1], center[2], size[0], size[1], size[2]],
            "size_mm": [size[0], size[1], size[2]],
        }
        sourceIndex = annotationNode.GetAttribute("DetectionViewer.SourceDetectionIndex")
        if sourceIndex:
            annotation["source_detection_index"] = int(sourceIndex)
        sourceScore = annotationNode.GetAttribute("DetectionViewer.SourceScore")
        if sourceScore:
            annotation["source_score"] = float(sourceScore)
        return annotation

    def findDetectionBoxByIndex(self, detectionIndex: int):
        requestedIndex = str(detectionIndex)
        for node in self.generatedDetectionBoxNodes():
            if node.GetAttribute("DetectionViewer.Index") == requestedIndex:
                return node
        return None

    def highlightDetectionBox(self, targetNode) -> None:
        for node in self.generatedDetectionBoxNodes():
            displayNode = node.GetDisplayNode()
            if displayNode is None:
                continue
            if node == targetNode:
                color = self.highlightColor()
            else:
                color = self.defaultBoxColor()
            try:
                displayNode.SetColor(color)
            except TypeError:
                displayNode.SetColor(*color)
            if hasattr(displayNode, "SetSelectedColor"):
                try:
                    displayNode.SetSelectedColor(color)
                except TypeError:
                    displayNode.SetSelectedColor(*color)

    def clearDetectionHighlight(self) -> None:
        color = self.defaultBoxColor()
        for node in self.generatedDetectionBoxNodes():
            displayNode = node.GetDisplayNode()
            if displayNode is None:
                continue
            try:
                displayNode.SetColor(color)
            except TypeError:
                displayNode.SetColor(*color)
            if hasattr(displayNode, "SetSelectedColor"):
                try:
                    displayNode.SetSelectedColor(color)
                except TypeError:
                    displayNode.SetSelectedColor(*color)

    def highlightColor(self) -> tuple[float, float, float]:
        return 1.0, 0.0, 0.0

    def detectionBoxInfoRows(self, boxNode) -> list[tuple[str, str]]:
        bounds = [0.0] * 6
        try:
            boxNode.GetBounds(bounds)
        except TypeError:
            bounds = list(boxNode.GetBounds())

        xMin, xMax, yMin, yMax, zMin, zMax = bounds
        center = ((xMin + xMax) / 2.0, (yMin + yMax) / 2.0, (zMin + zMax) / 2.0)
        size = (xMax - xMin, yMax - yMin, zMax - zMin)

        rows = [
            ("Index", boxNode.GetAttribute("DetectionViewer.Index") or "-"),
            ("Score", f"{float(boxNode.GetAttribute('DetectionViewer.Score') or 0.0):.3f}"),
        ]
        label = boxNode.GetAttribute("DetectionViewer.Label")
        if label not in (None, ""):
            rows.append(("Label", label))

        rows.append(("Center RAS", f"({center[0]:.1f}, {center[1]:.1f}, {center[2]:.1f})"))
        rows.append(("Size RAS", f"{size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm"))

        diameter = boxNode.GetAttribute("DetectionViewer.DiameterMm")
        if diameter:
            rows.append(("Diameter", f"{float(diameter):.1f} mm"))

        originalSize = boxNode.GetAttribute("DetectionViewer.SizeMm")
        if originalSize:
            rows.append(("JSON size", f"{originalSize} mm"))

        return rows

    def centerViewsOnBoxes(
        self,
        boxNodes: list[vtk.vtkObject],
        fitToBounds: bool = False,
        fovZoomFactor: float = 1.0,
    ) -> None:
        if not boxNodes:
            return
        bounds = self.boundsForNodes(boxNodes)
        if bounds is not None:
            xMin, xMax, yMin, yMax, zMin, zMax = bounds
            center = ((xMin + xMax) / 2.0, (yMin + yMax) / 2.0, (zMin + zMax) / 2.0)
            self.jumpSlicesToLocation(center)
            if fitToBounds:
                self.fitSliceViewsToBounds(bounds, fovZoomFactor)

    def boundsForNodes(self, nodes: list[vtk.vtkObject]) -> tuple[float, float, float, float, float, float] | None:
        validBounds = []
        for node in nodes:
            nodeBounds = [0.0] * 6
            try:
                node.GetBounds(nodeBounds)
            except TypeError:
                nodeBounds = list(node.GetBounds())
            except AttributeError:
                continue
            if nodeBounds[0] <= nodeBounds[1] and nodeBounds[2] <= nodeBounds[3] and nodeBounds[4] <= nodeBounds[5]:
                validBounds.append(nodeBounds)

        if not validBounds:
            return None

        return (
            min(bounds[0] for bounds in validBounds),
            max(bounds[1] for bounds in validBounds),
            min(bounds[2] for bounds in validBounds),
            max(bounds[3] for bounds in validBounds),
            min(bounds[4] for bounds in validBounds),
            max(bounds[5] for bounds in validBounds),
        )

    def jumpSlicesToLocation(self, rasPoint: tuple[float, float, float]) -> None:
        x, y, z = rasPoint
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return

        for sliceName in ("Red", "Yellow", "Green"):
            sliceWidget = layoutManager.sliceWidget(sliceName)
            if sliceWidget is None:
                continue
            sliceNode = sliceWidget.mrmlSliceNode()
            if sliceNode is None or not hasattr(sliceNode, "JumpSliceByCentering"):
                continue
            sliceNode.JumpSliceByCentering(x, y, z)

    def fitSliceViewsToBounds(
        self,
        bounds: tuple[float, float, float, float, float, float],
        fovZoomFactor: float = 1.0,
    ) -> None:
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for sliceName in ("Red", "Yellow", "Green"):
            sliceWidget = layoutManager.sliceWidget(sliceName)
            if sliceWidget is None:
                continue

            sliceNode = sliceWidget.mrmlSliceNode()
            if sliceNode is None:
                continue
            boxWidth, boxHeight = self.boxSizeInSliceXY(bounds, sliceNode)
            if boxWidth is None or boxHeight is None:
                continue
            widthPx, heightPx = self.sliceViewPixelSize(sliceWidget)
            fovWidth, fovHeight = self.fieldOfViewForBox(
                boxWidth,
                boxHeight,
                widthPx,
                heightPx,
                fovZoomFactor,
            )
            currentFov = sliceNode.GetFieldOfView()
            sliceNode.SetFieldOfView(fovWidth, fovHeight, currentFov[2])

    def sliceViewPixelSize(self, sliceWidget) -> tuple[int, int]:
        sliceView = sliceWidget.sliceView()
        size = sliceView.size() if callable(getattr(sliceView, "size", None)) else sliceView.size
        width = size.width() if callable(getattr(size, "width", None)) else self.widgetDimension(sliceView, "width")
        height = size.height() if callable(getattr(size, "height", None)) else self.widgetDimension(sliceView, "height")
        return max(int(width), 1), max(int(height), 1)

    def widgetDimension(self, widget, name: str) -> int:
        value = getattr(widget, name)
        return value() if callable(value) else value

    def boxSizeInSliceXY(
        self,
        bounds: tuple[float, float, float, float, float, float],
        sliceNode,
    ) -> tuple[float | None, float | None]:
        xyToRas = sliceNode.GetXYToRAS()
        if xyToRas is None:
            return None, None

        xAxis = self.normalizedMatrixColumn(xyToRas, 0)
        yAxis = self.normalizedMatrixColumn(xyToRas, 1)
        if xAxis is None or yAxis is None:
            return None, None

        xMin, xMax, yMin, yMax, zMin, zMax = bounds
        xProjections = []
        yProjections = []
        for x in (xMin, xMax):
            for y in (yMin, yMax):
                for z in (zMin, zMax):
                    rasPoint = (x, y, z)
                    xProjections.append(self.dot(rasPoint, xAxis))
                    yProjections.append(self.dot(rasPoint, yAxis))

        return (
            max(max(xProjections) - min(xProjections), 1.0),
            max(max(yProjections) - min(yProjections), 1.0),
        )

    def normalizedMatrixColumn(self, matrix, column: int) -> tuple[float, float, float] | None:
        vector = (
            float(matrix.GetElement(0, column)),
            float(matrix.GetElement(1, column)),
            float(matrix.GetElement(2, column)),
        )
        length = vtk.vtkMath.Norm(vector)
        if length <= 0.0:
            return None
        return vector[0] / length, vector[1] / length, vector[2] / length

    def dot(self, left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
        return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]

    def fieldOfViewForBox(
        self,
        boxWidth: float,
        boxHeight: float,
        viewWidthPx: int,
        viewHeightPx: int,
        zoomFactor: float = 1.0,
    ) -> tuple[float, float]:
        margin = 4.0
        minFovMm = 10.0
        zoomFactor = max(float(zoomFactor), 0.1)

        targetWidth = max(boxWidth * margin / zoomFactor, minFovMm)
        targetHeight = max(boxHeight * margin / zoomFactor, minFovMm)
        viewAspect = max(viewWidthPx / viewHeightPx, 0.01)
        targetAspect = targetWidth / targetHeight

        if targetAspect > viewAspect:
            fovWidth = targetWidth
            fovHeight = targetWidth / viewAspect
        else:
            fovHeight = targetHeight
            fovWidth = targetHeight * viewAspect

        fovWidth = max(fovWidth, minFovMm)
        fovHeight = max(fovHeight, minFovMm)
        return fovWidth, fovHeight


#
# mainTest
#


class mainTest(ScriptedLoadableModuleTest):
    """Basic tests for detection parsing and box creation."""

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_detection_json_parsing()

    def test_detection_json_parsing(self):
        self.delayDisplay("Starting detection viewer test")

        logic = mainLogic()
        volumePath, detectionPath = logic.findTestData()
        self.assertTrue(os.path.isfile(volumePath))
        self.assertTrue(os.path.isfile(detectionPath))

        detectionData = logic.readDetectionData(detectionPath)
        detections = logic.detectionsFromData(detectionData, minScore=0.2)
        self.assertGreater(len(detections), 0)

        bounds = logic.detectionBoundsRAS(detections[0])
        self.assertEqual(len(bounds), 6)
        self.assertGreater(bounds[1] - bounds[0], 0.0)
        self.assertGreater(bounds[3] - bounds[2], 0.0)
        self.assertGreater(bounds[5] - bounds[4], 0.0)

        self.delayDisplay("Test passed")
